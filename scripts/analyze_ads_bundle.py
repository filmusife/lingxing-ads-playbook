# -*- coding: utf-8 -*-
"""Turn fetched Lingxing JSON into product-fit analysis tables.

Upgraded features:
- Dynamic product-fit rule injection (decoupled category hardcoding)
- N-Gram bleeding phrase root analysis (for negative phrase discovery)
- Cross-campaign cannibalization and internal competition detection
- Campaign placement audit (TOS/PP/ROS health check)
- Actionable bulk adjustments table generation (bulk_adjustments.tsv)
- Control-variable snapshot diff & cooling-off protection (Config Snapshot Diff & Observation Lock)

User-facing copy should say 基础竞价, never BID. Default evidence window is 30 days.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


STOP_WORDS = {
    # English
    "the", "a", "an", "and", "or", "for", "with", "in", "on", "at", "to", "from",
    "by", "of", "is", "it", "as", "be", "this", "that", "are", "was", "were",
    "my", "your", "its", "into", "over", "after", "before", "between", "under",
    "all", "any", "both", "each", "few", "more", "most", "other", "some", "such",
    # German
    "der", "die", "das", "und", "oder", "fur", "für", "mit", "von", "aus", "auf", "in", "zu",
    # French
    "le", "la", "les", "et", "ou", "pour", "avec", "de", "des", "du", "en", "sur",
    # Spanish
    "el", "la", "los", "las", "y", "o", "para", "por", "con", "de", "del", "en",
    # Italian
    "il", "la", "lo", "i", "gli", "le", "e", "o", "per", "con", "di", "da", "in", "su"
}


def as_rows(obj):
    if obj is None:
        return []
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    cur = obj
    for _ in range(6):
        if isinstance(cur, list):
            return [x for x in cur if isinstance(x, dict)]
        if not isinstance(cur, dict):
            return []
        if isinstance(cur.get("list"), list):
            return [x for x in cur["list"] if isinstance(x, dict)]
        data = cur.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            cur = data
            continue
        for key in ("records", "rows", "items"):
            if isinstance(cur.get(key), list):
                return [x for x in cur[key] if isinstance(x, dict)]
        return []
    return []


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def num(x, default=0.0):
    if x is None or x == "" or x == "--":
        return default
    try:
        s = str(x).replace("%", "").replace(",", "").replace("$", "").replace("¥", "").replace("€", "").replace("£", "").strip()
        return float(s)
    except Exception:
        return default


def acos(spend, sales):
    if sales <= 0:
        return 9999.0 if spend > 0 else 0.0
    return spend / sales * 100


def skip_total(row: dict) -> bool:
    name = str(row.get("name") or row.get("campaign_name") or row.get("keyword_text") or row.get("query") or "").strip()
    return not name and not row.get("campaign_id") and not row.get("keyword_id")


def load_product_rules(rule_path: Path | None, indir: Path) -> dict:
    candidates = []
    if rule_path and rule_path.exists():
        candidates.append(rule_path)
    candidates.extend([
        indir / "product_rules.json",
        indir / "rules.json",
    ])
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {
        "is_patterns": [],
        "is_not_patterns": [],
        "competitor_brands": [],
        "adjacent_patterns": [],
        "target_acos": None,
    }


def load_financial_profile(indir: Path) -> dict:
    rows = as_rows(load_json(indir / "order_profit.json"))
    if not rows:
        return {}
    r = rows[0]
    total_sales = num(r.get("net_amount") or r.get("amount"))
    gross_profit = num(r.get("gross_profit"))
    gross_margin = num(r.get("gross_margin") or r.get("net_gross_margin"))
    purchase_costs = abs(num(r.get("purchase_costs")))
    logistics_costs = abs(num(r.get("logistics_costs")))
    fba_fulfillment_fee = abs(num(r.get("fba_fulfillment_fee")))
    selling_fee = abs(num(r.get("selling_fee")))
    vol = num(r.get("volume"))
    avg_price = (total_sales / vol) if vol > 0 else 0.0
    cogs_per_unit = (purchase_costs / vol) if vol > 0 else num(r.get("avg_purchase_costs"))
    logistics_per_unit = (logistics_costs / vol) if vol > 0 else num(r.get("avg_logistics_costs"))
    fba_per_unit = (fba_fulfillment_fee / vol) if vol > 0 else 0.0
    comm_per_unit = (selling_fee / vol) if vol > 0 else 0.0

    break_even_profit = avg_price - cogs_per_unit - logistics_per_unit - fba_per_unit - comm_per_unit
    break_even_acos = (break_even_profit / avg_price * 100) if avg_price > 0 else 0.0

    return {
        "total_sales": total_sales,
        "volume": int(vol),
        "avg_price": round(avg_price, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_margin": round(gross_margin * (100 if gross_margin <= 1.0 else 1.0), 2),
        "cogs_per_unit": round(cogs_per_unit, 2),
        "logistics_per_unit": round(logistics_per_unit, 2),
        "fba_per_unit": round(fba_per_unit, 2),
        "comm_per_unit": round(comm_per_unit, 2),
        "refund_rate": round(num(r.get("refund_amount_rate") or r.get("return_rate")) * 100, 2),
        "total_ad_cost": abs(num(r.get("spend") or r.get("ads_sp_cost"))),
        "break_even_acos": round(break_even_acos, 1),
    }


def load_traffic_profile(indir: Path) -> dict:
    rows = as_rows(load_json(indir / "product_performance.json"))
    if not rows:
        return {}
    r = rows[0]
    total_sessions = int(num(r.get("sessions_total") or r.get("sessions")))
    total_pageviews = int(num(r.get("page_views_total") or r.get("page_views")))
    ad_cvr = round(num(r.get("ad_cvr")) * 100, 2)
    nature_cvr = round(num(r.get("nature_cvr")) * 100, 2)
    total_cvr = round(num(r.get("cvr")) * 100, 2)
    adv_rate = round(num(r.get("adv_rate")) * 100, 2)
    tacos = round(num(r.get("acoas")) * 100, 2)
    return {
        "sessions_total": total_sessions,
        "page_views_total": total_pageviews,
        "sessions_mobile": int(num(r.get("sessions_mobile"))),
        "page_views_mobile": int(num(r.get("page_views_mobile"))),
        "ad_cvr": ad_cvr,
        "nature_cvr": nature_cvr,
        "total_cvr": total_cvr,
        "adv_sales_share": adv_rate,
        "tacos": tacos,
        "return_goods_rate": round(num(r.get("return_goods_rate")) * 100, 2),
        "cvr_ad_vs_nature_ratio": round(ad_cvr / nature_cvr, 2) if nature_cvr > 0 else 0.0,
    }


def load_dayparting_analysis(indir: Path) -> tuple[list[dict], list[str]]:
    h_rows = as_rows(load_json(indir / "order_analysis_hourly.json"))
    table_lines = []
    hourly_records = []
    for r in h_rows:
        k = str(r.get("key"))
        if k == "sub_total" or not k.isdigit():
            continue
        hr = int(k)
        sp = num(r.get("spend"))
        ad_ord = int(num(r.get("ad_orders")))
        st_ord = int(num(r.get("store_orders")))
        cvr_v = num(r.get("cvr"))
        acos_v = num(r.get("acos"))
        cpc_v = num(r.get("cpc"))

        if cvr_v >= 15.0 and acos_v <= 18.0:
            classification = "PEAK_GOLDEN_HOUR"
            dayparting_adj = "Boost budget/bid +15%~+25%"
        elif acos_v >= 24.0 or cvr_v <= 11.0:
            classification = "BLEEDING_OFFPEAK_HOUR"
            dayparting_adj = "Throttle bid -20%~-30%"
        else:
            classification = "STEADY_TRANSITION_HOUR"
            dayparting_adj = "Maintain normal baseline"

        hourly_records.append({
            "hour": hr, "spend": sp, "ad_orders": ad_ord, "store_orders": st_ord,
            "cvr": cvr_v, "acos": acos_v, "cpc": cpc_v,
            "classification": classification, "recommendation": dayparting_adj
        })
        table_lines.append(f"{hr:02d}:00-{hr:02d}:59\t{sp:.2f}\t{ad_ord}\t{st_ord}\t{cvr_v:.1f}%\t{acos_v:.1f}%\t{cpc_v:.2f}\t{classification}\t{dayparting_adj}")

    return hourly_records, table_lines


def listing_profile(listing: dict | None, stock: dict | None) -> dict:
    item = listing or {}
    stock_rows = []
    if isinstance(stock, list):
        stock_rows = stock
    elif isinstance(stock, dict):
        data = stock.get("data")
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            stock_rows = inner.get("list") or []
        elif isinstance(data, list):
            stock_rows = data
    s0 = stock_rows[0] if stock_rows else {}
    small_rank = item.get("small_rank")
    if isinstance(small_rank, str):
        try:
            small_rank = json.loads(small_rank)
        except Exception:
            pass
    title = item.get("item_name") or item.get("title") or s0.get("product_name") or ""
    brand = item.get("seller_brand") or item.get("product_brand_text") or s0.get("product_brand_text") or ""
    # Extract feature segments from Title for product functional understanding
    features = []
    chunks = re.split(r"[,|\-–—;()\[\]/]+", title)
    for c in chunks:
        c_clean = c.strip()
        if brand and c_clean.lower().startswith(str(brand).lower()):
            c_clean = c_clean[len(str(brand)):].strip()
        if len(c_clean) >= 3:
            features.append(c_clean)

    return {
        "asin": item.get("asin") or s0.get("asin"),
        "parent_asin": item.get("parent_asin"),
        "msku": item.get("msku") or s0.get("seller_sku"),
        "local_sku": item.get("local_sku") or s0.get("sku"),
        "title": title,
        "brand": brand,
        "features": features,
        "price": num(item.get("listing_price") or item.get("landed_price") or item.get("price")),
        "stars": num(item.get("stars")),
        "reviews": int(num(item.get("reviews_num"))),
        "rank": item.get("rank") or item.get("seller_rank"),
        "small_rank": small_rank,
        "fulfillable": item.get("afn_fulfillable_quantity") or s0.get("afn_fulfillable_quantity"),
        "available_total": s0.get("available_total"),
        "days_of_supply": s0.get("short_term_historical_days_of_supply"),
        "inv_age_91_180": s0.get("inv_age_91_to_180_days"),
        "seven_volume": item.get("average_seven_volume"),
        "fourteen_volume": item.get("average_fourteen_volume") or item.get("fourteen_volume"),
        "thirty_volume": item.get("average_thirty_volume") or item.get("thirty_volume"),
        "fourteen_sales": item.get("fourteen_amount"),
        "fourteen_ad_spend": item.get("fourteen_spend"),
        "thirty_sales": item.get("thirty_amount"),
        "thirty_ad_spend": item.get("thirty_spend"),
    }


def placement(row: dict) -> tuple:
    bidding = row.get("bidding") or {}
    tos = num(row.get("placement_top"))
    pp = num(row.get("placement_product_page"))
    ros = num(row.get("placement_rest_of_search"))
    if isinstance(bidding, dict):
        for adj in bidding.get("adjustments") or []:
            pred = adj.get("predicate")
            pct = num(adj.get("percentage"))
            if pred == "placementTop":
                tos = pct
            elif pred == "placementProductPage":
                pp = pct
            elif pred == "placementRestOfSearch":
                ros = pct
    return tos, pp, ros


def campaign_rows(raw: list[dict]) -> list[dict]:
    out = []
    for r in raw:
        if skip_total(r) or not r.get("name"):
            continue
        tos, pp, ros = placement(r)
        bidding = r.get("bidding") or {}
        strat = bidding.get("strategy") if isinstance(bidding, dict) else r.get("bidding_strategy")
        btype = str(r.get("bidding_type") or "")
        if strat == "manual" or "固定" in btype or "fixed" in btype.lower():
            bidding_strategy_desc = "固定竞价 (Fixed Bids)"
        elif strat == "legacyForSales" or "仅降低" in btype or "down" in btype.lower():
            bidding_strategy_desc = "动态竞价 - 仅降低 (Dynamic Bids - Down Only)"
        elif strat == "autoForSales" or "提高和降低" in btype or "up" in btype.lower():
            bidding_strategy_desc = "动态竞价 - 提高和降低 (Dynamic Bids - Up & Down)"
        elif strat == "ruleBased" or "规则" in btype:
            bidding_strategy_desc = "基于规则的竞价 (Rule-based Bidding)"
        else:
            bidding_strategy_desc = btype if btype else (str(strat) if strat else "固定竞价 (Fixed Bids)")
        out.append({
            "name": r.get("name"),
            "id": r.get("campaign_id"),
            "state": r.get("state"),
            "targeting": r.get("targeting_type"),
            "sponsored": r.get("sponsored_type"),
            "budget": num(r.get("daily_budget") or r.get("budget")),
            "tos": tos, "pp": pp, "ros": ros,
            "bidding_strategy": bidding_strategy_desc,
            "spend": num(r.get("spends")),
            "sales": num(r.get("sales")),
            "acos": num(r.get("acos")),
            "cpc": num(r.get("cpc")),
            "cvr": num(r.get("cvr")),
            "ctr": num(r.get("ctr")),
            "imp": num(r.get("impressions")),
            "clk": num(r.get("clicks")),
            "ord": num(r.get("orders")),
            "direct_ord": num(r.get("direct_orders")),
            "indirect_ord": num(r.get("indirect_orders")),
            "tos_share": num(r.get("top_of_search_impression_share")),
            "portfolio": r.get("portfolio_name") or r.get("portfolio_id"),
            "portfolio_id": str(r.get("portfolio_id") or "").strip(),
            "portfolio_name": str(r.get("portfolio_name") or "").strip(),
        })
    return sorted(out, key=lambda x: -x["spend"])


def classify_query(q: str, profile: dict, rules: dict) -> tuple[str, str, bool]:
    """Classify query into intent buckets without category hardcoding.

    Returns (bucket, matched_pattern_or_reason, is_strong_relevance).
    """
    ql = (q or "").lower().strip()
    if not ql:
        return "empty", "", False
    if re.search(r"b0[0-9a-z]{8}", ql):
        return "asin", "asin_target", False

    brand = str(profile.get("brand") or "").lower().strip()
    if brand and len(brand) >= 2 and brand in ql:
        return "brand", brand, True

    # 1. Custom IS_NOT patterns (explicit misfits, wrong features, wrong forms)
    for item in rules.get("is_not_patterns", []):
        pat = item if isinstance(item, str) else item.get("pattern", "")
        label = "is_not" if isinstance(item, str) else item.get("label", "is_not")
        if pat and re.search(pat, ql, re.I):
            return label, pat, False

    # 2. Competitor brands
    for comp in rules.get("competitor_brands", []):
        c = (comp or "").lower().strip()
        if c and len(c) >= 2 and c in ql:
            return "competitor_brand", c, False

    # 3. Custom IS patterns (core product, primary selling points)
    for item in rules.get("is_patterns", []):
        pat = item if isinstance(item, str) else item.get("pattern", "")
        label = "core_feature" if isinstance(item, str) else item.get("label", "core_feature")
        if pat and re.search(pat, ql, re.I):
            return label, pat, True

    # 4. Adjacent patterns (compatible but not primary category food)
    for item in rules.get("adjacent_patterns", []):
        pat = item if isinstance(item, str) else item.get("pattern", "")
        label = "adjacent" if isinstance(item, str) else item.get("label", "adjacent")
        if pat and re.search(pat, ql, re.I):
            return label, pat, False

    # 5. Smart Title / Token Overlap
    title = str(profile.get("title") or "").lower()
    title_tokens = set(re.findall(r"[\w]+", title, re.UNICODE))
    title_tokens = {t for t in title_tokens if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}

    q_tokens = set(re.findall(r"[\w]+", ql, re.UNICODE))
    q_tokens = {t for t in q_tokens if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}

    common = q_tokens & title_tokens
    if len(common) >= 2:
        return "core_or_title", f"tokens:{','.join(list(common)[:3])}", True
    if len(common) == 1 and len(q_tokens) <= 2:
        return "core_or_title", f"token:{list(common)[0]}", True

    # 6. Feature chunk matching
    for feat in profile.get("features", []):
        fl = feat.lower()
        if len(fl) >= 4 and fl in ql:
            return "core_feature", f"feature:{feat}", True

    return "other", "", False


def load_searchrank_map(src: Path) -> dict[str, int]:
    """Load search rank from keyword_analysis.json (Amazon ABA search frequency rank)."""
    kw_analysis_raw = load_json(src / "keyword_analysis.json")
    rows = as_rows(kw_analysis_raw)
    sr_map: dict[str, int] = {}
    for r in rows:
        kw = str(r.get("keyword_text") or r.get("key") or "").lower().strip()
        sr = r.get("searchrank")
        if kw and sr is not None and str(sr).isdigit():
            sr_map[kw] = int(sr)
    return sr_map


def get_searchrank(term: str, sr_map: dict[str, int]) -> tuple[int | None, str]:
    """Map query/keyword to Amazon ABA searchrank and search volume tier."""
    t = (term or "").lower().strip()
    if t in sr_map:
        sr = sr_map[t]
    else:
        matches = [sr_map[k] for k in sr_map if k == t or k in t or t in k]
        sr = min(matches) if matches else None

    if sr is None:
        return None, "LONG_TAIL"
    if sr < 5000:
        return sr, "MEGA_HEAD"       # Top 5K 全美超级大词
    if sr < 30000:
        return sr, "CORE_HEAD"       # Top 30K 头部主粮词
    if sr < 150000:
        return sr, "MID_TAIL"        # Top 150K 腰部特征词
    return sr, "LONG_TAIL"           # 长尾细分词


def extract_asin_target_info(r: dict) -> dict | None:
    """Extract competitor ASIN target metadata from target row."""
    expr = r.get("expression") or r.get("targeting_text") or ""
    asin_val = None
    if isinstance(expr, str):
        m = re.search(r"b0[0-9a-z]{8}", expr, re.I)
        if m:
            asin_val = m.group(0).upper()
    elif isinstance(expr, list):
        for item in expr:
            if isinstance(item, dict) and item.get("type") == "asinSameAs":
                asin_val = str(item.get("value", "")).upper()
                break
    if not asin_val:
        key = str(r.get("key") or "")
        m = re.search(r"b0[0-9a-z]{8}", key, re.I)
        if m:
            asin_val = m.group(0).upper()
    if not asin_val:
        return None

    title = r.get("asin_title") or ""
    price = num(r.get("asin_price"))
    stars = num(r.get("asin_stars"))
    reviews = int(num(r.get("asin_review_count")))
    return {
        "asin": asin_val,
        "title": title,
        "price": price,
        "stars": stars,
        "reviews": reviews,
        "spend": num(r.get("spends")),
        "sales": num(r.get("sales")),
        "orders": int(num(r.get("orders"))),
        "clicks": int(num(r.get("clicks"))),
        "cpc": num(r.get("cpc")),
        "acos": num(r.get("acos")),
        "campaign_name": r.get("campaign_name") or "",
    }


def evaluate_competitor_target(comp: dict, our_profile: dict, target_acos: float) -> tuple[str, str, str]:
    """Compare product competitiveness against competitor ASIN and decide action.

    Returns (action, recommended_value, reason).
    """
    comp_asin = comp["asin"]
    comp_price = comp["price"]
    comp_stars = comp["stars"]
    comp_reviews = comp["reviews"]

    our_price = num(our_profile.get("price"))
    our_stars = num(our_profile.get("stars"))
    our_reviews = int(num(our_profile.get("reviews")))

    spend = comp["spend"]
    orders = comp["orders"]
    c_acos = comp["acos"]
    cpc = comp["cpc"]

    # 1. Winnable competitor target
    if orders >= 3 and c_acos <= target_acos:
        return (
            "Harvest_ASIN",
            "Keep & Scale Dedicated ASIN Campaign",
            f"Winnable competitor page: orders={orders}, ACOS={c_acos:.1f}% <= {target_acos:.0f}%. Our product converts successfully against {comp_asin}."
        )

    # 2. Deficit / High bleed competitor target
    if spend >= 10 and (orders == 0 or c_acos >= target_acos * 1.5):
        has_massive_reviews = (comp_reviews > max(our_reviews * 3, 3000) and comp_stars >= our_stars)
        is_substantially_cheaper = (comp_price > 0 and our_price > 0 and comp_price < our_price * 0.75)

        if has_massive_reviews or is_substantially_cheaper:
            reason_detail = []
            if has_massive_reviews:
                reason_detail.append(f"competitor review moat is overwhelming ({comp_reviews} reviews vs our {our_reviews})")
            if is_substantially_cheaper:
                reason_detail.append(f"competitor price is much cheaper (${comp_price:.2f} vs our ${our_price:.2f})")
            return (
                "Negative_Target",
                "Add Negative Target / Pause Target",
                f"Structurally unwinnable competitor: {'; '.join(reason_detail)}. High spend=${spend:.2f} with {orders} orders. Negate to eliminate waste."
            )

        # Competitor price is similar or higher, and our product has feature parity or advantages
        # Decision: Lower bid / reduce PP modifier rather than blind negation!
        price_similar_or_higher = (comp_price >= our_price * 0.85) if (comp_price > 0 and our_price > 0) else True
        if price_similar_or_higher:
            suggested_bid = max(0.20, round(cpc * 0.7, 2)) if cpc > 0 else 0.50
            return (
                "Lower_Bid",
                f"Lower Base Bid to ${suggested_bid:.2f} / Reduce PP modifier",
                f"Feature & price competitiveness exists (competitor ${comp_price:.2f} vs our ${our_price:.2f}, stars {comp_stars} vs {our_stars}). High ACOS is due to excessive CPC (${cpc:.2f}); lower bid by 30% to harvest at low CPC instead of blind negation."
            )
        else:
            return (
                "Negative_Target",
                "Add Negative Target",
                f"Competitor conversion advantage; high spend (${spend:.2f}) with zero/poor returns (ACOS={c_acos:.1f}%)."
            )

    return ("Keep_Monitoring", "Keep current bid", "Performance within normal observation range")


def extract_ngrams(text: str) -> list[tuple[str, str]]:
    """Extract 1-grams and 2-grams after cleaning and stopword removal.

    Returns list of (ngram, gram_type).
    """
    words = re.findall(r"[\w]+", (text or "").lower(), re.UNICODE)
    valid_words = [w for w in words if w not in STOP_WORDS and len(w) > 1 and not w.isdigit()]
    out = []
    for w in valid_words:
        out.append((w, "1-gram"))
    for i in range(len(valid_words) - 1):
        bigram = f"{valid_words[i]} {valid_words[i+1]}"
        out.append((bigram, "2-gram"))
    return out


def tsv(path: Path, header: str, lines: list[str]) -> None:
    path.write_text(header + "\n" + "\n".join(lines), encoding="utf-8")


# ==============================================================================
# ACTIVE NEGATIVE AUDIT & RANK PROTECTION MODULE
# ==============================================================================

def extract_active_negatives(sts: list[dict]) -> tuple[dict[tuple[str, str], dict], set[tuple[str, str]], set[str]]:
    """Extract all active negative search term records from search_terms report.

    Lingxing's ad_campaign_search_term_report returns negative status in `st_negative`
    (e.g., '广告组精准否定', '广告组已否定') and `kw_negative`.

    Returns:
    - active_neg_dict: (campaign_name, query_lower) -> aggregated metrics dict
    - active_neg_camp_pairs: set of (campaign_name_lower, query_lower)
    - all_neg_queries: set of query_lower across all campaigns
    """
    active_neg_dict = {}
    active_neg_camp_pairs = set()
    all_neg_queries = set()

    for r in sts:
        st_neg = r.get("st_negative")
        kw_neg = r.get("kw_negative")
        neg_val = str(st_neg or kw_neg or "").strip()
        if not neg_val or neg_val in ("--", "0", "null", "None"):
            continue

        c_name = r.get("campaign_name") or ""
        q = (r.get("query") or "").strip()
        if not q or not c_name:
            continue

        ql = q.lower()
        cl = c_name.lower()
        key = (c_name, ql)
        active_neg_camp_pairs.add((cl, ql))
        all_neg_queries.add(ql)

        spend = num(r.get("spends"))
        sales = num(r.get("sales"))
        orders = int(num(r.get("orders")))
        clicks = int(num(r.get("clicks")))

        if key not in active_neg_dict:
            active_neg_dict[key] = {
                "campaign": c_name,
                "query": q,
                "neg_status": neg_val,
                "match_type": r.get("match_type") or r.get("target_match_type") or "",
                "target_text": r.get("keyword_text") or r.get("target_text") or "",
                "spend": spend,
                "sales": sales,
                "orders": orders,
                "clicks": clicks,
            }
        else:
            active_neg_dict[key]["spend"] += spend
            active_neg_dict[key]["sales"] += sales
            active_neg_dict[key]["orders"] += orders
            active_neg_dict[key]["clicks"] += clicks

    return active_neg_dict, active_neg_camp_pairs, all_neg_queries


def find_exact_harvest_campaign(camps: list[dict], kws: list[dict]) -> str:
    """Find the best ENABLED campaign for harvesting exact terms (e.g. *-exact-prove or *-精准)."""
    for c in camps:
        if c.get("state") == "enabled":
            name = c.get("name") or c.get("campaign_name") or ""
            if "exact-prove" in name.lower() or "prove" in name.lower():
                return name
    for c in camps:
        if c.get("state") == "enabled":
            name = c.get("name") or c.get("campaign_name") or ""
            if "exact" in name.lower() or "精准" in name:
                return name
    for k in kws:
        if k.get("state") == "enabled":
            name = k.get("campaign_name") or ""
            if "exact-prove" in name.lower() or "prove" in name.lower():
                return name
    enabled_camps = [c for c in camps if c.get("state") == "enabled"]
    if enabled_camps:
        first_c = enabled_camps[0].get("name") or ""
        parts = first_c.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}-SP-EXACT-PROVE"
    return "SP-EXACT-PROVE"


def calc_dynamic_migration_bid(
    orig_item: dict,
    dest_camp_name: str,
    camp_lookup: dict,
    prior_target: dict = None,
) -> tuple[float, float, str]:
    """Calculate dynamic inverted base bid for keyword migration/unpausing.

    Formula:
    recom_base_bid = anchor_cpc / (1 + dest_tos / 100.0)
    effective_tos_bid = recom_base_bid * (1 + dest_tos / 100.0)
    """
    dest_camp_obj = camp_lookup.get(dest_camp_name, {})
    dest_tos = float(dest_camp_obj.get("tos") or dest_camp_obj.get("placement_top") or 0.0)

    orig_orders = orig_item.get("orders", 0)
    orig_clicks = orig_item.get("clicks", 0)
    orig_spend = orig_item.get("spend", 0.0)
    orig_cpc = round(orig_spend / orig_clicks, 2) if orig_clicks else 0.60

    if orig_orders >= 1:
        anchor_cpc = orig_cpc
        cpc_note = f"原活动历史出单 CPC (${anchor_cpc:.2f})"
    elif orig_clicks >= 5:
        anchor_cpc = round(orig_cpc * 0.85, 2)
        cpc_note = f"原活动测试 CPC 折扣价 (${anchor_cpc:.2f})"
    else:
        anchor_cpc = round(orig_cpc, 2) if orig_cpc > 0 else 0.65
        cpc_note = f"基准出价参考 (${anchor_cpc:.2f})"

    recom_base_bid = round(anchor_cpc / (1.0 + dest_tos / 100.0), 2)
    recom_base_bid = max(0.20, min(recom_base_bid, 1.20))
    eff_tos_bid = round(recom_base_bid * (1.0 + dest_tos / 100.0), 2)

    prior_notes = []
    if prior_target:
        prior_bid = float(prior_target.get("bid") or 0)
        prior_cpc = float(prior_target.get("cpc") or 0)
        if prior_bid > 0 and prior_cpc > anchor_cpc:
            prior_notes.append(
                f"此前在精准仓基础竞价设置偏高 (${prior_bid:.2f})，叠加 TOS +{int(dest_tos)}% 导致实际 CPC 飙升至 ${prior_cpc:.2f} 而误暂停"
            )

    calc_explanation = (
        f"依据{cpc_note}与目标仓 TOS +{int(dest_tos)}% 溢价，"
        f"动态倒推建议基础竞价为 ${recom_base_bid:.2f}（折合顶部出价 ${eff_tos_bid:.2f}，精准咬合出单成本）。"
    )
    if prior_notes:
        calc_explanation += f" 历史对账：{'; '.join(prior_notes)}。"

    return recom_base_bid, eff_tos_bid, calc_explanation


def infer_search_term_placement(
    query: str,
    spend: float,
    sales: float,
    orders: int,
    clicks: int,
    impressions: int,
    c_name: str,
    match_type: str,
    target_text: str,
    bucket: str,
    is_strong: bool,
    v_tier: str,
    sr_display: str,
    camp_lookup: dict,
    kw_lookup: dict,
    target_acos: float = 25.0,
) -> dict:
    """Infer search term placement using multi-dimensional behavioral fingerprinting.

    Dimensions:
    1. CTR fingerprint (TOS: >=1.2%~3%+, PP carousel: <=0.40%, ROS: 0.40%~1.2%)
    2. CPC vs Base Bid & placement modifiers ceiling:
       MaxBid_non_tos = BaseBid * (1 + max(PP%, ROS%)/100)
       If CPC > MaxBid_non_tos * 1.02: Mathematically CONFIRMED_TOS
    3. Impression-to-click grazing ratio (Thousands of impressions with low CTR = detail page carousel)
    4. Campaign macro placement context

    Strategic Action Categories:
    - TRAPPED_CORE_HERO: Relevant core/feature term trapped on PP/ROS -> DO NOT NEGATE! Elevate_To_TOS / Harvest_Exact_TOS.
    - FAILED_AT_TOS: Term already received premier TOS traffic and failed -> Throttle_TOS_Bid / Negative_Exact (if non-core).
    - HARD_MISFIT: Physical conflict or competitor brand -> Negative_Exact regardless of placement.
    - PERIPHERAL_BLEED: Low-intent peripheral terms -> Lower_Bid or Negative_Exact.
    """
    camp_obj = camp_lookup.get(c_name, {})
    kw_key = f"{c_name}::{match_type}::{target_text.lower().strip()}"
    kw_obj = kw_lookup.get(kw_key, {})

    ctr = round((clicks / impressions * 100.0) if impressions > 0 else 0.0, 2)
    cpc = round((spend / clicks) if clicks > 0 else 0.0, 2)
    base_bid = float(kw_obj.get("bid") or kw_obj.get("default_bid") or camp_obj.get("default_bid") or 0.60)
    tos_pct = float(camp_obj.get("tos", 0))
    pp_pct = float(camp_obj.get("pp", 0))
    ros_pct = float(camp_obj.get("ros", 0))
    max_non_tos = round(base_bid * (1.0 + max(pp_pct, ros_pct) / 100.0), 2)
    max_tos_bid = round(base_bid * (1.0 + tos_pct / 100.0), 2)

    # Inferred placement
    if base_bid > 0 and cpc > (max_non_tos * 1.02):
        inferred_pos = "CONFIRMED_TOS"
        pos_reason = f"实际CPC (${cpc:.2f}) > 非TOS最高竞价上限 (${max_non_tos:.2f})，数学确定为首页顶部扣费"
    elif ctr >= 1.5:
        inferred_pos = "PROBABLE_TOS"
        pos_reason = f"高CTR ({ctr:.2f}%) 符合首页顶部黄金展示位特征"
    elif ctr <= 0.40 and impressions >= 500:
        inferred_pos = "CONFIRMED_PP_CAROUSEL"
        pos_reason = f"极低CTR ({ctr:.2f}%) 与高曝光 ({impressions})，典型商品详情页轮播位/底部位"
    elif ctr <= 0.65:
        inferred_pos = "PROBABLE_PP_OR_ROS"
        pos_reason = f"低CTR ({ctr:.2f}%)，偏向商品页或搜索结果底部位"
    else:
        inferred_pos = "MIXED_PLACEMENT"
        pos_reason = f"中等CTR ({ctr:.2f}%) 与中等CPC (${cpc:.2f})，混合展示位"

    # Actionable diagnosis
    action_type = None
    recommended_val = None
    diagnostic_reason = None
    strategy_tag = "NORMAL"

    if orders >= 1:
        if acos(spend, sales) <= target_acos and (orders >= 3 or spend >= 10):
            strategy_tag = "CONVERTING_WINNER"
        else:
            strategy_tag = "CONVERTING_MARGINAL"
    elif clicks >= 8 or spend >= 6:
        if bucket in ("is_not", "competitor_brand"):
            strategy_tag = "HARD_MISFIT"
            action_type = "Negative_Exact"
            recommended_val = "Add Negative Exact"
            diagnostic_reason = (
                f"物理冲突或竞品品牌词 ({bucket})，已累积 {clicks} 点击 0 订单 (花费 ${spend:.2f})，"
                f"无论在任何位置展示均无法形成正向转化，果断精准否定彻底止血。"
            )
        elif is_strong or bucket in ("core", "feature") or v_tier in ("MEGA_HEAD", "CORE_HEAD", "MID_TAIL"):
            if inferred_pos in ("CONFIRMED_PP_CAROUSEL", "PROBABLE_PP_OR_ROS"):
                strategy_tag = "TRAPPED_CORE_HERO"
                action_type = "Elevate_To_TOS"
                recommended_val = "提权拯救：提高基础竞价 15-20% 或提高活动 TOS% 至 60-80%，或移入专用精准仓测试顶部"
                diagnostic_reason = (
                    f"核心主粮/功能词（ABA梯队: {v_tier}，排名: {sr_display}），当前 CTR 仅 {ctr:.2f}%、CPC ${cpc:.2f}，"
                    f"数据指纹证实大量点击耗散在转化极低的商品详情页或页面底部位 ({inferred_pos})，从未在首页顶部（TOS）充分曝光。"
                    f"严禁盲目否定！应提权测试首屏黄金位真实出单潜力，或提取至独立精准仓专打顶部收割！"
                )
            elif inferred_pos in ("CONFIRMED_TOS", "PROBABLE_TOS"):
                strategy_tag = "FAILED_AT_TOS"
                action_type = "Throttle_TOS_Bid"
                recommended_val = "降竞价防守：基础竞价下调 25-30% 并排查 Listing 首屏转化壁垒"
                diagnostic_reason = (
                    f"核心主粮词在首页顶部黄金位已积累 {clicks} 次高意图点击 (CTR: {ctr:.2f}%, CPC: ${cpc:.2f}) 依然未出单。"
                    f"已享受顶级流量红利但无法转化，说明首屏存在价格、评分数或痛点呈现劣势。"
                    f"严禁继续盲目加价或推高 TOS；应降竞价退守防守位，同时重点排查主图、Coupon 与竞品差异化痛点。"
                )
            else:
                strategy_tag = "MIXED_CORE_TEST"
                action_type = "Refine_Bid_Placement"
                recommended_val = "隔离测试：移入精准仓独立控价测试"
                diagnostic_reason = (
                    f"核心功能词混杂在宽泛活动中耗损 (${spend:.2f}, {clicks} 点击)，"
                    f"建议移入精准仓独立控制基础竞价与位置溢价，排查真实转化能力。"
                )
        else:
            # Adjacent / peripheral
            strategy_tag = "PERIPHERAL_BLEED"
            if clicks >= 18 or spend >= 12 or inferred_pos in ("CONFIRMED_TOS", "PROBABLE_TOS"):
                action_type = "Negative_Exact"
                recommended_val = "Add Negative Exact"
                diagnostic_reason = f"边缘弱相关词在 {inferred_pos} 耗损，点击={clicks}，花费=${spend:.2f}，0订单，建议精准否定。"
            else:
                action_type = "Lower_Bid"
                recommended_val = "Lower Base Bid 25%"
                diagnostic_reason = f"边缘词转化动力不足，点击={clicks}，花费=${spend:.2f}，降低基础竞价控费观察。"

    return {
        "query": query,
        "campaign": c_name,
        "bucket": bucket,
        "volume_tier": v_tier,
        "searchrank": sr_display,
        "spend": spend,
        "sales": sales,
        "orders": orders,
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "cpc": cpc,
        "base_bid": base_bid,
        "max_non_tos": max_non_tos,
        "max_tos_bid": max_tos_bid,
        "inferred_placement": inferred_pos,
        "placement_evidence": pos_reason,
        "strategy_tag": strategy_tag,
        "action_type": action_type,
        "recommended_value": recommended_val,
        "diagnostic_reason": diagnostic_reason,
    }


def audit_active_negatives(
    active_neg_dict: dict[tuple[str, str], dict],
    profile: dict,
    rules: dict,
    sr_map: dict[str, int],
    kws: list[dict] = None,
    camps: list[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """Audit existing active negatives cross-referencing portfolio targeting.

    Takes into account the automated Keyword Migration Funnel (移词规则):
    - When Broad produces an order, it is promoted to Exact and negated in Broad.
    - If actively targeted in Exact: PROMOTED_MIGRATED_PROTECTED (Maintain negative in Broad).
    - If migrated to Exact but PAUSED in Exact: MIGRATED_BUT_PAUSED_IN_EXACT (Unpause in Exact, keep negated in Broad).
    - If untargeted anywhere and high intent/converting: TRUE_ORPHAN_NEGATIVE (Harvest to Exact).
    - Physical conflicts and competitor brands: SAFE_HARD_NEGATIVE (Maintain negative).
    - Own ASIN in Auto: SAFE_ASIN_DEFENSE (Maintain negative).

    Returns:
    - audit_records: list of audited items for active_negatives_audit.tsv
    - unnegative_adjustments: list of Unpause_Keyword and Harvest_To_Exact adjustments for bulk_adjustments.tsv
    """
    audit_records = []
    unnegative_adjustments = []

    harvest_camp = find_exact_harvest_campaign(camps or [], kws or [])
    camp_lookup = {c.get("name") or c.get("campaign_name"): c for c in (camps or [])}

    # Build portfolio-wide keyword targeting lookup
    targeted_map = defaultdict(list)
    if kws:
        for k in kws:
            txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
            if not txt:
                continue
            targeted_map[txt].append({
                "campaign": k.get("campaign_name") or "",
                "match": k.get("match_type") or "exact",
                "state": (k.get("state") or "").lower(),
                "bid": num(k.get("bid")),
                "cpc": num(k.get("cpc")),
                "spend": num(k.get("spends")),
                "sales": num(k.get("sales")),
                "orders": int(num(k.get("orders"))),
            })

    for (c_name, ql), item in sorted(active_neg_dict.items(), key=lambda kv: -kv[1]["spend"]):
        q = item["query"]
        spend = item["spend"]
        sales = item["sales"]
        orders = item["orders"]
        clicks = item["clicks"]
        cpc = round(spend / clicks, 2) if clicks else 0.0
        cvr = round(orders / clicks * 100, 1) if clicks else 0.0
        c_acos = round(spend / sales * 100, 1) if sales > 0 else (9999.0 if spend > 0 else 0.0)

        bucket, match_reason, is_strong_relevance = classify_query(q, profile, rules)
        sr, v_tier = get_searchrank(q, sr_map)
        sr_display = str(sr) if sr is not None else "--"

        is_hard_misfit = bucket.startswith("is_not") or bucket in ("is_not", "competitor_brand")
        is_own_asin = (bucket == "asin" and (ql == str(profile.get("asin") or "").lower()))

        # 1. Physical conflicts & Competitor brands
        if is_hard_misfit:
            audit_status = "SAFE_HARD_NEGATIVE"
            recom_action = "Keep_Negated"
            recom_val = "Maintain Negative (Permanent Misfit)"
            detail_reason = f"Confirmed physical/category conflict or excluded competitor ({match_reason}). Hard negative eliminates structural waste/bad reviews."

        # 2. Own ASIN defense in Auto
        elif is_own_asin:
            audit_status = "SAFE_ASIN_DEFENSE"
            recom_action = "Keep_Negated"
            recom_val = "Maintain Negative in Auto (Own ASIN)"
            detail_reason = "Own ASIN negated in Auto to prevent self-cannibalization. Normal practice."

        # 3. Portfolio targeting cross-reference (Migration Funnel Analysis)
        else:
            targets = targeted_map.get(ql, [])
            enabled_other = [t for t in targets if t["state"] == "enabled" and t["campaign"] != c_name]
            paused_other = [t for t in targets if t["state"] != "enabled" and t["campaign"] != c_name]

            # Case A: Already promoted and actively running in Exact / dedicated campaigns
            if enabled_other:
                audit_status = "PROMOTED_MIGRATED_PROTECTED"
                recom_action = "Keep_Negated_Migrated"
                dest_desc = "; ".join([f"{t['campaign']}[{t['match']}, bid=${t['bid']:.2f}, ord={t['orders']}]" for t in enabled_other])
                tot_exact_ord = sum(t["orders"] for t in enabled_other)
                recom_val = f"Maintain Negative in {c_name} (Active in Exact Funnel: {tot_exact_ord} orders)"
                detail_reason = (
                    f"Automated Migration Funnel Active: Already targeted and active in destination campaign(s): {dest_desc}. "
                    f"Negation in {c_name} prevents internal cannibalization and protects dedicated Exact traffic. DO NOT un-negate in Broad!"
                )

            # Case B: Migrated to Exact, but currently PAUSED (Traffic Blackout!)
            elif paused_other:
                # Check if it's a converting or high-relevance query worth reviving
                if is_strong_relevance or orders >= 1 or (sr is not None and sr < 40000):
                    # Pick best destination campaign to unpause (prefer exact-prove or highest past orders)
                    best_dest = sorted(paused_other, key=lambda x: -x["orders"])[0]
                    dest_camp = best_dest["campaign"]
                    dest_match = best_dest["match"]
                    dest_camp_obj = camp_lookup.get(dest_camp, {})
                    dest_camp_state = dest_camp_obj.get("state", "enabled")

                    if dest_camp_state != "enabled":
                        audit_status = "MIGRATED_TO_PAUSED_CAMPAIGN"
                        recom_action = "Keep_Negated_In_Broad"
                        recom_val = f"Maintain Negated in {c_name} (Destination {dest_camp} is Paused)"
                        detail_reason = (
                            f"该词此前移入的活动 {dest_camp} 目前已整组暂停，原广泛仓保持否定防内耗；"
                            f"如需重新测试该词，建议启用目标活动或在其他活跃精准仓投放。"
                        )
                    else:
                        audit_status = "MIGRATED_BUT_PAUSED_IN_EXACT"
                        recom_action = "Unpause_In_Exact"
                        recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                            item, dest_camp, camp_lookup, prior_target=best_dest
                        )
                        recom_val = f"Unpause in {dest_camp} (Exact, 基础竞价 ${recom_bid:.2f}); Maintain Negative in {c_name}"
                        detail_reason = (
                            f"移词断流全账户堵死：该词已移词并在原活动 ({c_name}) 否定，但在精准仓 ({dest_camp}) 处于暂停状态，导致全账户完全断流 "
                            f"({orders}单, SR={sr_display}, {v_tier})。{bid_calc_desc} "
                            f"处理：在 {dest_camp} 重新开启投放（基础竞价 ${recom_bid:.2f}）；原活动 {c_name} 继续保持否定防内耗。"
                        )
                        unnegative_adjustments.append({
                            "action_type": "Unpause_Keyword",
                            "campaign": dest_camp,
                            "target_entity": q,
                            "match_type": dest_match,
                            "current_value": "state=paused",
                            "recommended_value": f"state=enabled (基础竞价 ${recom_bid:.2f})",
                            "reason": f"移词断流恢复：在 {c_name} 保持否定防内耗，在 {dest_camp} 开启投放。{bid_calc_desc}",
                        })
                else:
                    audit_status = "SOFT_MONITOR"
                    recom_action = "Keep_Paused"
                    recom_val = f"Maintain Paused in {paused_other[0]['campaign']} and Negated in {c_name}"
                    detail_reason = f"Peripheral or low conversion term paused across campaigns (spend=${spend:.2f}, ord={orders}). Maintain current status."

            # Case C: Not targeted in ANY other campaign (True Orphan or Soft Monitor)
            else:
                misfit_found = False
                for pat_item in rules.get("is_not_patterns", []):
                    pat = pat_item if isinstance(pat_item, str) else pat_item.get("pattern", "")
                    if pat and re.search(pat, q, re.I):
                        misfit_found = True
                        break

                if misfit_found:
                    audit_status = "SAFE_HARD_NEGATIVE"
                    recom_action = "Keep_Negated"
                    recom_val = "Maintain Negative (Permanent Misfit)"
                    detail_reason = f"Confirmed physical/category conflict via rules. Hard negative eliminates structural waste/bad reviews."
                else:
                    is_core_asset = (
                        bucket == "brand"
                        or is_strong_relevance
                        or bucket in ("core_feature", "core_or_title")
                        or bucket.startswith("core_")
                        or orders >= 1
                        or (sr is not None and sr < 35000)
                    )

                    harvest_camp_obj = camp_lookup.get(harvest_camp, {})
                    harvest_camp_state = harvest_camp_obj.get("state", "enabled")

                    if is_core_asset and harvest_camp_state == "enabled":
                        audit_status = "TRUE_ORPHAN_NEGATIVE"
                        recom_action = "Harvest_To_Exact"
                        recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                            item, harvest_camp, camp_lookup, prior_target=None
                        )
                        recom_val = f"Harvest into {harvest_camp} (Exact, 基础竞价 ${recom_bid:.2f}) or Un-negate in Broad"
                        detail_reason = (
                            f"全账户未投放孤儿核心资产：强相关意图/出单/搜索词 ({orders}单, SR={sr_display}, {v_tier})，"
                            f"但在 {c_name} 被否定且全账户未在任何活动投放。{bid_calc_desc} "
                            f"处理：移入精准仓 ({harvest_camp}) 收割（基础竞价 ${recom_bid:.2f}）；原活动 {c_name} 保持否定防内耗。"
                        )
                        unnegative_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": harvest_camp,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_value": f"Negated in {c_name}, untargeted in portfolio",
                            "recommended_value": f"Add Exact Target (基础竞价 ${recom_bid:.2f})",
                            "reason": f"孤儿核心资产收割：在 {c_name} 保持否定防内耗，移入 {harvest_camp} 独立收割。{bid_calc_desc}",
                        })
                    else:
                        audit_status = "SOFT_MONITOR"
                        recom_action = "Keep_Negated_Monitor"
                        recom_val = f"Maintain Negated in {c_name}"
                        detail_reason = f"Untargeted search term (orders={orders}, spend=${spend:.2f}); maintain negative and monitor."

                    audit_status = "SOFT_MONITOR"
                    recom_action = "Keep_Negated_Offpeak"
                    recom_val = "Maintain Negative; Re-test in Q4 Holiday Season"
                    detail_reason = f"Adjacent/peripheral query with low off-peak momentum (spend=${spend:.2f}, ord={orders}). Keep negated during routine period."

        audit_records.append({
            "campaign": c_name,
            "query": q,
            "neg_status": item["neg_status"],
            "bucket": bucket,
            "volume_tier": v_tier,
            "searchrank": sr_display,
            "spend": spend,
            "sales": sales,
            "orders": orders,
            "clicks": clicks,
            "cvr": cvr,
            "cpc": cpc,
            "audit_status": audit_status,
            "recommended_action": recom_action,
            "recommended_value": recom_val,
            "reason": detail_reason,
        })

    return audit_records, unnegative_adjustments


def audit_keyword_misfits(kws: list[dict], rules: dict, camps: list[dict] = None) -> list[dict]:
    """Audit enabled keywords in enabled campaigns to detect physical misfits targeting wrong attributes."""
    adjustments = []
    camp_lookup = {c.get("name") or c.get("campaign_name"): c for c in (camps or [])}
    is_not_patterns = rules.get("is_not_patterns", [])
    for k in kws:
        if k.get("state") != "enabled":
            continue
        c_name = k.get("campaign_name") or ""
        if camp_lookup and camp_lookup.get(c_name, {}).get("state") != "enabled":
            continue
        text = str(k.get("keyword_text") or k.get("targeting_text") or "").strip()
        bid = num(k.get("bid"))
        for pat_item in is_not_patterns:
            pat = pat_item if isinstance(pat_item, str) else pat_item.get("pattern", "")
            label = "is_not" if isinstance(pat_item, str) else pat_item.get("label", "is_not")
            if pat and re.search(pat, text, re.I):
                adjustments.append({
                    "action_type": "Pause_Keyword",
                    "campaign": c_name,
                    "target_entity": text,
                    "match_type": k.get("match_type") or "exact",
                    "current_value": f"bid=${bid:.2f}, state=enabled, label={label}",
                    "recommended_value": "Pause Keyword Immediately",
                    "reason": f"Physical attribute conflict ({label}, pattern: {pat}). Product is blade grinder; targeting this generates customer dissatisfaction and returns.",
                })
                break
    return adjustments


# ==============================================================================
# LOCAL DATABASE & PERSISTENT LEDGER MODULE
# ==============================================================================

def resolve_db_dirs(user_db_dir: str | None, src: Path) -> tuple[Path, Path, Path, Path]:
    """Resolve database root, daily_records, ledger, and snapshots directories."""
    if user_db_dir:
        root = Path(user_db_dir)
    elif Path("db").exists() or Path("db").is_dir():
        root = Path("db")
    elif (src.parent / "db").exists() or (src.parent / "db").is_dir():
        root = src.parent / "db"
    elif (src.parent.parent / "db").exists():
        root = src.parent.parent / "db"
    else:
        root = Path("db")

    daily_records_dir = root / "daily_records"
    ledger_dir = root / "ledger"
    snapshots_dir = root / "snapshots"
    daily_records_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    return root, daily_records_dir, ledger_dir, snapshots_dir


def get_snapshot_key(meta: dict, profile: dict, camps: list[dict] | None = None) -> str:
    """Derive unique snapshot filename key from portfolio or ASIN."""
    port_id = str(meta.get("portfolio_id") or "").strip()
    if port_id and port_id not in ("None", "0", ""):
        return f"portfolio_{port_id}"
    port_name = str(meta.get("portfolio_name") or "").strip()
    if port_name and port_name not in ("None", ""):
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", port_name)
        return f"portfolio_{clean_name}"

    # Check if campaigns share a common portfolio_id
    if camps:
        pf_ids = [str(c.get("portfolio_id")) for c in camps if c.get("portfolio_id") and str(c.get("portfolio_id")) not in ("None", "0", "")]
        if pf_ids:
            from collections import Counter
            top_pf = Counter(pf_ids).most_common(1)[0][0]
            return f"portfolio_{top_pf}"

    asin = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper().strip()
    country = str(meta.get("country") or "US").upper().strip()
    return f"{asin}_{country}"


def load_snapshot(snapshot_path: Path) -> dict:
    if snapshot_path.exists():
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "updated_at": "",
        "campaigns": {},
        "keywords": {},
        "targets": {},
        "active_cooldowns": [],
    }


def diff_and_track_cooldown(
    prev_snap: dict,
    curr_camps: list[dict],
    curr_kws: list[dict],
    curr_targets: list[dict],
    cooldown_days: int = 7,
) -> tuple[dict, list[dict], set[str], list[str], list[dict]]:
    """Compare current control variables vs previous snapshot.

    Returns:
    - new_snap: the updated snapshot dict to save
    - active_cooldowns: list of ongoing cooldown records
    - locked_campaigns: set of campaign names currently locked in cooldown
    - diff_events: human-readable diff descriptions
    - new_diff_items: structured diff items with baseline metrics for ledger
    """
    now = datetime.now()
    now_iso = now.isoformat()

    # Build current state maps
    curr_camp_map = {}
    for c in curr_camps:
        name = c.get("name")
        if not name:
            continue
        curr_camp_map[name] = {
            "name": name,
            "tos": int(c.get("tos", 0)),
            "pp": int(c.get("pp", 0)),
            "ros": int(c.get("ros", 0)),
            "budget": float(c.get("budget", 0.0)),
            "state": str(c.get("state", "")),
        }

    curr_kw_map = {}
    for k in curr_kws:
        c_name = k.get("campaign_name") or ""
        match = k.get("match_type") or ""
        text = k.get("keyword_text") or ""
        if not text:
            continue
        key = f"{c_name}::{match}::{text.lower().strip()}"
        curr_kw_map[key] = {
            "campaign": c_name,
            "text": text,
            "match": match,
            "bid": num(k.get("bid")),
            "state": str(k.get("state", "")),
        }

    curr_target_map = {}
    for t in curr_targets:
        c_name = t.get("campaign_name") or ""
        ttype = t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or ""
        text = t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or ""
        if not text:
            continue
        key = f"{c_name}::{ttype}::{text.lower().strip()}"
        curr_target_map[key] = {
            "campaign": c_name,
            "text": text,
            "type": ttype,
            "bid": num(t.get("bid")),
            "state": str(t.get("state", "")),
        }

    prev_camps = prev_snap.get("campaigns") or {}
    prev_kws = prev_snap.get("keywords") or {}
    prev_targets = prev_snap.get("targets") or {}
    prev_cooldowns = prev_snap.get("active_cooldowns") or []

    # 1. Prune expired cooldowns and retain active ones
    active_cooldowns = []
    locked_campaigns = set()
    for item in prev_cooldowns:
        c_until_str = item.get("cooldown_until")
        if c_until_str:
            try:
                c_until = datetime.fromisoformat(c_until_str)
                if now < c_until:
                    # Still in cooldown
                    changed_at = datetime.fromisoformat(item.get("changed_at", now_iso))
                    days_elapsed = max(0, (now - changed_at).days)
                    item["days_elapsed"] = days_elapsed
                    item["days_remaining"] = max(0, (c_until - now).days)
                    active_cooldowns.append(item)
                    if item.get("campaign"):
                        locked_campaigns.add(item["campaign"])
            except Exception:
                pass

    diff_events = []
    new_diff_items = []

    # 2. Check campaign diffs (only if prev_camps exists)
    if prev_camps:
        for name, curr in curr_camp_map.items():
            if name in prev_camps:
                prev = prev_camps[name]
                matching_c = next((x for x in curr_camps if x.get("name") == name), {})
                base_cpc = round(num(matching_c.get("cpc_3d") or matching_c.get("cpc_7d") or matching_c.get("cpc")), 2)
                base_imp = round(num(matching_c.get("daily_imp_3d") or matching_c.get("daily_imp_30d")), 1)
                base_acos = round(num(matching_c.get("acos_7d") or matching_c.get("acos")), 1)

                # Check TOS
                if curr["tos"] != prev.get("tos"):
                    msg = f"TOS changed: {prev.get('tos')}% -> {curr['tos']}%"
                    diff_events.append(f"Campaign '{name}': {msg}")
                    cd = {
                        "entity_type": "campaign_tos",
                        "campaign": name,
                        "field": "tos",
                        "old_value": prev.get("tos"),
                        "new_value": curr["tos"],
                        "changed_at": now_iso,
                        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
                        "days_elapsed": 0,
                        "days_remaining": cooldown_days,
                        "detail": msg,
                    }
                    active_cooldowns.append(cd)
                    locked_campaigns.add(name)
                    new_diff_items.append({
                        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items)+1:02d}",
                        "change_date": now.strftime("%Y-%m-%d"),
                        "changed_at": now_iso,
                        "campaign": name,
                        "entity_type": "campaign_tos",
                        "entity_name": name,
                        "field": "tos",
                        "old_val": f"{prev.get('tos')}%",
                        "new_val": f"{curr['tos']}%",
                        "baseline_cpc": base_cpc,
                        "baseline_daily_imp": base_imp,
                        "baseline_acos": base_acos,
                        "detail": msg,
                    })

                # Check ROS
                if curr["ros"] != prev.get("ros"):
                    msg = f"ROS changed: {prev.get('ros')}% -> {curr['ros']}%"
                    diff_events.append(f"Campaign '{name}': {msg}")
                    cd = {
                        "entity_type": "campaign_ros",
                        "campaign": name,
                        "field": "ros",
                        "old_value": prev.get("ros"),
                        "new_value": curr["ros"],
                        "changed_at": now_iso,
                        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
                        "days_elapsed": 0,
                        "days_remaining": cooldown_days,
                        "detail": msg,
                    }
                    active_cooldowns.append(cd)
                    locked_campaigns.add(name)
                    new_diff_items.append({
                        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items)+1:02d}",
                        "change_date": now.strftime("%Y-%m-%d"),
                        "changed_at": now_iso,
                        "campaign": name,
                        "entity_type": "campaign_ros",
                        "entity_name": name,
                        "field": "ros",
                        "old_val": f"{prev.get('ros')}%",
                        "new_val": f"{curr['ros']}%",
                        "baseline_cpc": base_cpc,
                        "baseline_daily_imp": base_imp,
                        "baseline_acos": base_acos,
                        "detail": msg,
                    })

                # Check State (e.g. paused)
                if curr["state"] != prev.get("state"):
                    msg = f"State changed: {prev.get('state')} -> {curr['state']}"
                    diff_events.append(f"Campaign '{name}': {msg}")
                    cd = {
                        "entity_type": "campaign_state",
                        "campaign": name,
                        "field": "state",
                        "old_value": prev.get("state"),
                        "new_value": curr["state"],
                        "changed_at": now_iso,
                        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
                        "days_elapsed": 0,
                        "days_remaining": cooldown_days,
                        "detail": msg,
                    }
                    active_cooldowns.append(cd)
                    locked_campaigns.add(name)
                    new_diff_items.append({
                        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items)+1:02d}",
                        "change_date": now.strftime("%Y-%m-%d"),
                        "changed_at": now_iso,
                        "campaign": name,
                        "entity_type": "campaign_state",
                        "entity_name": name,
                        "field": "state",
                        "old_val": str(prev.get("state")),
                        "new_val": str(curr["state"]),
                        "baseline_cpc": base_cpc,
                        "baseline_daily_imp": base_imp,
                        "baseline_acos": base_acos,
                        "detail": msg,
                    })

    # 3. Check keyword diffs (bids)
    if prev_kws:
        for key, curr in curr_kw_map.items():
            if key in prev_kws:
                prev = prev_kws[key]
                if abs(curr["bid"] - prev.get("bid", 0.0)) >= 0.02 and curr["bid"] > 0 and prev.get("bid", 0) > 0:
                    msg = f"Keyword '{curr['text']}' [{curr['match']}] bid changed: ${prev.get('bid'):.2f} -> ${curr['bid']:.2f}"
                    diff_events.append(f"Campaign '{curr['campaign']}': {msg}")
                    cd = {
                        "entity_type": "keyword_bid",
                        "campaign": curr["campaign"],
                        "entity_key": key,
                        "field": "bid",
                        "old_value": prev.get("bid"),
                        "new_value": curr["bid"],
                        "changed_at": now_iso,
                        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
                        "days_elapsed": 0,
                        "days_remaining": cooldown_days,
                        "detail": msg,
                    }
                    active_cooldowns.append(cd)
                    locked_campaigns.add(curr["campaign"])
                    matching_c = next((x for x in curr_camps if x.get("name") == curr["campaign"]), {})
                    base_cpc = round(num(matching_c.get("cpc_3d") or matching_c.get("cpc_7d") or matching_c.get("cpc")), 2)
                    base_imp = round(num(matching_c.get("daily_imp_3d") or matching_c.get("daily_imp_30d")), 1)
                    base_acos = round(num(matching_c.get("acos_7d") or matching_c.get("acos")), 1)
                    new_diff_items.append({
                        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items)+1:02d}",
                        "change_date": now.strftime("%Y-%m-%d"),
                        "changed_at": now_iso,
                        "campaign": curr["campaign"],
                        "entity_type": "keyword_bid",
                        "entity_name": f"{curr['text']} [{curr['match']}]",
                        "field": "bid",
                        "old_val": f"${prev.get('bid'):.2f}",
                        "new_val": f"${curr['bid']:.2f}",
                        "baseline_cpc": base_cpc,
                        "baseline_daily_imp": base_imp,
                        "baseline_acos": base_acos,
                        "detail": msg,
                    })

    # 4. Check target diffs (bids)
    if prev_targets:
        for key, curr in curr_target_map.items():
            if key in prev_targets:
                prev = prev_targets[key]
                if abs(curr["bid"] - prev.get("bid", 0.0)) >= 0.02 and curr["bid"] > 0 and prev.get("bid", 0) > 0:
                    msg = f"Target '{curr['text']}' [{curr['type']}] bid changed: ${prev.get('bid'):.2f} -> ${curr['bid']:.2f}"
                    diff_events.append(f"Campaign '{curr['campaign']}': {msg}")
                    cd = {
                        "entity_type": "target_bid",
                        "campaign": curr["campaign"],
                        "entity_key": key,
                        "field": "bid",
                        "old_value": prev.get("bid"),
                        "new_value": curr["bid"],
                        "changed_at": now_iso,
                        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
                        "days_elapsed": 0,
                        "days_remaining": cooldown_days,
                        "detail": msg,
                    }
                    active_cooldowns.append(cd)
                    locked_campaigns.add(curr["campaign"])
                    matching_c = next((x for x in curr_camps if x.get("name") == curr["campaign"]), {})
                    base_cpc = round(num(matching_c.get("cpc_3d") or matching_c.get("cpc_7d") or matching_c.get("cpc")), 2)
                    base_imp = round(num(matching_c.get("daily_imp_3d") or matching_c.get("daily_imp_30d")), 1)
                    base_acos = round(num(matching_c.get("acos_7d") or matching_c.get("acos")), 1)
                    new_diff_items.append({
                        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items)+1:02d}",
                        "change_date": now.strftime("%Y-%m-%d"),
                        "changed_at": now_iso,
                        "campaign": curr["campaign"],
                        "entity_type": "target_bid",
                        "entity_name": f"{curr['text']} [{curr['type']}]",
                        "field": "bid",
                        "old_val": f"${prev.get('bid'):.2f}",
                        "new_val": f"${curr['bid']:.2f}",
                        "baseline_cpc": base_cpc,
                        "baseline_daily_imp": base_imp,
                        "baseline_acos": base_acos,
                        "detail": msg,
                    })

    new_snap = {
        "updated_at": now_iso,
        "campaigns": curr_camp_map,
        "keywords": curr_kw_map,
        "targets": curr_target_map,
        "active_cooldowns": active_cooldowns,
    }
    return new_snap, active_cooldowns, locked_campaigns, diff_events, new_diff_items


def load_and_sync_ledger(
    ledger_file: Path,
    prev_snapshot: dict,
    new_diff_items: list[dict],
    curr_camps: list[dict],
    cooldown_days: int = 7,
) -> tuple[list[dict], list[dict]]:
    """Load persistent ledger, seed from snapshot cooldowns if new, append new diffs,
    and dynamically attribute post-adjustment performance (Delta CPC, Delta Imp, Verdict).
    """
    now = datetime.now()
    ledger = []
    if ledger_file.exists():
        try:
            ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
            if not isinstance(ledger, list):
                ledger = []
        except Exception:
            ledger = []

    # If ledger is empty, seed from prev_snapshot.active_cooldowns
    if not ledger and prev_snapshot.get("active_cooldowns"):
        for idx, item in enumerate(prev_snapshot["active_cooldowns"]):
            c_name = item.get("campaign") or ""
            matching_camp = next((x for x in curr_camps if x.get("name") == c_name), {})
            base_cpc = round(num(matching_camp.get("cpc_3d") or matching_camp.get("cpc_7d") or matching_camp.get("cpc")), 2)
            base_imp = round(num(matching_camp.get("daily_imp_3d") or matching_camp.get("daily_imp_30d")), 1)
            base_acos = round(num(matching_camp.get("acos_7d") or matching_camp.get("acos")), 1)
            ch_at = item.get("changed_at") or now.isoformat()
            ch_date = ch_at[:10]
            old_v = str(item.get("old_value"))
            new_v = str(item.get("new_value"))
            field = str(item.get("field") or "")
            if "bid" in field:
                old_v = f"${num(old_v):.2f}"
                new_v = f"${num(new_v):.2f}"
            elif field in ("tos", "pp", "ros"):
                old_v = f"{old_v}%"
                new_v = f"{new_v}%"

            ledger.append({
                "id": f"ADJ-HIST-{idx+1:02d}",
                "change_date": ch_date,
                "changed_at": ch_at,
                "campaign": c_name,
                "entity_type": item.get("entity_type") or "adjustment",
                "entity_name": item.get("entity_key") or c_name,
                "field": field,
                "old_val": old_v,
                "new_val": new_v,
                "baseline_cpc": base_cpc,
                "baseline_daily_imp": base_imp,
                "baseline_acos": base_acos,
                "detail": item.get("detail") or "",
            })

    # Append new diff items
    existing_keys = {f"{x.get('campaign')}::{x.get('field')}::{x.get('change_date')}::{x.get('old_val')}->{x.get('new_val')}" for x in ledger}
    for item in new_diff_items:
        key = f"{item.get('campaign')}::{item.get('field')}::{item.get('change_date')}::{item.get('old_val')}->{item.get('new_val')}"
        if key not in existing_keys:
            ledger.append(item)
            existing_keys.add(key)

    # Dynamic Post-Adjustment Effect Attribution
    active_cooldowns = []
    for item in ledger:
        c_name = item.get("campaign") or ""
        matching_camp = next((x for x in curr_camps if x.get("name") == c_name), None)
        ch_at_str = item.get("changed_at") or now.isoformat()
        try:
            ch_at = datetime.fromisoformat(ch_at_str)
            days_elapsed = max(0, (now - ch_at).days)
        except Exception:
            days_elapsed = 0
        days_remaining = max(0, cooldown_days - days_elapsed)
        item["days_elapsed"] = days_elapsed
        item["days_remaining"] = days_remaining

        if matching_camp:
            curr_cpc = round(num(matching_camp.get("cpc_3d") or matching_camp.get("cpc_7d") or matching_camp.get("cpc")), 2)
            curr_imp = round(num(matching_camp.get("daily_imp_3d") or matching_camp.get("daily_imp_30d")), 1)
            curr_acos = round(num(matching_camp.get("acos_7d") or matching_camp.get("acos")), 1)
            item["current_cpc"] = curr_cpc
            item["current_daily_imp"] = curr_imp
            item["current_acos"] = curr_acos

            base_cpc = num(item.get("baseline_cpc"))
            base_imp = num(item.get("baseline_daily_imp"))

            delta_cpc = round(((curr_cpc - base_cpc) / base_cpc * 100), 1) if base_cpc > 0 else 0.0
            delta_imp = round(((curr_imp - base_imp) / base_imp * 100), 1) if base_imp > 0 else 0.0
            item["delta_cpc_pct"] = delta_cpc
            item["delta_imp_pct"] = delta_imp

            if delta_cpc <= -15.0 and delta_imp > -70.0:
                verdict = "【调后生效·见效保持】"
                advice = "见效保持，严禁二次砍价"
            elif delta_imp <= -70.0 and (matching_camp.get("ord_3d", 0) == 0):
                verdict = "【调后休克断流】"
                advice = "立即回踩探底 (+10%~+15%) 激活曝光"
            elif delta_cpc >= 40.0:
                verdict = "【调后失血暴冲】"
                advice = "紧急下调溢价/竞价控费"
            elif days_elapsed <= 3:
                verdict = f"【动态观察期 Day {days_elapsed}/7】"
                advice = "数据重敛中，保持观察"
            else:
                verdict = "【稳态监控中】"
                advice = "指标平稳，维持基线"

            item["effect_verdict"] = verdict
            item["action_advice"] = advice
        else:
            item["current_cpc"] = "--"
            item["current_daily_imp"] = "--"
            item["current_acos"] = "--"
            item["delta_cpc_pct"] = 0.0
            item["delta_imp_pct"] = 0.0
            item["effect_verdict"] = "【活动已归档/未匹配】"
            item["action_advice"] = "保持监控"

        if days_remaining > 0:
            active_cooldowns.append({
                "entity_type": item.get("entity_type"),
                "campaign": item.get("campaign"),
                "entity_key": item.get("entity_name"),
                "field": item.get("field"),
                "old_value": item.get("old_val"),
                "new_value": item.get("new_val"),
                "changed_at": item.get("changed_at"),
                "days_elapsed": days_elapsed,
                "days_remaining": days_remaining,
                "detail": item.get("detail"),
                "status": item.get("effect_verdict"),
            })

    return ledger, active_cooldowns


def archive_daily_record(
    daily_records_dir: Path,
    snap_key: str,
    today_str: str,
    now_iso: str,
    meta: dict,
    profile: dict,
    financial_prof: dict,
    traffic_prof: dict,
    camps: list[dict],
    dayparting_records: list[dict],
) -> Path:
    record_file = daily_records_dir / f"{snap_key}_{today_str}.json"
    data = {
        "date": today_str,
        "recorded_at": now_iso,
        "snap_key": snap_key,
        "meta": meta,
        "profile": {
            "title": profile.get("title"),
            "price": profile.get("price"),
            "stars": profile.get("stars"),
            "reviews": profile.get("reviews"),
            "small_rank": profile.get("small_rank"),
            "fulfillable": profile.get("fulfillable"),
            "days_of_supply": profile.get("days_of_supply"),
        },
        "financial": financial_prof,
        "traffic": traffic_prof,
        "campaigns_summary": [
            {
                "name": c["name"],
                "state": c["state"],
                "budget": c["budget"],
                "tos": c["tos"],
                "pp": c["pp"],
                "ros": c["ros"],
                "spend_30d": round(c["spend"], 2),
                "sales_30d": round(c["sales"], 2),
                "acos_30d": round(c["acos"], 1),
                "cpc_30d": round(c["cpc"], 2),
                "cpc_7d": round(c["cpc_7d"], 2),
                "cpc_3d": round(c["cpc_3d"], 2),
                "delta_cpc_pct": round(c["delta_cpc_pct"], 1),
                "delta_imp_pct": round(c["delta_imp_pct"], 1),
                "ord_30d": c["ord"],
                "cvr_30d": round(c["cvr"], 1),
                "assessment": c.get("assessment", "HEALTHY"),
            }
            for c in camps if c["state"] == "enabled"
        ],
        "dayparting_peak_hours": [f"{r['hour']:02d}:00" for r in dayparting_records if r.get("classification") == "PEAK_GOLDEN_HOUR"],
        "dayparting_bleeding_hours": [f"{r['hour']:02d}:00" for r in dayparting_records if r.get("classification") == "BLEEDING_OFFPEAK_HOUR"],
    }
    record_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_file


def save_ledger_files(ledger: list[dict], ledger_dir: Path, snap_key: str, dest: Path, export_dir: Path | None, prefix: str, gen_time_str: str) -> None:
    ledger_json = ledger_dir / f"{snap_key}_ledger.json"
    ledger_json.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")

    headers = [
        "change_date", "campaign", "entity_type", "entity_name", "field",
        "old_val", "new_val", "baseline_cpc", "current_cpc", "delta_cpc_pct",
        "baseline_daily_imp", "current_daily_imp", "delta_imp_pct",
        "days_elapsed", "days_remaining", "effect_verdict", "action_advice", "detail"
    ]
    lines = []
    for r in sorted(ledger, key=lambda x: str(x.get("changed_at", "")), reverse=True):
        row = [
            str(r.get("change_date", "")),
            str(r.get("campaign", "")),
            str(r.get("entity_type", "")),
            str(r.get("entity_name", "")),
            str(r.get("field", "")),
            str(r.get("old_val", "")),
            str(r.get("new_val", "")),
            f"${num(r.get('baseline_cpc')):.2f}" if num(r.get("baseline_cpc")) > 0 else "--",
            f"${num(r.get('current_cpc')):.2f}" if num(r.get("current_cpc")) > 0 else "--",
            f"{r.get('delta_cpc_pct', 0.0):.1f}%",
            str(r.get("baseline_daily_imp", "")),
            str(r.get("current_daily_imp", "")),
            f"{r.get('delta_imp_pct', 0.0):.1f}%",
            str(r.get("days_elapsed", 0)),
            str(r.get("days_remaining", 0)),
            str(r.get("effect_verdict", "")),
            str(r.get("action_advice", "")),
            str(r.get("detail", "")),
        ]
        lines.append("\t".join(row))

    tsv_content = "\t".join(headers) + "\n" + "\n".join(lines)
    (ledger_dir / f"{snap_key}_ledger.tsv").write_text(tsv_content, encoding="utf-8")
    (dest / "historical_ledger.tsv").write_text(tsv_content, encoding="utf-8")

    if export_dir and export_dir.exists():
        (export_dir / f"{prefix}-historical-ledger-{gen_time_str}.tsv").write_text(tsv_content, encoding="utf-8")
        (export_dir / f"{prefix}-historical-ledger.tsv").write_text(tsv_content, encoding="utf-8")



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--rules", default=None, help="Optional path to product_rules.json")
    parser.add_argument("--snapshots-dir", default=None, help="Optional path to snapshots directory")
    parser.add_argument("--db-dir", default=None, help="Optional path to database directory (default: workspace db/)")
    parser.add_argument("--cooldown-days", type=int, default=7, help="Cooldown observation period in days (default: 7)")
    parser.add_argument("--target-acos", type=float, default=None, help="Target ACOS percent threshold (e.g. 25.0)")
    parser.add_argument("--timestamp", default=None, help="Optional timestamp string (e.g. 20260926-104901). Defaults to current local time.")
    parser.add_argument("--known-negatives", default=None, help="Comma-separated or file path of already negated keywords/phrases")
    parser.add_argument("--export-dir", default=None, help="Optional export directory (e.g. outputs) to export timestamped bulk adjustments table.")
    args = parser.parse_args()

    src = Path(args.indir)
    dest = Path(args.outdir)
    dest.mkdir(parents=True, exist_ok=True)

    # Load known negatives to prevent redundant recommendations
    known_negatives = set()
    for kn_candidate in [src / "known_negatives.txt", Path("known_negatives.txt"), src / "known_negatives.json", Path("known_negatives.json")]:
        if kn_candidate.exists():
            try:
                if kn_candidate.suffix == ".json":
                    data = json.loads(kn_candidate.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        known_negatives.update(str(x).strip().lower() for x in data if str(x).strip())
                else:
                    for l in kn_candidate.read_text(encoding="utf-8").splitlines():
                        t = l.strip().lower()
                        if t and not t.startswith("#"):
                            known_negatives.add(t)
            except Exception:
                pass
    if getattr(args, "known_negatives", None):
        for item in str(args.known_negatives).split(","):
            t = item.strip().lower()
            if t:
                known_negatives.add(t)

    meta = load_json(src / "meta.json") or {}
    listing = load_json(src / "listing.json")
    stock = load_json(src / "fba_stock.json")
    profile = listing_profile(listing, stock)
    profile["meta"] = meta

    # Load Advanced Intelligence Profiles
    financial_prof = load_financial_profile(src)
    traffic_prof = load_traffic_profile(src)
    dayparting_records, dayparting_table_lines = load_dayparting_analysis(src)

    if financial_prof:
        profile["financial_profile"] = financial_prof
        dest.joinpath("financial_profile.json").write_text(
            json.dumps(financial_prof, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if traffic_prof:
        profile["traffic_profile"] = traffic_prof
        dest.joinpath("traffic_profile.json").write_text(
            json.dumps(traffic_prof, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if dayparting_table_lines:
        tsv(
            dest / "dayparting_analysis.tsv",
            "hour_window\tspend\tad_orders\tstore_orders\tcvr\tacos\tcpc\tclassification\trecommendation",
            dayparting_table_lines,
        )

    rule_path = Path(args.rules) if args.rules else None
    rules = load_product_rules(rule_path, src)
    gen_time_str = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")

    # Determine target ACOS: CLI argument > rules config > financial break-even anchor (68%) > default 25.0
    target_acos = 25.0
    if args.target_acos is not None:
        target_acos = float(args.target_acos)
    elif rules.get("target_acos") is not None:
        target_acos = float(rules.get("target_acos"))
    elif financial_prof and financial_prof.get("break_even_acos", 0) > 0:
        target_acos = round(financial_prof["break_even_acos"] * 0.68, 1)
    profile["target_acos"] = target_acos

    profile["rules_loaded"] = {
        "is_patterns_count": len(rules.get("is_patterns", [])),
        "is_not_patterns_count": len(rules.get("is_not_patterns", [])),
        "competitor_brands_count": len(rules.get("competitor_brands", [])),
        "target_acos": target_acos,
    }

    dest.joinpath("product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    product_ads = as_rows(load_json(src / "product_ads.json"))
    asin = str((meta.get("asin") or profile.get("asin") or "")).upper()
    asin_campaigns = set()
    for row in product_ads:
        row_asin = str(row.get("asin") or row.get("advertised_asin") or "").upper()
        if asin and row_asin == asin and row.get("campaign_name"):
            asin_campaigns.add(row.get("campaign_name"))

    raw_campaigns = as_rows(load_json(src / "campaign_report.json"))
    for c_raw in raw_campaigns:
        c_asins = [str(x).upper() for x in (c_raw.get("creative_asins") or []) if x]
        c_name = c_raw.get("name") or c_raw.get("campaign_name")
        if asin and asin in c_asins and c_name:
            asin_campaigns.add(c_name)
        if meta.get("portfolio_id") and c_name:
            asin_campaigns.add(c_name)

    camps = campaign_rows(raw_campaigns)
    if asin_campaigns:
        camps = [c for c in camps if c["name"] in asin_campaigns]

    camps_7d_raw = load_json(src / "campaign_report_7d.json")
    camps_7d = campaign_rows(as_rows(camps_7d_raw)) if camps_7d_raw else []
    map_7d = {c["name"]: c for c in camps_7d}

    camps_3d_raw = load_json(src / "campaign_report_3d.json")
    camps_3d = campaign_rows(as_rows(camps_3d_raw)) if camps_3d_raw else []
    map_3d = {c["name"]: c for c in camps_3d}

    for c in camps:
        c7 = map_7d.get(c["name"], {})
        c3 = map_3d.get(c["name"], {})

        c["spend_7d"] = c7.get("spend", 0.0)
        c["sales_7d"] = c7.get("sales", 0.0)
        c["cpc_7d"] = c7.get("cpc", 0.0)
        c["acos_7d"] = c7.get("acos", 0.0)
        c["cvr_7d"] = c7.get("cvr", 0.0)
        c["imp_7d"] = c7.get("imp", 0)
        c["clk_7d"] = c7.get("clk", 0)
        c["ord_7d"] = c7.get("ord", 0)

        c["spend_3d"] = c3.get("spend", 0.0)
        c["sales_3d"] = c3.get("sales", 0.0)
        c["cpc_3d"] = c3.get("cpc", 0.0)
        c["acos_3d"] = c3.get("acos", 0.0)
        c["cvr_3d"] = c3.get("cvr", 0.0)
        c["imp_3d"] = c3.get("imp", 0)
        c["clk_3d"] = c3.get("clk", 0)
        c["ord_3d"] = c3.get("ord", 0)

        daily_imp_30d = c["imp"] / 30.0 if c["imp"] > 0 else 0.0
        daily_imp_7d = c["imp_7d"] / 7.0 if c["imp_7d"] > 0 else 0.0
        daily_imp_3d = c["imp_3d"] / 3.0 if c["imp_3d"] > 0 else 0.0

        c["daily_imp_30d"] = round(daily_imp_30d, 1)
        c["daily_imp_7d"] = round(daily_imp_7d, 1)
        c["daily_imp_3d"] = round(daily_imp_3d, 1)

        # Delta CPC: compare recent 3d/7d CPC vs 30d baseline
        if c["cpc"] > 0 and c["cpc_3d"] > 0:
            c["delta_cpc_pct"] = round((c["cpc_3d"] - c["cpc"]) / c["cpc"] * 100, 1)
        elif c["cpc"] > 0 and c["cpc_7d"] > 0:
            c["delta_cpc_pct"] = round((c["cpc_7d"] - c["cpc"]) / c["cpc"] * 100, 1)
        else:
            c["delta_cpc_pct"] = 0.0

        # Delta Impression Velocity: compare recent 3d daily imp vs 30d daily imp
        if daily_imp_30d >= 15:
            c["delta_imp_pct"] = round((daily_imp_3d - daily_imp_30d) / daily_imp_30d * 100, 1)
        else:
            c["delta_imp_pct"] = 0.0

    kws = [r for r in as_rows(load_json(src / "keywords.json")) if not skip_total(r)]
    if asin_campaigns:
        kws = [r for r in kws if r.get("campaign_name") in asin_campaigns]

    kws_7d_raw = load_json(src / "keywords_7d.json")
    kws_7d = [r for r in as_rows(kws_7d_raw) if not skip_total(r)] if kws_7d_raw else []
    if asin_campaigns:
        kws_7d = [r for r in kws_7d if r.get("campaign_name") in asin_campaigns]

    kws_3d_raw = load_json(src / "keywords_3d.json")
    kws_3d = [r for r in as_rows(kws_3d_raw) if not skip_total(r)] if kws_3d_raw else []
    if asin_campaigns:
        kws_3d = [r for r in kws_3d if r.get("campaign_name") in asin_campaigns]

    targets = as_rows(load_json(src / "targets.json"))
    if asin_campaigns:
        targets = [r for r in targets if r.get("campaign_name") in asin_campaigns]

    targets_7d_raw = load_json(src / "targets_7d.json")
    targets_7d = as_rows(targets_7d_raw) if targets_7d_raw else []
    if asin_campaigns:
        targets_7d = [r for r in targets_7d if r.get("campaign_name") in asin_campaigns]

    targets_3d_raw = load_json(src / "targets_3d.json")
    targets_3d = as_rows(targets_3d_raw) if targets_3d_raw else []
    if asin_campaigns:
        targets_3d = [r for r in targets_3d if r.get("campaign_name") in asin_campaigns]

    # --------------------------------------------------------------------------
    # Database, Snapshot & Persistent Ledger Execution
    # --------------------------------------------------------------------------
    db_root, daily_records_dir, ledger_dir, db_snapshots_dir = resolve_db_dirs(args.db_dir, src)
    snapshots_dir = Path(args.snapshots_dir) if args.snapshots_dir else db_snapshots_dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snap_key = get_snapshot_key(meta, profile, camps)
    snap_file = snapshots_dir / f"{snap_key}_snapshot.json"

    # Also check if snapshot exists in workspace snapshots/ if not yet in db
    if not snap_file.exists() and (src.parent / "snapshots" / f"{snap_key}_snapshot.json").exists():
        snap_file = src.parent / "snapshots" / f"{snap_key}_snapshot.json"

    prev_snapshot = load_snapshot(snap_file)
    curr_snapshot, raw_cooldowns, locked_campaigns, diff_events, new_diff_items = diff_and_track_cooldown(
        prev_snapshot, camps, kws, targets, cooldown_days=args.cooldown_days
    )
    snap_file.write_text(json.dumps(curr_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    # Load and sync Persistent Ledger with dynamic effect attribution
    ledger_file = ledger_dir / f"{snap_key}_ledger.json"
    ledger, active_cooldowns = load_and_sync_ledger(
        ledger_file, prev_snapshot, new_diff_items, camps, cooldown_days=args.cooldown_days
    )
    for cd in active_cooldowns:
        if cd.get("campaign"):
            locked_campaigns.add(cd["campaign"])

    # Archive Daily Record into Database
    gen_date_str = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()
    archive_daily_record(
        daily_records_dir, snap_key, gen_date_str, now_iso,
        meta, profile, financial_prof, traffic_prof, camps, dayparting_records
    )

    # 1. Output campaigns.tsv (With 30d Macro, 7d Trend, 3d Latest Slice and Deltas)
    camp_lines = []
    for c in camps:
        camp_lines.append(
            "\t".join(str(x) for x in [
                c["state"], c["targeting"], c["sponsored"], int(c["budget"]),
                int(c["tos"]), int(c["pp"]), int(c["ros"]),
                c.get("bidding_strategy", "固定竞价 (Fixed Bids)"),
                round(c["spend"], 2), round(c["sales"], 2), round(c["acos"], 1),
                round(c["cpc"], 2), round(c["cpc_7d"], 2), round(c["cpc_3d"], 2),
                round(c["delta_cpc_pct"], 1), round(c["delta_imp_pct"], 1),
                round(c["cvr"], 1), round(c["ctr"], 2),
                int(c["clk"]), int(c["ord"]), int(c["direct_ord"]), int(c["indirect_ord"]),
                round(c["spend_7d"], 2), round(c["sales_7d"], 2), round(c["acos_7d"], 1), int(c["ord_7d"]), int(c["clk_7d"]), int(c["imp_7d"]),
                round(c["spend_3d"], 2), round(c["sales_3d"], 2), round(c["acos_3d"], 1), int(c["ord_3d"]), int(c["clk_3d"]), int(c["imp_3d"]),
                round(c["tos_share"], 2), c["name"],
            ])
        )
    tsv(
        dest / "campaigns.tsv",
        "state\ttype\tsponsored\tbudget\tTOS\tPP\tROS\tbidding_strategy\tspend_30d\tsales_30d\tacos_30d\tcpc_30d\tcpc_7d\tcpc_3d\tdelta_cpc_pct\tdelta_imp_pct\tcvr_30d\tctr_30d\tclk_30d\tord_30d\tdir\tindir\tspend_7d\tsales_7d\tacos_7d\tord_7d\tclk_7d\timp_7d\tspend_3d\tsales_3d\tacos_3d\tord_3d\tclk_3d\timp_3d\tshare\tname",
        camp_lines,
    )

    # 2. Output placement_audit.tsv (Multi-Window Dynamic Delta & Adaptive Correction)
    placement_lines = []
    for c in camps:
        if c["state"] != "enabled":
            continue
        assessment = "HEALTHY"
        recom = "Keep monitoring"

        # Effective CPC for forward recommendations is RECENT CPC (cpc_3d > cpc_7d > cpc_30d)
        effective_cpc = c["cpc_3d"] if c["cpc_3d"] > 0 else (c["cpc_7d"] if c["cpc_7d"] > 0 else c["cpc"])

        # Check if campaign is currently in observation/cooldown period
        if c["name"] in locked_campaigns:
            cd_match = [x for x in active_cooldowns if x.get("campaign") == c["name"]]
            detail = cd_match[0].get("detail", "Recent parameter adjustment") if cd_match else "Recent parameter adjustment"
            elapsed = cd_match[0].get("days_elapsed", 0) if cd_match else 0

            # 1. Post-adjustment Asphyxiation (调后休克断流): Recent 3d daily imp dropped by >70%
            if c["delta_imp_pct"] <= -70.0 and c["daily_imp_30d"] >= 50:
                assessment = "CIRCUIT_BREAKER_SHOCK"
                recom = f"[调后休克断流·立即回踩探底] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 暴跌至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；出价降破市场门槛导致断流，立即打破观察期，基础竞价回提 10%-15% 探底拉回展示池"

            # 2. Post-adjustment Ongoing Bleed (调后持续失血): High spend with 0 orders in recent 3d, or ACOS doubled
            elif (c["spend_3d"] >= 15 and c["ord_3d"] == 0) or (c["spend"] >= 15 and c["ord"] == 0 and c["spend_7d"] >= 10):
                assessment = "CIRCUIT_BREAKER_BLEED"
                recom = f"[调后持续失血·紧急纠偏] 调整后近3天花费 ${c['spend_3d']:.1f} 仍为 0 单 (7天花 ${c['spend_7d']:.1f})；上次幅度不足或词流未阻断，打破观察期，二次削减 TOS/PP 溢价或下调基础竞价"
            elif c["ord_3d"] > 0 and c["spend_3d"] >= 25 and c["acos_3d"] >= (target_acos * 2.0):
                assessment = "CIRCUIT_BREAKER_BLEED"
                recom = f"[调后持续失血·ACOS爆表] 近3天 ACOS 达 {c['acos_3d']:.1f}% (目标 {target_acos:.0f}%)；打破观察期，紧急下调基础竞价防守"

            # 3. Post-adjustment Effective (调后生效·见效保持): CPC dropped significantly, volume stable
            elif c["delta_cpc_pct"] <= -15.0 and c["delta_imp_pct"] > -60.0:
                assessment = "ADJUSTMENT_EFFECTIVE"
                recom = f"[调后生效·见效保持] 近3天 CPC 成功压至 ${c['cpc_3d']:.2f} ({c['delta_cpc_pct']:+.1f}% vs 30天均值 ${c['cpc']:.2f})，曝光量稳定；调整已见成效，严禁二次砍价！保持当前稳态运行"

            # 4. Normal Monitoring (指标在公差范围内)
            else:
                assessment = "IN_OBSERVATION"
                recom = f"[动态观察期 Day {elapsed}/{args.cooldown_days}·正常公差] {detail}；近3天 CPC ${effective_cpc:.2f} (变动 {c['delta_cpc_pct']:+.1f}%)，指标在合理公差内，保持监控"

        elif c["spend"] >= 60 and c["ord"] == 0 and c["tos"] >= 50:
            assessment = "DEAD_CAMPAIGN_CANDIDATE"
            recom = f"30天花费 ${c['spend']:.1f} 仍为 0 单，近7天 CPC ${effective_cpc:.2f}；确诊为算法冷宫死活动，建议执行归档并按净水器协议新建精准仓"
        elif c["tos"] >= 150:
            assessment = "HIGH_TOS_RISK"
            recom = f"TOS {int(c['tos'])}% 虚高，近7天实际 CPC ${effective_cpc:.2f}；下调 TOS 至 50-80% 消除暴涨风险，若曝光下跌则回补基础竞价"
        elif c["pp"] >= 100 and (c["acos_7d"] >= (target_acos * 1.4) if c["acos_7d"] > 0 else c["acos"] >= (target_acos * 1.4)):
            current_eval_acos = c["acos_7d"] if c["acos_7d"] > 0 else c["acos"]
            assessment = "INEFFICIENT_PP"
            recom = f"商品页溢价 {int(c['pp'])}% 过高，近期 ACOS {current_eval_acos:.1f}% 严重超标；下调 PP 至 <=20%，商品页缺乏对比壁垒"
        elif effective_cpc >= 1.20 and c["acos"] >= target_acos:
            assessment = "HIGH_CPC_THROTTLE"
            recom = f"近期实际 CPC ${effective_cpc:.2f} 偏高 (30天均值 ${c['cpc']:.2f})；按近期成本下调基础竞价 20-25% 控费"
        elif c["spend_7d"] >= 15 and c["ord_7d"] == 0:
            assessment = "ZERO_CONVERSION_BLEED"
            recom = f"近7天花费 ${c['spend_7d']:.1f} 为 0 单；排查最新搜索词相关性，下调基础竞价或暂停广告组"
        elif c["tos"] <= 30 and c["tos_share"] < 15 and (c["cvr_7d"] >= 15 if c["cvr_7d"] > 0 else c["cvr"] >= 15):
            assessment = "TOS_UNDERBID"
            recom = f"高转化潜质 (CVR {c.get('cvr_7d') or c['cvr']:.1f}%)，顶部份额仅 {c['tos_share']:.0f}%；建议提高 TOS 至 50-70% 抢占首屏黄金位"

        c["assessment"] = assessment
        c["recom"] = recom

        placement_lines.append("\t".join(str(x) for x in [
            c["name"], c["targeting"], round(c["spend"], 2), round(c["cpc"], 2), round(c["acos"], 1),
            round(c["cpc_7d"], 2), round(c["acos_7d"], 1), round(c["cpc_3d"], 2),
            round(c["delta_cpc_pct"], 1), round(c["delta_imp_pct"], 1),
            int(c["tos"]), int(c["pp"]), int(c["ros"]),
            round(c["tos_share"], 2), assessment, recom
        ]))
    tsv(
        dest / "placement_audit.tsv",
        "campaign\ttargeting\tspend_30d\tcpc_30d\tacos_30d\tcpc_7d\tacos_7d\tcpc_3d\tdelta_cpc_pct\tdelta_imp_pct\tTOS\tPP\tROS\ttos_share\tassessment\trecommendation",
        placement_lines,
    )

    # Load searchrank map from keyword_analysis.json (Amazon ABA search rank)
    sr_map = load_searchrank_map(src)

    # 3. Output keywords.tsv (with Searchrank and Volume Tier)
    kw_lines = []
    for r in sorted(kws, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 1 and num(r.get("orders")) < 1:
            continue
        kw_text = r.get("keyword_text") or r.get("targeting_text") or ""
        sr, v_tier = get_searchrank(kw_text, sr_map)
        sr_display = str(sr) if sr is not None else "--"
        kw_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("match_type"), kw_text,
            sr_display, v_tier,
            r.get("bid"), r.get("default_bid"), r.get("state"),
            round(spend, 2), round(num(r.get("sales")), 2), round(num(r.get("acos")), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))),
            round(num(r.get("cvr")), 1), round(num(r.get("cpc")), 2), round(num(r.get("ctr")), 2),
        ]))
    tsv(
        dest / "keywords.tsv",
        "campaign\tmatch\ttext\tsearchrank\tvolume_tier\tbid\tdefbid\tstate\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tctr",
        kw_lines,
    )

    # 4. Search terms, N-Grams, Active Negatives, and Cannibalization Analysis
    sts = [r for r in as_rows(load_json(src / "search_terms.json")) if r.get("query") or r.get("campaign_name")]
    if asin_campaigns:
        sts = [r for r in sts if r.get("campaign_name") in asin_campaigns]

    active_neg_dict, active_neg_camp_pairs, all_neg_queries = extract_active_negatives(sts)
    audit_records, unnegative_adjustments = audit_active_negatives(
        active_neg_dict, profile, rules, sr_map, kws=kws, camps=camps
    )
    kw_misfit_adjustments = audit_keyword_misfits(kws, rules, camps=camps)

    bucket_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0})
    ngram_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0, "queries": set(), "gram_type": "1-gram"})
    cannibal_map = defaultdict(list)

    st_lines = []
    waste = []
    harvest = []
    bulk_adjustments = []

    # Build 7d and 3d keyword maps for fresh slice analysis
    kw_7d_map = {}
    for k in kws_7d:
        cn = k.get("campaign_name") or ""
        mt = (k.get("match_type") or "").lower().strip()
        txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if txt:
            kw_7d_map[(cn, mt, txt)] = k

    kw_3d_map = {}
    for k in kws_3d:
        cn = k.get("campaign_name") or ""
        mt = (k.get("match_type") or "").lower().strip()
        txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if txt:
            kw_3d_map[(cn, mt, txt)] = k

    # Build lookups for placement fingerprinting and fresh slice verification
    kw_lookup = {}
    for k in kws:
        c_name_k = k.get("campaign_name") or ""
        text_k = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        match_k = (k.get("match_type") or "").lower().strip()
        if not text_k:
            continue
        key_k = f"{c_name_k}::{match_k}::{text_k}"
        k7 = kw_7d_map.get((c_name_k, match_k, text_k), {})
        k3 = kw_3d_map.get((c_name_k, match_k, text_k), {})

        # Current bid: prefer latest report (3d -> 7d -> baseline)
        current_bid = num(k3.get("bid") or k7.get("bid") or k.get("bid") or k.get("default_bid"))
        current_state = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()

        imp_30d = int(num(k.get("impressions")))
        clk_30d = int(num(k.get("clicks")))
        spend_30d = num(k.get("cost") or k.get("spend") or k.get("spends"))
        ord_30d = int(num(k.get("orders")))

        imp_7d = int(num(k7.get("impressions"))) if k7 else 0
        clk_7d = int(num(k7.get("clicks"))) if k7 else 0
        spend_7d = num(k7.get("cost") or k7.get("spend") or k7.get("spends")) if k7 else 0.0
        ord_7d = int(num(k7.get("orders"))) if k7 else 0

        imp_3d = int(num(k3.get("impressions"))) if k3 else 0
        clk_3d = int(num(k3.get("clicks"))) if k3 else 0
        spend_3d = num(k3.get("cost") or k3.get("spend") or k3.get("spends")) if k3 else 0.0
        ord_3d = int(num(k3.get("orders"))) if k3 else 0

        # Zero recent impressions / bleeding halted post adjustment
        has_zero_recent_traffic = (
            (imp_3d == 0 and spend_3d == 0.0) and
            (imp_7d == 0 or spend_7d <= 0.5)
        ) or (imp_7d == 0 and spend_7d == 0.0)

        # Active recent bleeding requirement for bid reductions
        has_active_bleeding = (
            (spend_3d >= 1.5 and ord_3d == 0) or
            (spend_7d >= 3.0 and ord_7d == 0) or
            (clk_3d >= 2 and ord_3d == 0)
        )

        kw_lookup[key_k] = {
            "bid": current_bid,
            "cpc": num(k.get("cpc")),
            "state": current_state,
            "imp_30d": imp_30d, "clk_30d": clk_30d, "spend_30d": spend_30d, "ord_30d": ord_30d,
            "imp_7d": imp_7d, "clk_7d": clk_7d, "spend_7d": spend_7d, "ord_7d": ord_7d,
            "imp_3d": imp_3d, "clk_3d": clk_3d, "spend_3d": spend_3d, "ord_3d": ord_3d,
            "has_zero_recent_traffic": has_zero_recent_traffic,
            "has_active_bleeding": has_active_bleeding,
        }

    camp_lookup = {c["name"]: c for c in camps if c.get("name")}
    placement_diagnoses = []

    for r in sorted(sts, key=lambda x: -num(x.get("spends"))):
        q = (r.get("query") or "").strip()
        if not q:
            continue
        spend = num(r.get("spends"))
        sales = num(r.get("sales"))
        ordn = num(r.get("orders"))
        clk = num(r.get("clicks"))
        imp_cnt = int(num(r.get("impressions")))
        c_name = r.get("campaign_name") or ""
        match_type = r.get("match_type") or r.get("target_match_type") or ""
        target_text = r.get("keyword_text") or r.get("target_text") or ""

        # Intent classification & strong relevance check
        bucket, match_reason, is_strong_relevance = classify_query(q, profile, rules)
        sr, v_tier = get_searchrank(q, sr_map)
        sr_display = str(sr) if sr is not None else "--"

        bucket_stats[bucket]["spend"] += spend
        bucket_stats[bucket]["sales"] += sales
        bucket_stats[bucket]["ord"] += ordn
        bucket_stats[bucket]["clk"] += clk

        # N-Gram collection
        for ng, gtype in extract_ngrams(q):
            ngram_stats[ng]["spend"] += spend
            ngram_stats[ng]["sales"] += sales
            ngram_stats[ng]["ord"] += ordn
            ngram_stats[ng]["clk"] += clk
            ngram_stats[ng]["queries"].add(q)
            ngram_stats[ng]["gram_type"] = gtype

        # Cannibalization record
        cannibal_map[q.lower()].append({
            "campaign": c_name,
            "match": match_type,
            "target": target_text,
            "spend": spend,
            "sales": sales,
            "orders": ordn,
            "clicks": clk,
            "cpc": num(r.get("cpc")),
        })

        if spend >= 3 or ordn >= 1:
            st_lines.append("\t".join(str(x) for x in [
                bucket, v_tier, sr_display, c_name, q, match_type, target_text,
                round(spend, 2), round(sales, 2), round(num(r.get("acos")), 1),
                int(ordn), int(clk), round(num(r.get("cvr")), 1), round(num(r.get("cpc")), 2),
            ]))

        # Multi-dimensional placement fingerprint diagnosis
        diag = infer_search_term_placement(
            query=q,
            spend=spend,
            sales=sales,
            orders=int(ordn),
            clicks=int(clk),
            impressions=imp_cnt,
            c_name=c_name,
            match_type=match_type,
            target_text=target_text,
            bucket=bucket,
            is_strong=is_strong_relevance,
            v_tier=v_tier,
            sr_display=sr_display,
            camp_lookup=camp_lookup,
            kw_lookup=kw_lookup,
            target_acos=target_acos,
        )
        if spend >= 3 or ordn >= 1 or clk >= 5:
            placement_diagnoses.append(diag)

        # Search term optimization & placement-based dynamic adjustment
        is_already_negated = (c_name.lower(), q.lower()) in active_neg_camp_pairs or any(kn == q.lower() or kn in q.lower() for kn in known_negatives)

        if diag.get("action_type") and not is_already_negated:
            action_code = diag["action_type"]

            # Guardrail 1: NEVER output adjustments on paused campaigns!
            camp_obj = camp_lookup.get(c_name, {})
            camp_state = camp_obj.get("state", "enabled")
            if camp_state != "enabled":
                continue

            matching_kw_matches = [m for m in ("exact", "phrase", "broad") if f"{c_name}::{m}::{q.lower()}" in kw_lookup]
            is_targeted_kw = bool(matching_kw_matches)

            if is_targeted_kw:
                actual_match = matching_kw_matches[0]
                kw_info = kw_lookup[f"{c_name}::{actual_match}::{q.lower()}"]
                actual_base_bid = kw_info["bid"]
                kw_state = kw_info["state"]

                # Guardrail 2: Strictly exclude paused keywords!
                if kw_state != "enabled":
                    continue

                if action_code in ("Throttle_TOS_Bid", "Lower_Bid"):
                    # Guardrail 3: Zero Recent Impressions / Bleeding Halted (调后已零曝光·严禁再降)
                    if kw_info["has_zero_recent_traffic"]:
                        continue

                    # Guardrail 4: Must have active recent bleeding
                    if not kw_info["has_active_bleeding"] and kw_info["spend_3d"] == 0.0 and kw_info["spend_7d"] < 2.0:
                        continue

                    # Guardrail 5: Bid floor protection
                    if actual_base_bid <= 0.20:
                        continue

                    new_bid = max(0.18, round(actual_base_bid * 0.75, 2))
                    waste.append(f"{q}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag={diag['strategy_tag']}\taction={action_code}")
                    bulk_adjustments.append({
                        "action_type": "Throttle_TOS_Bid",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": actual_match,
                        "current_value": f"投放词 (当前基础竞价 ${actual_base_bid:.2f}), 30天花费=${spend:.2f}({int(clk)}点击0单), 近3天花费=${kw_info['spend_3d']:.2f}, CPC=${diag['cpc']:.2f} [{diag['inferred_placement']}]",
                        "recommended_value": f"基础竞价下调 25% 至 ${new_bid:.2f}",
                        "reason": f"投放关键词在首页顶部黄金位近期仍在活跃流血（近3天花费 ${kw_info['spend_3d']:.2f}），且历史累计 {int(clk)} 次高意图点击仍未转化，下调基础竞价退守防守位避损。",
                    })
                elif action_code == "Elevate_To_TOS":
                    new_bid = round(actual_base_bid * 1.15, 2)
                    bulk_adjustments.append({
                        "action_type": "Elevate_To_TOS",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": actual_match,
                        "current_value": f"投放词 (当前基础竞价 ${actual_base_bid:.2f}), 花费=${spend:.2f}, 点击={int(clk)}, CTR={diag['ctr']:.2f}% [{diag['inferred_placement']}]",
                        "recommended_value": f"基础竞价提高 15% 至 ${new_bid:.2f} 或提升活动 TOS%",
                        "reason": f"核心投放词在详情页/底部位受抑，提权专打首屏黄金位测试真实转化潜力。",
                    })
                elif action_code == "Negative_Exact":
                    bulk_adjustments.append({
                        "action_type": "Negative_Exact",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": "negativeExact",
                        "current_value": f"投放词, 花费=${spend:.2f}, 点击={int(clk)}, 0订单",
                        "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                        "reason": diag["diagnostic_reason"],
                    })
            else:
                # q is NOT a targeted keyword in c_name; it is a customer query triggered by broad/auto/phrase
                if action_code in ("Throttle_TOS_Bid", "Lower_Bid", "Negative_Exact"):
                    waste.append(f"{q}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag={diag['strategy_tag']}\taction=Negative_Exact")
                    bulk_adjustments.append({
                        "action_type": "Negative_Exact",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": "negativeExact",
                        "current_value": f"买家搜索词 (触发投放词: {target_text} [{match_type}]), 花费=${spend:.2f}, 点击={int(clk)}, 0订单 [{diag['inferred_placement']}]",
                        "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                        "reason": f"买家搜索词由广泛/自动投放词匹配触发，非独立投放词，无法直接调竞价；在首屏顶部已高耗损未转化，应在活动 {c_name} 中精准否定以阻断流血。",
                    })
                elif action_code in ("Elevate_To_TOS", "Refine_Bid_Placement"):
                    harvest_camp = find_exact_harvest_campaign(camps, kws)
                    harvest_camp_obj = camp_lookup.get(harvest_camp, {})
                    if harvest_camp_obj.get("state") == "enabled":
                        bulk_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": c_name,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_value": f"买家搜索词 (触发投放词: {target_text} [{match_type}]), 花费=${spend:.2f}, 点击={int(clk)}, CTR={diag['ctr']:.2f}% [{diag['inferred_placement']}]",
                            "recommended_value": f"在原活动精准否定防内耗，移入专用精准仓 ({harvest_camp}) 独立出价测试首屏",
                            "reason": f"优质买家搜索词在广泛仓被困在低效展示位，无法单独为其加价；应移入精准仓独立控制基础竞价与溢价专打顶部，并在广泛仓精准否定防内耗。",
                        })

        # High-potential winning query (harvest)
        if ordn >= 5 and acos(spend, sales) <= target_acos and spend >= 12:
            camp_obj = camp_lookup.get(c_name, {})
            if camp_obj.get("state") == "enabled":
                harvest.append(f"{q}\t{c_name}\tspend={spend:.2f}\tacos={acos(spend,sales):.1f}\tord={int(ordn)}\tbucket={bucket}\ttier={v_tier}")
                bulk_adjustments.append({
                    "action_type": "Harvest_Exact",
                    "campaign": c_name,
                    "target_entity": q,
                    "match_type": "exact",
                    "current_value": f"orders={int(ordn)}, acos={acos(spend,sales):.1f}%",
                    "recommended_value": "Promote to Dedicated Exact Campaign",
                    "reason": f"Proven conversion engine (orders={int(ordn)}, acos={acos(spend,sales):.1f}% <= target {target_acos:.0f}%), bucket: {bucket}, tier: {v_tier}",
                })

    tsv(
        dest / "search_terms.tsv",
        "bucket\tcampaign\tquery\tmatch\ttarget\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        st_lines,
    )
    dest.joinpath("waste.txt").write_text("\n".join(waste), encoding="utf-8")
    dest.joinpath("harvest.txt").write_text("\n".join(harvest), encoding="utf-8")

    # Output search_term_placement_diagnosis.tsv
    diag_lines = []
    for d in placement_diagnoses:
        diag_lines.append("\t".join(str(x) for x in [
            d["query"], d["campaign"], d["bucket"], d["volume_tier"], d["searchrank"],
            round(d["spend"], 2), round(d["sales"], 2), d["orders"], d["clicks"], d["impressions"],
            d["ctr"], d["cpc"], d["base_bid"], d["inferred_placement"], d["placement_evidence"],
            d["strategy_tag"], d["action_type"] or "--", d["recommended_value"] or "--", d["diagnostic_reason"] or "--"
        ]))
    tsv(
        dest / "search_term_placement_diagnosis.tsv",
        "query\tcampaign\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\timpressions\tctr\tcpc\tbase_bid\tinferred_placement\tplacement_evidence\tstrategy_tag\taction_type\trecommended_value\tdiagnostic_reason",
        diag_lines,
    )
    tsv(
        dest / f"search_term_placement_diagnosis_{gen_time_str}.tsv",
        "query\tcampaign\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\timpressions\tctr\tcpc\tbase_bid\tinferred_placement\tplacement_evidence\tstrategy_tag\taction_type\trecommended_value\tdiagnostic_reason",
        diag_lines,
    )
    if args.export_dir:
        exp_dir = Path(args.export_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        tsv(
            exp_dir / f"{asin_str}-{country_str}-placement-diagnosis-{gen_time_str}.tsv",
            "query\tcampaign\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\timpressions\tctr\tcpc\tbase_bid\tinferred_placement\tplacement_evidence\tstrategy_tag\taction_type\trecommended_value\tdiagnostic_reason",
            diag_lines,
        )

    # 5. Output buckets.tsv
    b_lines = ["bucket\tspend\tsales\tacos\tord\tclk\tcvr"]
    for b, v in sorted(bucket_stats.items(), key=lambda kv: -kv[1]["spend"]):
        b_lines.append("\t".join(str(x) for x in [
            b, round(v["spend"], 1), round(v["sales"], 1), round(acos(v["spend"], v["sales"]), 1),
            int(v["ord"]), int(v["clk"]),
            round((v["ord"] / v["clk"] * 100) if v["clk"] else 0, 1),
        ]))
    dest.joinpath("buckets.tsv").write_text("\n".join(b_lines), encoding="utf-8")

    # 6. Output ngrams.tsv & bleeding_ngrams.txt
    title_str = str(profile.get("title") or "").lower()
    title_tokens = {t for t in re.findall(r"[\w]+", title_str, re.UNICODE) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}
    ng_lines = []
    bleeding_ng = []
    for ng, stat in sorted(ngram_stats.items(), key=lambda kv: -kv[1]["spend"]):
        sp = stat["spend"]
        sl = stat["sales"]
        od = stat["ord"]
        ck = stat["clk"]
        if sp < 2 and od < 1:
            continue
        cvr_val = round((od / ck * 100) if ck else 0.0, 1)
        cpc_val = round((sp / ck) if ck else 0.0, 2)
        acos_val = round(acos(sp, sl), 1)
        ng_lines.append("\t".join(str(x) for x in [
            ng, stat["gram_type"], round(sp, 2), round(sl, 2), acos_val,
            int(od), int(ck), cvr_val, cpc_val, len(stat["queries"])
        ]))

        # Detect bleeding root phrases
        if sp >= 10 and od == 0 and ck >= 8 and len(stat["queries"]) >= 2:
            # Strong relevance & core title token protection for phrase negations
            _, _, ng_strong = classify_query(ng, profile, rules)
            ng_tokens = {t for t in re.findall(r"[\w]+", ng.lower(), re.UNICODE) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}

            # If ng has strong relevance, overlaps with core title tokens, or appears in strongly relevant queries:
            # DO NOT phrase negate! Doing so would block legitimate traffic and conflict with query protection.
            has_strong_query = any(classify_query(q_item, profile, rules)[2] for q_item in stat["queries"])
            if ng_strong or (ng_tokens and (ng_tokens & title_tokens)) or has_strong_query:
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tPROTECTED(strong_query_overlap)")
                continue

            # Deduplication: Check if this root phrase is already negated or in known_negatives
            all_queries_negated = all(q_item.lower() in all_neg_queries for q_item in stat["queries"])
            is_known_neg = ng.lower() in known_negatives or any(kn == ng.lower() or kn in ng.lower() for kn in known_negatives)
            if ng.lower() in all_neg_queries or all_queries_negated or is_known_neg:
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tALREADY_NEGATED")
                continue

            bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}")
            bulk_adjustments.append({
                "action_type": "Negative_Phrase",
                "campaign": "ALL_NON_EXACT",
                "target_entity": ng,
                "match_type": "negativePhrase",
                "current_value": f"cum_spend=${sp:.2f}, 0 orders across {len(stat['queries'])} queries",
                "recommended_value": "Add Negative Phrase to Broad/Auto Campaigns",
                "reason": f"Bleeding root phrase: wasted ${sp:.2f} with 0 orders",
            })

    tsv(
        dest / "ngrams.tsv",
        "ngram\ttype\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tquery_count",
        ng_lines,
    )
    dest.joinpath("bleeding_ngrams.txt").write_text("\n".join(bleeding_ng), encoding="utf-8")

    # 7. Output cannibalization.tsv
    cannibal_lines = []
    for q_text, occurrences in sorted(cannibal_map.items(), key=lambda kv: -sum(x["spend"] for x in kv[1])):
        if len(occurrences) < 2:
            continue
        total_sp = sum(x["spend"] for x in occurrences)
        total_od = sum(x["orders"] for x in occurrences)
        if total_sp < 2 and total_od == 0:
            continue

        c_names = [x["campaign"] for x in occurrences]
        # Check risk: e.g. Exact vs Broad/Phrase/Auto competing
        risk = "NORMAL_OVERLAP"
        has_exact = any("exact" in str(x["match"]).lower() or "精准" in x["campaign"] or "exact" in x["campaign"].lower() for x in occurrences)
        has_broad_or_auto = any(
            "broad" in str(x["match"]).lower() or "phrase" in str(x["match"]).lower()
            or "auto" in x["campaign"].lower() or "自动" in x["campaign"]
            or "广泛" in x["campaign"] or "词组" in x["campaign"]
            for x in occurrences
        )
        if has_exact and has_broad_or_auto:
            risk = "HIGH_INTERNAL_COMPETITION"

        detail_strs = [
            f"{x['campaign']}({x['match']}|sp=${x['spend']:.1f}|ord={int(x['orders'])}|cpc=${x['cpc']:.2f})"
            for x in occurrences
        ]
        cannibal_lines.append("\t".join([
            q_text, str(len(occurrences)), f"{total_sp:.2f}", str(int(total_od)),
            risk, " ; ".join(detail_strs)
        ]))

    tsv(
        dest / "cannibalization.tsv",
        "query\tcampaign_count\ttotal_spend\ttotal_orders\trisk_level\tdetails",
        cannibal_lines,
    )

    # 8. Output groups.tsv
    groups = as_rows(load_json(src / "ad_groups.json"))
    if asin_campaigns:
        groups = [r for r in groups if r.get("campaign_name") in asin_campaigns]
    g_lines = []
    for r in sorted(groups, key=lambda x: -num(x.get("spends"))):
        if not r.get("campaign_name") and not r.get("name"):
            continue
        g_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("name") or r.get("ad_group_name"), r.get("state"),
            r.get("default_bid") or r.get("bid"),
            round(num(r.get("spends")), 2), round(num(r.get("sales")), 2), round(num(r.get("acos")), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(num(r.get("cvr")), 1),
            round(num(r.get("cpc")), 2),
        ]))
    tsv(
        dest / "groups.tsv",
        "campaign\tadgroup\tstate\tdefbid\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        g_lines,
    )

    # 9. Output targets.tsv (with Competitor Profile and Competitiveness Evaluation)
    t_lines = []
    for r in sorted(targets, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 0.5 and num(r.get("orders")) < 1:
            continue
        c_info = extract_asin_target_info(r)
        comp_str = "--"
        comp_eval = "--"
        if c_info:
            comp_str = f"ASIN={c_info['asin']}|P=${c_info['price']:.2f}|R={c_info['stars']}|REV={c_info['reviews']}"
            action_code, rec_val, reason_text = evaluate_competitor_target(c_info, profile, target_acos)
            comp_eval = action_code
            if action_code in ("Negative_Target", "Lower_Bid", "Harvest_ASIN"):
                c_name_t = c_info["campaign_name"]
                t_state = str(r.get("state") or "enabled").lower().strip()
                camp_state_t = camp_lookup.get(c_name_t, {}).get("state", "enabled")
                if camp_state_t == "enabled" and t_state == "enabled":
                    bulk_adjustments.append({
                        "action_type": action_code,
                        "campaign": c_info["campaign_name"],
                        "target_entity": c_info["asin"],
                        "match_type": "asinSameAs",
                        "current_value": f"spend=${c_info['spend']:.2f}, ord={c_info['orders']}, comp_price=${c_info['price']:.2f} vs our=${profile.get('price')}",
                        "recommended_value": rec_val,
                        "reason": reason_text,
                    })
        t_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("exp_type_zh") or r.get("exp_type_text") or r.get("match_type"),
            r.get("targeting_text") or r.get("expression") or r.get("keyword_text"),
            comp_str, comp_eval,
            r.get("bid"), r.get("default_bid"), r.get("state"),
            round(spend, 2), round(num(r.get("sales")), 2), round(num(r.get("acos")), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(num(r.get("cvr")), 1),
            round(num(r.get("cpc")), 2),
        ]))
    tsv(
        dest / "targets.tsv",
        "campaign\ttype\ttext\tcompetitor_info\tevaluation\tbid\tdefbid\tstate\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        t_lines,
    )

    # 10. Output active_negatives.tsv, active_negatives_audit.tsv, unnegative_recommendations.tsv
    active_neg_lines = []
    for (c_name, ql), item in sorted(active_neg_dict.items(), key=lambda kv: -kv[1]["spend"]):
        sp = item["spend"]
        sl = item["sales"]
        od = item["orders"]
        ck = item["clicks"]
        cvr_v = round(od / ck * 100, 1) if ck else 0.0
        cpc_v = round(sp / ck, 2) if ck else 0.0
        acos_v = round(sp / sl * 100, 1) if sl > 0 else (9999.0 if sp > 0 else 0.0)
        active_neg_lines.append("\t".join(str(x) for x in [
            item["campaign"], item["query"], item["neg_status"],
            round(sp, 2), round(sl, 2), acos_v, od, ck, cvr_v, cpc_v
        ]))
    tsv(
        dest / "active_negatives.tsv",
        "campaign\tquery\tnegative_status\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        active_neg_lines,
    )

    audit_lines = []
    for rec in audit_records:
        audit_lines.append("\t".join(str(x) for x in [
            rec["campaign"], rec["query"], rec["neg_status"], rec["bucket"],
            rec["volume_tier"], rec["searchrank"],
            round(rec["spend"], 2), round(rec["sales"], 2), str(rec["orders"]), str(rec["clicks"]),
            str(rec["cvr"]), str(rec["cpc"]), rec["audit_status"], rec["recommended_action"],
            rec["recommended_value"], rec["reason"]
        ]))
    tsv(
        dest / "active_negatives_audit.tsv",
        "campaign\tquery\tnegative_status\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\tcvr\tcpc\taudit_status\trecommended_action\trecommended_value\treason",
        audit_lines,
    )

    unneg_lines = []
    for rec in audit_records:
        if rec["audit_status"] in ("MIGRATED_BUT_PAUSED_IN_EXACT", "TRUE_ORPHAN_NEGATIVE"):
            unneg_lines.append("\t".join(str(x) for x in [
                rec["campaign"], rec["query"], rec["volume_tier"], rec["searchrank"],
                round(rec["spend"], 2), str(rec["orders"]), rec["audit_status"],
                rec["recommended_action"], rec["recommended_value"], rec["reason"]
            ]))
    tsv(
        dest / "unnegative_recommendations.tsv",
        "campaign\tquery\tvolume_tier\tsearchrank\tspend\torders\taudit_status\trecommended_action\trecommended_value\treason",
        unneg_lines,
    )

    # 11. Output bulk_adjustments.tsv (Consolidated & Deduplicated with Cooling-off Lock Filter)
    all_final_adjustments = []

    # 11.1 Placement adjustments (only if not locked in cooling-off, UNLESS circuit breaker triggered)
    for c in camps:
        if c["state"] != "enabled":
            continue
        c_assessment = c.get("assessment", "")
        if c["name"] in locked_campaigns:
            if c_assessment == "CIRCUIT_BREAKER_BLEED":
                if c["tos"] >= 120:
                    all_final_adjustments.append({
                        "action_type": "Emergency_Placement_Throttle",
                        "campaign": c["name"],
                        "target_entity": "PlacementTop",
                        "match_type": "placement",
                        "current_value": f"TOS={int(c['tos'])}%",
                        "recommended_value": "TOS=60%",
                        "reason": f"AI Circuit Breaker: Bleeding spend during observation (${c['spend']:.1f}, ord={c['ord']}), emergency throttle",
                    })
                elif c["pp"] >= 80:
                    all_final_adjustments.append({
                        "action_type": "Emergency_Placement_Throttle",
                        "campaign": c["name"],
                        "target_entity": "PlacementProductPage",
                        "match_type": "placement",
                        "current_value": f"PP={int(c['pp'])}%",
                        "recommended_value": "PP=20%",
                        "reason": f"AI Circuit Breaker: PP bleeding during observation (${c['spend']:.1f}, acos={c['acos']:.1f}%), emergency throttle",
                    })
            elif c_assessment == "CIRCUIT_BREAKER_SHOCK":
                all_final_adjustments.append({
                    "action_type": "Probe_Bid_Recovery",
                    "campaign": c["name"],
                    "target_entity": "CampaignBaseBid",
                    "match_type": "bidding",
                    "current_value": "Current Lowered Bid",
                    "recommended_value": "+10% ~ +15% Step-up Bid Probe (or +$0.05)",
                    "reason": f"AI Circuit Breaker: Post-adjustment asphyxiation (recent 3d imp plunged {c.get('delta_imp_pct', 0):+.1f}%), step-up probe to recover traffic pool",
                })
            continue

        if c["tos"] >= 150:
            all_final_adjustments.append({
                "action_type": "Placement_TOS",
                "campaign": c["name"],
                "target_entity": "PlacementTop",
                "match_type": "placement",
                "current_value": f"TOS={int(c['tos'])}%",
                "recommended_value": "TOS=60%",
                "reason": "Severe CPC inflation risk, ROS starved",
            })
        elif c["pp"] >= 100 and c["acos"] >= (target_acos * 1.4):
            all_final_adjustments.append({
                "action_type": "Placement_PP",
                "campaign": c["name"],
                "target_entity": "PlacementProductPage",
                "match_type": "placement",
                "current_value": f"PP={int(c['pp'])}%",
                "recommended_value": "PP=20%",
                "reason": f"High PP bleed without conversion edge (acos={c['acos']:.1f}% vs target {target_acos:.0f}%)",
            })

    # 11.2 Keyword misfit pauses (e.g. burr keyword in exact campaign)
    all_final_adjustments.extend(kw_misfit_adjustments)

    # 11.3 Mistaken negative recovery actions
    all_final_adjustments.extend(unnegative_adjustments)

    # 11.4 Search terms & ASIN targets adjustments
    all_final_adjustments.extend(bulk_adjustments)

    # 11.5 Dayparting schedule recommendations (from hourly conversion intelligence)
    if dayparting_records:
        peak_hours = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR"]
        bleed_hours = [r["hour"] for r in dayparting_records if r["classification"] == "BLEEDING_OFFPEAK_HOUR"]
        if peak_hours:
            peak_str = ", ".join(f"{h:02d}:00" for h in sorted(peak_hours))
            all_final_adjustments.append({
                "action_type": "Dayparting_Boost",
                "campaign": "Portfolio/All_Active_SP",
                "target_entity": f"Peak Hours [{peak_str}]",
                "match_type": "schedule",
                "current_value": "Baseline Budget/Bid (100%)",
                "recommended_value": "+15% ~ +25% Bid/Budget Schedule",
                "reason": "High CVR (>=15%) and low ACOS (<=18%) golden conversion window; maximize impression share during purchase peaks",
            })
        if bleed_hours:
            bleed_str = ", ".join(f"{h:02d}:00" for h in sorted(bleed_hours))
            all_final_adjustments.append({
                "action_type": "Dayparting_Throttle",
                "campaign": "Portfolio/All_Active_SP",
                "target_entity": f"Bleeding Hours [{bleed_str}]",
                "match_type": "schedule",
                "current_value": "Baseline Budget/Bid (100%)",
                "recommended_value": "-20% ~ -30% Bid Throttle",
                "reason": "Low CVR (<=11%) or high ACOS (>=24%) window; high click waste without purchase intent",
            })

    # Deduplicate actions by (action_type, campaign, target_entity)
    deduped_adjustments = []
    seen_adj_keys = set()
    for item in all_final_adjustments:
        k = (item["action_type"], item["campaign"], item["target_entity"].lower().strip())
        if k in seen_adj_keys:
            continue
        seen_adj_keys.add(k)
        deduped_adjustments.append(item)

    bulk_lines = []
    for item in deduped_adjustments:
        bulk_lines.append("\t".join([
            item["action_type"], item["campaign"], item["target_entity"],
            item["match_type"], item["current_value"], item["recommended_value"], item["reason"]
        ]))
    gen_time_str = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    tsv(
        dest / "bulk_adjustments.tsv",
        "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\treason",
        bulk_lines,
    )
    tsv(
        dest / f"bulk_adjustments_{gen_time_str}.tsv",
        "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\treason",
        bulk_lines,
    )

    if args.export_dir:
        exp_dir = Path(args.export_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        tsv(
            exp_dir / f"{asin_str}-{country_str}-bulk-adjustments-{gen_time_str}.tsv",
            "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\treason",
            bulk_lines,
        )
        tsv(
            exp_dir / f"{asin_str}-{country_str}-bulk-adjustments.tsv",
            "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\treason",
            bulk_lines,
        )
        if dayparting_table_lines:
            tsv(
                exp_dir / f"{asin_str}-{country_str}-dayparting-analysis.tsv",
                "hour_window\tspend\tad_orders\tstore_orders\tcvr\tacos\tcpc\tclassification\trecommendation",
                dayparting_table_lines,
            )
        if financial_prof:
            exp_dir.joinpath(f"{asin_str}-{country_str}-financial-profile.json").write_text(
                json.dumps(financial_prof, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if traffic_prof:
            exp_dir.joinpath(f"{asin_str}-{country_str}-traffic-profile.json").write_text(
                json.dumps(traffic_prof, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # 12. Output observation_status.tsv
    obs_lines = []
    for cd in active_cooldowns:
        c_name = cd.get("campaign", "")
        matching_camp = next((c for c in camps if c["name"] == c_name), None)
        camp_assessment = matching_camp.get("assessment", "IN_OBSERVATION") if matching_camp else "IN_OBSERVATION"
        if camp_assessment == "CIRCUIT_BREAKER_BLEED":
            obs_status = "CIRCUIT_BREAKER_BLEED"
        elif camp_assessment == "CIRCUIT_BREAKER_SHOCK":
            obs_status = "CIRCUIT_BREAKER_SHOCK"
        elif camp_assessment == "ADJUSTMENT_EFFECTIVE":
            obs_status = "ADJUSTMENT_EFFECTIVE"
        else:
            obs_status = "NORMAL_MONITORING"

        obs_lines.append("\t".join([
            str(cd.get("entity_type", "")),
            str(cd.get("campaign", "")),
            str(cd.get("field", "")),
            str(cd.get("old_value", "")),
            str(cd.get("new_value", "")),
            str(cd.get("changed_at", "")[:10]),
            str(cd.get("days_elapsed", 0)),
            str(cd.get("days_remaining", 0)),
            obs_status,
            str(cd.get("detail", "")),
        ]))
    tsv(
        dest / "observation_status.tsv",
        "entity_type\tcampaign\tfield\told_value\tnew_value\tchanged_date\tdays_elapsed\tdays_remaining\tstatus\tdetail",
        obs_lines,
    )

    # 13. Output persistent historical_ledger files into DB, dest, and exports
    asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
    country_str = str(meta.get("country") or "US").upper()
    file_prefix = f"{asin_str}-{country_str}"
    exp_dir_p = Path(args.export_dir) if args.export_dir else None
    save_ledger_files(ledger, ledger_dir, snap_key, dest, exp_dir_p, file_prefix, gen_time_str)

    # 14. Output summary.txt
    enabled = [c for c in camps if c["state"] == "enabled"]
    total_spend = sum(c["spend"] for c in camps)
    total_sales = sum(c["sales"] for c in camps)
    total_ord = sum(c["ord"] for c in camps)
    total_clk = sum(c["clk"] for c in camps)
    effective_adj_count = sum(1 for x in ledger if "见效" in str(x.get("effect_verdict", "")))
    shock_adj_count = sum(1 for x in ledger if "休克" in str(x.get("effect_verdict", "")))
    bleed_adj_count = sum(1 for x in ledger if "失血" in str(x.get("effect_verdict", "")))

    summary = []
    summary.append(f"window\t{meta.get('window')}")
    summary.append(f"shop\t{meta.get('shop_alias')}\tsid={meta.get('sid')}\tprofile={meta.get('profile_id')}")
    summary.append(f"title\t{profile.get('title')}")
    summary.append(f"price/stars/reviews\t{profile.get('price')}\t{profile.get('stars')}\t{profile.get('reviews')}")
    summary.append(f"small_rank\t{profile.get('small_rank')}")
    summary.append(f"stock\tfulfillable={profile.get('fulfillable')}\tdos={profile.get('days_of_supply')}")
    # 7d and 3d totals
    total_cpc = (total_spend / total_clk) if total_clk > 0 else 0.0
    total_spend_7d = sum(c.get("spend_7d", 0.0) for c in camps)
    total_sales_7d = sum(c.get("sales_7d", 0.0) for c in camps)
    total_ord_7d = sum(c.get("ord_7d", 0) for c in camps)
    total_clk_7d = sum(c.get("clk_7d", 0) for c in camps)
    total_cpc_7d = (total_spend_7d / total_clk_7d) if total_clk_7d > 0 else 0.0

    total_spend_3d = sum(c.get("spend_3d", 0.0) for c in camps)
    total_sales_3d = sum(c.get("sales_3d", 0.0) for c in camps)
    total_ord_3d = sum(c.get("ord_3d", 0) for c in camps)
    total_clk_3d = sum(c.get("clk_3d", 0) for c in camps)
    total_cpc_3d = (total_spend_3d / total_clk_3d) if total_clk_3d > 0 else 0.0

    # Enabled-only totals
    en_spend_30d = sum(c.get("spend", 0.0) for c in enabled)
    en_sales_30d = sum(c.get("sales", 0.0) for c in enabled)
    en_ord_30d = sum(c.get("ord", 0) for c in enabled)
    en_clk_30d = sum(c.get("clk", 0) for c in enabled)
    en_cpc_30d = (en_spend_30d / en_clk_30d) if en_clk_30d > 0 else 0.0

    en_spend_7d = sum(c.get("spend_7d", 0.0) for c in enabled)
    en_sales_7d = sum(c.get("sales_7d", 0.0) for c in enabled)
    en_ord_7d = sum(c.get("ord_7d", 0) for c in enabled)
    en_clk_7d = sum(c.get("clk_7d", 0) for c in enabled)
    en_cpc_7d = (en_spend_7d / en_clk_7d) if en_clk_7d > 0 else 0.0

    en_spend_3d = sum(c.get("spend_3d", 0.0) for c in enabled)
    en_sales_3d = sum(c.get("sales_3d", 0.0) for c in enabled)
    en_ord_3d = sum(c.get("ord_3d", 0) for c in enabled)
    en_clk_3d = sum(c.get("clk_3d", 0) for c in enabled)
    en_cpc_3d = (en_spend_3d / en_clk_3d) if en_clk_3d > 0 else 0.0

    summary.append(f"portfolio_totals_30d_all\tspend={total_spend:.2f}\tsales={total_sales:.2f}\tacos={acos(total_spend,total_sales):.2f}%\tord={int(total_ord)}\tcpc={total_cpc:.2f}")
    summary.append(f"portfolio_totals_30d_enabled\tspend={en_spend_30d:.2f}\tsales={en_sales_30d:.2f}\tacos={acos(en_spend_30d,en_sales_30d):.2f}%\tord={int(en_ord_30d)}\tcpc={en_cpc_30d:.2f}")
    summary.append(f"portfolio_totals_7d_all\tspend={total_spend_7d:.2f}\tsales={total_sales_7d:.2f}\tacos={acos(total_spend_7d,total_sales_7d):.2f}%\tord={int(total_ord_7d)}\tcpc={total_cpc_7d:.2f}")
    summary.append(f"portfolio_totals_7d_enabled\tspend={en_spend_7d:.2f}\tsales={en_sales_7d:.2f}\tacos={acos(en_spend_7d,en_sales_7d):.2f}%\tord={int(en_ord_7d)}\tcpc={en_cpc_7d:.2f}")
    summary.append(f"portfolio_totals_3d_all\tspend={total_spend_3d:.2f}\tsales={total_sales_3d:.2f}\tacos={acos(total_spend_3d,total_sales_3d):.2f}%\tord={int(total_ord_3d)}\tcpc={total_cpc_3d:.2f}")
    summary.append(f"portfolio_totals_3d_enabled\tspend={en_spend_3d:.2f}\tsales={en_sales_3d:.2f}\tacos={acos(en_spend_3d,en_sales_3d):.2f}%\tord={int(en_ord_3d)}\tcpc={en_cpc_3d:.2f}")
    summary.append(f"enabled_campaigns\t{len(enabled)} / {len(camps)}")
    summary.append(f"asin_campaigns_filtered\t{len(asin_campaigns)}")
    high_tos = [c["name"] for c in enabled if c["tos"] >= 180]
    summary.append("high_tos>=180\t" + " | ".join(high_tos[:12]))
    summary.append(f"active_negatives_total\t{len(active_neg_dict)}")
    promoted_active_count = sum(1 for r in audit_records if r["audit_status"] == "PROMOTED_MIGRATED_PROTECTED")
    summary.append(f"promoted_migrated_protected\t{promoted_active_count}")
    migrated_paused_count = sum(1 for r in audit_records if r["audit_status"] == "MIGRATED_BUT_PAUSED_IN_EXACT")
    summary.append(f"migrated_paused_in_exact\t{migrated_paused_count}")
    true_orphan_count = sum(1 for r in audit_records if r["audit_status"] == "TRUE_ORPHAN_NEGATIVE")
    summary.append(f"true_orphan_negatives\t{true_orphan_count}")
    safe_hard_count = sum(1 for r in audit_records if r["audit_status"] in ("SAFE_HARD_NEGATIVE", "SAFE_ASIN_DEFENSE"))
    summary.append(f"safe_hard_negatives_confirmed\t{safe_hard_count}")
    soft_monitor_count = sum(1 for r in audit_records if r["audit_status"] == "SOFT_MONITOR")
    summary.append(f"soft_monitor_negatives\t{soft_monitor_count}")
    summary.append(f"conflicting_keywords_paused\t{len(kw_misfit_adjustments)}")
    summary.append(f"bleeding_ngrams_count\t{len(bleeding_ng)}")
    summary.append(f"cannibalized_queries_count\t{len(cannibal_lines)}")
    summary.append(f"bulk_adjustments_count\t{len(deduped_adjustments)}")
    summary.append(f"historical_adjustments_tracked\t{len(ledger)}")
    summary.append(f"effective_adjustments_count\t{effective_adj_count}")
    summary.append(f"shock_adjustments_count\t{shock_adj_count}")
    summary.append(f"bleed_adjustments_count\t{bleed_adj_count}")
    summary.append(f"active_cooldowns_count\t{len(active_cooldowns)}")
    summary.append(f"locked_campaigns_count\t{len(locked_campaigns)}")
    summary.append(f"database_daily_record_saved\tdb/daily_records/{snap_key}_{gen_date_str}.json")
    summary.append(f"database_ledger_saved\tdb/ledger/{snap_key}_ledger.json")
    summary.append(f"searchrank_keywords_tracked\t{len(sr_map)}")
    trapped_count = sum(1 for d in placement_diagnoses if d.get("strategy_tag") == "TRAPPED_CORE_HERO")
    failed_tos_count = sum(1 for d in placement_diagnoses if d.get("strategy_tag") == "FAILED_AT_TOS")
    summary.append(f"placement_diagnosed_queries\t{len(placement_diagnoses)}")
    summary.append(f"trapped_core_heroes_elevate\t{trapped_count}")
    summary.append(f"premier_tos_failed_throttle\t{failed_tos_count}")
    protected_strong_count = sum(1 for a in deduped_adjustments if a.get("action_type") in ("Refine_Bid_Placement", "Elevate_To_TOS", "Throttle_TOS_Bid"))
    summary.append(f"strong_relevance_queries_protected\t{protected_strong_count}")
    competitor_actions_count = sum(1 for a in deduped_adjustments if a.get("match_type") == "asinSameAs")
    if financial_prof:
        summary.append(f"financial_gross_profit\t${financial_prof.get('gross_profit', 0):.2f}\tmargin={financial_prof.get('gross_margin', 0):.1f}%\tbreak_even_acos={financial_prof.get('break_even_acos', 0):.1f}%")
        summary.append(f"financial_cost_structure\tcogs=${financial_prof.get('cogs_per_unit', 0):.2f}\tfreight=${financial_prof.get('logistics_per_unit', 0):.2f}\tfba=${financial_prof.get('fba_per_unit', 0):.2f}\tcomm=${financial_prof.get('comm_per_unit', 0):.2f}")
    if traffic_prof:
        tot_sess = traffic_prof.get('sessions_total', 0)
        mob_sess = traffic_prof.get('sessions_mobile', 0)
        mob_share = (mob_sess / tot_sess * 100) if tot_sess > 0 else 0.0
        summary.append(f"listing_traffic_macro\tsessions={tot_sess}\tpageviews={traffic_prof.get('page_views_total', 0)}\tmobile_share={mob_share:.1f}%")
        summary.append(f"listing_cvr_comparison\tad_cvr={traffic_prof.get('ad_cvr', 0):.2f}%\tnature_cvr={traffic_prof.get('nature_cvr', 0):.2f}%\ttacos={traffic_prof.get('tacos', 0):.2f}%\tadv_sales_share={traffic_prof.get('adv_sales_share', 0):.2f}%")
    if dayparting_records:
        peak_hrs = [f"{r['hour']:02d}:00" for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR"]
        bleed_hrs = [f"{r['hour']:02d}:00" for r in dayparting_records if r["classification"] == "BLEEDING_OFFPEAK_HOUR"]
        summary.append(f"dayparting_peak_hours\t{', '.join(peak_hrs) if peak_hrs else 'None'}")
        summary.append(f"dayparting_bleeding_hours\t{', '.join(bleed_hrs) if bleed_hrs else 'None'}")
    if diff_events:
        summary.append(f"diff_events_detected\t{' ; '.join(diff_events[:5])}")
    summary.append(f"generation_timestamp\t{gen_time_str}")
    dest.joinpath("summary.txt").write_text("\n".join(summary), encoding="utf-8")

    print(f"Analysis complete. Tables saved to {dest}")
    print("\n".join(summary[:16]))


if __name__ == "__main__":
    main()
