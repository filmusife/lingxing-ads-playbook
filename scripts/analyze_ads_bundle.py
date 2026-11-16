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
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lx_client import rows as lx_rows


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
    """Pull the row list out of a Lingxing payload, whatever wrapper depth it arrived in."""
    return lx_rows(obj)


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


def clean_api_acos(raw_acos, spend: float = 0.0, sales: float = 0.0, zero_sales_sentinel: float = 0.0) -> float:
    """Sanitize LingXing API acos where 0-order spend returns 99999999.0."""
    sp = num(spend)
    sl = num(sales)
    if sl > 0:
        return round(sp / sl * 100.0, 2)
    v = num(raw_acos)
    if 0.0 < v < 5000.0:
        return round(v, 2)
    return zero_sales_sentinel if sp > 0 else 0.0


def calc_roas(spend: float, sales: float, raw_roas=None) -> float:
    sp = num(spend)
    sl = num(sales)
    if sp > 0 and sl >= 0:
        return round(sl / sp, 2)
    v = num(raw_roas)
    return round(v, 2) if 0.0 < v < 1000.0 else 0.0


ASIN_TERM_RE = re.compile(r"^b0[0-9a-z]{8}$", re.I)


def is_asin_term(text: str) -> bool:
    return bool(ASIN_TERM_RE.match((text or "").strip()))


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


def sanitize_product_rules(rules: dict, profile: dict) -> dict:
    """Quarantine any self-contradictory is_not_patterns that match the product's own positive title nouns.

    Prevents catastrophic false-positive Pause_Keyword / misfit classification when a rule file
    accidentally includes a primary product noun (e.g. 'grinder' on a Coffee Grinder listing),
    while preserving legitimate negated-claim exclusions (e.g. 'plastic' when title says 'plastic free').
    """
    if not isinstance(rules, dict):
        return rules
    raw_title = str(profile.get("title") or "").lower()
    if not raw_title.strip():
        return rules

    # Strip negated claims from title before checking positive self-overlap
    positive_title = re.sub(r"\b(?:no|not|without|non)[-\s]+\w+(?:\s+\w+)?", " ", raw_title)
    positive_title = re.sub(r"\b\w+[-\s]+free\b", " ", positive_title)

    safe_is_not = []
    quarantined = []
    for item in rules.get("is_not_patterns", []) or []:
        pat = item if isinstance(item, str) else item.get("pattern", "")
        if pat:
            try:
                if re.search(pat, positive_title, re.I):
                    quarantined.append(item)
                    continue
            except re.error:
                quarantined.append(item)
                continue

            # Quarantine consumable is_not patterns that conflict with explicit positive is_patterns
            is_pattern_conflict = False
            for is_item in rules.get("is_patterns", []) or []:
                is_pat = is_item if isinstance(is_item, str) else is_item.get("pattern", "")
                if is_pat:
                    is_sample = re.sub(r"[\^\$\b\\]", "", is_pat).replace(r"\s+", " ").strip()
                    try:
                        if re.search(pat, is_sample, re.I):
                            lbl = item.get("label", "") if isinstance(item, dict) else ""
                            if lbl.startswith("is_not_coffee_beans") or "beans" in lbl or "consumable" in lbl:
                                is_pattern_conflict = True
                                break
                    except re.error:
                        pass
            if is_pattern_conflict:
                quarantined.append(item)
                continue

        safe_is_not.append(item)

    out = dict(rules)
    out["is_not_patterns"] = safe_is_not
    if quarantined:
        out["quarantined_self_title_patterns"] = quarantined
    return out


def _parse_window_bounds(meta: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(meta, dict):
        return None, None
    w = meta.get("window")
    if isinstance(w, dict):
        s_dt = str(w.get("start_date") or meta.get("start_date") or "").strip()[:10] or None
        e_dt = str(w.get("end_date") or meta.get("end_date") or "").strip()[:10] or None
        return s_dt, e_dt
    w_str = str(w or "").strip()
    parts = [p.strip() for p in w_str.split(" - ") if p.strip()]
    if len(parts) == 2:
        return parts[0][:10], parts[1][:10]
    s_fb = str(meta.get("start_date") or "").strip()[:10] or None
    e_fb = str(meta.get("end_date") or "").strip()[:10] or None
    return s_fb, e_fb


def _select_store_row(rows: list[dict], sid: str | int | None) -> dict:
    if not rows:
        return {}
    sid_str = str(sid or "").strip()
    if sid_str and sid_str not in ("None", "0", ""):
        # Pass 1: Prefer an exact single-store row [sid_str] over a multi-store aggregated summary row [1075, 1132]
        for r in rows:
            if not isinstance(r, dict):
                continue
            r_sids = [str(x).strip() for x in (r.get("sids") or []) if str(x).strip()]
            r_sid_single = str(r.get("sid") or "").strip()
            if (len(r_sids) == 1 and r_sids[0] == sid_str) or (not r_sids and r_sid_single == sid_str):
                return r
        # Pass 2: Fallback to multi-store summary row containing sid_str
        for r in rows:
            if not isinstance(r, dict):
                continue
            r_sids = [str(x).strip() for x in (r.get("sids") or []) if str(x).strip()]
            if sid_str in r_sids or str(r.get("sid") or "").strip() == sid_str:
                return r
    return rows[0] if isinstance(rows[0], dict) else {}


def _extract_row_ad_costs(r: dict) -> dict[str, float]:
    sp_cost = abs(num(r.get("ads_sp_cost") if r.get("ads_sp_cost") is not None else r.get("sp_cost")))
    sb_cost = abs(num(r.get("ads_sb_cost") if r.get("ads_sb_cost") is not None else r.get("sb_cost"))) + abs(num(r.get("shared_ads_sb_cost")))
    sbv_cost = abs(num(r.get("ads_sbv_cost") if r.get("ads_sbv_cost") is not None else r.get("sbv_cost"))) + abs(num(r.get("shared_ads_sbv_cost")))
    sd_cost = abs(num(r.get("ads_sd_cost") if r.get("ads_sd_cost") is not None else r.get("sd_cost"))) + abs(num(r.get("shared_ads_sd_cost")))
    shared_other = abs(num(r.get("shared_cost_of_advertising")))
    summed = sp_cost + sb_cost + sbv_cost + sd_cost + shared_other
    explicit_spend = abs(num(r.get("spend") or r.get("cost_of_advertising")))
    total_ad = max(explicit_spend, summed) if explicit_spend > 0 else summed
    return {
        "total_ad_cost": round(total_ad, 2),
        "sp_ad_cost": round(sp_cost, 2),
        "sb_sbv_ad_cost": round(sb_cost + sbv_cost, 2),
        "sd_ad_cost": round(sd_cost, 2),
    }


def load_financial_profile(indir: Path, meta: dict | None = None) -> dict:
    rows = [r for r in as_rows(load_json(indir / "order_profit.json")) if isinstance(r, dict)]
    if not rows:
        return {}

    target_sid = (meta or {}).get("sid")
    r = _select_store_row(rows, target_sid)
    if not r:
        return {}

    total_sales = num(r.get("net_amount") or r.get("amount") or r.get("total_sales_amount"))
    gross_profit = num(r.get("gross_profit") if r.get("gross_profit") is not None else r.get("order_gross_profit"))
    raw_gm = num(r.get("gross_margin") if r.get("gross_margin") is not None else r.get("net_gross_margin"))
    # Bug fix: cross-check raw_gm against gross_profit / total_sales so decimal ratios < -1.0 (e.g. -2.7248 = -272.48%)
    # are properly scaled by 100 while percentage values (e.g. -17.3) are kept as-is!
    if total_sales > 0 and raw_gm != 0:
        expected_ratio = gross_profit / total_sales
        if abs(raw_gm - expected_ratio) <= 0.08:
            gross_margin_pct = round(raw_gm * 100.0, 2)
        elif abs(raw_gm - expected_ratio * 100.0) <= 5.0:
            gross_margin_pct = round(raw_gm, 2)
        else:
            gross_margin_pct = round(expected_ratio * 100.0, 2)
    else:
        gross_margin_pct = round(raw_gm * (100.0 if abs(raw_gm) <= 1.0 else 1.0), 2)

    purchase_costs = abs(num(r.get("purchase_costs") if r.get("purchase_costs") is not None else r.get("cg_price")))
    afn_purchase_costs = abs(num(r.get("afn_purchase_costs")))
    logistics_costs = abs(num(r.get("logistics_costs") if r.get("logistics_costs") is not None else r.get("cg_transport_costs")))
    fba_fulfillment_fee = abs(num(r.get("fba_fulfillment_fee") if r.get("fba_fulfillment_fee") is not None else r.get("fba_delivery_fee")))
    selling_fee = abs(num(r.get("selling_fee") if r.get("selling_fee") is not None else r.get("commission_amount")))
    storage_base = r.get("fba_storage_fee") if r.get("fba_storage_fee") is not None else r.get("monthly_storage_fee")
    if storage_base is None:
        storage_base = r.get("storage_fee")
    # Bug fix: LingXing order_profit stores FBA monthly & long-term storage fees in total_stock_fee / shared_fba_storage_fee
    explicit_storage = (
        abs(num(storage_base))
        + abs(num(r.get("long_term_storage_fee")))
        + abs(num(r.get("shared_fba_storage_fee")))
        + abs(num(r.get("shared_long_term_storage_fee")))
    )
    storage_fee_total = max(abs(num(r.get("total_stock_fee"))), explicit_storage)

    vol = num(r.get("volume") or r.get("order_items_quantity") or r.get("total_sales_quantity"))
    afn_vol = num(r.get("afn_quantity")) or vol
    avg_price = (total_sales / vol) if vol > 0 else 0.0
    cogs_per_unit = (purchase_costs / vol) if vol > 0 else abs(num(r.get("avg_purchase_costs")))
    afn_cogs_per_unit = (afn_purchase_costs / afn_vol) if (afn_purchase_costs > 0 and afn_vol > 0) else 0.0
    logistics_per_unit = (logistics_costs / vol) if vol > 0 else abs(num(r.get("avg_logistics_costs")))
    fba_per_unit = (fba_fulfillment_fee / vol) if vol > 0 else 0.0
    comm_per_unit = (selling_fee / vol) if vol > 0 else 0.0
    storage_per_unit = (storage_fee_total / vol) if vol > 0 else 0.0

    # Detect ERP COGS currency/entry anomalies (e.g. RMB purchase cost entered as USD > avg_price while afn_purchase_costs is normal)
    effective_cogs_per_unit = cogs_per_unit
    cogs_anomaly_warning = ""
    if avg_price > 0 and cogs_per_unit > avg_price * 1.05:
        if 0 < afn_cogs_per_unit <= avg_price * 0.80:
            effective_cogs_per_unit = afn_cogs_per_unit
            cogs_anomaly_warning = (
                f"ERP总采购成本异常 (${cogs_per_unit:.2f}/件 > 售价${avg_price:.2f}，疑为币种或批次录入偏差)，"
                f"保本ACOS已自动采用FBA实发采购成本 (${afn_cogs_per_unit:.2f}/件) 核算"
            )
        else:
            effective_cogs_per_unit = round(avg_price * 0.28, 2)
            cogs_anomaly_warning = (
                f"ERP采购成本异常 (${cogs_per_unit:.2f}/件 > 售价${avg_price:.2f})，"
                f"保本ACOS已采用行业标准货值率 (28% = ${effective_cogs_per_unit:.2f}/件) 兜底防失真"
            )

    break_even_profit = avg_price - effective_cogs_per_unit - logistics_per_unit - fba_per_unit - comm_per_unit
    raw_be_acos = (break_even_profit / avg_price * 100.0) if avg_price > 0 else 0.0
    # Guard operational break_even_acos so transient shipping/cost spikes never produce negative break-even ACOS
    break_even_acos = round(raw_be_acos, 1) if raw_be_acos >= 15.0 else round(max(18.0, (avg_price - effective_cogs_per_unit - fba_per_unit - comm_per_unit) / avg_price * 100.0) if avg_price > 0 else 25.0, 1)
    if break_even_acos < 15.0:
        break_even_acos = 22.0
    break_even_acos_after_storage = round(((break_even_profit - storage_per_unit) / avg_price * 100.0), 1) if avg_price > 0 else 0.0

    raw_refund = num(r.get("refund_amount_rate") or r.get("return_rate"))
    refund_rate_pct = round(raw_refund * (100.0 if abs(raw_refund) <= 1.0 else 1.0), 2)
    ad_costs = _extract_row_ad_costs(r)

    # Cross-shop shared ASIN aggregation without double-counting multi-sid summary rows
    single_store_rows: dict[str, dict] = {}
    multi_store_rows: list[tuple[list[str], dict]] = []
    for row_item in rows:
        r_sids = sorted({str(x).strip() for x in (row_item.get("sids") or []) if str(x).strip()})
        if not r_sids and str(row_item.get("sid") or "").strip():
            r_sids = [str(row_item.get("sid")).strip()]
        if len(r_sids) == 1:
            single_store_rows.setdefault(r_sids[0], row_item)
        elif len(r_sids) > 1:
            multi_store_rows.append((r_sids, row_item))

    all_distinct_sids = set(single_store_rows.keys())
    for m_sids, _ in multi_store_rows:
        all_distinct_sids.update(m_sids)

    is_cross_shop = len(all_distinct_sids) > 1
    if len(single_store_rows) >= 2:
        agg_source = list(single_store_rows.values())
        omni_sales = sum(num(x.get("net_amount") or x.get("amount") or x.get("total_sales_amount")) for x in agg_source)
        omni_vol = int(sum(num(x.get("volume") or x.get("order_items_quantity") or x.get("total_sales_quantity")) for x in agg_source))
        omni_gp = sum(num(x.get("gross_profit") if x.get("gross_profit") is not None else x.get("order_gross_profit")) for x in agg_source)
        omni_ad = sum(_extract_row_ad_costs(x)["total_ad_cost"] for x in agg_source)
    elif multi_store_rows:
        # Only multi-sid summary row returned by API
        _, top_multi = max(multi_store_rows, key=lambda item: len(item[0]))
        omni_sales = num(top_multi.get("net_amount") or top_multi.get("amount") or top_multi.get("total_sales_amount"))
        omni_vol = int(num(top_multi.get("volume") or top_multi.get("order_items_quantity") or top_multi.get("total_sales_quantity")))
        omni_gp = num(top_multi.get("gross_profit") if top_multi.get("gross_profit") is not None else top_multi.get("order_gross_profit"))
        omni_ad = _extract_row_ad_costs(top_multi)["total_ad_cost"]
    else:
        omni_sales, omni_vol, omni_gp, omni_ad = total_sales, int(vol), gross_profit, ad_costs["total_ad_cost"]

    real_tacos_pct = round((ad_costs["total_ad_cost"] / total_sales * 100.0), 2) if total_sales > 0 else 0.0

    result = {
        "matched_sids": r.get("sids") or ([str(target_sid)] if target_sid else []),
        "total_sales": round(total_sales, 2),
        "volume": int(vol),
        "avg_price": round(avg_price, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_margin": gross_margin_pct,
        "cogs_per_unit": round(cogs_per_unit, 2),
        "effective_cogs_per_unit": round(effective_cogs_per_unit, 2),
        "logistics_per_unit": round(logistics_per_unit, 2),
        "fba_per_unit": round(fba_per_unit, 2),
        "comm_per_unit": round(comm_per_unit, 2),
        "storage_per_unit": round(storage_per_unit, 2),
        "storage_fee_total": round(storage_fee_total, 2),
        "refund_rate": refund_rate_pct,
        "total_ad_cost": ad_costs["total_ad_cost"],
        "sp_ad_cost": ad_costs["sp_ad_cost"],
        "sb_sbv_ad_cost": ad_costs["sb_sbv_ad_cost"],
        "sd_ad_cost": ad_costs["sd_ad_cost"],
        "real_tacos_pct": real_tacos_pct,
        "break_even_acos": round(break_even_acos, 1),
        "break_even_acos_raw": round(raw_be_acos, 1),
        "break_even_acos_after_storage": break_even_acos_after_storage,
        "is_cross_shop_asin": is_cross_shop,
    }
    if cogs_anomaly_warning:
        result["cogs_anomaly_warning"] = cogs_anomaly_warning
    if is_cross_shop:
        result["omni_shop_summary"] = {
            "shop_count": len(all_distinct_sids),
            "sids": sorted(all_distinct_sids),
            "omni_total_sales": round(omni_sales, 2),
            "omni_volume": omni_vol,
            "omni_gross_profit": round(omni_gp, 2),
            "omni_total_ad_cost": round(omni_ad, 2),
            "omni_tacos": round((omni_ad / omni_sales * 100.0), 2) if omni_sales > 0 else 0.0,
        }
    return result


def load_traffic_profile(indir: Path, db_dir: Path | None = None, financial_prof: dict | None = None) -> dict:
    rows = as_rows(load_json(indir / "product_performance.json"))
    r = rows[0] if rows else {}
    total_sessions = int(num(r.get("sessions_total") or r.get("sessions")))
    total_pageviews = int(num(r.get("page_views_total") or r.get("page_views")))

    # Direction 4 & Rule 46: Graceful Fallback from Historical Snapshots when Traffic API Drops
    is_fallback = False
    if not r or total_sessions == 0:
        candidate_dirs = []
        if db_dir:
            candidate_dirs.append(Path(db_dir) / "daily_records")
        candidate_dirs.append(indir.parent.parent / "db" / "daily_records")
        candidate_dirs.append(indir.parent / "db" / "daily_records")

        for cdir in candidate_dirs:
            if cdir.exists():
                daily_files = sorted([f for f in cdir.glob("*.json") if not f.name.endswith("_snap.json")])
                for df in reversed(daily_files):
                    try:
                        d_data = json.loads(df.read_text(encoding="utf-8"))
                        prev_trf = d_data.get("traffic")
                        if prev_trf and isinstance(prev_trf, dict) and prev_trf.get("sessions_total", 0) > 0:
                            r = {
                                "sessions_total": prev_trf.get("sessions_total"),
                                "page_views_total": prev_trf.get("page_views_total"),
                                "sessions_mobile": prev_trf.get("sessions_mobile"),
                                "page_views_mobile": prev_trf.get("page_views_mobile"),
                                "ad_cvr": (prev_trf.get("ad_cvr") or 0) / 100.0,
                                "nature_cvr": (prev_trf.get("nature_cvr_raw") or 0) / 100.0,
                                "cvr": (prev_trf.get("total_cvr") or 0) / 100.0,
                                "adv_rate": (prev_trf.get("adv_sales_share") or 0) / 100.0,
                                "acoas": (prev_trf.get("tacos") or 0) / 100.0,
                                "return_goods_rate": (prev_trf.get("return_goods_rate") or 0) / 100.0,
                            }
                            total_sessions = int(num(r.get("sessions_total")))
                            total_pageviews = int(num(r.get("page_views_total")))
                            is_fallback = True
                            break
                    except Exception:
                        continue
                if is_fallback:
                    break

    if not r or total_sessions == 0:
        tacos_fin = 0.0
        if financial_prof and financial_prof.get("total_sales", 0) > 0:
            tacos_fin = round(financial_prof.get("total_ad_cost", 0) / financial_prof["total_sales"] * 100, 2)
        return {
            "sessions_total": 0,
            "page_views_total": 0,
            "sessions_mobile": 0,
            "page_views_mobile": 0,
            "ad_cvr": 0.0,
            "nature_cvr": 0.0,
            "nature_cvr_raw": 0.0,
            "total_cvr": 0.0,
            "adv_sales_share": 0.0,
            "attribution_lag": False,
            "attribution_interpolated": True,
            "tacos": tacos_fin,
            "return_goods_rate": financial_prof.get("refund_rate", 0.0) if financial_prof else 0.0,
            "cvr_ad_vs_nature_ratio": 0.0,
            "organic_readiness": "ORGANIC_BALANCED",
            "organic_guidance": "流量接口未返回数据，TACOS 由财务数据真实推导，已防止指标归零。",
        }

    def _perf_ratio_to_pct(raw_val) -> float:
        """LingXing query_product_performance_asin_lists always returns ratio fields as decimal (e.g. '0.2928'=29.28%, '1.3370'=133.70%).
        Only keep unscaled if explicitly formatted with a trailing '%' sign.
        """
        if raw_val is None:
            return 0.0
        s = str(raw_val).strip()
        v = num(raw_val)
        if s.endswith("%"):
            return round(v, 2)
        return round(v * 100.0, 2)

    ad_cvr = _perf_ratio_to_pct(r.get("ad_cvr"))
    nature_cvr_raw = _perf_ratio_to_pct(r.get("nature_cvr"))
    nature_cvr = max(0.0, nature_cvr_raw)
    total_cvr = _perf_ratio_to_pct(r.get("cvr"))

    nature_order_items_raw = int(num(r.get("nature_order_items")))
    order_items_total = int(num(r.get("order_items") or r.get("volume")))
    ad_order_quantity = int(num(r.get("ad_order_quantity")))
    ad_direct_order_quantity = int(num(r.get("ad_direct_order_quantity")))
    if nature_order_items_raw < 0:
        # When cross-variant/halo indirect orders push ad_order_quantity > order_items, recover direct organic floor or clamp to 0
        if ad_direct_order_quantity > 0 and order_items_total >= ad_direct_order_quantity:
            nature_order_items = order_items_total - ad_direct_order_quantity
        else:
            nature_order_items = 0
    else:
        nature_order_items = nature_order_items_raw

    adv_rate_unclamped = _perf_ratio_to_pct(r.get("adv_rate"))
    # Bug fix: evaluate attribution_lag BEFORE clamping adv_rate to [0, 100] and include negative nature_order_items_raw!
    has_attribution_lag = (adv_rate_unclamped > 100.0) or (nature_cvr_raw < 0) or (nature_order_items_raw < 0)

    tacos = _perf_ratio_to_pct(r.get("acoas"))

    # Cross-verify TACOS with authoritative financial ground truth (total_ad_cost / total_sales)
    if financial_prof and financial_prof.get("total_sales", 0) > 0:
        fin_tacos = round(financial_prof.get("total_ad_cost", 0) / financial_prof["total_sales"] * 100, 2)
        if tacos <= 0.0 or abs(tacos - fin_tacos) > 20.0:
            tacos = fin_tacos

    adv_rate = min(100.0, max(0.0, adv_rate_unclamped))

    # Assess Listing Organic Readiness Index (Rule 27 & Rule 50):
    # Require minimum volume (nature_order_items >= 45 & total_sessions >= 1500) before granting ORGANIC_FLYWHEEL!
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
    if nature_cvr <= 1.0 or adv_rate >= 90.0 or nature_order_items < 15:
        organic_readiness = "ORGANIC_FRAGILE"
        organic_guidance = (
            "Listing 自然承接力脆弱（纯广告依赖型或自然单底座极薄）：自然位缓冲厚度不足，"
            "严禁执行全账户多活动同步大砍 TOS 与基础竞价的双重抑制（Double-Damping），"
            "必须执行单变量阶梯式微降（每次下调 TOS <= 25%），并将预算向细分具体卖点词转移，严防大盘失速休克！"
        )
    elif nature_cvr >= 5.0 and adv_rate <= 75.0 and nature_order_items >= 45 and total_sessions >= 1500:
        organic_readiness = "ORGANIC_FLYWHEEL"
        organic_guidance = (
            "Listing 具备成熟自然搜索飞轮（自然转化良好/自然单量充沛/BSR靠前）："
            "果断拉平畸形 TOS，清洗外围泛流，集中兵力于核心精准仓，可安全释放自然位扩展红利。"
        )
    else:
        organic_readiness = "ORGANIC_BALANCED"
        organic_guidance = "Listing 处于标准稳态演进期，按标准单变量阶梯调控执行。"

    if is_high_refund:
        organic_guidance = f"[退款率高危警报 refund_rate={financial_prof.get('refund_rate')}%] {organic_guidance} 注意：启动增量毛利与销售底盘硬刹车，单靠广告调参无法扭亏，必须联动供应链/Listing排查！"

    if is_fallback:
        organic_guidance = f"[容灾平滑兜底: 沿用上期有效访客流并由财务真实数据核算] {organic_guidance}"

    raw_ret_rate = num(r.get("return_goods_rate"))
    ret_rate_pct = round(raw_ret_rate * (100.0 if abs(raw_ret_rate) <= 1.0 else 1.0), 2)

    raw_bb = num(r.get("buy_box_percentage"))
    bb_pct = round(raw_bb * 100.0, 2) if 0.0 < raw_bb <= 1.0 else round(raw_bb, 2)

    return {
        "sessions_total": total_sessions,
        "page_views_total": total_pageviews,
        "sessions_mobile": int(num(r.get("sessions_mobile"))),
        "page_views_mobile": int(num(r.get("page_views_mobile"))),
        "buy_box_percentage": bb_pct,
        "nature_order_items": nature_order_items,
        "nature_order_items_raw": nature_order_items_raw,
        "ad_order_quantity": ad_order_quantity,
        "ad_direct_order_quantity": ad_direct_order_quantity,
        "ad_cvr": ad_cvr,
        "nature_cvr": nature_cvr,
        "nature_cvr_raw": nature_cvr_raw,
        "total_cvr": total_cvr,
        "adv_sales_share": adv_rate,
        "adv_sales_share_raw": adv_rate_unclamped,
        "attribution_lag": has_attribution_lag,
        "attribution_interpolated": is_fallback,
        "tacos": tacos,
        "return_goods_rate": ret_rate_pct,
        "cvr_ad_vs_nature_ratio": round(ad_cvr / nature_cvr, 2) if nature_cvr > 0 else 0.0,
        "organic_readiness": organic_readiness,
        "organic_guidance": organic_guidance,
    }


def load_deal_info(indir: Path, meta: dict | None = None) -> dict:
    """Load deal, promotion, and discount intelligence from deal_info.json, anchored to report window end_date."""
    p = indir / "deal_info.json"
    data = None
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = None

    if not data or not isinstance(data, dict):
        data = {
            "asin": "",
            "country": "",
            "has_deal_history": False,
            "is_active_deal": False,
            "is_deal_transition": False,
            "days_since_last_deal": None,
            "last_deal_date": None,
            "deal_days": [],
            "coupon_days": [],
            "prime_days": [],
            "promotion_days": [],
            "sec_kill_records": [],
            "prime_discount_records": [],
            "financial_promotions": {
                "promotion_discount": 0.0,
                "promotion_amount": 0.0,
                "promotion_volume": 0,
                "sp_discount": 0.0,
                "promotion_fee": 0.0,
            },
            "summary_badge": "NONE",
            "summary_desc": "近30天无秒杀/促销活动记录",
        }

    _, window_end = _parse_window_bounds(meta)
    anchor_str = window_end or str(data.get("anchor_date") or "")[:10]
    try:
        ref_dt = datetime.strptime(anchor_str, "%Y-%m-%d").date() if anchor_str else datetime.now().date()
    except Exception:
        ref_dt = datetime.now().date()
    anchor_iso = ref_dt.strftime("%Y-%m-%d")

    # Enhance and validate deal dates anchored to ref_dt (prevent future promotionEndTime from polluting last_deal_date)
    deal_dates = []
    active_deal_window = None
    for d in data.get("deal_days", []) or []:
        if isinstance(d, dict) and d.get("date"):
            dt_s = str(d["date"])[:10]
            if dt_s <= anchor_iso:
                deal_dates.append(dt_s)
    for p_day in data.get("prime_days", []) or []:
        if isinstance(p_day, dict) and p_day.get("date"):
            dt_s = str(p_day["date"])[:10]
            if dt_s <= anchor_iso:
                deal_dates.append(dt_s)
    for s in data.get("sec_kill_records", []) or []:
        if not isinstance(s, dict):
            continue
        st_val = str(s.get("originStatus") or s.get("status") or "").upper()
        if st_val in ("CANCELED", "CANCELLED", "SUPPRESSED"):
            continue
        p_start = str(s.get("promotionStartTime") or "")[:10]
        p_end = str(s.get("promotionEndTime") or "")[:10]
        if p_start and p_end:
            if p_start <= anchor_iso <= p_end:
                deal_dates.append(anchor_iso)
                active_deal_window = f"{p_start} ~ {p_end}"
            elif p_end <= anchor_iso:
                deal_dates.append(p_end)
            # Also expand historical days of the sec_kill record up to anchor_iso for overlap detection
            try:
                cur_d = datetime.strptime(p_start, "%Y-%m-%d").date()
                lim_d = min(ref_dt, datetime.strptime(p_end, "%Y-%m-%d").date())
                while cur_d <= lim_d:
                    deal_dates.append(cur_d.strftime("%Y-%m-%d"))
                    cur_d += timedelta(days=1)
            except Exception:
                pass

    deal_dates_unique = sorted(set(deal_dates))
    data["all_deal_dates"] = deal_dates_unique
    if active_deal_window:
        data["active_deal_window"] = active_deal_window

    if deal_dates_unique:
        data["has_deal_history"] = True
        latest_deal = max(deal_dates_unique)
        data["last_deal_date"] = latest_deal
    elif data.get("last_deal_date") and str(data["last_deal_date"])[:10] > anchor_iso:
        data["last_deal_date"] = anchor_iso

    prof_rows = [r for r in as_rows(load_json(indir / "order_profit.json")) if isinstance(r, dict)]
    if prof_rows:
        row = _select_store_row(prof_rows, (meta or {}).get("sid"))
        promo_disc = float(num(row.get("promotion_discount")))
        data.setdefault("financial_promotions", {})
        data["financial_promotions"]["promotion_discount"] = promo_disc
        data["financial_promotions"]["promotion_fee"] = float(num(row.get("promotion_fee")))
        data["financial_promotions"]["sp_discount"] = float(num(row.get("sp_discount")))
        if promo_disc < -10.0 and not data.get("has_deal_history"):
            data["has_deal_history"] = True

    perf_rows = [r for r in as_rows(load_json(indir / "product_performance.json")) if isinstance(r, dict)]
    if perf_rows:
        row = perf_rows[0]
        data.setdefault("financial_promotions", {})
        data["financial_promotions"]["promotion_amount"] = float(num(row.get("promotion_amount")))
        data["financial_promotions"]["promotion_volume"] = int(num(row.get("promotion_volume")))

    data["anchor_date"] = anchor_iso
    data.setdefault("is_active_deal", False)

    if data.get("last_deal_date"):
        try:
            last_dt = datetime.strptime(str(data["last_deal_date"])[:10], "%Y-%m-%d").date()
            days_diff = max(0, (ref_dt - last_dt).days)
            data["days_since_last_deal"] = days_diff
            data["is_active_deal"] = (days_diff == 0) or bool(active_deal_window)
            data["is_deal_transition"] = (1 <= days_diff <= 10) and not data["is_active_deal"]
            if data["is_active_deal"]:
                data["summary_badge"] = "ACTIVE_DEAL"
                disp_w = active_deal_window or data["last_deal_date"]
                data["summary_desc"] = f"商品当前（{disp_w}）正处于秒杀/大促活动期，严禁因流量激增误砍正常出价"
            elif data["is_deal_transition"]:
                data["summary_badge"] = "DEAL_TRANSITION"
                data["summary_desc"] = f"商品于 {data['last_deal_date']} 结束秒杀/促销（距报表期末 {days_diff} 天），处于促后流量回落过渡窗口"
            else:
                data["summary_badge"] = "RECENT_DEAL_HISTORY"
                data["summary_desc"] = f"近30天含秒杀/促销记录（最近结束于 {data['last_deal_date']}，距期末 {days_diff} 天已回归常态）"
        except Exception:
            pass
    elif data.get("has_deal_history"):
        p_disc = abs(num(data.get("financial_promotions", {}).get("promotion_discount", 0.0)))
        data["summary_badge"] = "COUPON_OR_PROMOTION"
        data["summary_desc"] = f"财务账单显示近期有显著促销折让 (${p_disc:.2f})"
    else:
        data["summary_badge"] = "NONE"
        data["summary_desc"] = "近30天无秒杀/促销活动记录"

    return data


def load_dayparting_analysis(indir: Path) -> tuple[list[dict], list[str]]:
    h_rows = as_rows(load_json(indir / "order_analysis_hourly.json"))
    table_lines = []
    hourly_records = []
    parsed_rows = []
    for r in h_rows:
        k = str(r.get("key"))
        if k == "sub_total" or not k.isdigit():
            continue
        parsed_rows.append({
            "hr": int(k),
            "sp": num(r.get("spend")),
            "ad_ord": int(num(r.get("ad_orders"))),
            "st_ord": int(num(r.get("store_orders"))),
            "cvr_v": num(r.get("cvr")),
            "acos_v": num(r.get("acos")),
            "cpc_v": num(r.get("cpc")),
        })

    # Adaptive baseline benchmarks based on the ASIN's actual hourly distribution
    active_hours = [x for x in parsed_rows if x["sp"] > 0 or x["ad_ord"] > 0]
    if active_hours:
        avg_cvr = sum(x["cvr_v"] for x in active_hours) / len(active_hours)
        valid_acos = [x["acos_v"] for x in active_hours if x["acos_v"] > 0]
        avg_acos = (sum(valid_acos) / len(valid_acos)) if valid_acos else 30.0
        avg_ord = sum(x["ad_ord"] for x in active_hours) / len(active_hours)
    else:
        avg_cvr, avg_acos, avg_ord = 2.5, 30.0, 3.0

    for item in parsed_rows:
        hr = item["hr"]
        sp = item["sp"]
        ad_ord = item["ad_ord"]
        st_ord = item["st_ord"]
        cvr_v = item["cvr_v"]
        acos_v = item["acos_v"]
        cpc_v = item["cpc_v"]

        # 1. Peak hour: High orders or superior CVR with controlled ACOS
        is_peak = (
            (ad_ord >= max(5, avg_ord * 1.3) and acos_v <= max(28.0, avg_acos * 0.95))
            or (cvr_v >= max(2.8, avg_cvr * 1.25) and acos_v <= max(25.0, avg_acos * 0.85) and ad_ord >= 3)
            or (ad_ord >= 8 and acos_v <= max(35.0, avg_acos * 1.1))
        )

        # 2. Bleeding hour: Zero orders with spend, or blown out ACOS, or dismal CVR with high spend
        is_bleeding = (
            (sp >= 10.0 and ad_ord == 0)
            or (acos_v >= max(50.0, avg_acos * 1.4) and sp >= 15.0)
            or (cvr_v <= min(1.2, avg_cvr * 0.5) and sp >= 20.0 and acos_v >= 40.0)
        )

        if is_peak and not is_bleeding:
            classification = "PEAK_GOLDEN_HOUR"
            dayparting_adj = "Boost budget/bid +15%~+25%"
        elif is_bleeding and not is_peak:
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
        elif strat == "autoForSales" or "提高和降低" in btype or "up" in btype.lower():
            bidding_strategy_desc = "动态竞价 - 提高和降低 (Dynamic Bids - Up & Down)"
        elif strat == "legacyForSales" or "仅降低" in btype or "down" in btype.lower():
            bidding_strategy_desc = "动态竞价 - 仅降低 (Dynamic Bids - Down Only)"
        elif strat == "ruleBased" or "规则" in btype:
            bidding_strategy_desc = "基于规则的竞价 (Rule-based Bidding)"
        else:
            bidding_strategy_desc = btype if btype else (str(strat) if strat else "固定竞价 (Fixed Bids)")
        # Dayparting & Rule Strategy Metadata from LingXing ERP
        is_apply_time = r.get("is_apply_time") is True or r.get("is_apply_time") == 1
        timing_base_val = num(r.get("timing_base_value")) if r.get("timing_base_value") is not None else None
        applied_templates = r.get("applied_templates") or []
        opt_rule_desc = str(r.get("optimization_rule_desc") or "").strip()
        is_apply_rule = r.get("is_apply_rule") is True or r.get("is_apply_rule") == 1

        is_up_and_down = (strat == "autoForSales" or "提高和降低" in bidding_strategy_desc or "up & down" in bidding_strategy_desc.lower())
        ord_tot = num(r.get("orders"))
        dir_ord = num(r.get("direct_orders"))
        indir_ord = num(r.get("indirect_orders"))
        indir_share = round((indir_ord / ord_tot * 100.0), 1) if ord_tot > 0 else 0.0

        sp_val = num(r.get("spends"))
        sl_val = num(r.get("sales"))
        out.append({
            "name": r.get("name"),
            "id": r.get("campaign_id"),
            "state": r.get("state"),
            "targeting": r.get("targeting_type"),
            "sponsored": r.get("sponsored_type"),
            "budget": num(r.get("daily_budget") or r.get("budget")),
            "tos": tos, "pp": pp, "ros": ros,
            "bidding_strategy": bidding_strategy_desc,
            "is_up_and_down": is_up_and_down,
            "spend": sp_val,
            "sales": sl_val,
            "acos": clean_api_acos(r.get("acos"), sp_val, sl_val),
            "roas": calc_roas(sp_val, sl_val, r.get("roas")),
            "cpc": num(r.get("cpc")),
            "cvr": num(r.get("cvr")),
            "ctr": num(r.get("ctr")),
            "imp": num(r.get("impressions")),
            "clk": num(r.get("clicks")),
            "ord": ord_tot,
            "direct_ord": dir_ord,
            "indirect_ord": indir_ord,
            "indirect_order_share": indir_share,
            "tos_share": num(r.get("top_of_search_impression_share")),
            "portfolio": r.get("portfolio_name") or r.get("portfolio_id"),
            "portfolio_id": str(r.get("portfolio_id") or "").strip(),
            "portfolio_name": str(r.get("portfolio_name") or "").strip(),
            "is_apply_time": is_apply_time,
            "timing_base_value": timing_base_val,
            "applied_templates": applied_templates,
            "opt_rule_desc": opt_rule_desc,
            "is_apply_rule": is_apply_rule,
        })
    return sorted(out, key=lambda x: -x["spend"])


def build_adgroup_bid_lookups(src: Path, asin_campaigns: set[str] | None = None) -> tuple[dict, dict]:
    """Build unified ad-group default bid lookups so keywords/targets with null bid inherit parent group default_bid."""
    raw_ad_groups = as_rows(load_json(src / "ad_groups.json"))
    group_bid_lookup: dict = {}
    camp_group_bid_lookup: dict[str, float] = {}
    for g in raw_ad_groups:
        cn = g.get("campaign_name") or ""
        if asin_campaigns and cn and cn not in asin_campaigns:
            continue
        gn = g.get("name") or g.get("ad_group_name") or ""
        gid = str(g.get("ad_group_id") or "").strip()
        dbid = num(g.get("default_bid") or g.get("bid"))
        g_state = str(g.get("state") or "enabled").lower().strip()
        if dbid > 0:
            if gid:
                group_bid_lookup[gid] = dbid
            if cn and gn:
                group_bid_lookup[(cn, gn)] = dbid
            if cn:
                if cn not in camp_group_bid_lookup or (g_state == "enabled" and dbid > 0):
                    camp_group_bid_lookup[cn] = max(dbid, camp_group_bid_lookup.get(cn, 0.0)) if g_state != "enabled" else dbid
    return group_bid_lookup, camp_group_bid_lookup


def resolve_effective_entity_bid(
    row: dict,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    fallback: float = 0.35,
) -> tuple[float, bool]:
    """Return (effective_base_bid, is_inherited_from_adgroup)."""
    explicit_bid = num(row.get("bid"))
    if explicit_bid > 0:
        return round(explicit_bid, 2), False
    row_def = num(row.get("default_bid"))
    if row_def > 0:
        return round(row_def, 2), True
    c_name = row.get("campaign_name") or row.get("campaign") or ""
    ag_id = str(row.get("ad_group_id") or "").strip()
    ag_name = row.get("ad_group_name") or ""
    if group_bid_lookup:
        if ag_id and ag_id in group_bid_lookup:
            return round(float(group_bid_lookup[ag_id]), 2), True
        if c_name and ag_name and (c_name, ag_name) in group_bid_lookup:
            return round(float(group_bid_lookup[(c_name, ag_name)]), 2), True
    if camp_group_bid_lookup and c_name in camp_group_bid_lookup:
        return round(float(camp_group_bid_lookup[c_name]), 2), True
    return round(fallback, 2), True


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
        # 语义保护：若为耗材类 pattern，但搜索词包含器具/机器名词，属于研磨对象而非耗材错配！
        if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
            if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", ql, re.I):
                continue
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

    # 4.5 Multi-Purpose Cross-Category Hero Engine (Direction 5 & Rule 64)
    # Automatically identify high-converting cross-category multi-purpose selling points
    multi_purpose_patterns = [
        (r"\b(spice|spices|herb|herbs|seed|seeds|flax|flaxseed|chia|grain|grains|nut|nuts|pepper|peppercorn|masala)\b", "multi_purpose_spice_seed"),
        (r"\b(tea|cold brew|iced coffee|single serve|hot water)\b", "multi_purpose_beverage"),
        (r"\b(smoothie|smoothies|shake|protein|juice|baby food)\b", "multi_purpose_blender"),
        (r"\b(pour over|gooseneck|tea|temperature control|drip)\b", "multi_purpose_kettle"),
    ]
    title_or_desc = (str(profile.get("title") or "") + " " + " ".join(profile.get("features", []))).lower()
    for mp_pat, mp_label in multi_purpose_patterns:
        m_mp = re.search(mp_pat, ql, re.I)
        if m_mp and re.search(mp_pat, title_or_desc, re.I):
            return "core_feature", f"hero_multi_purpose:{m_mp.group(0)}", True

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
    """Map query/keyword to Amazon ABA searchrank and search volume tier (exact match only, never substring!)."""
    t = re.sub(r"\s+", " ", (term or "").lower().strip())
    sr = sr_map.get(t)
    if sr is None and t:
        t_norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()
        if t_norm and t_norm != t:
            sr = sr_map.get(t_norm)

    if sr is None:
        return None, "LONG_TAIL"
    if sr < 5000:
        return sr, "MEGA_HEAD"       # Top 5K 全美超级大词
    if sr < 30000:
        return sr, "CORE_HEAD"       # Top 30K 头部主粮词
    if sr < 150000:
        return sr, "MID_TAIL"        # Top 150K 腰部特征词
    return sr, "LONG_TAIL"           # 长尾细分词


def load_xydc_ranks(src: Path) -> dict[str, dict]:
    """Load keyword organic & sponsored ranks from xydc_ranks.json (西柚洞察)."""
    p = src / "xydc_ranks.json"
    if not p.exists():
        return {}
    data = load_json(p)
    if not data or not isinstance(data, dict):
        return {}
    ranks_map = {}
    for kw, item in data.items():
        if not isinstance(item, dict):
            continue
        kw_clean = str(kw).lower().strip()
        ranks_map[kw_clean] = {
            "search_term": item.get("search_term") or kw_clean,
            "organic_rank": item.get("organic_rank"),
            "organic_page": item.get("organic_page"),
            "sp_rank": item.get("sp_rank"),
            "sp_page": item.get("sp_page"),
            "sb_rank": item.get("sb_rank"),
            "organic_traffic": num(item.get("organic_traffic")),
            "ad_traffic": num(item.get("ad_traffic")),
            "total_traffic": num(item.get("total_traffic")),
            "organic_rate": num(item.get("organic_rate")),
            "ad_rate": num(item.get("ad_rate")),
            "organic_growth": num(item.get("organic_growth")),
            "ad_growth": num(item.get("ad_growth")),
        }
    return ranks_map


def load_xydc_traffic(src: Path) -> dict:
    """Load ASIN/competitor traffic scores, parent 4-channel split, keyword count trends & BSR trends from xydc_traffic.json."""
    p = src / "xydc_traffic.json"
    if not p.exists():
        return {}
    data = load_json(p)
    return data if isinstance(data, dict) else {}


def evaluate_rank_synergy(
    term: str,
    xydc_map: dict[str, dict],
    spend: float = 0.0,
    sales: float = 0.0,
    orders: int = 0,
    clicks: int = 0,
    target_acos: float = 25.0,
    current_bid: float = 0.0,
) -> dict:
    """Evaluate organic rank vs sponsored rank synergy under Rule 38.
    
    Categorizes keyword into 5 strategic tiers and determines exact bidding / TOS actions:
    - Tier 1: 王者双霸屏 vs 防内耗控价 (Organic Rank 1-3)
    - Tier 2: 临界冲刺期 (Flywheel Ascender, Organic Rank 4-15)
    - Tier 3: 腰部攻坚期 (Mid Breakthrough, Organic Rank 16-50)
    - Tier 4: 自然脱节/远距离 (Organic Disconnect, Organic Rank > 50 or unranked)
    - Tier 5: 暂无排名数据 (Unranked)
    """
    t = (term or "").lower().strip()
    info = xydc_map.get(t)
    if not info:
        t_sub = re.sub(r"[^\w\s]", "", t).strip()
        info = xydc_map.get(t_sub)

    if not info:
        return {
            "organic_rank": None,
            "sp_rank": None,
            "sb_rank": None,
            "organic_traffic": 0.0,
            "ad_traffic": 0.0,
            "total_traffic": 0.0,
            "organic_share_pct": 0.0,
            "ad_share_pct": 0.0,
            "organic_rate": 0.0,
            "ad_rate": 0.0,
            "organic_growth": 0.0,
            "ad_growth": 0.0,
            "synergy_tier": "Tier 5: 暂无排名",
            "synergy_tag": "【未建自然权重】",
            "action_type": "",
            "recommended_value": "",
            "reason": "西柚洞察未收录该词自然/广告排名数据，按常规广告绩效调整",
        }

    org_rank = info.get("organic_rank")
    sp_rank = info.get("sp_rank")
    sb_rank = info.get("sb_rank")
    org_traffic = info.get("organic_traffic", 0.0)
    ad_traffic = info.get("ad_traffic", 0.0)
    tot_traffic = info.get("total_traffic", 0.0)
    org_rate = info.get("organic_rate", 0.0)
    ad_rate = info.get("ad_rate", 0.0)
    org_growth = info.get("organic_growth", 0.0)
    ad_growth = info.get("ad_growth", 0.0)
    denom_traffic = tot_traffic if tot_traffic > 0 else (org_traffic + ad_traffic)
    if denom_traffic > 0:
        org_share_pct = round(min(100.0, max(0.0, (org_traffic / denom_traffic) * 100.0)), 1)
        ad_share_pct = round(min(100.0, max(0.0, 100.0 - org_share_pct if abs((org_traffic + ad_traffic) - denom_traffic) <= 1.0 else (ad_traffic / denom_traffic) * 100.0)), 1)
    else:
        org_share_pct = 0.0
        ad_share_pct = 0.0
    actual_acos = acos(spend, sales)

    action_type = ""
    recom = ""
    reason = ""

    # Tier 1: Organic Rank 1 ~ 3
    if org_rank is not None and org_rank <= 3:
        if org_rank == 1:
            # Scenario A: Healthy conversion -> Defensive Dominance
            if orders > 0 and actual_acos <= (target_acos * 1.15):
                tier = "Tier 1: 王者双霸屏"
                tag = "【自然#1·双霸屏稳流】"
                action_type = "Defensive_Dominance_Maintain"
                recom = "保持基础竞价，锁定 TOS 溢价 30%-50% 防竞品夺位"
                reason = f"自然排名高居全网第 1 名，且广告 ACOS {actual_acos:.1f}% 处于健康受控区间。SP 广告与自然位形成双霸屏，封锁首屏 60% 视觉流量。坚决不盲目关停或大降出价，防止竞品顶位分流导致总单量与自然权坠落。"
            # Scenario B: High ACOS or zero-conversion spend -> Cannibalization Throttle
            elif actual_acos > (target_acos * 1.2) or (spend >= 15.0 and orders == 0):
                tier = "Tier 1: 防内耗控价"
                tag = "【自然#1·防内耗控价】"
                action_type = "Throttle_Cannibalization_Bid"
                new_bid = max(0.25, round((current_bid or 0.40) * 0.82, 2))
                recom = f"基础竞价下调 18% 至 ${new_bid:.2f}，TOS 溢价压至 25%-30%"
                reason = f"自然排名已高居第 1 名，但广告 ACOS {actual_acos:.1f}% 严重超标。下调基础竞价避免付费点击侵蚀原本属于自然位的免费订单，释放净利润；启动 3-5 天总订单量熔断监控（总单跌幅≥15%即回提）。"
            else:
                tier = "Tier 1: 头部观察"
                tag = "【自然#1·头部观察】"
                recom = "维持当前竞价与展示，观察增量贡献"
                reason = f"自然排名第 1，当前广告花费较低（${spend:.2f}），维持低成本观察。"
        else: # org_rank in (2, 3)
            if actual_acos <= (target_acos * 1.2):
                tier = "Tier 1: 前三霸屏"
                tag = f"【自然#{org_rank}·保位霸屏】"
                action_type = "Defensive_Dominance_Maintain"
                recom = "维持基础竞价，TOS 溢价 40%-60% 压制竞品"
                reason = f"自然位在第 {org_rank} 位，处于黄金收获期。保持 SP 广告在前 3 位，协同自然位压制竞品，巩固护城河。"
            else:
                tier = "Tier 1: 防内耗控价"
                tag = f"【自然#{org_rank}·微调避耗】"
                action_type = "Throttle_Cannibalization_Bid"
                new_bid = max(0.25, round((current_bid or 0.40) * 0.85, 2))
                recom = f"基础竞价微调 15% 至 ${new_bid:.2f}，降低 TOS 溢价"
                reason = f"自然位第 {org_rank} 位，但广告 ACOS {actual_acos:.1f}% 偏高。微调降价防内耗，依靠自然位主收割。"

    # Tier 2: Organic Rank 4 ~ 15 (Critical Flywheel Ascender)
    elif org_rank is not None and 4 <= org_rank <= 15:
        if (clicks >= 5 and orders >= 1 and actual_acos <= target_acos * 1.35) or (clicks >= 3 and orders >= 1 and actual_acos <= target_acos):
            if sp_rank == 1:
                tier = "Tier 2: 顶展霸位"
                tag = "【广告#1·顶展守位】"
                action_type = "Defensive_Dominance_Maintain"
                new_bid = current_bid or 0.35
                recom = f"广告位已为 SP #1（顶展第一），保持当前基础竞价 ${new_bid:.2f}，锁定 TOS 溢价维持首位"
                reason = f"自然位在第 {org_rank} 位，而广告位已高居全网 SP #1 顶展位！出价已完全统治首屏，严禁继续盲目加价抬高 CPC，保持当前出价压制竞品，通过小时出单速度持续撬动自然位跃升。"
            else:
                tier = "Tier 2: 临界冲刺"
                tag = "【临界冲刺·拉升自然位】"
                action_type = "Boost_Ascender_TOS"
                new_bid = round((current_bid or 0.35) * 1.15, 2)
                recom = f"基础竞价提升 15% 至 ${new_bid:.2f}，加注 TOS 溢价至 60%-75%"
                reason = f"自然位在第 {org_rank} 位，正处于冲入前三的临界突破期！该词转化良好（{orders}单，ACOS {actual_acos:.1f}%），加大 SP 抢占首页顶展，通过密集出单拉升小时出单速度，撬动自然位破壁跃升至 Top 3！"
        elif spend >= 15.0 and orders == 0:
            tier = "Tier 2: 冲刺受阻"
            tag = "【冲刺受阻·控频观察】"
            action_type = "Throttle_TOS_Bid"
            new_bid = max(0.25, round((current_bid or 0.40) * 0.80, 2))
            recom = f"基础竞价下调 20% 至 ${new_bid:.2f}，暂停激进溢价"
            reason = f"自然位在第 {org_rank} 位，但广告近期无转化流血（花费${spend:.2f} 0单）。暂停盲目冲刺，降价控频避损。"
        else:
            tier = "Tier 2: 临界培育"
            tag = "【临界培育·蓄力突破】"
            recom = "保持竞价，紧盯转化与自然排名上升趋势"
            reason = f"自然位第 {org_rank} 位，保持观察，一旦出单提速即可加注冲刺。"

    # Tier 3: Organic Rank 16 ~ 50 (Mid-Page Breakthrough)
    elif org_rank is not None and 16 <= org_rank <= 50:
        if orders >= 2 and actual_acos <= target_acos:
            tier = "Tier 3: 腰部突围"
            tag = "【腰部突围·单向拉升】"
            recom = "建议精准 Exact 隔离提权投放，向首页冲刺"
            reason = f"自然位在第 {org_rank} 位（第2-3页）。转化良好（{orders}单，ACOS {actual_acos:.1f}%），具备冲首页潜质，建议精准隔离推流。"
        elif spend >= 15.0 and orders == 0:
            tier = "Tier 3: 腰部承压"
            tag = "【腰部承压·防守退让】"
            action_type = "Lower_Bid"
            new_bid = max(0.25, round((current_bid or 0.35) * 0.75, 2))
            recom = f"基础竞价下调 25% 至 ${new_bid:.2f}"
            reason = f"自然位第 {org_rank} 位，广告转化不佳，下调竞价退守商品详情页避损。"
        else:
            tier = "Tier 3: 腰部常态"
            tag = "【腰部常态·平价测流】"
            recom = "保持中等出价，不给高额 TOS 溢价"
            reason = f"自然位第 {org_rank} 位，处于常态腰部位置，按标准 ROI 控价。"

    # Tier 4: Organic Rank > 50 or Unranked
    else:
        if sp_rank is not None and sp_rank <= 10:
            tier = "Tier 4: 自然脱节"
            tag = "【自然脱节·警惕虚火】"
            action_type = "Throttle_TOS_Bid" if actual_acos > target_acos else ""
            recom = "严禁盲目高溢价打顶展，降价控频防虚耗"
            reason = f"SP 广告位第 {sp_rank} 位，但自然位严重脱节（第 {org_rank or '>50'} 位）。系统尚未认可 Listing 与该词强相关，过度用广告买顶展会导致极高 CPC 且无法沉淀自然权重。"
        else:
            tier = "Tier 4: 长尾边缘"
            tag = "【长尾深水·按效出价】"
            recom = "严格按 ACOS/CVR 控价，达标则投，超标即降"
            reason = f"自然位第 {org_rank or '>50'} 位，长尾边缘流量，按 ROI 模型精细化调整。"

    return {
        "organic_rank": org_rank,
        "organic_page": info.get("organic_page"),
        "sp_rank": sp_rank,
        "sp_page": info.get("sp_page"),
        "sb_rank": sb_rank,
        "organic_traffic": org_traffic,
        "ad_traffic": ad_traffic,
        "total_traffic": tot_traffic,
        "organic_share_pct": org_share_pct,
        "ad_share_pct": ad_share_pct,
        "organic_rate": org_rate,
        "ad_rate": ad_rate,
        "organic_growth": org_growth,
        "ad_growth": ad_growth,
        "synergy_tier": tier,
        "synergy_tag": tag,
        "action_type": action_type,
        "recommended_value": recom,
        "reason": reason,
    }


def extract_asin_target_info(
    r: dict,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> dict | None:
    """Extract competitor ASIN target metadata from target row with ad-group default_bid inheritance."""
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
    cur_bid = num(r.get("bid"))
    def_bid = num(r.get("default_bid"))
    base_bid, is_inherited = resolve_effective_entity_bid(
        r, group_bid_lookup=group_bid_lookup, camp_group_bid_lookup=camp_group_bid_lookup, fallback=0.40
    )
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
        "base_bid": base_bid,
        "bid": cur_bid,
        "default_bid": def_bid if def_bid > 0 else (base_bid if is_inherited else 0.0),
        "bid_inherited": is_inherited,
    }


def evaluate_competitor_target(comp: dict, our_profile: dict, target_acos: float, is_fragile: bool = False, is_high_refund: bool = False, camp_pp: float = 0.0) -> tuple[str, str, str]:
    """Compare product competitiveness against competitor ASIN and decide action.
    Direction 1 & Rule 60: ASIN Product Targeting Dedicated Bid Floor & Soft Step-down.
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
        # Direction 1 & Rule 60: Dedicated ASIN Targeting Bid Floor ($0.36 standard, $0.42 fragile/high-refund)
        price_similar_or_higher = (comp_price >= our_price * 0.85) if (comp_price > 0 and our_price > 0) else True
        if price_similar_or_higher:
            target_floor = 0.42 if (is_fragile or is_high_refund) else 0.36
            cur_base = num(comp.get("base_bid") or 0.40)

            # If current base bid is already at or below floor, DO NOT lower further (prevent shock)
            if cur_base <= target_floor:
                suggested_bid = cur_base
                return (
                    "Keep_Monitoring",
                    f"${suggested_bid:.2f}",
                    f"我品具备性价比优势（竞品 ${comp_price:.2f} vs 本品 ${our_price:.2f}）。当前基础竞价 ${cur_base:.2f} 已触及商品页安全底价 (${target_floor:.2f})，严禁盲目继续砍出价导致断流；建议保持当前基础竞价，若投产仍超标，优先通过微调活动商品页溢价 (PP) 控费。"
                )

            # Step down relative to CURRENT BASE BID, or reverse-engineer from CPC discounting PP modifier
            # Option 1: 12% gentle step-down from current base bid
            bid_from_base = round(cur_base * 0.88, 2)
            # Option 2: Target CPC discounted by 15% and de-biased by PP modifier: Base = Target_CPC / (1 + PP/100)
            bid_from_cpc = round((cpc * 0.85) / (1.0 + (camp_pp / 100.0)), 2) if (cpc > 0 and camp_pp > 0) else bid_from_base

            # New bid must be strictly less than cur_base, but at least target_floor
            suggested_bid = max(target_floor, min(bid_from_base, bid_from_cpc))
            # Ensure it is strictly lower than cur_base by at least $0.01 if cur_base > target_floor
            if suggested_bid >= cur_base:
                suggested_bid = max(target_floor, round(cur_base - 0.02, 2))

            drop_pct = max(1, int(round((cur_base - suggested_bid) / cur_base * 100)))
            eff_old_str = f" (折合商品页有效出价 ${(cur_base * (1.0 + camp_pp / 100.0)):.2f})" if camp_pp > 0 else ""
            eff_new_str = f"，拉平商品页有效扣费至 ${(suggested_bid * (1.0 + camp_pp / 100.0)):.2f}" if camp_pp > 0 else ""

            return (
                "Lower_Bid",
                f"${suggested_bid:.2f}",
                f"我品具备性价比与功能优势（竞品 ${comp_price:.2f} vs 本品 ${our_price:.2f}，评分 {comp_stars} vs {our_stars}）。当前 ACOS {c_acos:.1f}% 偏高系单次点击成本偏高 (${cpc:.2f})；建议将基础竞价阶梯下调 {drop_pct}%（由 ${cur_base:.2f}{eff_old_str} 降至 ${suggested_bid:.2f}，底价保护 ${target_floor:.2f}）{eff_new_str}，低成本捡漏防守收割。"
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


MIN_KNOWN_NEGATIVE_LEN = 3


def is_known_negative(text: str, known_negatives: set[str]) -> bool:
    """Match a query against the already-negated list.

    Substring matching needs a length floor: without it a 1-2 character entry
    (or a stray blank line in known_negatives.txt) suppresses every recommendation.
    """
    t = (text or "").lower().strip()
    if not t or not known_negatives:
        return False
    for kn in known_negatives:
        if kn == t:
            return True
        if len(kn) >= MIN_KNOWN_NEGATIVE_LEN and kn in t:
            return True
    return False


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
    recom_base_bid = max(0.25, min(recom_base_bid, 1.20))
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
    bleeding_ng_set: set[str] | None = None,
) -> dict:
    """Infer search term placement using multi-dimensional behavioral fingerprinting + bleeding N-gram veto."""
    camp_obj = camp_lookup.get(c_name, {})
    kw_key = f"{c_name}::{match_type.lower().strip()}::{target_text.lower().strip()}"
    kw_obj = kw_lookup.get(kw_key, {})

    ctr = round((clicks / impressions * 100.0) if impressions > 0 else 0.0, 2)
    cpc = round((spend / clicks) if clicks > 0 else 0.0, 2)
    base_bid = float(kw_obj.get("bid") or kw_obj.get("default_bid") or camp_obj.get("default_bid") or 0.60)
    tos_pct = float(camp_obj.get("tos", 0))
    pp_pct = float(camp_obj.get("pp", 0))
    ros_pct = float(camp_obj.get("ros", 0))
    is_up_down = bool(camp_obj.get("is_up_and_down"))
    non_tos_mult = 1.5 if is_up_down else 1.0
    tos_mult = 2.0 if is_up_down else 1.0
    max_non_tos = round(base_bid * (1.0 + max(pp_pct, ros_pct) / 100.0) * non_tos_mult, 2)
    max_tos_bid = round(base_bid * (1.0 + tos_pct / 100.0) * tos_mult, 2)

    # Check if campaign has real placement report evidence showing >=75% spend in a single placement
    real_p = camp_obj.get("real_placements_30d") or {}
    tos_spend_share = num((real_p.get("TOS") or {}).get("spend_share"))
    pp_spend_share = num((real_p.get("PP") or {}).get("spend_share"))

    # Inferred placement
    if base_bid > 0 and cpc > (max_non_tos * 1.02):
        inferred_pos = "CONFIRMED_TOS"
        pos_reason = f"实际CPC (${cpc:.2f}) > 非TOS最高竞价上限 (${max_non_tos:.2f})，数学确定为首页顶部扣费"
    elif tos_spend_share >= 80.0 and ctr >= 0.8:
        inferred_pos = "CONFIRMED_TOS"
        pos_reason = f"活动真实广告位报告显示首页顶部花费占比高达 {tos_spend_share:.1f}%，结合 CTR ({ctr:.2f}%) 确认为顶展流量"
    elif ctr >= 1.5:
        inferred_pos = "PROBABLE_TOS"
        pos_reason = f"高CTR ({ctr:.2f}%) 符合首页顶部黄金展示位特征"
    elif (ctr <= 0.40 and impressions >= 500) or (pp_spend_share >= 80.0 and ctr <= 0.65):
        inferred_pos = "CONFIRMED_PP_CAROUSEL"
        pos_reason = f"极低CTR ({ctr:.2f}%) 与高曝光 ({impressions})，典型商品详情页轮播位/底部位"
    elif ctr <= 0.65:
        inferred_pos = "PROBABLE_PP_OR_ROS"
        pos_reason = f"低CTR ({ctr:.2f}%)，偏向商品页或搜索结果底部位"
    else:
        inferred_pos = "MIXED_PLACEMENT"
        pos_reason = f"中等CTR ({ctr:.2f}%) 与中等CPC (${cpc:.2f})，混合展示位"

    query_ngrams = {ng for ng, _ in extract_ngrams(query)} if query else set()
    matched_bleed_ngs = sorted(query_ngrams & bleeding_ng_set) if bleeding_ng_set else []

    # Actionable diagnosis
    action_type = None
    recommended_val = None
    diagnostic_reason = None
    strategy_tag = "NORMAL"

    if orders >= 1:
        current_acos = acos(spend, sales)
        if current_acos <= target_acos and (orders >= 3 or spend >= 10):
            strategy_tag = "CONVERTING_WINNER"
        elif current_acos > target_acos * 1.15 and spend >= 10.0:
            strategy_tag = "CONVERTING_OVER_ACOS"
            action_type = "Throttle_TOS_Bid"
            recommended_val = "动态降竞价控保本：ACOS超标，依转化率阶梯下调基础竞价"
            diagnostic_reason = (
                f"投放出单词投产超标（{orders}单，花费 ${spend:.2f}，ACOS {current_acos:.1f}% > 目标 {target_acos:.0f}%），"
                f"依实际转化率动态下调基础竞价控保本，坚决不暂停不误杀。"
            )
        else:
            strategy_tag = "CONVERTING_MARGINAL"
    elif clicks >= 8 or spend >= 6:
        if is_asin_term(query):
            strategy_tag = "BLEEDING_ASIN_QUERY"
            action_type = "Negative_Target"
            recommended_val = f"Add Negative Target (asinSameAs={query.upper()})"
            diagnostic_reason = f"竞品详情页 ASIN 流量 ({query.upper()}) 累积 {clicks} 点击 0 订单 (花费 ${spend:.2f})，商品投放否定止血。"
        elif bucket in ("is_not", "competitor_brand") or str(bucket).startswith("is_not"):
            strategy_tag = "HARD_MISFIT"
            action_type = "Negative_Exact"
            recommended_val = "Add Negative Exact"
            diagnostic_reason = (
                f"物理冲突或竞品品牌词 ({bucket})，已累积 {clicks} 点击 0 订单 (花费 ${spend:.2f})，"
                f"无论在任何位置展示均无法形成正向转化，果断精准否定彻底止血。"
            )
        elif matched_bleed_ngs:
            # Rule 56 Veto: Never elevate or harvest a 0-order query containing a confirmed bleeding N-gram root!
            strategy_tag = "BLEEDING_NGRAM_VETO"
            action_type = "Negative_Exact" if (clicks >= 12 or spend >= 9.0) else "Lower_Bid"
            recommended_val = "Add Negative Exact" if action_type == "Negative_Exact" else "Lower Base Bid 20%"
            diagnostic_reason = (
                f"命中高耗零单出血词根 ({', '.join(matched_bleed_ngs)})，累计 {clicks} 点击 0 订单 (花费 ${spend:.2f})，"
                f"触发词根一票否决（严禁误判为受困好词加价提权），执行止血降权/否定。"
            )
        elif (is_strong or bucket in ("core", "feature", "core_feature", "core_or_title") or (v_tier in ("MEGA_HEAD", "CORE_HEAD", "MID_TAIL") and bucket not in ("adjacent", "is_not", "competitor_brand"))):
            if clicks >= 25 or spend >= 20.0:
                strategy_tag = "FAILED_EXTENDED_TEST"
                action_type = "Throttle_TOS_Bid"
                recommended_val = "降竞价防守：大样本高耗未转化，降价控费防守"
                diagnostic_reason = (
                    f"核心相关词已累积大样本耗损 ({clicks} 点击，花费 ${spend:.2f}) 但仍为 0 订单。"
                    f"数据证实该流量在本品无法有效转化，严禁盲目加价提权！应降基础竞价退守防守位控费避损。"
                )
            elif inferred_pos in ("CONFIRMED_PP_CAROUSEL", "PROBABLE_PP_OR_ROS"):
                strategy_tag = "TRAPPED_CORE_HERO"
                action_type = "Elevate_To_TOS"
                recommended_val = "提权拯救：提高基础竞价 15% 或移入专用精准仓测试顶部"
                diagnostic_reason = (
                    f"核心主粮/功能词（ABA梯队: {v_tier}，排名: {sr_display}），当前 CTR 仅 {ctr:.2f}%、CPC ${cpc:.2f}，"
                    f"数据指纹证实点击耗散在低效商品详情页或底部位 ({inferred_pos})，未获首屏顶部（TOS）充分验证。"
                    f"严禁盲目否定，适度提权测试首屏真实转化力。"
                )
            elif inferred_pos in ("CONFIRMED_TOS", "PROBABLE_TOS"):
                strategy_tag = "FAILED_AT_TOS"
                action_type = "Throttle_TOS_Bid"
                recommended_val = "降竞价防守：基础竞价下调 15-20% 并排查 Listing 首屏转化壁垒"
                diagnostic_reason = (
                    f"核心主粮词在首页顶部黄金位已积累 {clicks} 次高意图点击 (CTR: {ctr:.2f}%, CPC: ${cpc:.2f}) 依然未出单。"
                    f"已享受顶展红利但未转化，严禁继续加价或推高 TOS；应阶梯下调基础竞价退守防守位，并排查主图/价格/评论竞争力。"
                )
            else:
                strategy_tag = "MIXED_CORE_TEST"
                action_type = "Refine_Bid_Placement"
                recommended_val = "控价观察：维持或移入精准仓控价观察"
                diagnostic_reason = (
                    f"核心功能词混杂在宽泛活动中耗损 (${spend:.2f}, {clicks} 点击)，"
                    f"需结合自然排名与出单表现控价观察。"
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
                recommended_val = "Lower Base Bid 20%"
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
    xydc_map: dict = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Audit existing active negatives cross-referencing portfolio targeting."""
    audit_records = []
    unnegative_adjustments = []

    harvest_camp = find_exact_harvest_campaign(camps or [], kws or [])
    camp_lookup = {c.get("name") or c.get("campaign_name"): c for c in (camps or [])}

    # Build portfolio-wide keyword targeting lookup with inherited ad-group default bids
    targeted_map = defaultdict(list)
    if kws:
        for k in kws:
            txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
            if not txt:
                continue
            eff_bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
            targeted_map[txt].append({
                "campaign": k.get("campaign_name") or "",
                "match": k.get("match_type") or "exact",
                "state": (k.get("state") or "").lower(),
                "bid": eff_bid,
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

        # 2. Own ASIN or Competitor ASIN negative defense
        elif is_own_asin or is_asin_term(q):
            audit_status = "SAFE_ASIN_DEFENSE"
            recom_action = "Keep_Negated"
            recom_val = "Maintain Negative (ASIN Target Isolation)"
            detail_reason = "ASIN negated in Auto/Broad to prevent self-cannibalization or isolate ASIN product targeting."

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

            # Case B: Migrated to Exact, but currently PAUSED (Traffic Blackout vs Intentional Misfit Pause!)
            elif paused_other:
                misfit_in_paused = False
                for pat_item in rules.get("is_not_patterns", []):
                    pat = pat_item if isinstance(pat_item, str) else pat_item.get("pattern", "")
                    label = "is_not" if isinstance(pat_item, str) else pat_item.get("label", "is_not")
                    if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
                        if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", q, re.I):
                            continue
                    if pat and re.search(pat, q, re.I):
                        misfit_in_paused = True
                        break

                if misfit_in_paused or is_hard_misfit:
                    audit_status = "INTENTIONAL_PAUSE_CONFLICT"
                    recom_action = "Keep_Paused"
                    recom_val = f"Maintain Paused in {paused_other[0]['campaign']} and Negated in {c_name} (Physical Misfit)"
                    detail_reason = (
                        f"确凿物理属性冲突（{match_reason}）：该词此前因属性矛盾被合规暂停/否定，"
                        f"绝非移词断流误杀！严禁建议 Unpause 重新激活，彻底规避买家差评与高退货。"
                    )
                elif is_strong_relevance or orders >= 1 or (sr is not None and sr < 30000):
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
                        eval_q = evaluate_rank_synergy(term=q, xydc_map=xydc_map or {})
                        q_org_r = eval_q.get("organic_rank")
                        q_sp_r = eval_q.get("sp_rank")
                        if q_org_r is not None or q_sp_r is not None:
                            rank_tag_q = f"[自然位#{q_org_r or '--'}·广告位#{q_sp_r or '--'} {eval_q.get('synergy_tag', '')}] "
                            rank_cur_q = f"(自然位#{q_org_r or '--'}, 广告位#{q_sp_r or '--'}) "
                        else:
                            rank_tag_q = "[西柚未建位/暂无排名] "
                            rank_cur_q = "(西柚未建位) "

                        unnegative_adjustments.append({
                            "action_type": "Unpause_Keyword",
                            "campaign": dest_camp,
                            "target_entity": q,
                            "match_type": dest_match,
                            "current_value": f"{rank_cur_q}state=paused",
                            "recommended_value": f"state=enabled (基础竞价 ${recom_bid:.2f})",
                            "reason": f"{rank_tag_q}移词断流恢复：在 {c_name} 保持否定防内耗，在 {dest_camp} 开启投放。{bid_calc_desc}",
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
                    label = "is_not" if isinstance(pat_item, str) else pat_item.get("label", "is_not")
                    if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
                        if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", q, re.I):
                            continue
                    if pat and re.search(pat, q, re.I):
                        misfit_found = True
                        break

                if misfit_found:
                    audit_status = "SAFE_HARD_NEGATIVE"
                    recom_action = "Keep_Negated"
                    recom_val = "Maintain Negative (Permanent Misfit)"
                    detail_reason = "Confirmed physical/category conflict via rules. Hard negative eliminates structural waste/bad reviews."
                else:
                    generic_conflict_patterns = [
                        r"\bno\s+plastic\b", r"\bplastic\s+free\b", r"\ball\s+glass\b", r"\ball\s+metal\b",
                        r"\b5\s*cup\b", r"\b12\s*cup\b", r"\bpercolator\b", r"\bespresso\b", r"\bk\s*cup\b",
                        r"\bcapsule\b", r"\bpod\b", r"\bmanual\b", r"\bcommercial\b",
                        r"\bvitamix\b", r"\bblendtec\b", r"\bbreville\b", r"\bcuisinart\b",
                    ]
                    has_generic_conflict = any(re.search(pat, q, re.I) for pat in generic_conflict_patterns)

                    # Also check if already targeted as enabled exact inside c_name itself
                    already_enabled_here = any(t["state"] == "enabled" and t["match"] == "exact" for t in targets)

                    is_core_asset = (
                        not already_enabled_here
                        and (bucket == "brand" or is_strong_relevance or bucket in ("core_feature", "core_or_title"))
                        and not is_hard_misfit
                        and not has_generic_conflict
                        and (orders >= 2 or (orders >= 1 and is_strong_relevance and bucket not in ("competitor_brand", "adjacent")))
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
                        eval_q = evaluate_rank_synergy(term=q, xydc_map=xydc_map or {})
                        q_org_r = eval_q.get("organic_rank")
                        q_sp_r = eval_q.get("sp_rank")
                        if q_org_r is not None or q_sp_r is not None:
                            rank_tag_q = f"[自然位#{q_org_r or '--'}·广告位#{q_sp_r or '--'} {eval_q.get('synergy_tag', '')}] "
                            rank_cur_q = f"(自然位#{q_org_r or '--'}, 广告位#{q_sp_r or '--'}) "
                        else:
                            rank_tag_q = "[西柚未建位/暂无排名] "
                            rank_cur_q = "(西柚未建位) "

                        unnegative_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": harvest_camp,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_value": f"{rank_cur_q}Negated in {c_name}, untargeted in portfolio",
                            "recommended_value": f"Add Exact Target (基础竞价 ${recom_bid:.2f})",
                            "reason": f"{rank_tag_q}孤儿核心资产收割：在 {c_name} 保持否定防内耗，移入 {harvest_camp} 独立收割。{bid_calc_desc}",
                        })
                    else:
                        audit_status = "SOFT_MONITOR"
                        recom_action = "Keep_Negated_Offpeak"
                        recom_val = "Maintain Negative; Re-test in Q4 Holiday Season"
                        detail_reason = (
                            f"Adjacent/peripheral query with low off-peak momentum "
                            f"(spend=${spend:.2f}, ord={orders}); untargeted across the portfolio. "
                            f"Keep negated during routine period."
                        )

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


def audit_keyword_misfits(
    kws: list[dict],
    rules: dict,
    camps: list[dict] = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> list[dict]:
    """Audit enabled keywords in enabled campaigns to detect physical misfits targeting wrong attributes."""
    adjustments = []
    camp_lookup = {c.get("name") or c.get("campaign_name"): c for c in (camps or [])}
    is_not_patterns = rules.get("is_not_patterns", [])
    is_patterns = rules.get("is_patterns", [])
    for k in kws:
        if k.get("state") != "enabled":
            continue
        c_name = k.get("campaign_name") or ""
        if camp_lookup and camp_lookup.get(c_name, {}).get("state") != "enabled":
            continue
        text = str(k.get("keyword_text") or k.get("targeting_text") or "").strip()
        bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        orders = int(num(k.get("orders")))
        spend = num(k.get("spends"))
        sales = num(k.get("sales"))
        kw_acos = (spend / sales * 100) if sales > 0 else 999.0

        # 1. 核心正向投放词绝对保护：若匹配正向 is_patterns，绝非物理属性错配！
        is_core_match = False
        for is_item in is_patterns:
            is_pat = is_item if isinstance(is_item, str) else is_item.get("pattern", "")
            if is_pat and re.search(is_pat, text, re.I):
                is_core_match = True
                break
        if is_core_match:
            continue

        # 2. 出单词防误杀最高豁免权（Rule 31）：凡历史累计出单 (orders >= 1)，
        # 证明已有真实买家认可并下单，绝非物理属性相悖！若 ACOS 偏高应走保本降竞价优化流程，严禁直接暂停！
        if orders >= 1:
            continue

        for pat_item in is_not_patterns:
            pat = pat_item if isinstance(pat_item, str) else pat_item.get("pattern", "")
            label = "is_not" if isinstance(pat_item, str) else pat_item.get("label", "is_not")

            # 语义隔离：若为耗材类 pattern，但投放词包含器具名词，属于研磨对象而非耗材错配！
            if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
                if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", text, re.I):
                    continue

            if pat and re.search(pat, text, re.I):
                adjustments.append({
                    "action_type": "Pause_Keyword",
                    "campaign": c_name,
                    "target_entity": text,
                    "match_type": k.get("match_type") or "exact",
                    "current_bid_num": bid,
                    "current_value": f"基础竞价=${bid:.2f}, state=enabled, label={label}",
                    "recommended_value": "state=paused",
                    "reason": f"物理属性硬冲突 ({label}, pattern: {pat})：投放词与本品核心物理属性相悖（0订单已验证），易引流错配买家并推高退款率，建议暂停。",
                })
                break
    return adjustments


# ==============================================================================
# LOCAL DATABASE & PERSISTENT LEDGER MODULE
# ==============================================================================

def resolve_db_dirs(user_db_dir: str | None, src: Path) -> tuple[Path, Path, Path, Path]:
    """Resolve database root, daily_records, ledger, snapshots, expectations, and entity_memory directories."""
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
    expectations_dir = root / "expectations"
    entity_memory_dir = root / "entity_memory"
    for d in (daily_records_dir, ledger_dir, snapshots_dir, expectations_dir, entity_memory_dir):
        d.mkdir(parents=True, exist_ok=True)
    return root, daily_records_dir, ledger_dir, snapshots_dir


def get_snapshot_key(meta: dict, profile: dict, camps: list[dict] | None = None, shop_alias: str | None = None) -> str:
    """Derive unique snapshot filename key from portfolio or ASIN, supporting multi-shop isolation (Direction 5)."""
    shop_str = re.sub(r"[^a-zA-Z0-9_-]", "_", str(shop_alias or meta.get("shop_alias") or meta.get("shop_name") or profile.get("shop_name") or "").strip())
    
    port_id = str(meta.get("portfolio_id") or "").strip()
    if port_id and port_id not in ("None", "0", ""):
        return f"{shop_str}_portfolio_{port_id}" if shop_str else f"portfolio_{port_id}"
    port_name = str(meta.get("portfolio_name") or "").strip()
    if port_name and port_name not in ("None", ""):
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", port_name)
        return f"{shop_str}_portfolio_{clean_name}" if shop_str else f"portfolio_{clean_name}"

    # Check if campaigns share a common portfolio_id
    if camps:
        pf_ids = [str(c.get("portfolio_id")) for c in camps if c.get("portfolio_id") and str(c.get("portfolio_id")) not in ("None", "0", "")]
        if pf_ids:
            top_pf = Counter(pf_ids).most_common(1)[0][0]
            return f"{shop_str}_portfolio_{top_pf}" if shop_str else f"portfolio_{top_pf}"

    asin = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper().strip()
    country = str(meta.get("country") or "US").upper().strip()
    return f"{shop_str}_{asin}_{country}" if shop_str else f"{asin}_{country}"


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


# Control variables we snapshot and diff between runs. Each entry says how to read the
# value off a current row, how to render it in the ledger, and what entity_type to log.
# Adding a tracked field is one row here -- the diff, cooldown and ledger wiring is shared.
CAMPAIGN_FIELDS = [
    ("tos", "campaign_tos", lambda v: f"{v}%"),
    ("pp", "campaign_pp", lambda v: f"{v}%"),
    ("ros", "campaign_ros", lambda v: f"{v}%"),
    ("budget", "campaign_budget", lambda v: f"${num(v):.2f}"),
    ("state", "campaign_state", str),
]

# Bid moves below this are rounding noise, not a deliberate adjustment.
BID_EPSILON = 0.02
BUDGET_EPSILON = 0.01


def _campaign_baseline(camp: dict | None) -> dict:
    """Recent-slice metrics captured at adjustment time, so later runs can attribute the effect."""
    camp = camp or {}
    raw_ac = num(camp.get("acos_7d") or camp.get("acos"))
    return {
        "baseline_cpc": round(num(camp.get("cpc_3d") or camp.get("cpc_7d") or camp.get("cpc")), 2),
        "baseline_daily_imp": round(num(camp.get("daily_imp_3d") or camp.get("daily_imp_30d")), 1),
        "baseline_acos": 0.0 if raw_ac >= 5000.0 else round(raw_ac, 1),
    }


def _entity_baseline(ent: dict | None, camp: dict | None = None) -> dict:
    """Capture recent-slice metrics for a specific keyword or target entity.
    Falls back to campaign metrics if entity metrics are not yet accumulated.
    """
    ent = ent or {}
    camp = camp or {}

    # Entity CPC: prefer 3d -> 7d -> 30d -> camp
    e_cpc = ent.get("cpc_3d") or ent.get("cpc_7d") or ent.get("cpc")
    if not e_cpc or num(e_cpc) <= 0:
        e_cpc = camp.get("cpc_3d") or camp.get("cpc_7d") or camp.get("cpc") or 0.0

    # Entity daily impressions
    e_imp_3d = num(ent.get("imp_3d"))
    e_imp_7d = num(ent.get("imp_7d"))
    e_imp_30d = num(ent.get("imp_30d") or ent.get("impressions"))

    if e_imp_3d > 0:
        e_daily_imp = e_imp_3d / 3.0
    elif e_imp_7d > 0:
        e_daily_imp = e_imp_7d / 7.0
    elif e_imp_30d > 0:
        e_daily_imp = e_imp_30d / 30.0
    else:
        e_daily_imp = num(camp.get("daily_imp_3d") or camp.get("daily_imp_30d"))

    e_acos = num(ent.get("acos_7d") or ent.get("acos") or camp.get("acos") or 0.0)
    if e_acos >= 5000.0:
        e_acos = 0.0

    return {
        "baseline_cpc": round(num(e_cpc), 2),
        "baseline_daily_imp": round(num(e_daily_imp), 1),
        "baseline_acos": round(num(e_acos), 1),
    }


def _changed(field: str, old_val, new_val) -> bool:
    if field == "budget":
        return abs(num(new_val) - num(old_val)) >= BUDGET_EPSILON
    if field == "bid":
        return abs(num(new_val) - num(old_val)) >= BID_EPSILON and num(new_val) > 0 and num(old_val) > 0
    return new_val != old_val


def _record_change(
    *,
    now: datetime,
    cooldown_days: int,
    campaign: str,
    entity_type: str,
    entity_name: str,
    entity_key: str | None,
    field: str,
    old_val,
    new_val,
    render,
    msg: str,
    camp_lookup: dict,
    diff_events: list[str],
    new_diff_items: list[dict],
    active_cooldowns: list[dict],
    locked_campaigns: set[str],
    entity_baseline_dict: dict | None = None,
) -> None:
    """Log one control-variable change: diff line, cooldown lock, and ledger seed row."""
    now_iso = now.isoformat()
    diff_events.append(f"Campaign '{campaign}': {msg}")

    cooldown = {
        "entity_type": entity_type,
        "campaign": campaign,
        "field": field,
        "old_value": old_val,
        "new_value": new_val,
        "changed_at": now_iso,
        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
        "days_elapsed": 0,
        "days_remaining": cooldown_days,
        "detail": msg,
    }
    if entity_key:
        cooldown["entity_key"] = entity_key
    active_cooldowns.append(cooldown)
    locked_campaigns.add(campaign)

    base_metrics = entity_baseline_dict or _campaign_baseline(camp_lookup.get(campaign))

    new_diff_items.append({
        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items) + 1:02d}",
        "change_date": now.strftime("%Y-%m-%d"),
        "changed_at": now_iso,
        "campaign": campaign,
        "entity_type": entity_type,
        "entity_name": entity_name,
        "entity_key": entity_key or "",
        "field": field,
        "old_val": render(old_val),
        "new_val": render(new_val),
        **base_metrics,
        "detail": msg,
    })


def _snapshot_campaigns(camps: list[dict]) -> dict[str, dict]:
    out = {}
    for c in camps:
        name = c.get("name")
        if not name:
            continue
        out[name] = {
            "name": name,
            "tos": int(c.get("tos", 0)),
            "pp": int(c.get("pp", 0)),
            "ros": int(c.get("ros", 0)),
            "budget": float(c.get("budget", 0.0)),
            "state": str(c.get("state", "")),
            "cpc": num(c.get("cpc")),
            "cpc_3d": num(c.get("cpc_3d")),
            "cpc_7d": num(c.get("cpc_7d")),
            "daily_imp_3d": num(c.get("daily_imp_3d")),
            "daily_imp_30d": num(c.get("daily_imp_30d")),
        }
    return out


def _snapshot_keywords(
    kws: list[dict],
    kws_3d: list[dict] | None = None,
    kws_7d: list[dict] | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> dict[str, dict]:
    k3_map = {}
    for k in (kws_3d or []):
        c = k.get("campaign_name") or ""
        m = (k.get("match_type") or "").lower().strip()
        t = (k.get("keyword_text") or "").lower().strip()
        if t:
            k3_map[(c, m, t)] = k

    k7_map = {}
    for k in (kws_7d or []):
        c = k.get("campaign_name") or ""
        m = (k.get("match_type") or "").lower().strip()
        t = (k.get("keyword_text") or "").lower().strip()
        if t:
            k7_map[(c, m, t)] = k

    out = {}
    for k in kws:
        text = k.get("keyword_text") or ""
        if not text:
            continue
        c_name = k.get("campaign_name") or ""
        match = (k.get("match_type") or "").lower().strip()
        clean_t = text.lower().strip()
        k3 = k3_map.get((c_name, match, clean_t), {})
        k7 = k7_map.get((c_name, match, clean_t), {})

        eff_bid, is_inh = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        latest_explicit = num(k3.get("bid") or k7.get("bid"))
        if latest_explicit > 0:
            eff_bid = round(latest_explicit, 2)
            is_inh = False

        cpc_v = num(k3.get("cpc") or k7.get("cpc") or k.get("cpc"))
        imp_3d = num(k3.get("impressions") or k3.get("imp"))
        imp_7d = num(k7.get("impressions") or k7.get("imp"))
        imp_30d = num(k.get("impressions") or k.get("imp"))
        ord_3d = int(num(k3.get("orders")))
        ord_7d = int(num(k7.get("orders")))
        ord_30d = int(num(k.get("orders")))

        out[f"{c_name}::{match}::{clean_t}"] = {
            "campaign": c_name,
            "text": text,
            "match": match,
            "bid": eff_bid,
            "bid_inherited": is_inh,
            "state": str(k3.get("state") or k7.get("state") or k.get("state") or ""),
            "cpc": cpc_v,
            "cpc_3d": num(k3.get("cpc")),
            "cpc_7d": num(k7.get("cpc")),
            "imp_3d": imp_3d,
            "imp_7d": imp_7d,
            "imp_30d": imp_30d,
            "ord_3d": ord_3d,
            "ord_7d": ord_7d,
            "ord_30d": ord_30d,
            "acos": clean_api_acos(k.get("acos"), num(k.get("spends") or k.get("spend") or k.get("cost")), num(k.get("sales"))),
            "acos_7d": clean_api_acos(k7.get("acos"), num(k7.get("spends") or k7.get("spend") or k7.get("cost")), num(k7.get("sales"))),
        }
    return out


def _snapshot_targets(
    targets: list[dict],
    targets_3d: list[dict] | None = None,
    targets_7d: list[dict] | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> dict[str, dict]:
    t3_map = {}
    for t in (targets_3d or []):
        c = t.get("campaign_name") or ""
        tp = (t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or "").lower().strip()
        txt = (t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or "").lower().strip()
        if txt:
            t3_map[(c, tp, txt)] = t

    t7_map = {}
    for t in (targets_7d or []):
        c = t.get("campaign_name") or ""
        tp = (t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or "").lower().strip()
        txt = (t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or "").lower().strip()
        if txt:
            t7_map[(c, tp, txt)] = t

    out = {}
    for t in targets:
        text = t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or ""
        if not text:
            continue
        c_name = t.get("campaign_name") or ""
        ttype = t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or ""
        clean_txt = str(text).lower().strip()
        t3 = t3_map.get((c_name, ttype.lower().strip(), clean_txt), {})
        t7 = t7_map.get((c_name, ttype.lower().strip(), clean_txt), {})

        eff_bid, is_inh = resolve_effective_entity_bid(t, group_bid_lookup, camp_group_bid_lookup, fallback=0.40)
        latest_explicit = num(t3.get("bid") or t7.get("bid"))
        if latest_explicit > 0:
            eff_bid = round(latest_explicit, 2)
            is_inh = False

        cpc_v = num(t3.get("cpc") or t7.get("cpc") or t.get("cpc"))
        imp_3d = num(t3.get("impressions") or t3.get("imp"))
        imp_7d = num(t7.get("impressions") or t7.get("imp"))
        imp_30d = num(t.get("impressions") or t.get("imp"))
        ord_3d = int(num(t3.get("orders")))
        ord_7d = int(num(t7.get("orders")))
        ord_30d = int(num(t.get("orders")))

        out[f"{c_name}::{ttype}::{clean_txt}"] = {
            "campaign": c_name,
            "text": str(text),
            "type": ttype,
            "bid": eff_bid,
            "bid_inherited": is_inh,
            "state": str(t3.get("state") or t7.get("state") or t.get("state") or ""),
            "cpc": cpc_v,
            "cpc_3d": num(t3.get("cpc")),
            "cpc_7d": num(t7.get("cpc")),
            "imp_3d": imp_3d,
            "imp_7d": imp_7d,
            "imp_30d": imp_30d,
            "ord_3d": ord_3d,
            "ord_7d": ord_7d,
            "ord_30d": ord_30d,
            "acos": clean_api_acos(t.get("acos"), num(t.get("spends") or t.get("spend") or t.get("cost")), num(t.get("sales"))),
            "acos_7d": clean_api_acos(t7.get("acos"), num(t7.get("spends") or t7.get("spend") or t7.get("cost")), num(t7.get("sales"))),
        }
    return out


def _carry_forward_cooldowns(prev_cooldowns: list[dict], now: datetime) -> tuple[list[dict], set[str]]:
    """Keep cooldowns that have not expired yet and refresh their elapsed/remaining counters."""
    active: list[dict] = []
    locked: set[str] = set()
    now_iso = now.isoformat()
    for item in prev_cooldowns:
        until_str = item.get("cooldown_until")
        if not until_str:
            continue
        try:
            until = datetime.fromisoformat(until_str)
        except Exception:
            continue
        if now >= until:
            continue
        try:
            changed_at = datetime.fromisoformat(item.get("changed_at", now_iso))
        except Exception:
            changed_at = now
        item["days_elapsed"] = max(0, (now - changed_at).days)
        item["days_remaining"] = max(0, (until - now).days)
        active.append(item)
        if item.get("campaign"):
            locked.add(item["campaign"])
    return active, locked


def diff_and_track_cooldown(
    prev_snap: dict,
    curr_camps: list[dict],
    curr_kws: list[dict],
    curr_targets: list[dict],
    curr_kws_3d: list[dict] | None = None,
    curr_kws_7d: list[dict] | None = None,
    curr_targets_3d: list[dict] | None = None,
    curr_targets_7d: list[dict] | None = None,
    cooldown_days: int = 7,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> tuple[dict, list[dict], set[str], list[str], list[dict]]:
    """Compare current control variables vs previous snapshot."""
    now = datetime.now()

    curr_camp_map = _snapshot_campaigns(curr_camps)
    curr_kw_map = _snapshot_keywords(curr_kws, curr_kws_3d, curr_kws_7d, group_bid_lookup, camp_group_bid_lookup)
    curr_target_map = _snapshot_targets(curr_targets, curr_targets_3d, curr_targets_7d, group_bid_lookup, camp_group_bid_lookup)

    camp_lookup = {c.get("name"): c for c in curr_camps if c.get("name")}

    prev_camps = prev_snap.get("campaigns") or {}
    prev_kws = prev_snap.get("keywords") or {}
    prev_targets = prev_snap.get("targets") or {}

    active_cooldowns, locked_campaigns = _carry_forward_cooldowns(prev_snap.get("active_cooldowns") or [], now)
    diff_events: list[str] = []
    new_diff_items: list[dict] = []

    def log(**kw):
        _record_change(
            now=now, cooldown_days=cooldown_days, camp_lookup=camp_lookup,
            diff_events=diff_events, new_diff_items=new_diff_items,
            active_cooldowns=active_cooldowns, locked_campaigns=locked_campaigns, **kw
        )

    # 1. Campaign placement modifiers, budget and state
    if prev_camps:
        for name, curr in curr_camp_map.items():
            prev = prev_camps.get(name)
            if prev is None:
                continue
            for field, entity_type, render in CAMPAIGN_FIELDS:
                old_val, new_val = prev.get(field), curr[field]
                if old_val is None or not _changed(field, old_val, new_val):
                    continue
                label = "State" if field == "state" else field.upper()
                log(
                    campaign=name, entity_type=entity_type, entity_name=name, entity_key=None,
                    field=field, old_val=old_val, new_val=new_val, render=render,
                    msg=f"{label} changed: {render(old_val)} -> {render(new_val)}",
                    entity_baseline_dict=_campaign_baseline(camp_lookup.get(name)),
                )

    # 2. Keyword and product-target bids (Direction 3: Entity-Level Precision Baseline)
    for prev_map, curr_map, entity_type, label_key in (
        (prev_kws, curr_kw_map, "keyword_bid", "match"),
        (prev_targets, curr_target_map, "target_bid", "type"),
    ):
        if not prev_map:
            continue
        noun = "Keyword" if entity_type == "keyword_bid" else "Target"
        for key, curr in curr_map.items():
            prev = prev_map.get(key)
            if prev is None or not _changed("bid", prev.get("bid", 0.0), curr["bid"]):
                continue
            entity_name = f"{curr['text']} [{curr[label_key]}]"
            ent_base = _entity_baseline(curr, camp_lookup.get(curr["campaign"]))
            log(
                campaign=curr["campaign"], entity_type=entity_type, entity_name=entity_name,
                entity_key=key, field="bid", old_val=prev.get("bid"), new_val=curr["bid"],
                render=lambda v: f"${num(v):.2f}",
                msg=f"{noun} '{curr['text']}' [{curr[label_key]}] bid changed: "
                    f"${num(prev.get('bid')):.2f} -> ${curr['bid']:.2f}",
                entity_baseline_dict=ent_base,
            )

    new_snap = {
        "updated_at": now.isoformat(),
        "campaigns": curr_camp_map,
        "keywords": curr_kw_map,
        "targets": curr_target_map,
        "active_cooldowns": active_cooldowns,
    }
    return new_snap, active_cooldowns, locked_campaigns, diff_events, new_diff_items


def _parse_legacy_entity_name(e_name: str, campaign: str = "") -> tuple[str, str]:
    """Parse legacy ledger entity_name ('text [match]', 'camp::match::text', 'Keyword: text [match]', or "camp [match] 'text'")
    into exact (text_lower, match_lower).
    """
    raw = (e_name or "").strip()
    if "::" in raw:
        parts = raw.split("::", 2)
        if len(parts) == 3:
            return parts[2].lower().strip(), parts[1].lower().strip()
    m_quoted = re.match(r"^.*?\[([^\]]+)\]\s*['\"]([^'\"]+)['\"]\s*$", raw)
    if m_quoted:
        return m_quoted.group(2).lower().strip(), m_quoted.group(1).lower().strip()
    cleaned = re.sub(r"^(?:keyword|target)\s*:\s*", "", raw, flags=re.I).strip()
    m = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", cleaned)
    if m:
        return m.group(1).lower().strip(), m.group(2).lower().strip()
    return cleaned.lower().strip(), ""


def load_and_sync_ledger(
    ledger_file: Path,
    prev_snapshot: dict,
    new_diff_items: list[dict],
    curr_camps: list[dict],
    curr_kw_map: dict | None = None,
    curr_target_map: dict | None = None,
    cooldown_days: int = 7,
    xydc_map: dict | None = None,
    deal_info: dict | None = None,
    profile: dict | None = None,
    financial_prof: dict | None = None,
    traffic_prof: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Load persistent ledger, seed from snapshot cooldowns if new, append new diffs,
    and dynamically attribute post-adjustment performance with exact entity matching (no substring collision!).
    """
    now = datetime.now()
    camp_lookup = {c.get("name"): c for c in curr_camps if c.get("name")}
    ledger = []
    if ledger_file.exists():
        try:
            ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
            if not isinstance(ledger, list):
                ledger = []
        except Exception:
            ledger = []

    is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
    fulfillable_cnt = num(profile.get("fulfillable")) if profile and profile.get("fulfillable") is not None else None
    dos_cnt = num(profile.get("days_of_supply")) if profile and profile.get("days_of_supply") is not None else None
    is_stockout = bool(profile and ((fulfillable_cnt is not None and fulfillable_cnt == 0) or (dos_cnt is not None and dos_cnt <= 0 and fulfillable_cnt is not None and fulfillable_cnt <= 2)))
    is_low_inventory = bool(profile and not is_stockout and ((dos_cnt is not None and 0 < dos_cnt <= 7) or (fulfillable_cnt is not None and 0 < fulfillable_cnt <= 25)))

    # If ledger is empty, seed from prev_snapshot.active_cooldowns using entity-specific baseline where possible
    if not ledger and prev_snapshot.get("active_cooldowns"):
        for idx, item in enumerate(prev_snapshot["active_cooldowns"]):
            c_name = item.get("campaign") or ""
            ekey = item.get("entity_key") or ""
            ent_src = (curr_kw_map or {}).get(ekey) or (curr_target_map or {}).get(ekey)
            baseline = _entity_baseline(ent_src, camp_lookup.get(c_name)) if ent_src else _campaign_baseline(camp_lookup.get(c_name))
            base_cpc = baseline["baseline_cpc"]
            base_imp = baseline["baseline_daily_imp"]
            base_acos = baseline["baseline_acos"]
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
                "entity_name": ekey or c_name,
                "entity_key": ekey,
                "field": field,
                "old_val": old_v,
                "new_val": new_v,
                "baseline_cpc": base_cpc,
                "baseline_daily_imp": base_imp,
                "baseline_acos": base_acos,
                "detail": item.get("detail") or "",
            })

    # Append new diff items (include entity_key/entity_name in dedup key so two keywords changing $0.50->$0.40 on same day both persist!)
    def _ledger_dedup_key(x: dict) -> str:
        ent_id = x.get("entity_key") or x.get("entity_name") or ""
        return f"{x.get('campaign')}::{ent_id}::{x.get('field')}::{x.get('change_date')}::{x.get('old_val')}->{x.get('new_val')}"

    # Merge any sibling ledger files for the same portfolio_id (e.g. legacy unprefixed or case-variant shop alias)
    m_pf = re.search(r"(portfolio_[A-Za-z0-9_-]+)_ledger\.json$", ledger_file.name)
    if m_pf and ledger_file.parent.exists():
        pf_suffix = f"{m_pf.group(1)}_ledger.json"
        existing_keys_init = {_ledger_dedup_key(x) for x in ledger if isinstance(x, dict)}
        for sib in sorted(ledger_file.parent.glob(f"*{pf_suffix}")):
            if sib.resolve() == ledger_file.resolve():
                continue
            try:
                sib_rows = json.loads(sib.read_text(encoding="utf-8"))
                if isinstance(sib_rows, list):
                    for sr in sib_rows:
                        if isinstance(sr, dict):
                            k_sr = _ledger_dedup_key(sr)
                            if k_sr not in existing_keys_init:
                                ledger.append(sr)
                                existing_keys_init.add(k_sr)
            except Exception:
                pass

    existing_keys = {_ledger_dedup_key(x) for x in ledger}
    for item in new_diff_items:
        key = _ledger_dedup_key(item)
        if key not in existing_keys:
            ledger.append(item)
            existing_keys.add(key)

    # Sort chronologically by changed_at/change_date so latest adjustment per (campaign, entity, field) is deterministic
    ledger.sort(key=lambda x: (str(x.get("change_date") or ""), str(x.get("changed_at") or ""), str(x.get("id") or "")))

    def _norm_ledger_entity_slot(x: dict) -> tuple[str, str, str]:
        c_n = str(x.get("campaign") or "").strip()
        fld = str(x.get("field") or "").lower().strip()
        ek = str(x.get("entity_key") or "").strip().lower()
        if ek and "::" in ek:
            ent_norm = ek.split("::")[-1].strip()
        else:
            p_t, _ = _parse_legacy_entity_name(str(x.get("entity_name") or ""), c_n)
            ent_norm = p_t or c_n.lower()
        return (c_n, ent_norm, fld)

    latest_idx_by_slot: dict[tuple[str, str, str], int] = {}
    for idx_l, row_l in enumerate(ledger):
        latest_idx_by_slot[_norm_ledger_entity_slot(row_l)] = idx_l

    # Dynamic Post-Adjustment Effect Attribution
    active_cooldowns = []
    for idx_l, item in enumerate(ledger):
        if num(item.get("baseline_acos")) >= 5000.0:
            item["baseline_acos"] = 0.0
        slot_key = _norm_ledger_entity_slot(item)
        latest_idx = latest_idx_by_slot.get(slot_key, idx_l)
        is_superseded = (idx_l < latest_idx)
        superseded_by_date = str(ledger[latest_idx].get("change_date") or "") if is_superseded else ""

        c_name = item.get("campaign") or ""
        matching_camp = camp_lookup.get(c_name)
        ch_at_str = item.get("changed_at") or now.isoformat()
        try:
            ch_at = datetime.fromisoformat(ch_at_str)
            days_elapsed = max(0, (now - ch_at).days)
        except Exception:
            days_elapsed = 0
        days_remaining = 0 if is_superseded else max(0, cooldown_days - days_elapsed)
        item["days_elapsed"] = days_elapsed
        item["days_remaining"] = days_remaining

        ent_type = item.get("entity_type") or ""
        is_entity_attr = False
        curr_cpc = 0.0
        curr_imp = 0.0
        curr_acos = 0.0
        ent_state = ""
        ent_ord_3d = None
        ent_imp_30d_daily = None

        # Direction 3: Precise Entity-Level Metrics Attribution (STRICT exact text/key match, NO substring collision!)
        if ent_type == "keyword_bid" and curr_kw_map:
            e_key = item.get("entity_key") or ""
            kw_obj = curr_kw_map.get(e_key) if e_key else None
            if not kw_obj:
                p_txt, p_match = _parse_legacy_entity_name(item.get("entity_name") or "", c_name)
                if p_txt and p_match:
                    kw_obj = curr_kw_map.get(f"{c_name}::{p_match}::{p_txt}")
                if not kw_obj and p_txt:
                    for k_k, k_v in curr_kw_map.items():
                        if k_v.get("campaign") == c_name and k_v.get("text", "").lower().strip() == p_txt:
                            if not p_match or k_v.get("match", "").lower().strip() == p_match:
                                kw_obj = k_v
                                item["entity_key"] = k_k
                                break
            if kw_obj:
                curr_cpc = round(num(kw_obj.get("cpc_3d") or kw_obj.get("cpc_7d") or kw_obj.get("cpc")), 2)
                i_3d = num(kw_obj.get("imp_3d"))
                i_7d = num(kw_obj.get("imp_7d"))
                i_30d = num(kw_obj.get("imp_30d"))
                ent_imp_30d_daily = round(i_30d / 30.0, 1)
                ent_state = str(kw_obj.get("state") or "").lower().strip()
                if ent_state == "paused" or (matching_camp and matching_camp.get("state") == "paused"):
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_3d > 0 or days_elapsed >= 3:
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_7d > 0:
                    curr_imp = round(i_7d / 7.0, 1)
                else:
                    curr_imp = ent_imp_30d_daily
                raw_kw_ac = num(kw_obj.get("acos_7d") or kw_obj.get("acos"))
                curr_acos = 0.0 if raw_kw_ac >= 5000.0 else round(raw_kw_ac, 1)
                ent_ord_3d = int(num(kw_obj.get("ord_3d")))
                is_entity_attr = True

        elif ent_type == "target_bid" and curr_target_map:
            e_key = item.get("entity_key") or ""
            t_obj = curr_target_map.get(e_key) if e_key else None
            if not t_obj:
                p_txt, p_type = _parse_legacy_entity_name(item.get("entity_name") or "", c_name)
                if p_txt:
                    for t_k, t_v in curr_target_map.items():
                        if t_v.get("campaign") == c_name and t_v.get("text", "").lower().strip() == p_txt:
                            if not p_type or t_v.get("type", "").lower().strip() == p_type:
                                t_obj = t_v
                                item["entity_key"] = t_k
                                break
            if t_obj:
                curr_cpc = round(num(t_obj.get("cpc_3d") or t_obj.get("cpc_7d") or t_obj.get("cpc")), 2)
                i_3d = num(t_obj.get("imp_3d"))
                i_7d = num(t_obj.get("imp_7d"))
                i_30d = num(t_obj.get("imp_30d"))
                ent_imp_30d_daily = round(i_30d / 30.0, 1)
                ent_state = str(t_obj.get("state") or "").lower().strip()
                if ent_state == "paused" or (matching_camp and matching_camp.get("state") == "paused"):
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_3d > 0 or days_elapsed >= 3:
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_7d > 0:
                    curr_imp = round(i_7d / 7.0, 1)
                else:
                    curr_imp = ent_imp_30d_daily
                raw_t_ac = num(t_obj.get("acos_7d") or t_obj.get("acos"))
                curr_acos = 0.0 if raw_t_ac >= 5000.0 else round(raw_t_ac, 1)
                ent_ord_3d = int(num(t_obj.get("ord_3d")))
                is_entity_attr = True

        # Fallback to campaign macro metrics if entity metrics are not yet isolated
        if not is_entity_attr and matching_camp:
            curr_cpc = round(num(matching_camp.get("cpc_3d") or matching_camp.get("cpc_7d") or matching_camp.get("cpc")), 2)
            # Never fall back from 0.0 3d impressions to 30d historical impressions when campaign is paused or >=3d elapsed!
            if matching_camp.get("state") == "paused" or days_elapsed >= 3 or num(matching_camp.get("daily_imp_3d")) > 0:
                curr_imp = round(num(matching_camp.get("daily_imp_3d")), 1)
            else:
                curr_imp = round(num(matching_camp.get("daily_imp_7d") or matching_camp.get("daily_imp_30d")), 1)
            raw_c_ac = num(matching_camp.get("acos_7d") or matching_camp.get("acos"))
            curr_acos = 0.0 if raw_c_ac >= 5000.0 else round(raw_c_ac, 1)

        if matching_camp or is_entity_attr:
            item["current_cpc"] = curr_cpc
            item["current_daily_imp"] = curr_imp
            item["current_acos"] = curr_acos

            base_cpc = num(item.get("baseline_cpc"))
            base_imp = num(item.get("baseline_daily_imp"))

            # Auto-repair legacy contaminated campaign-level baseline_daily_imp on entity rows
            if is_entity_attr and ent_imp_30d_daily is not None and base_imp > max(80.0, ent_imp_30d_daily * 4.5):
                camp_30d_imp = num(matching_camp.get("daily_imp_30d")) if matching_camp else 0.0
                if ent_imp_30d_daily > 0 and (camp_30d_imp == 0 or base_imp >= camp_30d_imp * 0.5):
                    base_imp = ent_imp_30d_daily
                    item["baseline_daily_imp"] = base_imp

            # Only compute CPC delta when both baseline_cpc > 0 and current_cpc > 0 (0 clicks != -100% CPC improvement!)
            delta_cpc = round(((curr_cpc - base_cpc) / base_cpc * 100), 1) if (base_cpc > 0 and curr_cpc > 0) else 0.0
            delta_imp = round(((curr_imp - base_imp) / base_imp * 100), 1) if base_imp > 0 else 0.0
            item["delta_cpc_pct"] = delta_cpc
            item["delta_imp_pct"] = delta_imp

            entity_field = str(item.get("field") or "").lower().strip()
            new_val_str = str(item.get("new_val") or "").lower().strip()
            entity_type_str = str(item.get("entity_type") or "").lower().strip()
            camp_is_paused = bool(matching_camp and matching_camp.get("state") == "paused")
            entity_is_paused = (ent_state == "paused")

            # Direction 1 & Rule 54: Intentional State & Negative Reconciliation (include entity's own paused state!)
            is_intentional_pause = (
                (entity_field == "state" and new_val_str == "paused")
                or camp_is_paused
                or entity_is_paused
                or "pause" in entity_type_str
            )
            is_intentional_negative = (
                "negative" in entity_type_str
                or "negative" in entity_field
                or "negative" in str(item.get("action") or "").lower()
            )

            # Bug fix: check entity's OWN ord_3d when attributing entity-level shock!
            no_recent_orders = (
                (ent_ord_3d == 0)
                if (is_entity_attr and ent_ord_3d is not None)
                else (not matching_camp or matching_camp.get("ord_3d", 0) == 0)
            )

            if is_superseded:
                verdict = "【历史动作已由后续调优覆盖·归档】"
                advice = f"该实体已于 {superseded_by_date or '后续日期'} 执行新一轮参数调整，以最新一期台账裁决为准，本条历史记录归档备查"
            elif is_intentional_pause:
                verdict = "【合规意向暂停·阻断流血】"
                advice = "实体已按计划合规暂停，维持停运状态，严禁触发休克熔断或回踩探底"
            elif is_intentional_negative:
                verdict = "【否定拦截生效·预期归零】"
                advice = "否定词拦截生效，已阻断无效流量，维持否定"
            elif curr_cpc > 0 and delta_cpc <= -15.0 and delta_imp > -70.0:
                verdict = "【调后生效·见效保持】"
                advice = "见效保持，严禁二次砍价"
            elif delta_imp <= -70.0 and base_imp >= 3.0 and no_recent_orders:
                if is_stockout:
                    verdict = "【Listing断货·系统停推】"
                    advice = "FBA可售库存已归零，商品失去购物车并由亚马逊系统强制停推，严禁误判为出价脱靶或加价回踩，等待补货入仓"
                elif is_low_inventory:
                    verdict = "【极低库存告急·控流防断】"
                    advice = f"可售天数仅剩 {dos_cnt:.0f} 天，系统控流防断货，严禁大幅提价抢量加剧断货风险"
                elif days_elapsed > 14:
                    verdict = "【超期历史断流记录·归档】"
                    advice = f"距今已 {days_elapsed} 天（超出14天短期归因窗口），请以当前实时活动与投放词诊断为准"
                else:
                    entity_kw = str(item.get("entity_name") or "").lower().strip()
                    clean_term, _ = _parse_legacy_entity_name(entity_kw, c_name)
                    r_info = xydc_map.get(clean_term) if xydc_map else None
                    if not r_info and xydc_map:
                        r_info = xydc_map.get(re.sub(r"[^\w\s]", "", clean_term).strip())
                    sp_r = r_info.get("sp_rank") if r_info else None
                    is_top_rank = (sp_r is not None and int(sp_r) <= 3) or (not is_entity_attr and matching_camp and matching_camp.get("assessment") == "TOP_POSITION_MARKET_COOLDOWN")
                    is_deal_trans = bool(deal_info and deal_info.get("is_deal_transition")) or (matching_camp and matching_camp.get("assessment") == "DEAL_TRANSITION_COOLDOWN")

                    if is_top_rank:
                        verdict = "【首屏黄金位·常态冷却】"
                        advice = f"核心词广告位已稳居首屏顶部(SP #{sp_r or 1})，曝光回落非出价脱靶，严禁加价回踩，保持当前出价观察"
                    elif is_deal_trans:
                        verdict = "【秒杀后流量回落·常态冷却】"
                        advice = "近期结束秒杀/促销活动，曝光回落系常态均值回归，严禁误判断流，保持当前出价观察"
                    else:
                        verdict = "【调后休克断流】"
                        baseline_cpc = base_cpc or (matching_camp.get("cpc", 0.50) if matching_camp else 0.50)
                        cur_tos = num(matching_camp.get("tos", 0)) if matching_camp else 0
                        is_target_entity = (entity_type_str == "target_bid" or "target" in entity_type_str or (matching_camp and any(x in matching_camp.get("name", "").lower() for x in ["asin", "竞品", "b0", "target", "同类", "关联"])))
                        target_bid_floor = 0.42 if (is_fragile or is_high_refund) else 0.36
                        camp_bid_floor = target_bid_floor if is_target_entity else (0.30 if (is_fragile or is_high_refund) else 0.25)
                        target_bid = max(camp_bid_floor, round(baseline_cpc / (1.0 + (cur_tos / 100.0)), 2))
                        if entity_field in ["tos", "pp", "ros"]:
                            advice = f"位置溢价调整后休克断流，保持TOS {int(cur_tos)}%，建议广告组基础竞价科学托底补偿至 ${target_bid:.2f}"
                        elif entity_field == "budget":
                            advice = "预算调整后断流休克，建议适度回调日预算至合理出单水平"
                        else:
                            cur_bid = num(re.sub(r"[^\d.]", "", str(item.get("new_val") or "0")))
                            if cur_bid > 5.0 or cur_bid <= 0.0:
                                cur_bid = target_bid
                            probe_bid = max(round(cur_bid * 1.15, 2), target_bid)
                            probe_bid = min(probe_bid, max(2.50, round(baseline_cpc * 1.8, 2)))
                            if is_target_entity:
                                advice = f"商品投放详情页拍卖门槛较高，建议科学倒推回踩至 ${probe_bid:.2f} (底价保护 ${target_bid_floor:.2f}) 激活展示池"
                            else:
                                advice = f"立即科学倒推回踩探底至 ${probe_bid:.2f} 激活展示池"
            elif delta_cpc >= 40.0 and curr_imp > 0 and curr_cpc > 0:
                verdict = "【调后失血暴冲】"
                advice = "紧急下调溢价/基础竞价控费"
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

        if days_remaining > 0 and not is_superseded:
            active_cooldowns.append({
                "entity_type": item.get("entity_type"),
                "campaign": item.get("campaign"),
                "entity_key": item.get("entity_key") or item.get("entity_name"),
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



def load_known_negatives(args, src):
    """Collect keywords already negated outside this tool, so we stop re-recommending them."""
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

    return known_negatives

def load_product_context(args, dest, src):
    """Load the listing, financial, traffic and dayparting profiles and settle the target ACOS."""
    meta = load_json(src / "meta.json") or {}
    listing = load_json(src / "listing.json")
    stock = load_json(src / "fba_stock.json")
    profile = listing_profile(listing, stock)
    profile["meta"] = meta

    # Load Advanced Intelligence Profiles (store-sid aware!)
    financial_prof = load_financial_profile(src, meta=meta)
    traffic_prof = load_traffic_profile(src, db_dir=args.db_dir, financial_prof=financial_prof)
    deal_info = load_deal_info(src, meta=meta)
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
    if deal_info:
        profile["deal_info"] = deal_info
        dest.joinpath("deal_info.json").write_text(
            json.dumps(deal_info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if dayparting_table_lines:
        tsv(
            dest / "dayparting_analysis.tsv",
            "hour_window\tspend\tad_orders\tstore_orders\tcvr\tacos\tcpc\tclassification\trecommendation",
            dayparting_table_lines,
        )

    rule_path = Path(args.rules) if args.rules else None
    raw_rules = load_product_rules(rule_path, src)
    rules = sanitize_product_rules(raw_rules, profile)
    gen_time_str = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")

    # Determine target ACOS: CLI argument > rules config > financial break-even anchor (68%) > default 25.0
    target_acos = 25.0
    if args.target_acos is not None:
        target_acos = float(args.target_acos)
    elif rules.get("target_acos") is not None:
        target_acos = float(rules.get("target_acos"))
    elif financial_prof and financial_prof.get("break_even_acos", 0) >= 15.0:
        target_acos = max(18.0, min(35.0, round(financial_prof["break_even_acos"] * 0.68, 1)))
    elif financial_prof and financial_prof.get("break_even_acos", 0) > 0:
        # If break_even_acos is abnormally low (< 15%) due to temporary couponing/promo discounts,
        # fallback to an operational floor (22.0%) to prevent collapsing all ad thresholds.
        target_acos = 22.0

    # Rule 33: Refund Rate Crisis Contract
    refund_rate = financial_prof.get("refund_rate", 0.0) if financial_prof else 0.0
    if refund_rate >= 12.0:
        profile["critical_refund_alert"] = True
        target_acos = max(15.0, round(target_acos * 0.82, 1))

    profile["target_acos"] = target_acos

    profile["rules_loaded"] = {
        "is_patterns_count": len(rules.get("is_patterns", [])),
        "is_not_patterns_count": len(rules.get("is_not_patterns", [])),
        "quarantined_self_title_patterns_count": len(rules.get("quarantined_self_title_patterns", [])),
        "competitor_brands_count": len(rules.get("competitor_brands", [])),
        "target_acos": target_acos,
    }

    dest.joinpath("product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return dayparting_records, dayparting_table_lines, financial_prof, gen_time_str, meta, profile, rules, target_acos, traffic_prof, deal_info


def _normalize_placement_slot(raw_p: str) -> str:
    s = (raw_p or "").upper().strip()
    if "TOP" in s or "顶部" in s:
        return "TOS"
    if "DETAIL" in s or "PRODUCT" in s or "商品" in s:
        return "PP"
    if "OTHER" in s or "REST" in s or "其余" in s:
        return "ROS"
    if "OFF" in s or "站外" in s:
        return "OFF"
    return s or "UNKNOWN"


def load_real_placement_report(src: Path, camps: list[dict], dest: Path) -> list[dict]:
    """Load real SP placement report (30d & 7d) from get_sp_campaign_placement_report and attach to campaigns."""
    by_id = {str(c.get("id")): c for c in camps if c.get("id") is not None and str(c.get("id")) != ""}
    by_name = {str(c.get("name")): c for c in camps if c.get("name")}

    def _parse_file(fname: str) -> dict[str, dict[str, dict]]:
        rows_raw = as_rows(load_json(src / fname))
        grouped: dict[str, dict[str, dict]] = defaultdict(dict)
        for r in rows_raw:
            if not isinstance(r, dict):
                continue
            cid = str(r.get("campaign_id") or "").strip()
            cname = str(r.get("campaign_name") or r.get("name") or "").strip()
            if (not cid or cid == "None") and not cname:
                continue  # skip top summary row where campaign_id is None
            camp = by_id.get(cid) or by_name.get(cname)
            if not camp:
                continue
            slot = _normalize_placement_slot(str(r.get("placement") or r.get("placement_type") or ""))
            sp = num(r.get("spends") or r.get("spend") or r.get("cost"))
            sl = num(r.get("sales"))
            od = int(num(r.get("orders")))
            dir_od = int(num(r.get("direct_orders")))
            indir_od = int(num(r.get("indirect_orders")))
            ck = int(num(r.get("clicks")))
            im = int(num(r.get("impressions")))
            sp_pct_raw = num(r.get("spends_percent"))
            sp_share = round(sp_pct_raw * (100.0 if 0 < sp_pct_raw <= 1.0 else 1.0), 1)
            grouped[camp["name"]][slot] = {
                "slot": slot,
                "raw_placement": str(r.get("placement") or slot),
                "modifier_pct": num(r.get("percentage") or r.get("bid_adjustment")),
                "strategy": str(r.get("strategy") or ""),
                "spend": round(sp, 2),
                "sales": round(sl, 2),
                "orders": od,
                "direct_orders": dir_od,
                "indirect_orders": indir_od,
                "clicks": ck,
                "impressions": im,
                "cpc": round((sp / ck) if ck > 0 else num(r.get("cpc")), 2),
                "cvr": round((od / ck * 100.0) if ck > 0 else num(r.get("cvr")), 2),
                "ctr": round((ck / im * 100.0) if im > 0 else num(r.get("ctr")), 2),
                "acos": round(acos(sp, sl), 1),
                "spend_share": sp_share,
            }
        # Recompute spend_share if API returned 0
        for cname_k, slots in grouped.items():
            tot_sp = sum(v["spend"] for v in slots.values())
            if tot_sp > 0:
                for v in slots.values():
                    if v["spend_share"] <= 0 and v["spend"] > 0:
                        v["spend_share"] = round(v["spend"] / tot_sp * 100.0, 1)
        return grouped

    p30 = _parse_file("placement_report.json")
    p7 = _parse_file("placement_report_7d.json")

    tsv_lines = []
    flat_records = []
    for c in camps:
        cn = c["name"]
        c["real_placements_30d"] = p30.get(cn, {})
        c["real_placements_7d"] = p7.get(cn, {})
        for slot in ("TOS", "PP", "ROS", "OFF"):
            s30 = c["real_placements_30d"].get(slot)
            s7 = c["real_placements_7d"].get(slot, {})
            if not s30 and not s7:
                continue
            base = s30 or {"slot": slot, "spend": 0.0, "sales": 0.0, "orders": 0, "direct_orders": 0, "indirect_orders": 0, "clicks": 0, "impressions": 0, "cpc": 0.0, "cvr": 0.0, "acos": 0.0, "spend_share": 0.0}
            rec = {
                "campaign": cn,
                "state": c.get("state", ""),
                "bidding_strategy": c.get("bidding_strategy", ""),
                "slot": slot,
                "configured_pct": int(c.get(slot.lower(), 0)) if slot in ("TOS", "PP", "ROS") else 0,
                **base,
                "spend_7d": s7.get("spend", 0.0),
                "orders_7d": s7.get("orders", 0),
                "acos_7d": s7.get("acos", 0.0),
                "cpc_7d": s7.get("cpc", 0.0),
            }
            flat_records.append(rec)
            tsv_lines.append("\t".join(str(x) for x in [
                cn, c.get("state", ""), slot, rec["configured_pct"], c.get("bidding_strategy", ""),
                rec["spend"], rec["spend_share"], rec["sales"], rec["acos"],
                rec["orders"], rec["direct_orders"], rec["indirect_orders"],
                rec["clicks"], rec["cpc"], rec["cvr"],
                rec["spend_7d"], rec["orders_7d"], rec["acos_7d"], rec["cpc_7d"],
            ]))

    if tsv_lines:
        tsv(
            dest / "real_placement_performance.tsv",
            "campaign\tstate\tslot\tmodifier_pct\tbidding_strategy\tspend_30d\tspend_share_pct\tsales_30d\tacos_30d\torders_30d\tdirect_ord\tindirect_ord\tclicks_30d\tcpc_30d\tcvr_30d\tspend_7d\torders_7d\tacos_7d\tcpc_7d",
            tsv_lines,
        )
    return flat_records


def _extract_daily_date_str(row: dict) -> str | None:
    for k in ("rdate", "date", "report_date", "purchase_date", "stat_date", "day_date", "date_str", "time", "snapshot_date"):
        val = str(row.get(k) or "").strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", val)
        if m:
            return m.group(1)
    return None


def _extract_small_bsr_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    if isinstance(val, list) and val:
        ranks = [int(num(x.get("rank"))) for x in val if isinstance(x, dict) and num(x.get("rank")) > 0]
        return min(ranks) if ranks else None
    if isinstance(val, dict) and num(val.get("rank")) > 0:
        return int(num(val.get("rank")))
    m = re.search(r"#?\s*(\d{1,6})", str(val).replace(",", ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def evaluate_macro_scorecard(
    *,
    src: Path,
    dest: Path,
    export_dir: str | None,
    db_root: Path,
    snap_key: str,
    legacy_key: str,
    gen_time_str: str,
    meta: dict,
    profile: dict,
    financial_prof: dict,
    traffic_prof: dict,
    camps: list[dict],
    deal_info: dict,
    target_acos: float,
) -> dict:
    """Evaluate Macro North-Star KPI outcomes (Recent 7d vs Prior 7d & vs Prior Saved Expectations),
    isolate external confounders, activate Velocity-Shrinkage Guard if volume collapsed, and persist next expectations.
    """
    expectations_dir = db_root / "expectations"
    expectations_dir.mkdir(parents=True, exist_ok=True)
    exp_file = expectations_dir / f"{snap_key}_expectations.json"
    prev_exp = load_json(exp_file)
    if not prev_exp and (expectations_dir / f"{legacy_key}_expectations.json").exists():
        prev_exp = load_json(expectations_dir / f"{legacy_key}_expectations.json")

    # 1. Parse daily ASIN trajectory from product_performance_daily.json
    daily_raw = as_rows(load_json(src / "product_performance_daily.json"))
    by_day: dict[str, dict] = {}
    for r in daily_raw:
        if not isinstance(r, dict):
            continue
        d_str = _extract_daily_date_str(r)
        if not d_str:
            continue
        ad_sp = _extract_row_ad_costs(r)["total_ad_cost"]
        vol = int(num(r.get("volume") or r.get("order_items_quantity")))
        nat_vol = int(num(r.get("nature_order_items")))
        day_sales = num(r.get("amount") or r.get("sales_amount") or r.get("net_amount"))
        by_day[d_str] = {
            "date": d_str,
            "sales": day_sales,
            "units": vol,
            "organic_units": max(0, nat_vol),
            "ad_units": max(0, vol - max(0, nat_vol)),
            "sessions": int(num(r.get("sessions_total") or r.get("sessions"))),
            "ad_spend": ad_sp,
            "buy_box_pct": num(r.get("buy_box_percentage")),
            "avg_price": num(r.get("avg_landed_price") or r.get("landed_price") or r.get("price")) or (round(day_sales / vol, 2) if vol > 0 else 0.0),
            "small_bsr": _extract_small_bsr_int(r.get("small_cate_rank") or r.get("small_rank")),
            "returns": int(num(r.get("return_count"))),
        }

    sorted_days = [by_day[d] for d in sorted(by_day.keys())]

    def _agg_slice(days_slice: list[dict], fallback_days: int = 7) -> dict:
        n = len(days_slice)
        if n == 0:
            return {"days": 0, "daily_sales": 0.0, "daily_units": 0.0, "daily_organic_units": 0.0, "daily_ad_units": 0.0, "organic_unit_share_pct": 0.0, "daily_sessions": 0.0, "daily_ad_spend": 0.0, "real_tacos_pct": 0.0, "buy_box_pct": 0.0, "avg_price": 0.0, "small_bsr": None}
        tot_s = sum(x["sales"] for x in days_slice)
        tot_u = sum(x["units"] for x in days_slice)
        tot_org = sum(x["organic_units"] for x in days_slice)
        tot_adu = sum(x["ad_units"] for x in days_slice)
        sess_vals = [x["sessions"] for x in days_slice if x["sessions"] > 0]
        tot_sp = sum(x["ad_spend"] for x in days_slice)
        bb_vals = [x["buy_box_pct"] for x in days_slice if x["buy_box_pct"] > 0]
        pr_vals = [x["avg_price"] for x in days_slice if x["avg_price"] > 0]
        bsr_vals = [x["small_bsr"] for x in days_slice if x["small_bsr"]]
        bb_raw = (sum(bb_vals) / len(bb_vals)) if bb_vals else 0.0
        bb_pct = round(bb_raw * (100.0 if 0 < bb_raw <= 1.0 else 1.0), 1)
        return {
            "days": n,
            "start_date": days_slice[0]["date"],
            "end_date": days_slice[-1]["date"],
            "daily_sales": round(tot_s / n, 2),
            "daily_units": round(tot_u / n, 2),
            "daily_organic_units": round(tot_org / n, 2),
            "daily_ad_units": round(tot_adu / n, 2),
            "organic_unit_share_pct": round((tot_org / tot_u * 100.0), 1) if tot_u > 0 else 0.0,
            "daily_sessions": round(sum(sess_vals) / len(sess_vals), 1) if sess_vals else 0.0,
            "daily_ad_spend": round(tot_sp / n, 2),
            "real_tacos_pct": round((tot_sp / tot_s * 100.0), 2) if tot_s > 0 else (999.0 if tot_sp > 0 else 0.0),
            "buy_box_pct": bb_pct,
            "avg_price": round((sum(pr_vals) / len(pr_vals)), 2) if pr_vals else 0.0,
            "small_bsr": int(round(sum(bsr_vals) / len(bsr_vals))) if bsr_vals else None,
        }

    has_daily_series = len(sorted_days) >= 10
    all_deal_dates = set((deal_info or {}).get("all_deal_dates") or [])
    prior_deal_dates_hit: list[str] = []
    if has_daily_series:
        fetched_day_str = str((meta or {}).get("fetched_at") or "")[:10]
        eval_days = sorted_days
        if len(eval_days) >= 15 and (
            (fetched_day_str and eval_days[-1]["date"] == fetched_day_str)
            or (eval_days[-1]["sales"] == 0 and eval_days[-1]["ad_spend"] == 0)
        ):
            eval_days = eval_days[:-1]
        recent_slice = eval_days[-7:]
        prior_slice = eval_days[-14:-7] if len(eval_days) >= 14 else eval_days[:-7]
        recent_deal_hits = [d["date"] for d in recent_slice if d["date"] in all_deal_dates]
        prior_deal_hits = [d["date"] for d in prior_slice if d["date"] in all_deal_dates]
        if len(prior_deal_hits) > len(recent_deal_hits):
            prior_deal_dates_hit = prior_deal_hits
        recent_7d = _agg_slice(recent_slice)
        prior_7d = _agg_slice(prior_slice)
        baseline_30d = _agg_slice(eval_days)
        data_source = "product_performance_daily_usd"
    else:
        # Fallback for legacy cached bundles without product_performance_daily.json:
        # Use authoritative store-level 7d / 14d / 30d volumes from listing.json first so ad-spend cuts
        # accompanied by organic growth are NEVER misclassified as store shrinkage!
        fin_sales_30d = num((financial_prof or {}).get("total_sales"))
        fin_vol_30d = num((financial_prof or {}).get("volume"))
        fin_ad_30d = num((financial_prof or {}).get("total_ad_cost"))
        trf_sess_30d = num((traffic_prof or {}).get("sessions_total"))
        trf_nat_30d = num((traffic_prof or {}).get("nature_order_items"))
        camp_sp_30d = sum(c.get("spend", 0.0) for c in camps)
        camp_sl_30d = sum(c.get("sales", 0.0) for c in camps)
        camp_od_30d = sum(c.get("ord", 0.0) for c in camps)
        camp_sp_7d = sum(c.get("spend_7d", 0.0) for c in camps)
        camp_sl_7d = sum(c.get("sales_7d", 0.0) for c in camps)
        camp_od_7d = sum(c.get("ord_7d", 0.0) for c in camps)

        avg_p = num((financial_prof or {}).get("avg_price") or profile.get("price") or 25.0)
        base_d_sales = round((fin_sales_30d or num(profile.get("thirty_sales")) or camp_sl_30d) / 30.0, 2)
        base_d_units = round((fin_vol_30d or camp_od_30d) / 30.0, 2)
        base_d_org = round(trf_nat_30d / 30.0, 2) if trf_nat_30d > 0 else round(max(0.0, base_d_units - (camp_od_30d / 30.0)), 2)
        base_d_sp = round((fin_ad_30d or camp_sp_30d) / 30.0, 2)

        lst_7d_avg_u = num(profile.get("seven_volume"))
        lst_14d_raw = num(profile.get("fourteen_volume"))
        lst_14d_sales = num(profile.get("fourteen_sales"))
        lst_14d_sp = num(profile.get("fourteen_ad_spend"))

        if lst_7d_avg_u > 0 and lst_14d_raw > 0:
            # profile["seven_volume"] is daily avg over 7d; profile["fourteen_volume"] is daily avg if from average_fourteen_volume else 14d sum
            lst_14d_avg_u = (lst_14d_raw / 14.0) if (lst_14d_sales > 0 and avg_p > 0 and abs(lst_14d_raw * avg_p - lst_14d_sales) < abs(lst_14d_raw * 14.0 * avg_p - lst_14d_sales)) else lst_14d_raw
            rec_d_units = round(lst_7d_avg_u, 2)
            pri_d_units = round(max(0.01, 2.0 * lst_14d_avg_u - lst_7d_avg_u), 2)
            eff_unit_p = (lst_14d_sales / (lst_14d_avg_u * 14.0)) if (lst_14d_sales > 0 and lst_14d_avg_u > 0) else avg_p
            rec_d_sales = round(rec_d_units * eff_unit_p, 2)
            pri_d_sales = round(pri_d_units * eff_unit_p, 2)
            rec_d_ad_u = round(min(rec_d_units, camp_od_7d / 7.0), 2)
            rec_d_org = round(max(0.0, rec_d_units - rec_d_ad_u), 2)
            pri_ad_u_est = round(min(pri_d_units, max(0.0, (camp_od_30d - camp_od_7d) / 23.0)), 2)
            pri_d_org = round(max(0.0, pri_d_units - pri_ad_u_est), 2)
            rec_d_sp = round(camp_sp_7d / 7.0, 2) if camp_sp_7d > 0 else base_d_sp
            pri_d_sp = round(max(0.0, (lst_14d_sp - camp_sp_7d) / 7.0), 2) if lst_14d_sp > camp_sp_7d else base_d_sp
            data_source = "listing_7d_vs_14d_authoritative_fallback"
        else:
            adv_share = (num((traffic_prof or {}).get("adv_sales_share")) / 100.0) if traffic_prof else 0.0
            est_7d_total_sales = (camp_sl_7d / adv_share) if (adv_share >= 0.20 and camp_sl_7d > 0) else (base_d_sales * 7.0 if camp_sl_30d == 0 else (base_d_sales * 7.0 * (camp_sl_7d / max(1.0, camp_sl_30d / 30.0 * 7.0))))
            rec_d_sales = round(est_7d_total_sales / 7.0, 2) if camp_sl_7d > 0 else base_d_sales
            rec_d_units = round(rec_d_sales / avg_p, 2) if avg_p > 0 else round(camp_od_7d / 7.0, 2)
            rec_d_ad_u = round(camp_od_7d / 7.0, 2)
            rec_d_org = round(max(0.0, rec_d_units - rec_d_ad_u), 2)
            rec_d_sp = round(camp_sp_7d / 7.0, 2)
            pri_d_sales, pri_d_units, pri_d_org, pri_ad_u_est, pri_d_sp = base_d_sales, base_d_units, base_d_org, round(camp_od_30d / 30.0, 2), base_d_sp
            data_source = "aggregate_30d_and_campaign_7d_fallback"

        baseline_30d = {
            "days": 30, "daily_sales": base_d_sales, "daily_units": base_d_units,
            "daily_organic_units": base_d_org, "daily_ad_units": round(camp_od_30d / 30.0, 2),
            "organic_unit_share_pct": round((base_d_org / base_d_units * 100.0), 1) if base_d_units > 0 else 0.0,
            "daily_sessions": round(trf_sess_30d / 30.0, 1), "daily_ad_spend": base_d_sp,
            "real_tacos_pct": round(num((traffic_prof or {}).get("tacos")), 2),
            "buy_box_pct": round(num((traffic_prof or {}).get("buy_box_percentage")), 1),
            "avg_price": round(avg_p, 2), "small_bsr": _extract_small_bsr_int(profile.get("small_rank")),
        }
        prior_7d = {
            "days": 7, "daily_sales": pri_d_sales, "daily_units": pri_d_units,
            "daily_organic_units": pri_d_org, "daily_ad_units": pri_ad_u_est,
            "organic_unit_share_pct": round((pri_d_org / pri_d_units * 100.0), 1) if pri_d_units > 0 else 0.0,
            "daily_sessions": baseline_30d["daily_sessions"], "daily_ad_spend": pri_d_sp,
            "real_tacos_pct": round((pri_d_sp / pri_d_sales * 100.0), 2) if pri_d_sales > 0 else baseline_30d["real_tacos_pct"],
            "buy_box_pct": baseline_30d["buy_box_pct"], "avg_price": round(avg_p, 2),
            "small_bsr": baseline_30d["small_bsr"],
        }
        recent_7d = {
            "days": 7, "daily_sales": rec_d_sales, "daily_units": rec_d_units,
            "daily_organic_units": rec_d_org, "daily_ad_units": rec_d_ad_u,
            "organic_unit_share_pct": round((rec_d_org / rec_d_units * 100.0), 1) if rec_d_units > 0 else 0.0,
            "daily_sessions": baseline_30d["daily_sessions"], "daily_ad_spend": rec_d_sp,
            "real_tacos_pct": round((rec_d_sp / rec_d_sales * 100.0), 2) if rec_d_sales > 0 else 0.0,
            "buy_box_pct": baseline_30d["buy_box_pct"], "avg_price": round(avg_p, 2),
            "small_bsr": baseline_30d["small_bsr"],
        }

    def _pct_delta(curr_v: float, base_v: float) -> float:
        if base_v <= 0:
            return 0.0
        return round((curr_v - base_v) / base_v * 100.0, 1)

    deltas_7d_vs_prior7d = {
        "sales_delta_pct": _pct_delta(recent_7d["daily_sales"], prior_7d["daily_sales"]),
        "units_delta_pct": _pct_delta(recent_7d["daily_units"], prior_7d["daily_units"]),
        "organic_units_delta_pct": _pct_delta(recent_7d["daily_organic_units"], prior_7d["daily_organic_units"]),
        "sessions_delta_pct": _pct_delta(recent_7d["daily_sessions"], prior_7d["daily_sessions"]),
        "ad_spend_delta_pct": _pct_delta(recent_7d["daily_ad_spend"], prior_7d["daily_ad_spend"]),
        "tacos_delta_pp": round(recent_7d["real_tacos_pct"] - prior_7d["real_tacos_pct"], 2),
        "price_delta_pct": _pct_delta(recent_7d["avg_price"], prior_7d["avg_price"]),
    }

    # 2. Parse operational logs (operate_logs.json)
    op_logs_raw = load_json(src / "operate_logs.json")
    op_rows = op_logs_raw if isinstance(op_logs_raw, list) else as_rows(op_logs_raw)
    op_events = {"total_logs": len(op_rows), "price_changes": 0, "copy_or_image_changes": 0, "ad_or_promo_changes": 0, "highlights": []}
    for entry in op_rows[:80]:
        txt_blob = json.dumps(entry, ensure_ascii=False).lower()
        if any(w in txt_blob for w in ("price", "价格", "售价", "优惠券", "coupon")):
            op_events["price_changes"] += 1
        if any(w in txt_blob for w in ("title", "bullet", "image", "标题", "五点", "主图", "描述", "文案")):
            op_events["copy_or_image_changes"] += 1
        if any(w in txt_blob for w in ("广告", "竞价", "预算", "秒杀", "deal")):
            op_events["ad_or_promo_changes"] += 1

    # 3. Isolate external confounders before attributing outcome to ad changes
    confounders = []
    fulfillable_cnt = num(profile.get("fulfillable")) if profile.get("fulfillable") is not None else None
    dos_cnt = num(profile.get("days_of_supply")) if profile.get("days_of_supply") is not None else None
    if (fulfillable_cnt is not None and fulfillable_cnt == 0) or (dos_cnt is not None and 0 < dos_cnt <= 5):
        confounders.append({"code": "STOCKOUT_OR_CRITICAL_INVENTORY", "detail": f"FBA库存告急/断货 (可售={fulfillable_cnt}, 可售天数={dos_cnt})"})
    if recent_7d["buy_box_pct"] > 0 and recent_7d["buy_box_pct"] < 80.0:
        confounders.append({"code": "BUY_BOX_LOSS", "detail": f"近7天购物车赢得率仅 {recent_7d['buy_box_pct']:.1f}% (<80%)"})
    if financial_prof and financial_prof.get("is_cross_shop_asin"):
        omni = financial_prof.get("omni_shop_summary") or {}
        if omni.get("omni_total_sales", 0) > financial_prof.get("total_sales", 0) * 1.25:
            confounders.append({
                "code": "CROSS_SHOP_SHARED_BUYBOX",
                "detail": f"跨店铺共卖同ASIN：全店铺总销额 ${omni.get('omni_total_sales', 0):.2f} vs 当前店铺 ${financial_prof.get('total_sales', 0):.2f}（需防单店购物车分流误判为广告失效）",
            })
    if deal_info and deal_info.get("is_active_deal"):
        confounders.append({"code": "ACTIVE_DEAL", "detail": deal_info.get("summary_desc", "当前正处秒杀期")})
    elif deal_info and deal_info.get("is_deal_transition"):
        confounders.append({"code": "DEAL_POST_COOLDOWN", "detail": deal_info.get("summary_desc", "秒杀后回落过渡期")})
    elif prior_deal_dates_hit:
        confounders.append({
            "code": "DEAL_POST_COOLDOWN",
            "detail": f"前7天对比基期包含秒杀/促销峰值日 ({', '.join(prior_deal_dates_hit)})，近7天日均回落属促后正常均值回归（非广告断流）",
        })
    xydc_price_delta = num((deal_info or {}).get("price_summary", {}).get("price_delta_7d_pct"))
    eff_price_delta = deltas_7d_vs_prior7d["price_delta_pct"] or xydc_price_delta
    if eff_price_delta >= 8.0:
        confounders.append({"code": "PRICE_HIKE_CONFOUNDER", "detail": f"近7天成交均价上调 +{eff_price_delta:.1f}%，抑制转化率与自然流速"})
    if op_events["copy_or_image_changes"] > 0:
        confounders.append({"code": "LISTING_CONTENT_EDIT", "detail": f"运营日志检测到近期 {op_events['copy_or_image_changes']} 次文案/图片变更"})

    hard_volume_confounders = {"STOCKOUT_OR_CRITICAL_INVENTORY", "BUY_BOX_LOSS", "CROSS_SHOP_SHARED_BUYBOX", "DEAL_POST_COOLDOWN"}
    has_hard_confounder = any(c["code"] in hard_volume_confounders for c in confounders)

    # 4. Reconcile against North-Star Targets & Prior Saved Expectations
    raw_be = num((financial_prof or {}).get("break_even_acos"))
    be_acos = raw_be if raw_be >= 15.0 else round(target_acos / 0.68, 1)
    target_tacos = round(max(8.0, min(be_acos * 0.55, max(8.0, target_acos * 0.65))), 1)

    sales_drop = deltas_7d_vs_prior7d["sales_delta_pct"] <= -20.0 or deltas_7d_vs_prior7d["units_delta_pct"] <= -20.0
    org_drop = (
        prior_7d["daily_organic_units"] >= 0.8
        and deltas_7d_vs_prior7d["organic_units_delta_pct"] <= -25.0
        and deltas_7d_vs_prior7d["units_delta_pct"] <= -15.0
    )

    # Also check against prior saved expectation floor if available
    missed_prior_sales_floor = False
    prior_reconciliation = None
    if isinstance(prev_exp, dict) and num(prev_exp.get("circuit_breaker_daily_sales_floor")) > 0:
        exp_floor = num(prev_exp.get("circuit_breaker_daily_sales_floor"))
        exp_tacos_max = num(prev_exp.get("expected_tacos_max"))
        missed_prior_sales_floor = (recent_7d["daily_sales"] > 0 and recent_7d["daily_sales"] < exp_floor)
        prior_reconciliation = {
            "saved_at": prev_exp.get("generated_at"),
            "expected_daily_sales_min": num(prev_exp.get("expected_daily_sales_min")),
            "circuit_breaker_daily_sales_floor": exp_floor,
            "actual_recent_7d_daily_sales": recent_7d["daily_sales"],
            "expected_tacos_max": exp_tacos_max,
            "actual_recent_7d_tacos": recent_7d["real_tacos_pct"],
            "sales_goal_met": recent_7d["daily_sales"] >= num(prev_exp.get("expected_daily_sales_min")) * 0.95,
            "tacos_goal_met": (exp_tacos_max <= 0) or (recent_7d["real_tacos_pct"] <= exp_tacos_max * 1.05),
        }

    velocity_shrinkage_guard = False
    if (sales_drop or org_drop or missed_prior_sales_floor) and not has_hard_confounder and prior_7d["daily_sales"] >= 30.0:
        verdict = "VELOCITY_SHRINKAGE_ALERT"
        velocity_shrinkage_guard = True
        verdict_cn = "【大盘萎缩红色预警·启动防缩量刹车】"
        guidance = (
            f"近7天日均销额 (${recent_7d['daily_sales']:.1f}/d vs 前7天 ${prior_7d['daily_sales']:.1f}/d, {deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%) "
            f"或自然单流速 ({recent_7d['daily_organic_units']:.2f}/d, {deltas_7d_vs_prior7d['organic_units_delta_pct']:+.1f}%) 显著下滑且无断货/秒杀退潮干扰！"
            "本期自动激活【防缩量刹车锁】：严禁对任何出单词降价或大砍活动 TOS，仅清理纯0单硬出血流量，并优先回提核心高转化词展示。"
        )
    elif has_hard_confounder and (sales_drop or org_drop):
        verdict = "CONFOUNDER_DISTORTED"
        verdict_cn = "【外部非广告因子干扰·稳态防误判】"
        c_desc = "；".join(c["detail"] for c in confounders)
        guidance = f"大盘波动主要由外部因子驱动（{c_desc}），严禁将非广告销量回落误判为出价问题而激进乱调。"
    elif deltas_7d_vs_prior7d["sales_delta_pct"] <= -10.0 and deltas_7d_vs_prior7d["tacos_delta_pp"] <= 0:
        verdict = "EFFICIENCY_MET_VOLUME_DRIFT"
        verdict_cn = "【投产达标但销量轻度漂移·控幅稳量】"
        guidance = (
            f"TACOS 控制在 {recent_7d['real_tacos_pct']:.1f}%，但近7天日均销额轻度回落 ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)；"
            "本期严控降价范围，重点向高转化精准词与黄金时段倾斜预算以拉升总单量。"
        )
    elif (deltas_7d_vs_prior7d["sales_delta_pct"] <= -10.0 and deltas_7d_vs_prior7d["tacos_delta_pp"] > 0) or (
        not has_hard_confounder and prior_7d["daily_organic_units"] >= 1.0 and deltas_7d_vs_prior7d["organic_units_delta_pct"] <= -25.0
    ):
        verdict = "VOLUME_AND_EFFICIENCY_DRIFT"
        velocity_shrinkage_guard = True
        verdict_cn = "【销额回落且投产承压·启动防缩量稳盘】"
        guidance = (
            f"近7天日均销额回落 ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%) 且真实 TACOS 上浮 ({deltas_7d_vs_prior7d['tacos_delta_pp']:+.2f}pp) "
            f"或自然单流速下滑 ({deltas_7d_vs_prior7d['organic_units_delta_pct']:+.1f}%)；"
            "已激活【防缩量刹车锁】：严禁对有转化出单词一刀切降价，仅精准否定纯0单流血词并稳住核心出单词展示位。"
        )
    elif recent_7d["real_tacos_pct"] > max(target_tacos * 1.30, be_acos * 0.85) and recent_7d["daily_ad_spend"] >= 10.0:
        verdict = "BLEED_UNCONTAINED"
        verdict_cn = "【TACOS超标失血·精准止血控费】"
        guidance = (
            f"近7天真实 TACOS 达 {recent_7d['real_tacos_pct']:.1f}%（目标 <={target_tacos:.1f}%，保本ACOS {be_acos:.1f}%）；"
            "需果断切断零单出血词根与低效广告位溢价，将花费收敛至盈利核心词。"
        )
    elif prev_exp is None:
        verdict = "BASELINE_INITIALIZED"
        verdict_cn = "【首期北极星基线建档】"
        guidance = (
            f"已建立大盘北极星基线（近7天日均 ${recent_7d['daily_sales']:.1f}/d，自然单 {recent_7d['daily_organic_units']:.2f}/d，真实TACOS {recent_7d['real_tacos_pct']:.1f}%），"
            "下期将自动对账实际结果是否符合本期设定预期。"
        )
    else:
        verdict = "MET_EXPECTATION"
        verdict_cn = "【大盘符合预期·良性飞轮演进】"
        guidance = (
            f"近7天日均销额 ${recent_7d['daily_sales']:.1f}/d ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)，"
            f"自然单占比 {recent_7d['organic_unit_share_pct']:.1f}%，真实 TACOS {recent_7d['real_tacos_pct']:.1f}% 处于健康轨道。"
        )

    # 5. Build & persist next-period quantitative expectations (3d/7d)
    anchor_sales = max(recent_7d["daily_sales"], prior_7d["daily_sales"] * 0.90) if velocity_shrinkage_guard else recent_7d["daily_sales"]
    next_expectations = {
        "generated_at": datetime.now().isoformat(),
        "snap_key": snap_key,
        "window_end": (meta or {}).get("window", ""),
        "expected_daily_sales_min": round(anchor_sales * 0.95, 2),
        "expected_daily_sales_target": round(anchor_sales * 1.08, 2),
        "circuit_breaker_daily_sales_floor": round(anchor_sales * 0.82, 2),
        "expected_daily_organic_units_min": round(max(0.0, recent_7d["daily_organic_units"] * 0.95), 2),
        "expected_tacos_max": round(max(target_tacos, min(recent_7d["real_tacos_pct"] * 0.92, be_acos * 0.75)) if recent_7d["real_tacos_pct"] > target_tacos else target_tacos, 1),
        "expected_ad_acos_max": round(target_acos, 1),
    }
    exp_file.write_text(json.dumps(next_expectations, ensure_ascii=False, indent=2), encoding="utf-8")

    scorecard = {
        "verdict": verdict,
        "verdict_cn": verdict_cn,
        "velocity_shrinkage_guard": velocity_shrinkage_guard,
        "guidance": guidance,
        "data_source": data_source,
        "target_acos": target_acos,
        "target_tacos": target_tacos,
        "break_even_acos": round(be_acos, 1),
        "recent_7d": recent_7d,
        "prior_7d": prior_7d,
        "baseline_30d": baseline_30d,
        "deltas_7d_vs_prior7d": deltas_7d_vs_prior7d,
        "confounders": confounders,
        "operation_logs_summary": op_events,
        "prior_expectation_reconciliation": prior_reconciliation,
        "next_7d_expectations": next_expectations,
    }

    dest.joinpath("macro_scorecard.json").write_text(
        json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sc_tsv_lines = [
        f"verdict\t{verdict}\t{verdict_cn}",
        f"velocity_shrinkage_guard\t{velocity_shrinkage_guard}\t{guidance}",
        f"daily_sales_7d_vs_prior7d\t${recent_7d['daily_sales']:.2f}\t${prior_7d['daily_sales']:.2f} ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)",
        f"daily_units_7d_vs_prior7d\t{recent_7d['daily_units']:.2f}\t{prior_7d['daily_units']:.2f} ({deltas_7d_vs_prior7d['units_delta_pct']:+.1f}%)",
        f"daily_organic_units_7d_vs_prior7d\t{recent_7d['daily_organic_units']:.2f} ({recent_7d['organic_unit_share_pct']:.1f}%)\t{prior_7d['daily_organic_units']:.2f} ({deltas_7d_vs_prior7d['organic_units_delta_pct']:+.1f}%)",
        f"daily_sessions_7d_vs_prior7d\t{recent_7d['daily_sessions']:.1f}\t{prior_7d['daily_sessions']:.1f} ({deltas_7d_vs_prior7d['sessions_delta_pct']:+.1f}%)",
        f"real_tacos_7d_vs_prior7d\t{recent_7d['real_tacos_pct']:.2f}%\t{prior_7d['real_tacos_pct']:.2f}% ({deltas_7d_vs_prior7d['tacos_delta_pp']:+.2f}pp)",
        f"buy_box_and_price_7d\tBuyBox={recent_7d['buy_box_pct']:.1f}%\tAvgPrice=${recent_7d['avg_price']:.2f} ({deltas_7d_vs_prior7d['price_delta_pct']:+.1f}%)",
        f"small_bsr_7d\t{recent_7d['small_bsr'] or '--'}\tprior={prior_7d['small_bsr'] or '--'}",
        f"confounders\t{len(confounders)}\t{' ; '.join(c['code'] + ':' + c['detail'] for c in confounders) if confounders else 'NONE'}",
        f"next_7d_targets\tsales>=${next_expectations['expected_daily_sales_min']:.2f}/d (floor ${next_expectations['circuit_breaker_daily_sales_floor']:.2f}/d)\ttacos<={next_expectations['expected_tacos_max']:.1f}% | acos<={next_expectations['expected_ad_acos_max']:.1f}%",
    ]
    tsv(dest / "macro_scorecard.tsv", "metric\tcurrent_value\tcomparison_or_target", sc_tsv_lines)

    if export_dir:
        exp_p = Path(export_dir)
        exp_p.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        exp_p.joinpath(f"{asin_str}-{country_str}-macro-scorecard-{gen_time_str}.json").write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        exp_p.joinpath(f"{asin_str}-{country_str}-macro-scorecard.json").write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return scorecard


def load_report_frames(meta, profile, src):
    """Load every ads report, scope it to this ASIN's campaigns, and attach 7d/3d deltas."""
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
        c["roas_7d"] = c7.get("roas", 0.0)
        c["cvr_7d"] = c7.get("cvr", 0.0)
        c["imp_7d"] = c7.get("imp", 0)
        c["clk_7d"] = c7.get("clk", 0)
        c["ord_7d"] = c7.get("ord", 0)

        c["spend_3d"] = c3.get("spend", 0.0)
        c["sales_3d"] = c3.get("sales", 0.0)
        c["cpc_3d"] = c3.get("cpc", 0.0)
        c["acos_3d"] = c3.get("acos", 0.0)
        c["roas_3d"] = c3.get("roas", 0.0)
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

        if c["cpc"] > 0 and c["cpc_3d"] > 0:
            c["delta_cpc_pct"] = round((c["cpc_3d"] - c["cpc"]) / c["cpc"] * 100, 1)
        elif c["cpc"] > 0 and c["cpc_7d"] > 0:
            c["delta_cpc_pct"] = round((c["cpc_7d"] - c["cpc"]) / c["cpc"] * 100, 1)
        else:
            c["delta_cpc_pct"] = 0.0

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
    targets_7d = [r for r in as_rows(targets_7d_raw) if not skip_total(r)] if targets_7d_raw else []
    if asin_campaigns:
        targets_7d = [r for r in targets_7d if r.get("campaign_name") in asin_campaigns]

    targets_3d_raw = load_json(src / "targets_3d.json")
    targets_3d = [r for r in as_rows(targets_3d_raw) if not skip_total(r)] if targets_3d_raw else []
    if asin_campaigns:
        targets_3d = [r for r in targets_3d if r.get("campaign_name") in asin_campaigns]

    return asin_campaigns, camps, kws, kws_3d, kws_7d, targets, targets_3d, targets_7d


def run_state_tracking(
    args, camps, dayparting_records, financial_prof, kws, meta, profile, src, targets, traffic_prof,
    kws_3d=None, kws_7d=None, targets_3d=None, targets_7d=None, xydc_map=None, deal_info=None,
    group_bid_lookup=None, camp_group_bid_lookup=None,
):
    """Diff control variables against the last snapshot, sync the ledger, and return state context."""
    db_root, daily_records_dir, ledger_dir, db_snapshots_dir = resolve_db_dirs(args.db_dir, src)
    snapshots_dir = Path(args.snapshots_dir) if args.snapshots_dir else db_snapshots_dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    shop_alias = getattr(args, "shop", None) or meta.get("shop_alias") or profile.get("shop_name")
    snap_key = get_snapshot_key(meta, profile, camps, shop_alias=shop_alias)
    legacy_key = get_snapshot_key(meta, profile, camps, shop_alias="")

    snap_file = snapshots_dir / f"{snap_key}_snapshot.json"
    if not snap_file.exists() and (snapshots_dir / f"{legacy_key}_snapshot.json").exists():
        snap_file = snapshots_dir / f"{legacy_key}_snapshot.json"
    elif not snap_file.exists() and (src.parent / "snapshots" / f"{snap_key}_snapshot.json").exists():
        snap_file = src.parent / "snapshots" / f"{snap_key}_snapshot.json"
    elif not snap_file.exists() and (src.parent / "snapshots" / f"{legacy_key}_snapshot.json").exists():
        snap_file = src.parent / "snapshots" / f"{legacy_key}_snapshot.json"

    prev_snapshot = load_snapshot(snap_file)
    curr_snapshot, raw_cooldowns, locked_campaigns, diff_events, new_diff_items = diff_and_track_cooldown(
        prev_snapshot, camps, kws, targets,
        curr_kws_3d=kws_3d, curr_kws_7d=kws_7d,
        curr_targets_3d=targets_3d, curr_targets_7d=targets_7d,
        cooldown_days=args.cooldown_days,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
    )

    ledger_file = ledger_dir / f"{snap_key}_ledger.json"
    if not ledger_file.exists() and (ledger_dir / f"{legacy_key}_ledger.json").exists():
        ledger_file = ledger_dir / f"{legacy_key}_ledger.json"

    curr_kw_map = curr_snapshot.get("keywords")
    curr_target_map = curr_snapshot.get("targets")
    ledger, active_cooldowns = load_and_sync_ledger(
        ledger_file, prev_snapshot, new_diff_items, camps,
        curr_kw_map=curr_kw_map, curr_target_map=curr_target_map,
        cooldown_days=args.cooldown_days,
        xydc_map=xydc_map, deal_info=deal_info,
        profile=profile, financial_prof=financial_prof, traffic_prof=traffic_prof,
    )
    curr_snapshot["active_cooldowns"] = active_cooldowns
    snap_file.write_text(json.dumps(curr_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    locked_campaigns = {
        cd["campaign"] for cd in active_cooldowns
        if cd.get("campaign") and "合规意向暂停" not in str(cd.get("status", ""))
    }

    gen_date_str = datetime.now().strftime("%Y-%m-%d")

    return active_cooldowns, daily_records_dir, db_root, diff_events, gen_date_str, ledger, ledger_dir, legacy_key, locked_campaigns, snap_key


def write_campaigns_tsv(camps, dest):
    """campaigns.tsv -- 30d macro, 7d trend, 3d latest slice, deltas and dayparting tactic status."""
    camp_lines = []
    for c in camps:
        timing_str = "已生效" if c.get("is_apply_time") else "未应用"
        if c.get("is_apply_time") and c.get("timing_base_value") is not None:
            timing_str += f"(基准值:${c['timing_base_value']:.2f})"
        camp_lines.append(
            "\t".join(str(x) for x in [
                c["state"], c["targeting"], c["sponsored"], int(c["budget"]),
                int(c["tos"]), int(c["pp"]), int(c["ros"]),
                c.get("bidding_strategy", "固定竞价 (Fixed Bids)"),
                timing_str,
                round(c["spend"], 2), round(c["sales"], 2), round(c["acos"], 1),
                round(c["cpc"], 2), round(c["cpc_7d"], 2), round(c["cpc_3d"], 2),
                round(c["delta_cpc_pct"], 1), round(c["delta_imp_pct"], 1),
                round(c["cvr"], 1), round(c["ctr"], 2),
                int(c["clk"]), int(c["ord"]), int(c["direct_ord"]), int(c["indirect_ord"]),
                round(c["spend_7d"], 2), round(c["sales_7d"], 2), round(c["acos_7d"], 1), int(c["ord_7d"]), int(c["clk_7d"]), int(c["imp_7d"]),
                round(c["spend_3d"], 2), round(c["sales_3d"], 2), round(c["acos_3d"], 1), int(c["ord_3d"]), int(c["clk_3d"]), int(c["imp_3d"]),
                round(c["tos_share"], 2), c["name"],
                round(c.get("roas", 0.0), 2), round(c.get("roas_7d", 0.0), 2), round(c.get("roas_3d", 0.0), 2),
            ])
        )
    tsv(
        dest / "campaigns.tsv",
        "state\ttype\tsponsored\tbudget\tTOS\tPP\tROS\tbidding_strategy\ttiming_tactic\tspend_30d\tsales_30d\tacos_30d\tcpc_30d\tcpc_7d\tcpc_3d\tdelta_cpc_pct\tdelta_imp_pct\tcvr_30d\tctr_30d\tclk_30d\tord_30d\tdir\tindir\tspend_7d\tsales_7d\tacos_7d\tord_7d\tclk_7d\timp_7d\tspend_3d\tsales_3d\tacos_3d\tord_3d\tclk_3d\timp_3d\tshare\tname\troas_30d\troas_7d\troas_3d",
        camp_lines,
    )


def assess_and_write_placements(active_cooldowns, args, camps, dest, locked_campaigns, target_acos, traffic_prof=None, kws=None, xydc_map=None, deal_info=None, financial_prof=None, profile=None):
    """Grade each enabled campaign (circuit breakers, cooldown, real placement & bidding strategy risk) into placement_audit.tsv."""
    placement_lines = []
    for c in camps:
        if c["state"] != "enabled":
            continue
        assessment = "HEALTHY"
        recom = "Keep monitoring"

        effective_cpc = c["cpc_3d"] if c["cpc_3d"] > 0 else (c["cpc_7d"] if c["cpc_7d"] > 0 else c["cpc"])
        real_p30 = c.get("real_placements_30d") or {}
        real_tos = real_p30.get("TOS") or {}
        real_pp = real_p30.get("PP") or {}
        real_ros = real_p30.get("ROS") or {}

        camp_kws = [k for k in (kws or []) if (k.get("campaign_name") == c["name"] or str(k.get("campaign_id")) == str(c.get("id")))]
        best_sp_rank = None
        best_kw = None
        if xydc_map and camp_kws:
            for kw_obj in camp_kws:
                kw_str = str(kw_obj.get("keyword_text") or "").strip().lower()
                r_info = xydc_map.get(kw_str) or xydc_map.get(re.sub(r"[^\w\s]", "", kw_str).strip())
                if r_info and r_info.get("sp_rank") is not None:
                    sp_r = int(r_info["sp_rank"])
                    if best_sp_rank is None or sp_r < best_sp_rank:
                        best_sp_rank = sp_r
                        best_kw = kw_str

        is_top_ad_rank = (best_sp_rank is not None and best_sp_rank <= 3) or (best_sp_rank is not None and best_sp_rank <= 5 and c.get("tos_share", 0) >= 50.0)
        is_deal_trans = bool(deal_info and deal_info.get("is_deal_transition"))

        is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
        is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
        fulfillable_cnt = num(profile.get("fulfillable")) if profile and profile.get("fulfillable") is not None else None
        dos_cnt = num(profile.get("days_of_supply")) if profile and profile.get("days_of_supply") is not None else None
        is_stockout = bool(profile and ((fulfillable_cnt is not None and fulfillable_cnt == 0) or (dos_cnt is not None and dos_cnt <= 0 and fulfillable_cnt is not None and fulfillable_cnt <= 2)))
        is_low_inventory = bool(profile and not is_stockout and ((dos_cnt is not None and 0 < dos_cnt <= 7) or (fulfillable_cnt is not None and 0 < fulfillable_cnt <= 25)))

        # 1. Stockout / Asphyxiation check
        if is_stockout:
            assessment = "STOCKOUT_SUSPENDED"
            recom = (
                "[Listing断货·系统停推] FBA可售库存已归零（fulfillable=0）；"
                "商品失去购物车并被亚马逊系统强制停止广告展示，严禁盲目加价回踩探底，等待补货入仓"
            )
        elif is_low_inventory and c["delta_imp_pct"] <= -70.0:
            assessment = "LOW_INVENTORY_THROTTLE"
            recom = (
                f"[极低库存告急·控流防断] 可售天数仅剩 {dos_cnt:.0f} 天（库存 {fulfillable_cnt:.0f} 件）；"
                "曝光下降系库存告急控流，严禁提价抢量加剧断货"
            )
        elif c["delta_imp_pct"] <= -70.0 and c["daily_imp_30d"] >= 30:
            if is_top_ad_rank:
                assessment = "TOP_POSITION_MARKET_COOLDOWN"
                c["best_sp_rank"] = best_sp_rank
                recom = (
                    f"[首屏黄金位·大盘常态冷却] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 降至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；"
                    f"西柚洞察核验核心词广告位仍高居首屏顶部（SP #{best_sp_rank}" + (f" 词: {best_kw}" if best_kw else "") + "），"
                    "严禁盲目加价或回踩探底，保持当前稳态运行"
                )
            elif is_deal_trans:
                assessment = "DEAL_TRANSITION_COOLDOWN"
                last_d = deal_info.get("last_deal_date") or "近期"
                recom = (
                    f"[秒杀后常态冷却·流量异动归因消除] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 降至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；"
                    f"商品于 {last_d} 结束促销活动，回落系促后正常均值回归，严禁误判为断流休克"
                )
            else:
                assessment = "CIRCUIT_BREAKER_SHOCK"
                baseline_cpc = c.get("cpc", 0.50)
                cur_tos = c.get("tos", 0)
                is_target_camp = any(x in c["name"].lower() for x in ["asin", "竞品", "b0", "target", "同类", "关联"]) or c.get("targeting") == "TARGETING"
                camp_floor = 0.42 if (is_target_camp and (is_fragile or is_high_refund)) else (0.36 if is_target_camp else (0.30 if (is_fragile or is_high_refund) else 0.25))
                suggested_bid = max(camp_floor, round(baseline_cpc / (1.0 + (cur_tos / 100.0)), 2))
                recom = f"[调后休克断流·立即回踩探底] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 暴跌至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；建议回提基础竞价至 ${suggested_bid:.2f} (底价保护 ${camp_floor:.2f}) 探底拉回展示池"

        # 2. Ongoing Bleed
        elif (c["spend_3d"] >= 15 and c["ord_3d"] == 0) or (c["spend"] >= 15 and c["ord"] == 0 and c["spend_7d"] >= 10):
            assessment = "CIRCUIT_BREAKER_BLEED"
            recom = f"[调后持续失血·紧急纠偏] 近3天花费 ${c['spend_3d']:.1f} 仍为 0 单 (7天花 ${c['spend_7d']:.1f})；打破观察期，削减高耗广告位溢价或下调基础竞价"
        elif c["ord_3d"] > 0 and c["spend_3d"] >= 25 and c["acos_3d"] >= (target_acos * 2.0):
            assessment = "CIRCUIT_BREAKER_BLEED"
            recom = f"[调后持续失血·ACOS爆表] 近3天 ACOS 达 {c['acos_3d']:.1f}% (目标 {target_acos:.0f}%)；打破观察期，紧急下调基础竞价防守"

        # 3. Observation Cooldown
        elif c["name"] in locked_campaigns:
            cd_match = [x for x in active_cooldowns if x.get("campaign") == c["name"]]
            detail = cd_match[0].get("detail", "Recent parameter adjustment") if cd_match else "Recent parameter adjustment"
            elapsed = cd_match[0].get("days_elapsed", 0) if cd_match else 0
            if c["delta_cpc_pct"] <= -15.0 and c["delta_imp_pct"] > -60.0:
                assessment = "ADJUSTMENT_EFFECTIVE"
                recom = f"[调后生效·见效保持] 近3天 CPC 压至 ${c['cpc_3d']:.2f} ({c['delta_cpc_pct']:+.1f}%)，曝光稳定；严禁二次砍价"
            else:
                assessment = "IN_OBSERVATION"
                recom = f"[动态观察期 Day {elapsed}/{args.cooldown_days}·正常公差] {detail}；近3天 CPC ${effective_cpc:.2f}，保持监控"

        # 4. Real Placement & Bidding Strategy Diagnostics
        elif c.get("is_up_and_down") and c["tos"] >= 60 and (c["acos"] >= target_acos * 1.25 or effective_cpc >= 1.15):
            assessment = "UP_AND_DOWN_2X_AMPLIFICATION"
            recom = (
                f"[动态提高和降低·2倍叠乘预警] 当前竞价策略为“提高和降低”且 TOS={int(c['tos'])}%，"
                f"首屏实际最高出价被放大至基础竞价的 {2.0 * (1.0 + c['tos'] / 100.0):.2f} 倍（近期CPC ${effective_cpc:.2f}, ACOS {c['acos']:.1f}%）；"
                "建议切换为“动态竞价 - 仅降低 (Down Only)”或将 TOS 压至 <=30% 消除天价点击"
            )
        elif real_tos and real_tos.get("spend", 0) >= 18.0 and (real_tos.get("orders", 0) == 0 or real_tos.get("acos", 0) >= target_acos * 1.65) and c["tos"] >= 40:
            assessment = "REAL_TOS_BLEED"
            recom = (
                f"[真实广告位核验·TOS顶展失血] 真实位置报告显示 TOS 花费 ${real_tos['spend']:.2f} (占比 {real_tos.get('spend_share', 0):.0f}%)，"
                f"出单 {real_tos.get('orders', 0)} 单，ACOS {real_tos.get('acos', 0):.1f}% 严重超标；建议精准下调 TOS 溢价控费"
            )
        elif real_pp and real_pp.get("spend", 0) >= 15.0 and (real_pp.get("orders", 0) == 0 or real_pp.get("acos", 0) >= target_acos * 1.60) and c["pp"] >= 20:
            assessment = "INEFFICIENT_PP"
            recom = (
                f"[真实广告位核验·商品页PP失血] 真实位置报告显示 PP 花费 ${real_pp['spend']:.2f}，"
                f"出单 {real_pp.get('orders', 0)} 单，ACOS {real_pp.get('acos', 0):.1f}%；建议将 PP 溢价下调至 0%~10%"
            )
        elif c["spend"] >= 60 and c["ord"] == 0 and c["tos"] >= 50:
            assessment = "DEAD_CAMPAIGN_CANDIDATE"
            recom = f"30天花费 ${c['spend']:.1f} 仍为 0 单，近7天 CPC ${effective_cpc:.2f}；建议执行归档或重建精准仓"
        elif c["tos"] >= 150 and (not real_tos or real_tos.get("acos", 999.0) > target_acos):
            assessment = "HIGH_TOS_RISK"
            recom = f"TOS {int(c['tos'])}% 虚高，近期 CPC ${effective_cpc:.2f}；建议阶梯下调 TOS 至 50-80%"
        elif c["pp"] >= 100 and (c["acos_7d"] >= (target_acos * 1.4) if c["acos_7d"] > 0 else c["acos"] >= (target_acos * 1.4)):
            current_eval_acos = c["acos_7d"] if c["acos_7d"] > 0 else c["acos"]
            assessment = "INEFFICIENT_PP"
            recom = f"商品页溢价 {int(c['pp'])}% 过高，近期 ACOS {current_eval_acos:.1f}% 严重超标；下调 PP 至 <=20%"
        elif effective_cpc >= 1.20 and c["acos"] >= target_acos:
            assessment = "HIGH_CPC_THROTTLE"
            recom = f"近期实际 CPC ${effective_cpc:.2f} 偏高 (30天均值 ${c['cpc']:.2f})；按近期成本下调基础竞价 15-20% 控费"
        elif c["spend_7d"] >= 15 and c["ord_7d"] == 0:
            assessment = "ZERO_CONVERSION_BLEED"
            recom = f"近7天花费 ${c['spend_7d']:.1f} 为 0 单；排查最新搜索词相关性，下调基础竞价或暂停低效投放"
        elif c["tos"] <= 30 and c["tos_share"] < 15 and ((real_tos and real_tos.get("orders", 0) >= 3 and real_tos.get("acos", 999) <= target_acos * 0.9) or (c["cvr_7d"] >= 15 if c["cvr_7d"] > 0 else c["cvr"] >= 15)):
            assessment = "TOS_UNDERBID"
            recom = f"顶展转化优异但顶部份额仅 {c['tos_share']:.0f}%；建议提高 TOS 至 50-70% 放大首屏单量"

        # Dayparting-Aware De-Biased Assessment Guardrail (Rule 53)
        if bool(c.get("is_apply_time")):
            if assessment in ["CIRCUIT_BREAKER_BLEED", "HIGH_CPC_THROTTLE"] and (c.get("acos_3d", 0) <= target_acos * 1.35 or c.get("acos", 0) <= target_acos * 1.25) and c.get("delta_cpc_pct", 0) <= 25.0:
                assessment = "DAYPARTING_ACTIVE_STABLE"
                timing_val_str = f"${c['timing_base_value']:.2f}" if c.get("timing_base_value") is not None else f"${c['cpc']:.2f}"
                recom = (
                    f"[领星分时调价生效·良性溢价稳态] 已开启分时调价（基准值: {timing_val_str}），"
                    f"近3天 ACOS {c['acos_3d']:.1f}% 受控，保持分时调度稳态观察"
                )
            elif assessment == "CIRCUIT_BREAKER_SHOCK":
                recom += "（注意：该活动已启用领星分时策略，请同步核查分时基准值与低谷时段折扣梯度）"

        # Append real placement & indirect halo summary note when available
        if real_p30:
            p_notes = []
            for sl_k in ("TOS", "PP", "ROS"):
                if sl_k in real_p30 and real_p30[sl_k]["spend"] > 0:
                    rp_i = real_p30[sl_k]
                    p_notes.append(f"{sl_k}:${rp_i['spend']:.0f}/{rp_i['orders']}单/ACOS {rp_i['acos']:.0f}%")
            if p_notes:
                recom += f" [真实展位30d: {' | '.join(p_notes)}]"
        if c.get("indirect_order_share", 0) >= 35.0 and c.get("ord", 0) >= 3:
            recom += f" [光环关联单占比 {c['indirect_order_share']:.0f}%：带动变体出单显著]"

        c["assessment"] = assessment
        c["recom"] = recom

        timing_str = "已生效" if c.get("is_apply_time") else "未应用"
        if c.get("is_apply_time") and c.get("timing_base_value") is not None:
            timing_str += f"(基准值:${c['timing_base_value']:.2f})"

        placement_lines.append("\t".join(str(x) for x in [
            c["name"], c["targeting"], timing_str, round(c["spend"], 2), round(c["cpc"], 2), round(c["acos"], 1),
            round(c["cpc_7d"], 2), round(c["acos_7d"], 1), round(c["cpc_3d"], 2),
            round(c["delta_cpc_pct"], 1), round(c["delta_imp_pct"], 1),
            int(c["tos"]), int(c["pp"]), int(c["ros"]),
            round(c["tos_share"], 2), assessment, recom
        ]))
    tsv(
        dest / "placement_audit.tsv",
        "campaign\ttargeting\ttiming_tactic\tspend_30d\tcpc_30d\tacos_30d\tcpc_7d\tacos_7d\tcpc_3d\tdelta_cpc_pct\tdelta_imp_pct\tTOS\tPP\tROS\ttos_share\tassessment\trecommendation",
        placement_lines,
    )


def write_keywords_tsv(
    dest, kws, sr_map, xydc_map=None, kws_7d=None, kws_3d=None,
    target_acos: float = 25.0, group_bid_lookup=None, camp_group_bid_lookup=None,
):
    """keywords.tsv -- targeted keywords with ABA searchrank, volume tier, rank synergy, and 7d/3d fresh slice metrics."""
    kw_lines = []
    xydc_map = xydc_map or {}
    kw_7d_lookup = {(k.get("campaign_name"), (k.get("match_type") or "").lower(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_7d or [])}
    kw_3d_lookup = {(k.get("campaign_name"), (k.get("match_type") or "").lower(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_3d or [])}

    for r in sorted(kws, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 1 and num(r.get("orders")) < 1:
            continue
        kw_text = r.get("keyword_text") or r.get("targeting_text") or ""
        sr, v_tier = get_searchrank(kw_text, sr_map)
        sr_display = str(sr) if sr is not None else "--"

        eff_bid, is_inh = resolve_effective_entity_bid(r, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        def_bid_disp = r.get("default_bid") if num(r.get("default_bid")) > 0 else (eff_bid if is_inh else "--")

        # Bug fix: pass portfolio target_acos (NEVER keyword's own acos!) and effective inherited bid!
        eval_res = evaluate_rank_synergy(
            term=kw_text,
            xydc_map=xydc_map,
            spend=spend,
            sales=num(r.get("sales")),
            orders=int(num(r.get("orders"))),
            clicks=int(num(r.get("clicks"))),
            target_acos=target_acos or 25.0,
            current_bid=eff_bid,
        )
        org_rank_str = str(eval_res["organic_rank"]) if eval_res["organic_rank"] is not None else "--"
        sp_rank_str = str(eval_res["sp_rank"]) if eval_res["sp_rank"] is not None else "--"

        is_timing = bool(r.get("is_apply_time"))
        t_base = f"{num(r.get('timing_base_value')):.2f}" if r.get("timing_base_value") is not None else "--"
        app_t = r.get("applied_templates") or []
        t_name = app_t[0].get("name", "") if app_t else "--"
        timing_status = "分时生效" if is_timing else "未应用"

        c_name_k = r.get("campaign_name")
        m_type_k = (r.get("match_type") or "").lower()
        txt_k = (kw_text or "").strip().lower()
        k7 = kw_7d_lookup.get((c_name_k, m_type_k, txt_k), {})
        k3 = kw_3d_lookup.get((c_name_k, m_type_k, txt_k), {})
        sp_7d = round(num(k7.get("spends") or k7.get("spend") or k7.get("cost")), 2) if k7 else 0.0
        ord_7d = int(num(k7.get("orders"))) if k7 else 0
        sp_3d = round(num(k3.get("spends") or k3.get("spend") or k3.get("cost")), 2) if k3 else 0.0
        ord_3d = int(num(k3.get("orders"))) if k3 else 0
        sales_30d = num(r.get("sales"))
        acos_clean = clean_api_acos(r.get("acos"), spend, sales_30d)
        roas_30d = calc_roas(spend, sales_30d, r.get("roas"))
        dir_ord_k = int(num(r.get("direct_orders")))
        indir_ord_k = int(num(r.get("indirect_orders")))
        tos_is_k = round(num(r.get("top_of_search_impression_share")), 2)

        kw_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("match_type"), kw_text,
            sr_display, v_tier,
            eff_bid, def_bid_disp, r.get("state"),
            timing_status, t_base, t_name,
            round(spend, 2), round(sales_30d, 2), round(acos_clean, 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))),
            round(num(r.get("cvr")), 1), round(num(r.get("cpc")), 2), round(num(r.get("ctr")), 2),
            sp_7d, ord_7d, sp_3d, ord_3d,
            org_rank_str, sp_rank_str, eval_res["synergy_tier"], eval_res["synergy_tag"],
            dir_ord_k, indir_ord_k, tos_is_k, roas_30d,
        ]))
    tsv(
        dest / "keywords.tsv",
        "campaign\tmatch\ttext\tsearchrank\tvolume_tier\tbid\tdefbid\tstate\ttiming_status\ttiming_base_value\ttiming_template\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tctr\tspend_7d\tord_7d\tspend_3d\tord_3d\torganic_rank\tsp_rank\tsynergy_tier\tsynergy_tag\tdirect_ord\tindirect_ord\ttos_share\troas",
        kw_lines,
    )


def _compute_protected_ngram_tokens(profile: dict, rules: dict) -> set[str]:
    """Tokens from product title, brand, positive IS patterns, and morphological stems that must never be treated as bleeding roots."""
    blobs = [str(profile.get("title") or "").lower(), str(profile.get("brand") or "").lower()]
    for item in (rules or {}).get("is_patterns", []):
        pat = item if isinstance(item, str) else item.get("pattern", "")
        if pat:
            blobs.append(str(pat).lower())
    tokens = set()
    for b in blobs:
        for w in re.findall(r"[a-z0-9]+", b):
            if len(w) > 2 and not w.isdigit() and w not in STOP_WORDS:
                tokens.add(w)
                if w.endswith("er") and len(w) > 4:
                    tokens.add(w[:-2])
                elif w.endswith("ers") and len(w) > 5:
                    tokens.add(w[:-3])
                elif w.endswith("s") and len(w) > 3:
                    tokens.add(w[:-1])
    # Core appliance & functional nouns that must NEVER be killed as 1-gram roots
    core_functional_nouns = {
        "grind", "grinder", "grinders", "mill", "mills", "machine", "machines",
        "coffee", "bean", "beans", "electric", "blade", "blades", "spice", "spices",
        "herb", "herbs", "seed", "seeds", "bowl", "bowls", "motor", "capacity",
        "stainless", "steel", "dry", "wet", "nut", "nuts", "grain", "grains"
    }
    tokens |= core_functional_nouns
    return tokens


def analyze_search_terms(
    asin_campaigns,
    camps,
    known_negatives,
    kws,
    kws_3d,
    kws_7d,
    profile,
    rules,
    sr_map,
    src,
    target_acos,
    xydc_map=None,
    financial_prof=None,
    traffic_prof=None,
    targets=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    macro_scorecard=None,
    entity_memory=None,
):
    """The core pass: aggregate queries by (campaign, query), pre-compute bleeding N-gram veto,
    classify queries & ASINs cleanly, enforce 2-stage migration soft handoff, and build raw adjustments.
    """
    sts = [r for r in as_rows(load_json(src / "search_terms.json")) if r.get("query") or r.get("campaign_name")]
    if asin_campaigns:
        sts = [r for r in sts if r.get("campaign_name") in asin_campaigns]

    sts_7d_raw = load_json(src / "search_terms_7d.json")
    sts_7d = [r for r in as_rows(sts_7d_raw) if r.get("query") or r.get("campaign_name")] if sts_7d_raw else []
    if asin_campaigns and sts_7d:
        sts_7d = [r for r in sts_7d if r.get("campaign_name") in asin_campaigns]

    st_7d_map = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "impressions": 0})
    for r7 in sts_7d:
        q7 = (r7.get("query") or "").strip().lower()
        c7 = r7.get("campaign_name") or ""
        if q7 and c7:
            slot7 = st_7d_map[(c7, q7)]
            slot7["spend"] += num(r7.get("spends") or r7.get("spend") or r7.get("cost"))
            slot7["sales"] += num(r7.get("sales"))
            slot7["orders"] += int(num(r7.get("orders")))
            slot7["clicks"] += int(num(r7.get("clicks")))
            slot7["impressions"] += int(num(r7.get("impressions")))

    if group_bid_lookup is None or camp_group_bid_lookup is None:
        group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(src, asin_campaigns)

    active_neg_dict, active_neg_camp_pairs, all_neg_queries = extract_active_negatives(sts)
    audit_records, unnegative_adjustments = audit_active_negatives(
        active_neg_dict, profile, rules, sr_map, kws=kws, camps=camps, xydc_map=xydc_map,
        group_bid_lookup=group_bid_lookup, camp_group_bid_lookup=camp_group_bid_lookup,
    )
    kw_misfit_adjustments = audit_keyword_misfits(
        kws, rules, camps=camps, group_bid_lookup=group_bid_lookup, camp_group_bid_lookup=camp_group_bid_lookup,
    )

    bucket_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0})
    ngram_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0, "queries": set(), "gram_type": "1-gram"})
    cannibal_map = defaultdict(list)

    st_lines = []
    waste = []
    harvest = []
    bulk_adjustments = []

    shrinkage_guard = bool(macro_scorecard and macro_scorecard.get("velocity_shrinkage_guard"))

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

    if group_bid_lookup is None or camp_group_bid_lookup is None:
        group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(src, asin_campaigns)

    camp_lookup = {c["name"]: c for c in camps if c.get("name")}

    # Build lookups for placement fingerprinting, fresh slice verification, and exact-keyword presence
    kw_lookup = {}
    enabled_exact_by_text = defaultdict(list)
    paused_exact_by_camp_text = {}
    for k in kws:
        c_name_k = k.get("campaign_name") or ""
        text_k = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        match_k = (k.get("match_type") or "").lower().strip()
        if not text_k:
            continue
        key_k = f"{c_name_k}::{match_k}::{text_k}"
        k7 = kw_7d_map.get((c_name_k, match_k, text_k), {})
        k3 = kw_3d_map.get((c_name_k, match_k, text_k), {})

        eff_k_bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.30)
        current_bid = num(k3.get("bid") or k7.get("bid")) or eff_k_bid
        current_state = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()

        imp_30d = int(num(k.get("impressions")))
        clk_30d = int(num(k.get("clicks")))
        spend_30d = num(k.get("cost") or k.get("spend") or k.get("spends"))
        sales_30d = num(k.get("sales"))
        ord_30d = int(num(k.get("orders")))
        cvr_30d = round((ord_30d / clk_30d * 100.0) if clk_30d > 0 else num(k.get("cvr")), 2)
        acos_30d = round((spend_30d / sales_30d * 100.0) if sales_30d > 0 else (999.0 if spend_30d > 0 else 0.0), 1)
        cpc_30d = round((spend_30d / clk_30d) if clk_30d > 0 else num(k.get("cpc")), 2)

        imp_7d = int(num(k7.get("impressions"))) if k7 else 0
        clk_7d = int(num(k7.get("clicks"))) if k7 else 0
        spend_7d = num(k7.get("cost") or k7.get("spend") or k7.get("spends")) if k7 else 0.0
        sales_7d = num(k7.get("sales")) if k7 else 0.0
        ord_7d = int(num(k7.get("orders"))) if k7 else 0
        cvr_7d = round((ord_7d / clk_7d * 100.0) if clk_7d > 0 else 0.0, 2)
        acos_7d = round((spend_7d / sales_7d * 100.0) if sales_7d > 0 else (999.0 if spend_7d > 0 else 0.0), 1)
        cpc_7d = round((spend_7d / clk_7d) if clk_7d > 0 else 0.0, 2)

        imp_3d = int(num(k3.get("impressions"))) if k3 else 0
        clk_3d = int(num(k3.get("clicks"))) if k3 else 0
        spend_3d = num(k3.get("cost") or k3.get("spend") or k3.get("spends")) if k3 else 0.0
        sales_3d = num(k3.get("sales")) if k3 else 0.0
        ord_3d = int(num(k3.get("orders"))) if k3 else 0
        cvr_3d = round((ord_3d / clk_3d * 100.0) if clk_3d > 0 else 0.0, 2)
        acos_3d = round((spend_3d / sales_3d * 100.0) if sales_3d > 0 else (999.0 if spend_3d > 0 else 0.0), 1)
        cpc_3d = round((spend_3d / clk_3d) if clk_3d > 0 else 0.0, 2)

        has_zero_recent_traffic = (
            (imp_3d <= 25 and spend_3d == 0.0)
            or (imp_7d == 0 and spend_7d == 0.0)
            or (imp_3d == 0 and spend_3d == 0.0)
        )

        has_active_bleeding = (
            (spend_3d >= 1.5 and ord_3d == 0)
            or (spend_7d >= 3.0 and ord_7d == 0 and spend_3d > 0.5)
            or (clk_3d >= 2 and ord_3d == 0 and spend_3d > 0.5)
        )

        is_timing = bool(k.get("is_apply_time"))
        t_base = num(k.get("timing_base_value")) if k.get("timing_base_value") is not None else None
        app_t = k.get("applied_templates") or []
        t_name = app_t[0].get("name", "") if app_t else ""
        t_eff = app_t[0].get("effective_time", {}).get("from", "") if app_t else ""

        entry_k = {
            "campaign": c_name_k,
            "match": match_k,
            "text": text_k,
            "bid": current_bid,
            "cpc": num(k.get("cpc")),
            "state": current_state,
            "imp_30d": imp_30d, "clk_30d": clk_30d, "spend_30d": spend_30d, "sales_30d": sales_30d, "ord_30d": ord_30d, "cvr_30d": cvr_30d, "acos_30d": acos_30d, "cpc_30d": cpc_30d,
            "imp_7d": imp_7d, "clk_7d": clk_7d, "spend_7d": spend_7d, "sales_7d": sales_7d, "ord_7d": ord_7d, "cvr_7d": cvr_7d, "acos_7d": acos_7d, "cpc_7d": cpc_7d,
            "imp_3d": imp_3d, "clk_3d": clk_3d, "spend_3d": spend_3d, "sales_3d": sales_3d, "ord_3d": ord_3d, "cvr_3d": cvr_3d, "acos_3d": acos_3d, "cpc_3d": cpc_3d,
            "orders": ord_30d, "cvr": cvr_30d, "acos": acos_30d, "spends": spend_30d, "sales": sales_30d, "clicks": clk_30d,
            "has_zero_recent_traffic": has_zero_recent_traffic,
            "has_active_bleeding": has_active_bleeding,
            "is_apply_time": is_timing,
            "timing_base_value": t_base,
            "timing_template": t_name,
            "timing_effective_from": t_eff,
        }
        kw_lookup[key_k] = entry_k
        if match_k == "exact":
            camp_state_k = (camp_lookup.get(c_name_k) or {}).get("state", "enabled")
            if current_state == "enabled" and camp_state_k == "enabled":
                enabled_exact_by_text[text_k].append(entry_k)
            elif current_state == "paused" and camp_state_k == "enabled":
                paused_exact_by_camp_text[(c_name_k, text_k)] = entry_k

    # Track explicit ASIN targets so search_terms never double-processes or mis-classifies ASIN targets
    explicit_asin_targets_by_camp = set()
    enabled_asin_target_any = set()
    enabled_asin_campaigns = []
    for c in camps:
        if c.get("state") == "enabled":
            cn_low = (c.get("name") or "").lower()
            if any(w in cn_low for w in ("asin", "竞品", "target", "同类", "关联")) or str(c.get("targeting", "")).lower() in ("targets", "targeting"):
                enabled_asin_campaigns.append(c["name"])
    for t_row in (targets or []):
        c_info_t = extract_asin_target_info(t_row, group_bid_lookup, camp_group_bid_lookup)
        if c_info_t and c_info_t.get("asin"):
            asin_low = c_info_t["asin"].lower()
            cn_t = c_info_t.get("campaign_name") or ""
            explicit_asin_targets_by_camp.add((cn_t, asin_low))
            t_st = str(t_row.get("state") or "enabled").lower().strip()
            if t_st == "enabled" and (camp_lookup.get(cn_t) or {}).get("state") == "enabled":
                enabled_asin_target_any.add(asin_low)
                if cn_t not in enabled_asin_campaigns:
                    enabled_asin_campaigns.append(cn_t)

    # Step 1: Aggregate search_terms rows by (campaign_name, query_lower) so multi-adgroup / multi-keyword
    # rows inside the same campaign are evaluated on true campaign-level totals without self-contradiction.
    agg_sts_map = {}
    for r in sts:
        q_raw = (r.get("query") or "").strip()
        c_name = r.get("campaign_name") or ""
        if not q_raw or not c_name:
            continue
        ql = q_raw.lower()
        sp_r = num(r.get("spends"))
        sl_r = num(r.get("sales"))
        od_r = num(r.get("orders"))
        ck_r = num(r.get("clicks"))
        im_r = int(num(r.get("impressions")))
        mt_r = r.get("match_type") or r.get("target_match_type") or ""
        tt_r = r.get("keyword_text") or r.get("target_text") or ""
        akey = (c_name, ql)
        if akey not in agg_sts_map:
            agg_sts_map[akey] = {
                "query": q_raw,
                "ql": ql,
                "campaign_name": c_name,
                "spends": sp_r,
                "sales": sl_r,
                "orders": od_r,
                "clicks": ck_r,
                "impressions": im_r,
                "match_type": mt_r,
                "target_text": tt_r,
                "_top_row_spend": sp_r,
            }
        else:
            slot = agg_sts_map[akey]
            slot["spends"] += sp_r
            slot["sales"] += sl_r
            slot["orders"] += od_r
            slot["clicks"] += ck_r
            slot["impressions"] += im_r
            if sp_r > slot["_top_row_spend"]:
                slot["_top_row_spend"] = sp_r
                slot["match_type"] = mt_r
                slot["target_text"] = tt_r

    agg_sts = sorted(agg_sts_map.values(), key=lambda x: -x["spends"])

    # Step 2: Pre-compute bucket_stats, ngram_stats, cannibal_map, and bleeding_ng_set BEFORE placement diagnosis
    # so Rule 56 bleeding N-gram veto actively blocks boosting/harvesting 0-order bleeding root queries!
    for r in agg_sts:
        q = r["query"]
        ql = r["ql"]
        spend = r["spends"]
        sales = r["sales"]
        ordn = r["orders"]
        clk = r["clicks"]
        c_name = r["campaign_name"]

        bucket, match_reason, is_strong_relevance = classify_query(q, profile, rules)
        r["_bucket"] = bucket
        r["_is_strong"] = is_strong_relevance

        bucket_stats[bucket]["spend"] += spend
        bucket_stats[bucket]["sales"] += sales
        bucket_stats[bucket]["ord"] += ordn
        bucket_stats[bucket]["clk"] += clk

        if not is_asin_term(q):
            for ng, gtype in extract_ngrams(q):
                ngram_stats[ng]["spend"] += spend
                ngram_stats[ng]["sales"] += sales
                ngram_stats[ng]["ord"] += ordn
                ngram_stats[ng]["clk"] += clk
                ngram_stats[ng]["queries"].add(ql)
                ngram_stats[ng]["gram_type"] = gtype

        cannibal_map[ql].append({
            "campaign": c_name,
            "match": r["match_type"],
            "target": r["target_text"],
            "spend": spend,
            "sales": sales,
            "orders": ordn,
            "clicks": clk,
            "cpc": round(spend / clk, 2) if clk > 0 else 0.0,
        })

    protected_ng_tokens = _compute_protected_ngram_tokens(profile, rules)
    bleeding_ng_set: set[str] = set()
    for ng, stat in ngram_stats.items():
        if stat["spend"] >= 10.0 and stat["ord"] == 0 and stat["clk"] >= 8 and len(stat["queries"]) >= 2:
            _, _, ng_strong = classify_query(ng, profile, rules)
            ng_tokens = {t for t in re.findall(r"[a-z0-9]+", ng.lower()) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}
            if ng_strong or not ng_tokens:
                continue
            if stat["gram_type"] == "1-gram" and (ng_tokens & protected_ng_tokens):
                continue
            if stat["gram_type"] != "1-gram" and ng_tokens.issubset(protected_ng_tokens):
                continue
            bleeding_ng_set.add(ng)

    harvest_camp = find_exact_harvest_campaign(camps, kws)
    harvest_asin_camp = enabled_asin_campaigns[0] if enabled_asin_campaigns else ""
    placement_diagnoses = []

    # Step 3: Evaluate each aggregated (campaign, query) with full placement, rank, 7d freshness, and 2-stage migration awareness
    for r in agg_sts:
        q = r["query"]
        ql = r["ql"]
        spend = r["spends"]
        sales = r["sales"]
        ordn = r["orders"]
        clk = r["clicks"]
        imp_cnt = r["impressions"]
        c_name = r["campaign_name"]
        match_type = r["match_type"]
        target_text = r["target_text"]
        bucket = r["_bucket"]
        is_strong_relevance = r["_is_strong"]

        sr, v_tier = get_searchrank(q, sr_map)
        sr_display = str(sr) if sr is not None else "--"
        st7 = st_7d_map.get((c_name, ql), {})
        st7_spend = st7.get("spend", 0.0)
        st7_sales = st7.get("sales", 0.0)
        st7_ord = st7.get("orders", 0)
        st7_acos = acos(st7_spend, st7_sales) if st7_sales > 0 else (999.0 if st7_spend > 0 else 0.0)

        eval_st = evaluate_rank_synergy(
            term=q,
            xydc_map=xydc_map or {},
            spend=spend,
            sales=sales,
            orders=int(ordn),
            clicks=int(clk),
            target_acos=target_acos or 25.0,
        )
        st_org_rank = eval_st.get("organic_rank")
        st_sp_rank = eval_st.get("sp_rank")
        st_org_rank_str = str(st_org_rank) if st_org_rank is not None else "--"
        st_sp_rank_str = str(st_sp_rank) if st_sp_rank is not None else "--"
        if st_org_rank is not None or st_sp_rank is not None:
            rank_tag = f"[自然位#{st_org_rank_str}·广告位#{st_sp_rank_str} {eval_st.get('synergy_tag', '')}] "
            rank_cur_desc = f"(自然位#{st_org_rank_str}, 广告位#{st_sp_rank_str}) "
        else:
            rank_tag = "[西柚未建位/暂无排名] "
            rank_cur_desc = "(西柚未建位) "

        row_cpc = round(spend / clk, 2) if clk > 0 else 0.0
        row_cvr = round(ordn / clk * 100.0, 1) if clk > 0 else 0.0
        row_acos = round(acos(spend, sales), 1)
        if spend >= 3 or ordn >= 1:
            st_lines.append("\t".join(str(x) for x in [
                bucket, v_tier, sr_display, c_name, q, match_type, target_text,
                round(spend, 2), round(sales, 2), row_acos,
                int(ordn), int(clk), row_cvr, row_cpc,
                st_org_rank_str, st_sp_rank_str, eval_st["synergy_tier"],
            ]))

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
            bleeding_ng_set=bleeding_ng_set,
        )
        if spend >= 3 or ordn >= 1 or clk >= 5:
            placement_diagnoses.append(diag)

        camp_obj = camp_lookup.get(c_name, {})
        if camp_obj.get("state", "enabled") != "enabled":
            continue

        is_already_negated = (
            (c_name.lower(), ql) in active_neg_camp_pairs
            or is_known_negative(q, known_negatives)
        )

        # ---- Branch A: ASIN Customer Queries (Strictly Product-Targeting Domain, NEVER Keyword Domain) ----
        if is_asin_term(q):
            asin_upper = q.upper()
            # Skip if already an explicit asinSameAs target in this campaign (handled by write_targets_tsv with full competitor profile)
            if (c_name, ql) in explicit_asin_targets_by_camp:
                continue
            if not is_already_negated and ordn == 0 and (clk >= 8 or spend >= 6.0):
                waste.append(f"{asin_upper}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag=BLEEDING_ASIN_QUERY\taction=Negative_Target")
                bulk_adjustments.append({
                    "action_type": "Negative_Target",
                    "campaign": c_name,
                    "target_entity": asin_upper,
                    "match_type": "asinSameAs",
                    "current_value": f"竞品详情页搜索流量 (触发: {target_text} [{match_type}]), 30d花费=${spend:.2f}, {int(clk)}点击0单",
                    "recommended_value": f"Add Negative Target (asinSameAs={asin_upper})",
                    "reason": f"[竞品ASIN商品投放否定] 自动/类目活动跑出低效竞品详情页 {asin_upper}，累计 {int(clk)} 点击 0 订单（花费 ${spend:.2f}），执行商品投放否定（Negative Target）止血。",
                })
            elif ordn >= 2 and acos(spend, sales) <= target_acos and spend >= 6.0 and ql not in enabled_asin_target_any:
                dest_asin_camp = harvest_asin_camp or c_name
                if (camp_lookup.get(dest_asin_camp) or {}).get("state", "enabled") == "enabled":
                    dest_pp = num((camp_lookup.get(dest_asin_camp) or {}).get("pp", 0))
                    asin_bid = max(0.36, min(1.10, round((row_cpc or 0.55) / (1.0 + dest_pp / 100.0), 2)))
                    harvest.append(f"{asin_upper}\t{c_name}\tspend={spend:.2f}\tacos={row_acos:.1f}\tord={int(ordn)}\tbucket=asin\ttier=ASIN_TARGET")
                    bulk_adjustments.append({
                        "action_type": "Harvest_ASIN",
                        "campaign": dest_asin_camp,
                        "target_entity": asin_upper,
                        "match_type": "asinSameAs",
                        "current_value": f"自动/泛流跑出高转化竞品页 {asin_upper} in {c_name}: {int(ordn)}单, ACOS={row_acos:.1f}%, CPC=${row_cpc:.2f}",
                        "recommended_value": f"${asin_bid:.2f}",
                        "reason": f"[高转化竞品ASIN定向收割] 在 {c_name} 跑出 {int(ordn)} 单 (ACOS {row_acos:.1f}%)，作为商品投放 (asinSameAs) 移入 {dest_asin_camp} 独立出价 ${asin_bid:.2f} 精准拦截。",
                    })
            continue

        # ---- Branch B: 2-Stage Soft-Handoff Migration Closure Check ----
        # If q is ALREADY an enabled Exact keyword in ANOTHER enabled campaign, NEVER re-harvest it;
        # instead check if Stage-2 Negative_Exact isolation in c_name (Broad/Auto/Phrase) is now due!
        other_exact_entries = [e for e in enabled_exact_by_text.get(ql, []) if e["campaign"] != c_name]
        c_is_exact_camp = ("exact" in c_name.lower() or "精准" in c_name)
        matching_kw_matches = [m for m in ("exact", "phrase", "broad") if f"{c_name}::{m}::{ql}" in kw_lookup]
        is_targeted_kw = bool(matching_kw_matches)

        if other_exact_entries and not c_is_exact_camp and not is_targeted_kw and not is_already_negated:
            best_ex = max(other_exact_entries, key=lambda e: (e["ord_30d"], e["imp_7d"], e["imp_30d"]))
            exact_is_delivering = (best_ex["imp_7d"] >= 10 or best_ex["imp_30d"] >= 30 or best_ex["ord_30d"] >= 1)
            broad_bleeding_or_cannibalizing = (
                (ordn == 0 and (spend >= 5.0 or clk >= 6))
                or (ordn >= 1 and acos(spend, sales) > target_acos * 1.25)
                or (spend >= 8.0 and best_ex["ord_30d"] >= 1 and row_cpc >= max(0.01, best_ex["cpc_30d"]))
            )
            if exact_is_delivering and broad_bleeding_or_cannibalizing:
                bulk_adjustments.append({
                    "action_type": "Negative_Exact",
                    "campaign": c_name,
                    "target_entity": q,
                    "match_type": "negativeExact",
                    "current_value": f"买家搜索词 {rank_cur_desc}在 {c_name} 花费=${spend:.2f}({int(ordn)}单, CPC=${row_cpc:.2f}) | 已在精准仓 {best_ex['campaign']} 承接(7d曝光={best_ex['imp_7d']}, 30d={best_ex['ord_30d']}单)",
                    "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                    "reason": f"{rank_tag}[移词二阶段闭环隔离] 该词已在精准仓 ({best_ex['campaign']}) 稳定承接流量（7天曝光 {best_ex['imp_7d']}，30天 {best_ex['ord_30d']} 单）；原宽泛/自动活动 ({c_name}) 继续分流内耗（花费 ${spend:.2f}，{int(ordn)} 单），执行二阶段精准否定完成流量收口。",
                })
                continue

        # ---- Branch C: Placement-Based Dynamic Adjustment ----
        if diag.get("action_type") and not is_already_negated:
            action_code = diag["action_type"]

            if is_targeted_kw:
                actual_match = matching_kw_matches[0]
                kw_info = kw_lookup[f"{c_name}::{actual_match}::{ql}"]
                actual_base_bid = kw_info["bid"]
                kw_state = kw_info["state"]

                if kw_state != "enabled":
                    continue

                if action_code in ("Throttle_TOS_Bid", "Lower_Bid"):
                    kw_ord = int(kw_info.get("ord_30d", 0))
                    kw_ord_7d = int(kw_info.get("ord_7d", 0))
                    kw_acos_7d = float(kw_info.get("acos_7d", 0.0))
                    kw_ord_3d = int(kw_info.get("ord_3d", 0))
                    kw_sp_3d = float(kw_info.get("spend_3d", 0.0))
                    kw_imp_3d = int(kw_info.get("imp_3d", 0))
                    kw_acos_3d = float(kw_info.get("acos_3d", 0.0))

                    # Rule 24 Hard Guard: Active bleeding/active spend is a STRICT PREREQUISITE for cutting bids!
                    # If recent 3d spend is $0.00, or recent 3d impressions have collapsed (<= 25), NEVER cut bids purely on stale 30d cumulative numbers!
                    if kw_info["has_zero_recent_traffic"] or kw_sp_3d == 0.0 or kw_imp_3d <= 25:
                        continue

                    # Velocity-Shrinkage Guard: NEVER cut bids on converting keywords when macro sales velocity collapsed!
                    if shrinkage_guard and (kw_ord >= 1 or kw_ord_7d >= 1 or kw_ord_3d >= 1):
                        continue

                    # 7d Freshness Guard: If keyword already recovered in recent 7d with healthy ACOS, do NOT cut on stale 30d ACOS!
                    if kw_ord_7d >= 2 and 0 < kw_acos_7d <= target_acos * 1.05 and (kw_ord_3d > 0 or kw_sp_3d < 5.0):
                        continue

                    is_fragile = (traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE") or (financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
                    bid_floor = 0.30 if is_fragile else 0.25
                    if actual_base_bid <= bid_floor:
                        continue

                    kw_cvr = float(kw_info.get("cvr_30d", 0.0))
                    kw_acos = float(kw_info.get("acos_30d", 0.0))
                    kw_sp_30d = float(kw_info.get("spend_30d", 0.0))
                    kw_clk_30d = int(kw_info.get("clk_30d", 0))
                    kw_sp_7d = float(kw_info.get("spend_7d", 0.0))
                    kw_cpc = float(kw_info.get("cpc_30d", 0.0)) or float(kw_info.get("cpc", 0.0)) or float(diag.get("cpc", 0.0))
                    avg_p = num(financial_prof.get("avg_price", 25.0)) if financial_prof else 25.0
                    tos_pct = num(camp_obj.get("tos", 0))

                    if kw_ord >= 2 and kw_acos <= target_acos and (kw_ord_3d > 0 or kw_sp_3d < 5.0):
                        continue

                    is_over_acos = (
                        (kw_ord >= 1 and kw_acos > target_acos and kw_sp_3d >= 1.5)
                        or (kw_sp_3d >= 5.0 and kw_ord_3d > 0 and kw_acos_3d > target_acos * 1.2)
                    )
                    if not kw_info["has_active_bleeding"] and not is_over_acos:
                        continue

                    timing_desc = ""
                    if kw_info.get("is_apply_time"):
                        t_name_disp = kw_info.get("timing_template") or "未命名"
                        t_base_val = kw_info.get("timing_base_value")
                        t_base_str = f"${t_base_val:.2f}" if t_base_val is not None else "未设"
                        timing_desc = f" [领星分时策略运行中: 模板'{t_name_disp}', 当前分时基准值:{t_base_str}]"

                    if kw_ord >= 1 and kw_cvr > 0 and avg_p > 0:
                        target_cpc = (target_acos / 100.0) * avg_p * (kw_cvr / 100.0)
                        target_base_bid = round(target_cpc / (1.0 + (tos_pct / 100.0)), 2)
                        # Pillar Converting Keyword Protection: on fragile listings or high-order pillar keywords (>=6 orders)
                        # with moderate ACOS (<1.45x target), cap single-step reduction at 10% and protect >=85% of clearing CPC
                        is_pillar_kw = (kw_ord >= 6 and kw_acos <= max(target_acos * 1.45, 36.0) and (kw_ord_7d >= 1 or kw_ord_3d >= 1))
                        if is_fragile and is_pillar_kw:
                            max_drop = round(actual_base_bid * 0.90, 2)
                            min_drop = round(actual_base_bid * 0.92, 2)
                        else:
                            max_drop = round(actual_base_bid * (0.85 if is_pillar_kw else 0.80), 2)
                            min_drop = round(actual_base_bid * 0.90, 2)
                        auction_safe_base = round((kw_cpc * 0.85) / (1.0 + (tos_pct / 100.0)), 2) if (kw_cpc > 0 and is_pillar_kw) else bid_floor
                        eff_floor = max(bid_floor, min(min_drop, auction_safe_base))
                        new_bid = max(eff_floor, min(min_drop, max(max_drop, target_base_bid)))
                        if new_bid >= actual_base_bid:
                            new_bid = max(bid_floor, round(actual_base_bid - 0.02, 2))
                        if new_bid >= actual_base_bid:
                            continue
                        drop_pct = int(round((actual_base_bid - new_bid) / actual_base_bid * 100))

                        slice_str = f"，7d:${kw_sp_7d:.1f}/{kw_ord_7d}单，3d:${kw_sp_3d:.1f}/{kw_ord_3d}单"
                        pillar_note = "（脆弱品核心出单支柱词单次限降<=10%防断流）" if (is_fragile and is_pillar_kw) else ""
                        reason_msg = f"{rank_tag}投放词近期投产超标（30d:${kw_sp_30d:.2f}/{kw_ord}单/ACOS {kw_acos:.1f}%{slice_str}），依保本ACOS与转化率({kw_cvr:.1f}%)倒推保本基础竞价(${target_base_bid:.2f})，微调 {drop_pct}% 至 ${new_bid:.2f} 防断流控费{pillar_note}。"
                        current_val_str = f"投放词 {rank_cur_desc}(当前基础竞价 ${actual_base_bid:.2f}){timing_desc}, 30d=${kw_sp_30d:.2f}({kw_ord}单, ACOS={kw_acos:.1f}%), 7d=${kw_sp_7d:.2f}({kw_ord_7d}单), 3d=${kw_sp_3d:.2f}({kw_ord_3d}单), CPC=${kw_cpc:.2f} [{diag['inferred_placement']}]"
                    else:
                        is_severe = (kw_sp_3d >= 10.0 or kw_sp_30d >= (avg_p * 1.5))
                        if shrinkage_guard:
                            drop_pct = 0.90
                            drop_label = "10%(防缩量限幅)"
                        else:
                            drop_pct = 0.80 if is_severe else 0.85
                            drop_label = "20%" if is_severe else "15%"
                        new_bid = max(bid_floor, round(actual_base_bid * drop_pct, 2))
                        if new_bid >= actual_base_bid:
                            continue
                        clk_disp = kw_clk_30d if kw_clk_30d > 0 else int(clk)
                        reason_msg = f"{rank_tag}投放关键词近期活跃流血（30d:${kw_sp_30d:.2f}/{clk_disp}点击0单，7d:${kw_sp_7d:.2f}/0单，3d:${kw_sp_3d:.2f}/0单），阶梯微降基础竞价 {drop_label} 至 ${new_bid:.2f} 防守避损。"
                        current_val_str = f"投放词 {rank_cur_desc}(当前基础竞价 ${actual_base_bid:.2f}){timing_desc}, 30d=${kw_sp_30d:.2f}({clk_disp}点击0单), 7d=${kw_sp_7d:.2f}(0单), 3d=${kw_sp_3d:.2f}(0单), CPC=${kw_cpc:.2f} [{diag['inferred_placement']}]"

                    timing_rec = ""
                    if kw_info.get("is_apply_time"):
                        t_name_disp = kw_info.get("timing_template") or "未命名"
                        timing_rec = f" 【领星分时联动提示】：已绑定分时模板'{t_name_disp}'，请同步将领星分时基准值(timing_base_value)更新为 ${new_bid:.2f}。"

                    waste.append(f"{q}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag={diag['strategy_tag']}\taction={action_code}")
                    bulk_adjustments.append({
                        "action_type": "Throttle_TOS_Bid",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": actual_match,
                        "current_bid_num": actual_base_bid,
                        "current_value": current_val_str,
                        "recommended_value": f"${new_bid:.2f}",
                        "reason": f"{reason_msg}{timing_rec}",
                    })
                elif action_code == "Elevate_To_TOS":
                    # Never boost a targeted keyword if it has active 3d heavy bleed or bleeding n-gram root
                    if kw_info["spend_3d"] >= 6.0 and kw_info["ord_3d"] == 0:
                        continue
                    new_bid = round(actual_base_bid * 1.15, 2)
                    timing_desc = ""
                    timing_rec = ""
                    if kw_info.get("is_apply_time"):
                        t_name_disp = kw_info.get("timing_template") or "未命名"
                        t_base_val = kw_info.get("timing_base_value")
                        t_base_str = f"${t_base_val:.2f}" if t_base_val is not None else "未设"
                        timing_desc = f" [领星分时策略运行中: 模板'{t_name_disp}', 当前分时基准值:{t_base_str}]"
                        timing_rec = f" 【领星分时联动提示】：已绑定分时模板'{t_name_disp}'，建议同步更新领星分时基准值(timing_base_value)为 ${new_bid:.2f}。"

                    bulk_adjustments.append({
                        "action_type": "Elevate_To_TOS",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": actual_match,
                        "current_bid_num": actual_base_bid,
                        "current_value": f"投放词 {rank_cur_desc}(当前基础竞价 ${actual_base_bid:.2f}){timing_desc}, 30d=${kw_info['spend_30d']:.2f}({kw_info['clk_30d']}点击), CTR={diag['ctr']:.2f}% [{diag['inferred_placement']}]",
                        "recommended_value": f"${new_bid:.2f}",
                        "reason": f"{rank_tag}核心投放词在详情页/底部位受抑，基础竞价提至 ${new_bid:.2f} 专打首屏黄金位测试真实转化力。{timing_rec}",
                    })
                elif action_code == "Negative_Exact":
                    # If q is an explicit targeted keyword in c_name and is a hard misfit, Pause_Keyword handles it cleanly;
                    # only emit Negative_Exact if it's broad/phrase matching an unwanted query.
                    if actual_match in ("broad", "phrase"):
                        bulk_adjustments.append({
                            "action_type": "Negative_Exact",
                            "campaign": c_name,
                            "target_entity": q,
                            "match_type": "negativeExact",
                            "current_bid_num": actual_base_bid,
                            "current_value": f"投放词触发 {rank_cur_desc}30d=${kw_info['spend_30d']:.2f}, 点击={kw_info['clk_30d']}, 0订单",
                            "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                            "reason": f"{rank_tag}{diag['diagnostic_reason']}",
                        })
            else:
                # q is NOT a targeted keyword in c_name; it is a customer query triggered by broad/auto/phrase
                already_in_exact_anywhere = bool(enabled_exact_by_text.get(ql))
                harvest_camp_obj = camp_lookup.get(harvest_camp, {})
                can_harvest_to_exact = (
                    harvest_camp_obj.get("state", "enabled") == "enabled"
                    and c_name != harvest_camp
                    and not c_is_exact_camp
                    and not already_in_exact_anywhere
                    and (harvest_camp, ql) not in paused_exact_by_camp_text
                )

                if action_code in ("Throttle_TOS_Bid", "Lower_Bid", "Negative_Exact"):
                    # Rule 31 & Rule 24 Hard Guard:
                    # NEVER negate a converting search term (orders >= 1)!
                    # Converting customer search terms enjoy highest immunity and must never be labeled as '0单流血'.
                    # They are evaluated for winner harvesting in Branch D.
                    if ordn >= 1:
                        continue

                    # 7d Freshness Guard: Do not negate a query that started converting profitably in the last 7d!
                    if st7_ord >= 1 and st7_acos <= target_acos:
                        continue

                    is_core_hero = (bucket in ("brand", "core", "core_feature", "core_or_title") or is_strong_relevance) and diag.get("strategy_tag") != "BLEEDING_NGRAM_VETO"
                    has_proven_organic_rank = (st_org_rank is not None and 1 <= st_org_rank <= 25)
                    if is_core_hero and action_code == "Throttle_TOS_Bid":
                        # Bug fix: NEVER harvest into c_name (the Broad/Auto campaign!), ONLY into harvest_camp,
                        # and ONLY if q has proven organic rank (<=25) and is not already in an Exact campaign!
                        if has_proven_organic_rank and can_harvest_to_exact:
                            target_cpc = diag.get("cpc", 0.0) or (spend / max(1, clk))
                            dest_tos = num(harvest_camp_obj.get("tos", 0))
                            defensive_bid = max(0.25, round((target_cpc * 0.75) / (1.0 + dest_tos / 100.0), 2))
                            bulk_adjustments.append({
                                "action_type": "Harvest_To_Exact",
                                "campaign": harvest_camp,
                                "target_entity": q,
                                "match_type": "exact",
                                "current_bid_num": defensive_bid,
                                "current_value": f"核心有排名搜索词 {rank_cur_desc}in {c_name} (触发: {target_text}), 30d=${spend:.2f}, {int(clk)}点击{int(ordn)}单 [{diag['inferred_placement']}]",
                                "recommended_value": f"${defensive_bid:.2f}",
                                "reason": f"{rank_tag}自然位前25核心主粮词在宽泛仓高CPC耗损；移入专用精准仓 ({harvest_camp}) 以防守基础竞价 ${defensive_bid:.2f} 独立控费（软交接第1阶段：原仓观察3-5天后再隔离）。",
                            })
                            continue
                        # If unranked 0-order query with mild clicks (<16), hold off from hard negating core phrase prematurely
                        if not has_proven_organic_rank and clk < 16 and spend < 14.0:
                            continue

                    waste.append(f"{q}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag={diag['strategy_tag']}\taction=Negative_Exact")
                    bulk_adjustments.append({
                        "action_type": "Negative_Exact",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": "negativeExact",
                        "current_value": f"买家搜索词 {rank_cur_desc}(触发: {target_text} [{match_type}]), 30d=${spend:.2f}, {int(clk)}点击{int(ordn)}单 [{diag['inferred_placement']}]",
                        "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                        "reason": f"{rank_tag}非独立投放词由宽泛/自动匹配触发，累计 {int(clk)} 点击 {int(ordn)} 订单（花费 ${spend:.2f}），在活动 {c_name} 中精准否定阻断无效出血。",
                    })
                elif action_code in ("Elevate_To_TOS", "Refine_Bid_Placement"):
                    # Tightened Harvest Gate: NEVER spam 0-order unranked queries into Harvest_To_Exact!
                    # Require either >=1 profitable order OR proven organic rank <=20 on a strongly relevant term.
                    if bucket in ("is_not", "competitor_brand", "adjacent") or str(bucket).startswith("is_not"):
                        continue
                    qualifies_trapped_hero = (
                        (ordn >= 1 and acos(spend, sales) <= target_acos * 1.15)
                        or (st_org_rank is not None and 1 <= st_org_rank <= 20 and is_strong_relevance and action_code == "Elevate_To_TOS")
                    )
                    if qualifies_trapped_hero and can_harvest_to_exact:
                        target_cpc = diag.get("cpc", 0.0) or (spend / max(1, clk))
                        recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                            {"cpc": target_cpc, "spend": spend, "orders": ordn, "clicks": clk},
                            harvest_camp, camp_lookup, prior_target=None
                        )
                        bulk_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": harvest_camp,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_value": f"受困优质词 in {c_name} {rank_cur_desc}(30d=${spend:.2f}, {int(ordn)}单, CPC=${target_cpc:.2f})",
                            "recommended_value": f"${recom_bid:.2f}",
                            "reason": f"{rank_tag}高潜核心词在宽泛仓低效位受抑；移入专用精准仓 ({harvest_camp}) 独立控价测试首屏（软交接第1阶段）。{bid_calc_desc}",
                        })

        # ---- Branch D: Proven Converting Winner Harvest (with Dedup against Existing Exact Keywords) ----
        is_winning_harvest_candidate = (
            ((ordn >= 3 and acos(spend, sales) <= target_acos * 1.10 and spend >= 8.0)
             or (ordn >= 2 and acos(spend, sales) <= target_acos * 0.90 and is_strong_relevance and spend >= 6.0))
            and bucket not in ("is_not", "competitor_brand", "adjacent")
            and not str(bucket).startswith("is_not")
        )
        if is_winning_harvest_candidate:
            harvest_camp_obj = camp_lookup.get(harvest_camp, {})
            already_in_exact_anywhere = bool(enabled_exact_by_text.get(ql))
            if (
                harvest_camp_obj.get("state", "enabled") == "enabled"
                and c_name != harvest_camp
                and not c_is_exact_camp
                and not already_in_exact_anywhere
            ):
                harvest.append(f"{q}\t{c_name}\tspend={spend:.2f}\tacos={row_acos:.1f}\tord={int(ordn)}\tbucket={bucket}\ttier={v_tier}")
                target_cpc = round(spend / max(1, clk), 2)
                paused_prior = paused_exact_by_camp_text.get((harvest_camp, ql))
                recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                    {"cpc": target_cpc, "spend": spend, "orders": ordn, "clicks": clk},
                    harvest_camp, camp_lookup, prior_target=paused_prior
                )
                if paused_prior:
                    bulk_adjustments.append({
                        "action_type": "Unpause_Keyword",
                        "campaign": harvest_camp,
                        "target_entity": q,
                        "match_type": "exact",
                        "current_value": f"{rank_cur_desc}精准仓已存在但处于暂停；原仓 {c_name} 30d={int(ordn)}单, ACOS={row_acos:.1f}%",
                        "recommended_value": f"state=enabled (bid=${recom_bid:.2f})",
                        "reason": f"{rank_tag}高转化赢家词重启收割：该词在 {c_name} 出单 {int(ordn)} 单 (ACOS {row_acos:.1f}%)，无需重复新建，直接在精准仓 ({harvest_camp}) 恢复启用并校准基础竞价。{bid_calc_desc}",
                    })
                else:
                    bulk_adjustments.append({
                        "action_type": "Harvest_To_Exact",
                        "campaign": harvest_camp,
                        "target_entity": q,
                        "match_type": "exact",
                        "current_value": f"{rank_cur_desc}30d表现: {int(ordn)}单, ACOS={row_acos:.1f}%, 7d={st7_ord}单 in {c_name}",
                        "recommended_value": f"${recom_bid:.2f}",
                        "reason": f"{rank_tag}高转化赢家词收割至精准仓（软交接第1阶段：先在 {harvest_camp} 建词起量，原仓保留观察3-5天后再执行二阶段精准否定防断流）。{bid_calc_desc}",
                    })

    return active_neg_dict, all_neg_queries, audit_records, bucket_stats, bulk_adjustments, camp_lookup, cannibal_map, harvest, kw_misfit_adjustments, ngram_stats, placement_diagnoses, st_lines, unnegative_adjustments, waste


def write_search_term_outputs(args, dest, gen_time_str, harvest, meta, placement_diagnoses, profile, st_lines, waste):
    """search_terms.tsv, waste.txt, harvest.txt and the placement diagnosis tables."""
    tsv(
        dest / "search_terms.tsv",
        "bucket\tvolume_tier\tsearchrank\tcampaign\tquery\tmatch\ttarget\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\torganic_rank\tsp_rank\tsynergy_tier",
        st_lines,
    )
    dest.joinpath("waste.txt").write_text("\n".join(waste), encoding="utf-8")
    dest.joinpath("harvest.txt").write_text("\n".join(harvest), encoding="utf-8")

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


def _sanitize_tsv_cell(val) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(val if val is not None else "")).strip()


def write_rank_synergy_tsv(
    args,
    dest,
    gen_time_str,
    kws,
    meta,
    profile,
    target_acos,
    xydc_map,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    camps=None,
    rules=None,
    sr_map=None,
    entity_memory=None,
    gen_date_str=None,
):
    """Output rank_synergy.tsv & xydc_blindspot_opportunities.tsv -- comprehensive mapping of organic rank vs ad rank (西柚洞察)."""
    if not xydc_map:
        return [], []

    camp_state_lu = {
        str(c.get("name") or c.get("campaign_name") or "").strip(): str(c.get("state") or "enabled").lower().strip()
        for c in (camps or [])
    }
    kw_perf = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0, "clk": 0, "camps": set(), "enabled_camps": set(), "bids": []})
    conv_spend_sum = 0.0
    conv_click_sum = 0
    for k in (kws or []):
        t = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if not t:
            continue
        sp_k = num(k.get("spends"))
        sl_k = num(k.get("sales"))
        od_k = int(num(k.get("orders")))
        ck_k = int(num(k.get("clicks")))
        kw_perf[t]["spend"] += sp_k
        kw_perf[t]["sales"] += sl_k
        kw_perf[t]["ord"] += od_k
        kw_perf[t]["clk"] += ck_k
        if od_k > 0 and ck_k > 0:
            conv_spend_sum += sp_k
            conv_click_sum += ck_k
        cn_k = str(k.get("campaign_name") or "").strip()
        if cn_k:
            kw_perf[t]["camps"].add(cn_k)
            k_state = str(k.get("state") or "enabled").lower().strip()
            c_state = camp_state_lu.get(cn_k, "enabled")
            if k_state == "enabled" and c_state == "enabled":
                kw_perf[t]["enabled_camps"].add(cn_k)
        eff_b, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.0)
        if eff_b > 0:
            kw_perf[t]["bids"].append(eff_b)

    anchor_portfolio_cpc = (conv_spend_sum / conv_click_sum) if conv_click_sum > 0 else 0.55
    anchor_portfolio_cpc = max(0.35, min(1.10, anchor_portfolio_cpc))
    harvest_camp = find_exact_harvest_campaign(camps or [], kws or [])
    dest_camp_obj = next((c for c in (camps or []) if (c.get("name") or c.get("campaign_name")) == harvest_camp), {})
    dest_tos = float(num(dest_camp_obj.get("tos") or dest_camp_obj.get("placement_top") or 40.0))

    today_ref = gen_date_str or datetime.now().strftime("%Y-%m-%d")
    eff_rules = rules if isinstance(rules, dict) else {}
    eff_prof = profile if isinstance(profile, dict) else {}
    own_brand_low = str(eff_prof.get("brand") or "").strip().lower()
    own_title_low = str(eff_prof.get("title") or "").strip().lower()

    _COMP_BRAND_PATS = (
        r"\bhamilton\s*beach\b", r"\bmr\.?\s*coffee\b", r"\bkitchen\s*aid\b", r"\bkrups\b",
        r"\bcuisinart\b", r"\bbreville\b", r"\bbaratza\b", r"\bbodum\b", r"\bcapresso\b",
        r"\boxo\b", r"\bde'?longhi\b", r"\bninja\b", r"\bkeurig\b", r"\bnespresso\b",
        r"\bblack\s*(?:and|\+|&)?\s*decker\b", r"\boster\b", r"\bkingrinder\b", r"\btimemore\b",
        r"\b1zpresso\b", r"\bfellow\b", r"\bcomandante\b", r"\bhario\b", r"\bporlex\b",
        r"\bjavapresse\b", r"\bmueller\b", r"\bsecura\b", r"\bsecrua\b", r"\bwancle\b",
        r"\bmamba\b", r"\bvevor\b", r"\bnutribullet\b", r"\bmagic\s*bullet\b",
        r"\bvitamix\b", r"\bblendtec\b", r"\bbraun\b", r"\bphilips\b",
        r"\bpanasonic\b", r"\btoastmaster\b", r"\bproctor\s*silex\b", r"\bgevi\b",
        r"\bhibrew\b", r"\bcasabrews\b", r"\bk[246]\b",
    )
    _TITLE_GATED_CLAIM_PATS = (
        r"\bburr\b", r"\bconical\b", r"\bmanual\b", r"\bhand\s+crank\b",
        r"\bcordless\b", r"\brechargeable\b", r"\bbattery\b", r"\busb\b", r"\bportable\b",
        r"\bno\s+plastic\b", r"\bplastic[\s-]+free\b", r"\bzero\s+plastic\b", r"\ball\s+glass\b",
        r"\bceramic\b", r"\btitanium\b", r"\bespresso\b", r"\bturkish\b",
        r"\bk[\s-]*cup\b", r"\bpod\b", r"\bcapsule\b", r"\bcommercial\b", r"\bindustrial\b",
        r"\breplacement\b", r"\bparts?\b",
    )
    _POLICY_BLOCKED_PAT = re.compile(r"\b(?:weed|marijuana|cannabis|tobacco|kratom)\b", re.I)

    def _is_clean_blindspot_term(term_low: str) -> bool:
        if re.fullmatch(r"b0[0-9a-z]{8}", term_low):
            return False
        if _POLICY_BLOCKED_PAT.search(term_low):
            return False
        for bp in _COMP_BRAND_PATS:
            if re.search(bp, term_low, re.I) and not (
                (own_brand_low and re.search(bp, own_brand_low, re.I))
                or (own_title_low and re.search(bp, own_title_low, re.I))
            ):
                return False
        if own_title_low:
            for cp in _TITLE_GATED_CLAIM_PATS:
                if re.search(cp, term_low, re.I) and not re.search(cp, own_title_low, re.I):
                    return False
        return True

    synergy_rows = []
    blindspot_candidates = []
    sorted_kws = sorted(xydc_map.keys(), key=lambda k: -xydc_map[k].get("total_traffic", 0.0))

    for kw in sorted_kws:
        perf = kw_perf.get(kw, {"spend": 0.0, "sales": 0.0, "ord": 0, "clk": 0, "camps": set(), "enabled_camps": set(), "bids": []})
        sp = perf["spend"]
        sa = perf["sales"]
        od = perf["ord"]
        ck = perf["clk"]
        cur_bid = min(perf["bids"]) if perf["bids"] else 0.0

        eval_res = evaluate_rank_synergy(
            term=kw,
            xydc_map=xydc_map,
            spend=sp,
            sales=sa,
            orders=od,
            clicks=ck,
            target_acos=target_acos or 25.0,
            current_bid=cur_bid,
        )

        rk = _compute_rank_deltas(kw, xydc_map, sr_map or {}, entity_memory or {}, today_ref)
        org_rank = eval_res["organic_rank"]
        org_traffic = eval_res["organic_traffic"]
        q_bucket, _, q_strong = classify_query(kw, eff_prof, eff_rules)

        is_tier5_blindspot = (
            len(perf["enabled_camps"]) == 0
            and org_rank is not None
            and (1 <= org_rank <= 45 or (org_rank <= 60 and org_traffic >= 120.0))
            and q_bucket not in ("is_not", "competitor_brand", "asin", "adjacent")
            and not str(q_bucket).startswith("is_not")
            and (q_strong or q_bucket in ("brand", "core_feature", "core_or_title"))
            and _is_clean_blindspot_term(kw.lower())
        )

        syn_tier = eval_res["synergy_tier"]
        syn_tag = eval_res["synergy_tag"]
        syn_recom = eval_res["recommended_value"]
        syn_reason = eval_res["reason"]

        def _fmt_growth_pct(val: float) -> str:
            if val != val or val == float("inf") or val == float("-inf"):
                return "NEW"
            return f"{val * 100.0:+.1f}%"

        if is_tier5_blindspot:
            syn_tier = "Tier 5: 盲区潜力词"
            syn_tag = "【自然盲区·高潜未投】"
            blind_bid = max(0.30, min(0.95, round((anchor_portfolio_cpc * 0.90) / (1.0 + dest_tos / 100.0), 2)))
            eff_tos_b = round(blind_bid * (1.0 + dest_tos / 100.0), 2)
            syn_recom = f"建议拓词至 {harvest_camp} [Exact]，起步基础竞价 ${blind_bid:.2f}（折合顶展出价 ${eff_tos_b:.2f}）"
            syn_reason = (
                f"西柚洞察自然位已达 #{org_rank}（自然流量得分 {org_traffic:.1f}，自然占比 {eval_res['organic_share_pct']:.1f}%），"
                f"属本品强相关核心词（{q_bucket}），但当前全组合无在跑广告投放词覆盖；建议纳入精准仓低风险拓词放大飞轮。"
            )
            blindspot_candidates.append({
                "kw": kw,
                "org_rank": org_rank,
                "sp_rank": eval_res["sp_rank"],
                "aba_rank": rk["aba_rank"],
                "org_traffic": round(org_traffic, 1),
                "ad_traffic": round(eval_res["ad_traffic"], 1),
                "org_share": f"{eval_res['organic_share_pct']:.1f}%",
                "org_growth": _fmt_growth_pct(eval_res["organic_growth"]),
                "bucket": q_bucket,
                "dest_campaign": harvest_camp,
                "suggested_bid": f"${blind_bid:.2f}",
                "eff_tos_bid": f"${eff_tos_b:.2f}",
                "reason": syn_reason,
            })

        org_rank_str = str(org_rank) if org_rank is not None else "--"
        sp_rank_str = str(eval_res["sp_rank"]) if eval_res["sp_rank"] is not None else "--"
        ad_acos_str = f"{acos(sp, sa):.1f}%" if sa > 0 else ("--" if sp == 0 else "999.0%")
        ad_cvr_str = f"{(od / ck * 100):.1f}%" if ck > 0 else "--"
        ad_cpc_str = f"${(sp / ck):.2f}" if ck > 0 else "--"
        active_camps_list = sorted(perf["enabled_camps"]) if perf["enabled_camps"] else sorted(perf["camps"])
        campaigns_str = "; ".join(active_camps_list[:2]) if active_camps_list else "--"
        if not perf["enabled_camps"] and perf["camps"]:
            campaigns_str = f"[已暂停] {campaigns_str}"

        aba_str = str(rk["aba_rank"]) if rk["aba_rank"] is not None else "--"
        prev_or_str = str(rk["prev_organic_rank"]) if rk["prev_organic_rank"] is not None else "--"
        dor_str = f"{rk['delta_or']:+d}" if rk["delta_or"] is not None else "--"
        prev_sp_str = str(rk["prev_sp_rank"]) if rk["prev_sp_rank"] is not None else "--"
        dsp_str = f"{rk['delta_sp']:+d}" if rk["delta_sp"] is not None else "--"

        synergy_rows.append("\t".join(_sanitize_tsv_cell(x) for x in [
            kw,
            org_rank_str,
            sp_rank_str,
            round(eval_res["organic_traffic"], 1),
            round(eval_res["ad_traffic"], 1),
            round(eval_res["total_traffic"], 1),
            f"{eval_res['organic_share_pct']:.1f}%",
            f"{eval_res['ad_share_pct']:.1f}%",
            campaigns_str,
            round(sp, 2),
            round(sa, 2),
            ad_acos_str,
            od,
            ck,
            ad_cvr_str,
            ad_cpc_str,
            syn_tier,
            syn_tag,
            syn_recom,
            syn_reason,
            aba_str,
            prev_or_str,
            dor_str,
            prev_sp_str,
            dsp_str,
            f"{eval_res['organic_rate'] * 100.0:.1f}%",
            f"{eval_res['ad_rate'] * 100.0:.1f}%",
            _fmt_growth_pct(eval_res["organic_growth"]),
            _fmt_growth_pct(eval_res["ad_growth"]),
        ]))

    headers = (
        "query\torganic_rank\tsp_rank\torganic_traffic\tad_traffic\ttotal_traffic\torganic_share\tad_share\t"
        "active_campaigns\tad_spend\tad_sales\tad_acos\tad_orders\tad_clicks\tad_cvr\tad_cpc\t"
        "synergy_tier\tsynergy_tag\trecommended_value\treason\t"
        "aba_rank\tprev_org_rank\tdelta_org_rank\tprev_sp_rank\tdelta_sp_rank\t"
        "org_acq_rate\tad_acq_rate\torg_growth_pct\tad_growth_pct"
    )
    tsv(dest / "rank_synergy.tsv", headers, synergy_rows)
    tsv(dest / f"rank_synergy_{gen_time_str}.tsv", headers, synergy_rows)

    blindspot_candidates.sort(key=lambda x: (-x["org_traffic"], x["org_rank"]))
    blind_headers = (
        "query\torganic_rank\tsp_rank\taba_rank\torganic_traffic\tad_traffic\torganic_share\t"
        "org_growth_pct\tintent_bucket\trecommended_campaign\tsuggested_base_bid\teffective_tos_bid\treason"
    )
    blind_rows = [
        "\t".join(_sanitize_tsv_cell(c[k]) for k in (
            "kw", "org_rank", "sp_rank", "aba_rank", "org_traffic", "ad_traffic",
            "org_share", "org_growth", "bucket", "dest_campaign", "suggested_bid", "eff_tos_bid", "reason"
        ))
        for c in [
            {
                **cand,
                "sp_rank": cand["sp_rank"] if cand["sp_rank"] is not None else "--",
                "aba_rank": cand["aba_rank"] if cand["aba_rank"] is not None else "--",
            }
            for cand in blindspot_candidates
        ]
    ]
    tsv(dest / "xydc_blindspot_opportunities.tsv", blind_headers, blind_rows)

    if getattr(args, "export_dir", None):
        exp_dir = Path(args.export_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or eff_prof.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        tsv(exp_dir / f"{asin_str}-{country_str}-rank-synergy-{gen_time_str}.tsv", headers, synergy_rows)
        tsv(exp_dir / f"{asin_str}-{country_str}-rank-synergy.tsv", headers, synergy_rows)
        if blind_rows:
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-blindspot-opportunities-{gen_time_str}.tsv", blind_headers, blind_rows)
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-blindspot-opportunities.tsv", blind_headers, blind_rows)

    return synergy_rows, blind_rows


def write_xydc_traffic_and_competitor_tsvs(
    args,
    dest,
    gen_time_str,
    meta,
    profile,
    target_acos,
    targets,
    xydc_traffic,
    camp_lookup=None,
    financial_prof=None,
    traffic_prof=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    src=None,
    asin_campaigns=None,
):
    """Export xydc_traffic_overview.json and xydc_competitor_benchmark.tsv combining LingXing ASIN targets & Auto ASIN clicks with xydc-mcp 7d traffic."""
    asin_str = str((meta or {}).get("asin") or (profile or {}).get("asin") or "UNKNOWN").upper()
    country_str = str((meta or {}).get("country") or "US").upper()
    own_store_asins = {asin_str}
    if src is not None:
        for pa in as_rows(load_json(Path(src) / "product_ads.json")):
            pa_a = str(pa.get("asin") or pa.get("advertised_asin") or "").strip().upper()
            if pa_a:
                own_store_asins.add(pa_a)

    if xydc_traffic and isinstance(xydc_traffic, dict):
        dest.joinpath("xydc_traffic_overview.json").write_text(
            json.dumps(xydc_traffic, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if getattr(args, "export_dir", None):
            exp_dir = Path(args.export_dir)
            exp_dir.mkdir(parents=True, exist_ok=True)
            exp_dir.joinpath(f"{asin_str}-{country_str}-xydc-traffic.json").write_text(
                json.dumps(xydc_traffic, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    comp_traffic_map = (xydc_traffic or {}).get("competitor_traffic_7d") if isinstance(xydc_traffic, dict) else {}
    if not isinstance(comp_traffic_map, dict):
        comp_traffic_map = {}
    self_trf = (xydc_traffic or {}).get("self_traffic_7d") if isinstance(xydc_traffic, dict) else {}
    self_org_score = num((self_trf or {}).get("organic_traffic_score"))

    # Aggregate LingXing competitor ASIN targets
    by_comp: dict[str, dict] = {}
    for r in (targets or []):
        c_info = extract_asin_target_info(r, group_bid_lookup, camp_group_bid_lookup)
        if not c_info:
            continue
        ca = c_info["asin"].upper()
        if ca == asin_str:
            continue
        entry = by_comp.setdefault(ca, {
            "asin": ca,
            "title": c_info.get("title") or "",
            "price": c_info.get("price") or 0.0,
            "stars": c_info.get("stars") or 0.0,
            "reviews": c_info.get("reviews") or 0,
            "spend": 0.0,
            "sales": 0.0,
            "orders": 0,
            "clicks": 0,
            "base_bid": c_info.get("base_bid") or 0.40,
            "campaigns": set(),
            "max_pp": 0.0,
        })
        if not entry["title"] and c_info.get("title"):
            entry["title"] = c_info["title"]
        if entry["price"] <= 0 and num(c_info.get("price")) > 0:
            entry["price"] = num(c_info["price"])
        if entry["stars"] <= 0 and num(c_info.get("stars")) > 0:
            entry["stars"] = num(c_info["stars"])
        if entry["reviews"] <= 0 and int(num(c_info.get("reviews"))) > 0:
            entry["reviews"] = int(num(c_info["reviews"]))
        entry["spend"] += num(c_info.get("spend"))
        entry["sales"] += num(c_info.get("sales"))
        entry["orders"] += int(num(c_info.get("orders")))
        entry["clicks"] += int(num(c_info.get("clicks")))
        if c_info.get("campaign_name"):
            cn = c_info["campaign_name"]
            entry["campaigns"].add(cn)
            if camp_lookup and cn in camp_lookup:
                entry["max_pp"] = max(entry["max_pp"], num(camp_lookup[cn].get("pp")))

    target_covered_pairs = {(ca_k, cn_k) for ca_k, ent_k in by_comp.items() for cn_k in ent_k["campaigns"]}
    if src is not None:
        for sr in as_rows(load_json(Path(src) / "search_terms.json")):
            if skip_total(sr):
                continue
            cn_s = str(sr.get("campaign_name") or "").strip()
            if asin_campaigns and cn_s not in asin_campaigns:
                continue
            q_up = str(sr.get("query") or "").strip().upper()
            if not re.fullmatch(r"B0[0-9A-Z]{8}", q_up) or q_up in own_store_asins:
                continue
            if (q_up, cn_s) in target_covered_pairs:
                continue
            entry = by_comp.setdefault(q_up, {
                "asin": q_up, "title": "", "price": 0.0, "stars": 0.0, "reviews": 0,
                "spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0,
                "base_bid": (camp_group_bid_lookup or {}).get(cn_s, 0.40),
                "campaigns": set(), "max_pp": 0.0,
            })
            entry["spend"] += num(sr.get("spends"))
            entry["sales"] += num(sr.get("sales"))
            entry["orders"] += int(num(sr.get("orders")))
            entry["clicks"] += int(num(sr.get("clicks")))
            if cn_s:
                entry["campaigns"].add(cn_s)
                if camp_lookup and cn_s in camp_lookup:
                    entry["max_pp"] = max(entry["max_pp"], num(camp_lookup[cn_s].get("pp")))

    for ca_x, x_ent in comp_traffic_map.items():
        ca_up = str(ca_x).upper()
        if ca_up not in own_store_asins and ca_up not in by_comp:
            by_comp[ca_up] = {
                "asin": ca_up, "title": "", "price": 0.0, "stars": 0.0, "reviews": 0,
                "spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0,
                "base_bid": 0.40, "campaigns": set(), "max_pp": 0.0,
            }

    if not by_comp:
        return []

    is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
    our_price = num((profile or {}).get("price"))
    our_reviews = int(num((profile or {}).get("reviews")))

    comp_rows = []
    for ca, ent in sorted(by_comp.items(), key=lambda kv: (-kv[1]["spend"], -num((comp_traffic_map.get(kv[0]) or {}).get("total_traffic_score")))):
        if ent["spend"] < 0.5 and ent["orders"] < 1 and ca not in comp_traffic_map:
            continue
        c_acos = round(acos(ent["spend"], ent["sales"]), 1) if ent["sales"] > 0 else (999.0 if ent["spend"] > 0 else 0.0)
        c_cpc = round(ent["spend"] / ent["clicks"], 2) if ent["clicks"] > 0 else 0.0
        eval_input = {
            "asin": ca,
            "price": ent["price"],
            "stars": ent["stars"],
            "reviews": ent["reviews"],
            "spend": ent["spend"],
            "orders": ent["orders"],
            "acos": c_acos,
            "cpc": c_cpc,
            "base_bid": ent["base_bid"],
        }
        act_code, rec_v, rsn_v = evaluate_competitor_target(
            eval_input, profile or {}, target_acos or 25.0,
            is_fragile=is_fragile, is_high_refund=is_high_refund, camp_pp=ent["max_pp"],
        )
        xt = comp_traffic_map.get(ca) or {}
        c_org_sc = num(xt.get("organic_traffic_score"))
        c_ad_sc = num(xt.get("ad_traffic_score"))
        c_org_sh = f"{num(xt.get('organic_share_pct')):.1f}%" if xt else "--"
        c_org_kw = str(int(num(xt.get("organic_keyword_count")))) if xt else "--"
        c_org_gr = f"{num(xt.get('organic_growth_pct')):+.1f}%" if xt else "--"

        if xt and self_org_score > 0 and c_org_sc > 0:
            ratio = c_org_sc / self_org_score
            if ratio >= 2.5:
                moat_desc = f"流量霸主碾压 ({ratio:.1f}x本品自然流)"
            elif ratio >= 1.2:
                moat_desc = f"竞品自然流领先 ({ratio:.1f}x本品)"
            elif ratio >= 0.7:
                moat_desc = f"自然流势均力敌 ({ratio:.1f}x本品)"
            else:
                moat_desc = f"本品自然流压制 ({ratio:.2f}x本品)"
        elif xt:
            moat_desc = f"竞品自然得分 {c_org_sc:.1f}"
        else:
            moat_desc = "仅领星商品投放对标"

        comp_rows.append("\t".join(_sanitize_tsv_cell(x) for x in [
            ca,
            (ent["title"] or "--")[:70],
            f"${ent['price']:.2f}" if ent["price"] > 0 else "--",
            f"${our_price:.2f}" if our_price > 0 else "--",
            f"{ent['stars']:.1f}" if ent["stars"] > 0 else "--",
            ent["reviews"] if ent["reviews"] > 0 else "--",
            our_reviews,
            round(c_org_sc, 1) if xt else "--",
            round(c_ad_sc, 1) if xt else "--",
            c_org_sh,
            c_org_kw,
            c_org_gr,
            moat_desc,
            round(ent["spend"], 2),
            ent["orders"],
            f"{c_acos:.1f}%" if ent["spend"] > 0 else "--",
            "; ".join(sorted(ent["campaigns"])[:2]) if ent["campaigns"] else "--",
            f"{act_code} -> {rec_v} ({rsn_v[:90]})",
        ]))

    comp_headers = (
        "competitor_asin\ttitle\tcomp_price\tour_price\tcomp_stars\tcomp_reviews\tour_reviews\t"
        "xydc_org_score_7d\txydc_ad_score_7d\txydc_org_share_pct\txydc_org_kw_count\txydc_org_growth_pct\t"
        "traffic_moat_vs_self\tad_spend_30d\tad_orders_30d\tad_acos_30d\tactive_campaigns\tstrategic_verdict"
    )
    if comp_rows:
        tsv(dest / "xydc_competitor_benchmark.tsv", comp_headers, comp_rows)
        if getattr(args, "export_dir", None):
            exp_dir = Path(args.export_dir)
            exp_dir.mkdir(parents=True, exist_ok=True)
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-competitor-benchmark-{gen_time_str}.tsv", comp_headers, comp_rows)
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-competitor-benchmark.tsv", comp_headers, comp_rows)
    return comp_rows


def write_buckets_tsv(bucket_stats, dest):
    """buckets.tsv -- spend and conversion rolled up by intent bucket."""
    b_lines = ["bucket\tspend\tsales\tacos\tord\tclk\tcvr"]
    for b, v in sorted(bucket_stats.items(), key=lambda kv: -kv[1]["spend"]):
        b_lines.append("\t".join(str(x) for x in [
            b, round(v["spend"], 1), round(v["sales"], 1), round(acos(v["spend"], v["sales"]), 1),
            int(v["ord"]), int(v["clk"]),
            round((v["ord"] / v["clk"] * 100) if v["clk"] else 0, 1),
        ]))
    dest.joinpath("buckets.tsv").write_text("\n".join(b_lines), encoding="utf-8")


def write_ngram_outputs(all_neg_queries, bulk_adjustments, dest, known_negatives, ngram_stats, profile, rules):
    """ngrams.tsv and bleeding_ngrams.txt, appending negative-phrase adjustments."""
    protected_ng_tokens = _compute_protected_ngram_tokens(profile, rules)
    ng_lines = []
    bleeding_ng = []
    negated_1grams = set()
    for ng, stat in sorted(ngram_stats.items(), key=lambda kv: (0 if kv[1]["gram_type"] == "1-gram" else 1, -kv[1]["spend"])):
        sp = stat["spend"]
        sl = stat["sales"]
        od = stat["ord"]
        ck = stat["clk"]
        if sp < 2 and od < 1:
            continue
        cvr_val = round((od / ck * 100) if ck else 0.0, 1)
        cpc_val = round((sp / ck) if ck else 0.0, 2)
        acos_val = round(acos(sp, sl), 1)
        ng_lines.append((sp, "\t".join(str(x) for x in [
            ng, stat["gram_type"], round(sp, 2), round(sl, 2), acos_val,
            int(od), int(ck), cvr_val, cpc_val, len(stat["queries"])
        ])))

        if sp >= 10 and od == 0 and ck >= 8 and len(stat["queries"]) >= 2:
            _, _, ng_strong = classify_query(ng, profile, rules)
            ng_tokens = {t for t in re.findall(r"[a-z0-9]+", ng.lower()) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}
            is_protected = (
                ng_strong
                or not ng_tokens
                or (stat["gram_type"] == "1-gram" and bool(ng_tokens & protected_ng_tokens))
                or (stat["gram_type"] != "1-gram" and ng_tokens.issubset(protected_ng_tokens))
            )
            if is_protected:
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tPROTECTED(core_title_or_positive_token)")
                continue

            if stat["gram_type"] != "1-gram" and (ng_tokens & negated_1grams):
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tCOVERED_BY_1GRAM")
                continue

            all_queries_negated = all(q_item.lower() in all_neg_queries for q_item in stat["queries"])
            is_known_neg = is_known_negative(ng, known_negatives)
            if ng.lower() in all_neg_queries or all_queries_negated or is_known_neg:
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tALREADY_NEGATED")
                continue

            if stat["gram_type"] == "1-gram":
                negated_1grams |= ng_tokens
            bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}")
            bulk_adjustments.append({
                "action_type": "Negative_Phrase",
                "campaign": "ALL_NON_EXACT",
                "target_entity": ng,
                "match_type": "negativePhrase",
                "current_value": f"[词根词组否定·非单独立词] 累计花费=${sp:.2f}, {int(ck)}点击0订单 (覆盖{len(stat['queries'])}个搜索词)",
                "recommended_value": "Add Negative Phrase to Broad/Auto Campaigns",
                "reason": f"[高耗零单词根词组否定] 词根 '{ng}' 跨 {len(stat['queries'])} 个搜索词累计花费 ${sp:.2f} 且 0 转化，在广泛/自动仓添加词组否定一举止血。",
            })

    ng_lines_sorted = [line for _, line in sorted(ng_lines, key=lambda x: -x[0])]
    tsv(
        dest / "ngrams.tsv",
        "ngram\ttype\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tquery_count",
        ng_lines_sorted,
    )
    dest.joinpath("bleeding_ngrams.txt").write_text("\n".join(bleeding_ng), encoding="utf-8")

    return bleeding_ng


def write_cannibalization_tsv(cannibal_map, dest):
    """cannibalization.tsv -- queries competing across two or more DISTINCT campaigns."""
    cannibal_lines = []
    for q_text, occurrences in sorted(cannibal_map.items(), key=lambda kv: -sum(x["spend"] for x in kv[1])):
        distinct_camps = {x["campaign"] for x in occurrences if x.get("campaign")}
        if len(distinct_camps) < 2:
            continue
        total_sp = sum(x["spend"] for x in occurrences)
        total_od = sum(x["orders"] for x in occurrences)
        if total_sp < 2 and total_od == 0:
            continue

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
            q_text, str(len(distinct_camps)), f"{total_sp:.2f}", str(int(total_od)),
            risk, " ; ".join(detail_strs)
        ]))

    tsv(
        dest / "cannibalization.tsv",
        "query\tcampaign_count\ttotal_spend\ttotal_orders\trisk_level\tdetails",
        cannibal_lines,
    )

    return cannibal_lines


def write_groups_tsv(asin_campaigns, dest, src):
    """groups.tsv -- ad group level performance."""
    groups = as_rows(load_json(src / "ad_groups.json"))
    if asin_campaigns:
        groups = [r for r in groups if r.get("campaign_name") in asin_campaigns]
    g_lines = []
    for r in sorted(groups, key=lambda x: -num(x.get("spends"))):
        if not r.get("campaign_name") and not r.get("name"):
            continue
        sp_g = num(r.get("spends"))
        sl_g = num(r.get("sales"))
        g_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("name") or r.get("ad_group_name"), r.get("state"),
            r.get("default_bid") or r.get("bid"),
            round(sp_g, 2), round(sl_g, 2), round(clean_api_acos(r.get("acos"), sp_g, sl_g), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(num(r.get("cvr")), 1),
            round(num(r.get("cpc")), 2),
        ]))
    tsv(
        dest / "groups.tsv",
        "campaign\tadgroup\tstate\tdefbid\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        g_lines,
    )


def write_targets_tsv(
    bulk_adjustments,
    camp_lookup,
    dest,
    profile,
    target_acos,
    targets,
    financial_prof=None,
    traffic_prof=None,
    targets_7d=None,
    targets_3d=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    macro_scorecard=None,
    locked_campaigns=None,
):
    """targets.tsv -- product targets with competitor profile, inherited bids, and 7d/3d freshness guard."""
    is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
    shrinkage_guard = bool(macro_scorecard and macro_scorecard.get("velocity_shrinkage_guard"))
    locked_set = set(locked_campaigns or [])

    t7_map = {}
    for t7 in (targets_7d or []):
        ci7 = extract_asin_target_info(t7, group_bid_lookup, camp_group_bid_lookup)
        raw_t7_txt = str(t7.get("targeting_text") or t7.get("expression") or t7.get("keyword_text") or "").strip()
        cn7 = t7.get("campaign_name") or ""
        if ci7 and ci7.get("asin"):
            t7_map[(cn7, ci7["asin"])] = t7
        if raw_t7_txt:
            t7_map[(cn7, raw_t7_txt.lower())] = t7
    t3_map = {}
    for t3 in (targets_3d or []):
        ci3 = extract_asin_target_info(t3, group_bid_lookup, camp_group_bid_lookup)
        raw_t3_txt = str(t3.get("targeting_text") or t3.get("expression") or t3.get("keyword_text") or "").strip()
        cn3 = t3.get("campaign_name") or ""
        if ci3 and ci3.get("asin"):
            t3_map[(cn3, ci3["asin"])] = t3
        if raw_t3_txt:
            t3_map[(cn3, raw_t3_txt.lower())] = t3

    t_lines = []
    for r in sorted(targets, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 0.5 and num(r.get("orders")) < 1:
            continue
        c_info = extract_asin_target_info(r, group_bid_lookup, camp_group_bid_lookup)
        eff_t_bid, is_t_inh = resolve_effective_entity_bid(r, group_bid_lookup, camp_group_bid_lookup, fallback=0.40)
        comp_str = "--"
        comp_eval = "--"

        is_timing = bool(r.get("is_apply_time"))
        t_base = f"{num(r.get('timing_base_value')):.2f}" if r.get("timing_base_value") is not None else "--"
        app_t = r.get("applied_templates") or []
        t_name = app_t[0].get("name", "") if app_t else "--"
        timing_status = "分时生效" if is_timing else "未应用"

        c_name_r = r.get("campaign_name") or ""
        raw_txt_r = str(r.get("targeting_text") or r.get("expression") or r.get("keyword_text") or "").strip()
        t7_row = t7_map.get((c_name_r, c_info["asin"]), {}) if c_info else t7_map.get((c_name_r, raw_txt_r.lower()), {})
        t3_row = t3_map.get((c_name_r, c_info["asin"]), {}) if c_info else t3_map.get((c_name_r, raw_txt_r.lower()), {})
        sp_7d = round(num(t7_row.get("spends") or t7_row.get("spend") or t7_row.get("cost")), 2) if t7_row else 0.0
        sl_7d = num(t7_row.get("sales")) if t7_row else 0.0
        ord_7d = int(num(t7_row.get("orders"))) if t7_row else 0
        ac_7d = round(clean_api_acos(t7_row.get("acos"), sp_7d, sl_7d), 1) if t7_row else 0.0
        imp_3d = int(num(t3_row.get("impressions"))) if t3_row else 0
        sp_3d = round(num(t3_row.get("spends") or t3_row.get("spend") or t3_row.get("cost")), 2) if t3_row else 0.0
        sl_3d = num(t3_row.get("sales")) if t3_row else 0.0
        ord_3d = int(num(t3_row.get("orders"))) if t3_row else 0
        ac_3d = round(clean_api_acos(t3_row.get("acos"), sp_3d, sl_3d), 1) if t3_row else 0.0

        if c_info:
            comp_str = f"ASIN={c_info['asin']}|P=${c_info['price']:.2f}|R={c_info['stars']}|REV={c_info['reviews']}"
            c_name_t = c_info.get("campaign_name", "")
            camp_t = camp_lookup.get(c_name_t, {})
            camp_pp = num(camp_t.get("pp", 0))

            action_code, rec_val, reason_text = evaluate_competitor_target(
                c_info, profile, target_acos,
                is_fragile=is_fragile, is_high_refund=is_high_refund,
                camp_pp=camp_pp
            )
            comp_eval = action_code
            t_state = str(t3_row.get("state") or t7_row.get("state") or r.get("state") or "enabled").lower().strip()
            camp_state_t = camp_t.get("state", "enabled")

            if camp_state_t == "enabled" and t_state == "enabled":
                cur_base_num = c_info.get("base_bid", eff_t_bid)
                timing_note = f" [领星分时策略运行中: 模板'{t_name}', 当前分时基准值:${t_base}]" if is_timing else ""

                if action_code == "Lower_Bid":
                    # Zero recent traffic or Velocity-Shrinkage Guard protection
                    zero_recent = (t7_row and sp_7d <= 0.3 and imp_3d == 0 and sp_3d == 0.0)
                    healthy_7d = (ord_7d >= 2 and acos(sp_7d, sl_7d) <= target_acos)
                    if not zero_recent and not healthy_7d and not (shrinkage_guard and c_info["orders"] >= 1):
                        timing_rec = f" 【领星分时联动提示】：绑定分时模板'{t_name}'，建议同步更新分时基准值至 {rec_val}。" if is_timing else ""
                        bulk_adjustments.append({
                            "action_type": "Lower_Bid",
                            "campaign": c_info["campaign_name"],
                            "target_entity": c_info["asin"],
                            "match_type": "asinSameAs",
                            "current_bid_num": cur_base_num,
                            "current_value": f"[竞品ASIN投放]{timing_note} 当前基础竞价=${cur_base_num:.2f}, 30d=${c_info['spend']:.2f}({c_info['orders']}单), 7d=${sp_7d:.2f}({ord_7d}单), CPC=${c_info['cpc']:.2f}, 竞品=${c_info['price']:.2f} vs 本品=${profile.get('price')}",
                            "recommended_value": rec_val,
                            "reason": f"[竞品ASIN投放] {reason_text}{timing_rec}",
                        })
                elif action_code == "Negative_Target":
                    if not (ord_7d >= 1 and acos(sp_7d, sl_7d) <= target_acos):
                        bulk_adjustments.append({
                            "action_type": "Negative_Target",
                            "campaign": c_info["campaign_name"],
                            "target_entity": c_info["asin"],
                            "match_type": "asinSameAs",
                            "current_bid_num": cur_base_num,
                            "current_value": f"[竞品ASIN投放]{timing_note} 当前基础竞价=${cur_base_num:.2f}, 30d=${c_info['spend']:.2f}({c_info['orders']}单), 竞品=${c_info['price']:.2f}({c_info['reviews']}评) vs 本品=${profile.get('price')}",
                            "recommended_value": f"Add Negative Target (asinSameAs={c_info['asin']})",
                            "reason": f"[竞品ASIN投放止血] {reason_text}",
                        })
                elif action_code == "Harvest_ASIN" and c_info["acos"] <= target_acos * 0.80 and c_info["campaign_name"] not in locked_set:
                    # Already targeted in this campaign and converting well below target ACOS -> pure numerical scale-up bid!
                    scale_bid = round(min(1.20, cur_base_num * 1.12), 2)
                    if scale_bid > cur_base_num and not (sp_3d >= 6.0 and ord_3d == 0):
                        timing_rec = f" 【领星分时联动提示】：绑定分时模板'{t_name}'，建议同步更新分时基准值至 ${scale_bid:.2f}。" if is_timing else ""
                        bulk_adjustments.append({
                            "action_type": "Elevate_To_TOS",
                            "campaign": c_info["campaign_name"],
                            "target_entity": c_info["asin"],
                            "match_type": "asinSameAs",
                            "current_bid_num": cur_base_num,
                            "current_value": f"[赢家竞品ASIN放量]{timing_note} 当前基础竞价=${cur_base_num:.2f}, 30d=${c_info['spend']:.2f}({c_info['orders']}单, ACOS={c_info['acos']:.1f}%)",
                            "recommended_value": f"${scale_bid:.2f}",
                            "reason": f"[赢家竞品页扩量] 在竞品 {c_info['asin']} 详情页出单 {c_info['orders']} 单且 ACOS 仅 {c_info['acos']:.1f}%（远优于目标 {target_acos:.0f}%），小幅上调基础竞价 +12% 至 ${scale_bid:.2f} 抢占更高商品页首屏份额。{timing_rec}",
                        })

        def_disp = r.get("default_bid") if num(r.get("default_bid")) > 0 else (eff_t_bid if is_t_inh else "--")
        sales_r = num(r.get("sales"))
        acos_r = clean_api_acos(r.get("acos"), spend, sales_r)
        t_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("exp_type_zh") or r.get("exp_type_text") or r.get("match_type"),
            r.get("targeting_text") or r.get("expression") or r.get("keyword_text"),
            comp_str, comp_eval,
            eff_t_bid, def_disp, r.get("state"),
            timing_status, t_base, t_name,
            round(spend, 2), round(sales_r, 2), round(acos_r, 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(num(r.get("cvr")), 1),
            round(num(r.get("cpc")), 2),
            sp_7d, ord_7d, ac_7d, sp_3d, ord_3d, ac_3d,
        ]))
    tsv(
        dest / "targets.tsv",
        "campaign\ttype\ttext\tcompetitor_info\tevaluation\tbid\tdefbid\tstate\ttiming_status\ttiming_base_value\ttiming_template\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tspend_7d\tord_7d\tacos_7d\tspend_3d\tord_3d\tacos_3d",
        t_lines,
    )


def write_negative_tsvs(active_neg_dict, audit_records, dest):
    """active_negatives.tsv, active_negatives_audit.tsv and unnegative_recommendations.tsv."""
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


def load_entity_memory(db_root: Path, snap_key: str, legacy_key: str = "") -> dict:
    """Load persistent entity & keyword-rank memory across optimization runs."""
    mem_dir = db_root / "entity_memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_file = mem_dir / f"{snap_key}_memory.json"
    data = load_json(mem_file)
    if not data and legacy_key and (mem_dir / f"{legacy_key}_memory.json").exists():
        data = load_json(mem_dir / f"{legacy_key}_memory.json")
    if not isinstance(data, dict):
        return {"entities": {}, "terms": {}}
    data.setdefault("entities", {})
    data.setdefault("terms", {})
    return data


def _compute_rank_deltas(term: str, xydc_map: dict | None, sr_map: dict | None, entity_memory: dict | None, today_str: str) -> dict:
    """Compute organic/SP rank & longitudinal rank deltas (positive delta = climbed closer to #1)."""
    tl = (term or "").lower().strip()
    t_norm = re.sub(r"[^\w\s]", "", tl).strip()
    x_info = (xydc_map or {}).get(tl) or (xydc_map or {}).get(t_norm) or {}
    curr_or = x_info.get("organic_rank")
    curr_sp = x_info.get("sp_rank")
    aba_sr, v_tier = get_searchrank(tl, sr_map or {})

    t_mem = ((entity_memory or {}).get("terms") or {}).get(tl) or {}
    if t_mem.get("last_date") == today_str:
        prev_or = t_mem.get("prev_organic_rank")
        prev_sp = t_mem.get("prev_sp_rank")
    else:
        prev_or = t_mem.get("organic_rank")
        prev_sp = t_mem.get("sp_rank")

    delta_or = (int(prev_or) - int(curr_or)) if (prev_or is not None and curr_or is not None) else None
    delta_sp = (int(prev_sp) - int(curr_sp)) if (prev_sp is not None and curr_sp is not None) else None
    return {
        "organic_rank": curr_or,
        "prev_organic_rank": prev_or,
        "delta_or": delta_or,
        "sp_rank": curr_sp,
        "prev_sp_rank": prev_sp,
        "delta_sp": delta_sp,
        "aba_rank": aba_sr,
        "volume_tier": v_tier,
    }


def _recommended_pp_ros(c: dict, default_pp: int, default_ros: int, target_acos: float) -> tuple[int, int, str]:
    """Protect profitable PP / ROS placements verified by real placement report instead of blindly zeroing them out."""
    real_p30 = c.get("real_placements_30d") or {}
    real_pp = real_p30.get("PP") or {}
    real_ros = real_p30.get("ROS") or {}
    rec_pp = default_pp
    rec_ros = default_ros
    notes = []

    if real_pp.get("orders", 0) >= 2 and 0 < real_pp.get("acos", 999.0) <= target_acos * 1.15:
        rec_pp = min(int(c.get("pp", 0)), max(default_pp, 25)) if int(c.get("pp", 0)) > 0 else default_pp
        if rec_pp > default_pp:
            notes.append(f"真实展位PP出单{real_pp['orders']}单(ACOS {real_pp['acos']:.1f}%)保留PP={rec_pp}%")
    if real_ros.get("orders", 0) >= 2 and 0 < real_ros.get("acos", 999.0) <= target_acos * 1.15:
        rec_ros = min(int(c.get("ros", 0)), max(default_ros, 25)) if int(c.get("ros", 0)) > 0 else default_ros
        if rec_ros > default_ros:
            notes.append(f"真实展位ROS出单{real_ros['orders']}单(ACOS {real_ros['acos']:.1f}%)保留ROS={rec_ros}%")

    note_str = f"（{'；'.join(notes)}）" if notes else ""
    return rec_pp, rec_ros, note_str


ACTION_PRIORITY_RANK = {
    "Stockout_Conserve_Hold": 100,
    "Low_Inventory_Conserve_Hold": 99,
    "Pause_Keyword": 98,
    "Remove_Negative": 97,
    "Probe_Bid_Recovery": 94,
    "Top_Rank_Cooldown_Hold": 93,
    "Rank_Slide_Protection_Hold": 92,
    "Compensated_Base_Bid_Floor": 91,
    "Anti_Double_Damping_Hold": 90,
    "Update_Bidding_Strategy": 88,
    "Placement_TOS_PP_ROS": 87,
    "Emergency_Placement_Throttle": 86,
    "Placement_TOS": 85,
    "Placement_PP": 84,
    "High_Refund_Throttle_Budget": 83,
    "Archive_Fragmented_Campaign": 82,
    "Negative_Target": 78,
    "Negative_Exact": 77,
    "Negative_Phrase": 76,
    "Unpause_Keyword": 75,
    "Anti_Drift_Base_Bid_Throttle": 68,
    "Throttle_TOS_Bid": 66,
    "Lower_Bid": 65,
    "Harvest_To_Exact": 64,
    "Harvest_ASIN": 63,
    "Converge_Harvest_To_Exact": 62,
    "Elevate_To_TOS": 61,
    "Throttle_Cannibalization_Bid": 54,
    "Boost_Ascender_TOS": 53,
    "Dayparting_Budget_Pacing": 45,
    "Dayparting_Boost": 44,
    "Dayparting_Throttle": 43,
}

UPWARD_BID_ACTIONS = {"Elevate_To_TOS", "Boost_Ascender_TOS", "Probe_Bid_Recovery", "Compensated_Base_Bid_Floor"}
DOWNWARD_BID_ACTIONS = {"Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid", "Anti_Drift_Base_Bid_Throttle"}


def resolve_entity_action_conflicts(adjustments: list[dict]) -> list[dict]:
    """Deterministic single-winner conflict resolver guaranteeing 0 intra-TSV (campaign, target_entity) collisions
    and cross-campaign misfit veto (never harvest a paused/negated misfit term).
    """
    paused_misfit_terms = {
        str(x.get("target_entity") or "").lower().strip()
        for x in adjustments
        if x.get("action_type") == "Pause_Keyword" and x.get("target_entity")
    }
    neg_phrase_roots = {
        str(x.get("target_entity") or "").lower().strip()
        for x in adjustments
        if x.get("action_type") == "Negative_Phrase" and x.get("target_entity")
    }
    archived_camps = {
        str(x.get("campaign") or "").strip()
        for x in adjustments
        if x.get("action_type") == "Archive_Fragmented_Campaign" and x.get("campaign")
    }
    full_placement_camps = {
        str(x.get("campaign") or "").strip()
        for x in adjustments
        if x.get("action_type") == "Placement_TOS_PP_ROS" and x.get("campaign")
    }

    pre_filtered = []
    for item in adjustments:
        act = item.get("action_type", "")
        camp = str(item.get("campaign") or "").strip()
        ent_low = str(item.get("target_entity") or "").lower().strip()
        if not act or not camp or not ent_low:
            continue

        # 1. Cross-campaign misfit veto
        if act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact", "Elevate_To_TOS", "Boost_Ascender_TOS", "Unpause_Keyword"):
            if ent_low in paused_misfit_terms:
                continue
            ent_words = set(re.findall(r"[a-z0-9]+", ent_low))
            if any((nr in ent_words if " " not in nr else nr in ent_low) for nr in neg_phrase_roots):
                continue

        # 2. Archived campaign veto
        if camp in archived_camps and act != "Archive_Fragmented_Campaign":
            continue

        # 3. Campaign placement unification
        if camp in full_placement_camps and act in ("Placement_TOS", "Placement_PP", "Emergency_Placement_Throttle"):
            continue

        pre_filtered.append(item)

    winners_by_key: dict[tuple[str, str], dict] = {}
    order_keys: list[tuple[str, str]] = []
    for item in pre_filtered:
        camp = str(item.get("campaign") or "").strip()
        ent_low = str(item.get("target_entity") or "").lower().strip()
        key = (camp, ent_low)
        prio = ACTION_PRIORITY_RANK.get(item.get("action_type", ""), 50)
        if key not in winners_by_key:
            winners_by_key[key] = dict(item, _prio=prio)
            order_keys.append(key)
        else:
            existing = winners_by_key[key]
            if prio > existing.get("_prio", 50):
                prev_act = existing.get("action_type", "")
                new_winner = dict(item, _prio=prio)
                if prev_act and prev_act != new_winner.get("action_type"):
                    new_winner["reason"] = f"{new_winner.get('reason', '')} [冲突消解: 优先于同实体次级建议 {prev_act}]".strip()
                winners_by_key[key] = new_winner

    resolved = []
    for k in order_keys:
        w = dict(winners_by_key[k])
        w.pop("_prio", None)
        resolved.append(w)
    return resolved


def sync_and_write_entity_full_context(
    *,
    db_root: Path,
    dest: Path,
    snap_key: str,
    gen_date_str: str,
    entity_memory: dict,
    kws: list[dict],
    kws_7d: list[dict],
    kws_3d: list[dict],
    targets: list[dict],
    targets_7d: list[dict],
    targets_3d: list[dict],
    xydc_map: dict | None,
    sr_map: dict | None,
    group_bid_lookup: dict | None,
    camp_group_bid_lookup: dict | None,
    ledger: list[dict],
    deduped_adjustments: list[dict],
) -> None:
    """Persist longitudinal rank + recommendation memory to db/entity_memory/ and output entity_full_context.tsv."""
    mem_dir = db_root / "entity_memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_file = mem_dir / f"{snap_key}_memory.json"

    entities_mem = dict(entity_memory.get("entities") or {})
    terms_mem = dict(entity_memory.get("terms") or {})

    # Map today's emitted adjustments by (campaign, target_entity_lower)
    today_adj_map = {
        (str(a.get("campaign") or "").strip(), str(a.get("target_entity") or "").lower().strip()): a
        for a in (deduped_adjustments or [])
    }

    # Map latest ledger event by (campaign, entity_lower)
    ledger_by_ent = {}
    for l_item in (ledger or []):
        lc = str(l_item.get("campaign") or "").strip()
        ek = str(l_item.get("entity_key") or "")
        parts = ek.split("::")
        ent_txt = parts[-1].lower().strip() if len(parts) >= 3 else ""
        if not ent_txt:
            m_q = re.search(r"'([^']+)'", str(l_item.get("entity_name") or ""))
            if m_q:
                ent_txt = m_q.group(1).lower().strip()
        if lc and ent_txt:
            ledger_by_ent[(lc, ent_txt)] = l_item
            m_asin = re.search(r"(b0[0-9a-z]{8})", ent_txt)
            if m_asin:
                ledger_by_ent[(lc, m_asin.group(1))] = l_item

    kw_7d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_7d or [])}
    kw_3d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_3d or [])}
    tg_7d_lu = {(t.get("campaign_name") or "", (t.get("targeting_text") or t.get("expression") or "").strip().lower()): t for t in (targets_7d or [])}
    tg_3d_lu = {(t.get("campaign_name") or "", (t.get("targeting_text") or t.get("expression") or "").strip().lower()): t for t in (targets_3d or [])}

    ctx_rows = []
    # 1. Process all keywords
    for k in sorted(kws or [], key=lambda r: -num(r.get("spends"))):
        cn = k.get("campaign_name") or ""
        mt = (k.get("match_type") or "").lower().strip()
        txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip()
        tl = txt.lower()
        if not cn or not tl:
            continue
        sp30 = num(k.get("spends"))
        od30 = int(num(k.get("orders")))
        ac30 = round(clean_api_acos(k.get("acos"), sp30, num(k.get("sales"))), 1)
        k7 = kw_7d_lu.get((cn, mt, tl), {})
        k3 = kw_3d_lu.get((cn, mt, tl), {})
        sp7 = round(num(k7.get("spends") or k7.get("spend") or k7.get("cost")), 2) if k7 else 0.0
        od7 = int(num(k7.get("orders"))) if k7 else 0
        ac7 = round(acos(sp7, num(k7.get("sales"))), 1) if k7 and num(k7.get("sales")) > 0 else (999.0 if sp7 > 0 else 0.0)
        sp3 = round(num(k3.get("spends") or k3.get("spend") or k3.get("cost")), 2) if k3 else 0.0
        od3 = int(num(k3.get("orders"))) if k3 else 0

        eff_bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        st = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()
        timing_st = "分时生效" if k.get("is_apply_time") else "未应用"

        rk = _compute_rank_deltas(tl, xydc_map, sr_map, entity_memory, gen_date_str)
        ekey = f"{cn}::{mt}::{tl}"
        prev_ent = entities_mem.get(ekey) or {}
        l_ev = ledger_by_ent.get((cn, tl))
        prior_act_str = "--"
        if prev_ent.get("last_action_type"):
            prior_act_str = f"{prev_ent.get('last_action_date', '')}:{prev_ent['last_action_type']}->{prev_ent.get('last_recommended_value', '')}"
        elif l_ev:
            l_dt = str(l_ev.get("change_date") or l_ev.get("changed_at") or l_ev.get("adjusted_at") or "")[:10]
            l_old = l_ev.get("old_val") if l_ev.get("old_val") is not None else l_ev.get("old_value")
            l_new = l_ev.get("new_val") if l_ev.get("new_val") is not None else l_ev.get("new_value")
            prior_act_str = f"{l_dt}:{l_old}->{l_new}({l_ev.get('effect_verdict', '')})"

        today_adj = today_adj_map.get((cn, tl))
        today_act_type = today_adj["action_type"] if today_adj else "--"
        today_rec_val = today_adj["recommended_value"] if today_adj else "--"

        # Update persistent term rank memory
        if rk["organic_rank"] is not None or rk["sp_rank"] is not None:
            terms_mem[tl] = {
                "organic_rank": rk["organic_rank"],
                "sp_rank": rk["sp_rank"],
                "prev_organic_rank": rk["prev_organic_rank"],
                "prev_sp_rank": rk["prev_sp_rank"],
                "aba_rank": rk["aba_rank"],
                "last_date": gen_date_str,
            }

        # Update persistent entity action memory
        ent_record = dict(prev_ent)
        ent_record.update({
            "campaign": cn,
            "match_type": mt,
            "entity": txt,
            "current_bid": eff_bid,
            "state": st,
            "organic_rank": rk["organic_rank"],
            "sp_rank": rk["sp_rank"],
            "updated_at": gen_date_str,
        })
        if today_adj:
            hist = list(ent_record.get("history") or [])
            if not hist or hist[-1].get("date") != gen_date_str or hist[-1].get("action_type") != today_act_type:
                hist.append({
                    "date": gen_date_str,
                    "action_type": today_act_type,
                    "recommended_value": today_rec_val,
                    "organic_rank": rk["organic_rank"],
                    "sp_rank": rk["sp_rank"],
                })
            ent_record["history"] = hist[-8:]
            ent_record["last_action_date"] = gen_date_str
            ent_record["last_action_type"] = today_act_type
            ent_record["last_recommended_value"] = today_rec_val
        entities_mem[ekey] = ent_record

        if sp30 >= 1.0 or od30 >= 1 or today_adj or sp7 > 0:
            dor_s = f"{rk['delta_or']:+d}" if rk["delta_or"] is not None else "--"
            dsp_s = f"{rk['delta_sp']:+d}" if rk["delta_sp"] is not None else "--"
            ctx_rows.append("\t".join(str(x) for x in [
                cn, "keyword", mt, txt, st, f"${eff_bid:.2f}", timing_st,
                rk["aba_rank"] if rk["aba_rank"] is not None else "--",
                rk["prev_organic_rank"] if rk["prev_organic_rank"] is not None else "--",
                rk["organic_rank"] if rk["organic_rank"] is not None else "--",
                dor_s,
                rk["prev_sp_rank"] if rk["prev_sp_rank"] is not None else "--",
                rk["sp_rank"] if rk["sp_rank"] is not None else "--",
                dsp_s,
                round(sp30, 2), od30, ac30, sp7, od7, ac7, sp3, od3,
                prior_act_str, today_act_type, today_rec_val,
            ]))

    # 1b. Process all targets (Auto / Competitor ASIN / Category targets)
    for t in sorted(targets or [], key=lambda r: -num(r.get("spends"))):
        cn = t.get("campaign_name") or ""
        raw_txt = (t.get("targeting_text") or t.get("expression") or "").strip()
        raw_tl = raw_txt.lower()
        if not cn or not raw_tl:
            continue
        ci_info = extract_asin_target_info(t, group_bid_lookup, camp_group_bid_lookup)
        txt = ci_info["asin"] if ci_info else raw_txt
        tl = txt.lower()
        mt = "asinSameAs" if ci_info else (t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or "target").lower().strip()
        sp30 = num(t.get("spends"))
        od30 = int(num(t.get("orders")))
        ac30 = round(clean_api_acos(t.get("acos"), sp30, num(t.get("sales"))), 1)
        t7 = tg_7d_lu.get((cn, raw_tl), {})
        t3 = tg_3d_lu.get((cn, raw_tl), {})
        sp7 = round(num(t7.get("spends") or t7.get("spend") or t7.get("cost")), 2) if t7 else 0.0
        od7 = int(num(t7.get("orders"))) if t7 else 0
        ac7 = round(acos(sp7, num(t7.get("sales"))), 1) if t7 and num(t7.get("sales")) > 0 else (999.0 if sp7 > 0 else 0.0)
        sp3 = round(num(t3.get("spends") or t3.get("spend") or t3.get("cost")), 2) if t3 else 0.0
        od3 = int(num(t3.get("orders"))) if t3 else 0

        eff_bid, _ = resolve_effective_entity_bid(t, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        st = str(t3.get("state") or t7.get("state") or t.get("state") or "enabled").lower().strip()
        timing_st = "分时生效" if t.get("is_apply_time") else "未应用"

        ekey = f"{cn}::{mt}::{tl}"
        prev_ent = entities_mem.get(ekey) or {}
        l_ev = ledger_by_ent.get((cn, tl)) or ledger_by_ent.get((cn, raw_tl))
        prior_act_str = "--"
        if prev_ent.get("last_action_type"):
            prior_act_str = f"{prev_ent.get('last_action_date', '')}:{prev_ent['last_action_type']}->{prev_ent.get('last_recommended_value', '')}"
        elif l_ev:
            l_dt = str(l_ev.get("change_date") or l_ev.get("changed_at") or l_ev.get("adjusted_at") or "")[:10]
            l_old = l_ev.get("old_val") if l_ev.get("old_val") is not None else l_ev.get("old_value")
            l_new = l_ev.get("new_val") if l_ev.get("new_val") is not None else l_ev.get("new_value")
            prior_act_str = f"{l_dt}:{l_old}->{l_new}({l_ev.get('effect_verdict', '')})"

        today_adj = today_adj_map.get((cn, tl)) or today_adj_map.get((cn, raw_tl))
        today_act_type = today_adj["action_type"] if today_adj else "--"
        today_rec_val = today_adj["recommended_value"] if today_adj else "--"

        ent_record = dict(prev_ent)
        ent_record.update({
            "campaign": cn,
            "match_type": mt,
            "entity": txt,
            "current_bid": eff_bid,
            "state": st,
            "updated_at": gen_date_str,
        })
        if today_adj:
            hist = list(ent_record.get("history") or [])
            if not hist or hist[-1].get("date") != gen_date_str or hist[-1].get("action_type") != today_act_type:
                hist.append({
                    "date": gen_date_str,
                    "action_type": today_act_type,
                    "recommended_value": today_rec_val,
                })
            ent_record["history"] = hist[-8:]
            ent_record["last_action_date"] = gen_date_str
            ent_record["last_action_type"] = today_act_type
            ent_record["last_recommended_value"] = today_rec_val
        entities_mem[ekey] = ent_record

        if sp30 >= 1.0 or od30 >= 1 or today_adj or sp7 > 0:
            ctx_rows.append("\t".join(str(x) for x in [
                cn, "target", mt, txt, st, f"${eff_bid:.2f}", timing_st,
                "--", "--", "--", "--", "--", "--", "--",
                round(sp30, 2), od30, ac30, sp7, od7, ac7, sp3, od3,
                prior_act_str, today_act_type, today_rec_val,
            ]))

    # 2. Also record harvested/new entities from today's adjustments not yet in kws
    for a in (deduped_adjustments or []):
        cn = str(a.get("campaign") or "").strip()
        mt = str(a.get("match_type") or "").lower().strip()
        txt = str(a.get("target_entity") or "").strip()
        tl = txt.lower()
        ekey = f"{cn}::{mt}::{tl}"
        if ekey not in entities_mem and mt in ("exact", "phrase", "broad", "asinsameas"):
            rk = _compute_rank_deltas(tl, xydc_map, sr_map, entity_memory, gen_date_str)
            entities_mem[ekey] = {
                "campaign": cn,
                "match_type": mt,
                "entity": txt,
                "last_action_date": gen_date_str,
                "last_action_type": a.get("action_type", ""),
                "last_recommended_value": a.get("recommended_value", ""),
                "organic_rank": rk["organic_rank"],
                "sp_rank": rk["sp_rank"],
                "history": [{"date": gen_date_str, "action_type": a.get("action_type", ""), "recommended_value": a.get("recommended_value", "")}],
            }

    # Also update xydc terms not in kws so longitudinal rank memory covers all tracked terms
    for kw_x, x_item in (xydc_map or {}).items():
        tl = kw_x.lower().strip()
        if tl not in terms_mem:
            rk = _compute_rank_deltas(tl, xydc_map, sr_map, entity_memory, gen_date_str)
            terms_mem[tl] = {
                "organic_rank": rk["organic_rank"],
                "sp_rank": rk["sp_rank"],
                "prev_organic_rank": rk["prev_organic_rank"],
                "prev_sp_rank": rk["prev_sp_rank"],
                "aba_rank": rk["aba_rank"],
                "last_date": gen_date_str,
            }

    mem_payload = {"updated_at": datetime.now().isoformat(), "entities": entities_mem, "terms": terms_mem}
    mem_file.write_text(json.dumps(mem_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    tsv(
        dest / "entity_full_context.tsv",
        "campaign\tentity_type\tmatch_type\tentity\tstate\tcurrent_bid\ttiming_status\taba_rank\tprev_org_rank\tcurr_org_rank\tdelta_org_rank\tprev_sp_rank\tcurr_sp_rank\tdelta_sp_rank\tspend_30d\tord_30d\tacos_30d\tspend_7d\tord_7d\tacos_7d\tspend_3d\tord_3d\tprior_action\ttoday_action\ttoday_recommended_value",
        ctx_rows,
    )


def assemble_and_write_adjustments(
    args,
    bulk_adjustments,
    camps,
    dayparting_records,
    dayparting_table_lines,
    dest,
    financial_prof,
    gen_time_str,
    kw_misfit_adjustments,
    locked_campaigns,
    meta,
    profile,
    target_acos,
    traffic_prof,
    unnegative_adjustments,
    kws=None,
    kws_7d=None,
    kws_3d=None,
    xydc_map=None,
    sr_map=None,
    deal_info=None,
    targets=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    macro_scorecard=None,
    entity_memory=None,
    ledger=None,
):
    """Merge every adjustment source, apply real-placement & velocity-shrinkage guards, enforce full-context
    entity memory (rank deltas, 30d/7d/3d freshness, anti-flip-flop), resolve single-winner per entity, and write bulk_adjustments.tsv.
    """
    all_final_adjustments = []
    if gen_time_str and len(str(gen_time_str)) >= 8 and str(gen_time_str)[:8].isdigit():
        gs = str(gen_time_str)[:8]
        today_str = f"{gs[:4]}-{gs[4:6]}-{gs[6:8]}"
    else:
        today_str = datetime.now().strftime("%Y-%m-%d")
    shrinkage_guard = bool(macro_scorecard and macro_scorecard.get("velocity_shrinkage_guard"))

    if group_bid_lookup is None or camp_group_bid_lookup is None:
        group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(Path(args.indir))
    camp_base_bid_map = dict(camp_group_bid_lookup or {})

    organic_readiness = traffic_prof.get("organic_readiness", "ORGANIC_BALANCED") if traffic_prof else "ORGANIC_BALANCED"
    is_fragile = (organic_readiness == "ORGANIC_FRAGILE")

    # Build fresh 7d/3d keyword lookups for Rule 38 & full-context checks
    kw_7d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_7d or [])}
    kw_3d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_3d or [])}
    kw_30d_by_camp_ent = {}
    for k in (kws or []):
        cn_k = k.get("campaign_name") or ""
        tl_k = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if cn_k and tl_k and ((cn_k, tl_k) not in kw_30d_by_camp_ent or num(k.get("orders")) > num(kw_30d_by_camp_ent[(cn_k, tl_k)].get("orders"))):
            kw_30d_by_camp_ent[(cn_k, tl_k)] = k

    # 11.1 Placement & Bidding Strategy adjustments (with Real-Placement Protection & Velocity-Shrinkage Guard)
    campaigns_with_placement_change = set()
    camp_new_tos_map = {}
    for c in camps:
        if c["state"] != "enabled":
            continue
        c_assessment = c.get("assessment", "")

        c_kws = [k for k in (kws or []) if (k.get("campaign_name") or k.get("campaign")) == c["name"] and str(k.get("state") or "enabled").lower() != "paused"]
        c_targets = [t for t in (targets or []) if (t.get("campaign_name") or t.get("campaign")) == c["name"] and str(t.get("state") or "enabled").lower() != "paused"]

        target_ent_name = "CampaignBaseBid"
        target_ent_match = "bidding"
        cur_ent_bid = camp_base_bid_map.get(c["name"], 0.30)
        timing_ent_desc = ""
        timing_ent_rec = ""
        single_ent_type_label = "广告活动基础竞价"
        is_single_target = False

        if len(c_kws) == 1 and not c_targets:
            is_single_target = True
            sole_k = c_kws[0]
            kw_t = sole_k.get("keyword_text") or sole_k.get("text") or c["name"]
            target_ent_name = kw_t
            target_ent_match = sole_k.get("match_type") or "exact"
            cur_ent_bid, _ = resolve_effective_entity_bid(sole_k, group_bid_lookup, camp_group_bid_lookup, fallback=cur_ent_bid)
            single_ent_type_label = f"投放词 {kw_t} [{target_ent_match}]"
            if sole_k.get("is_apply_time"):
                app_t = sole_k.get("applied_templates") or []
                t_name = app_t[0].get("name", "") if app_t else "分时策略"
                t_base = num(sole_k.get("timing_base_value")) if sole_k.get("timing_base_value") is not None else cur_ent_bid
                timing_ent_desc = f" [领星分时策略运行中: 模板'{t_name}', 当前分时基准值:${t_base:.2f}]"
                timing_ent_rec = f" 【领星分时联动提示】：当前投放词已绑定分时策略'{t_name}'，请同步在领星后台将分时基准竞价(timing_base_value)更新为 ${{rec_val:.2f}}。"
        elif len(c_targets) == 1 and not c_kws:
            is_single_target = True
            sole_t = c_targets[0]
            tg_t = sole_t.get("targeting_text") or sole_t.get("expression") or sole_t.get("keyword_text") or c["name"]
            ci_sole = extract_asin_target_info(sole_t, group_bid_lookup, camp_group_bid_lookup)
            target_ent_name = ci_sole["asin"] if ci_sole else tg_t
            target_ent_match = "asinSameAs" if ci_sole else (sole_t.get("exp_type_zh") or sole_t.get("exp_type_text") or sole_t.get("match_type") or "target")
            cur_ent_bid, _ = resolve_effective_entity_bid(sole_t, group_bid_lookup, camp_group_bid_lookup, fallback=cur_ent_bid)
            single_ent_type_label = f"投放项 {target_ent_name} [{target_ent_match}]"
            if sole_t.get("is_apply_time"):
                app_t = sole_t.get("applied_templates") or []
                t_name = app_t[0].get("name", "") if app_t else "分时策略"
                t_base = num(sole_t.get("timing_base_value")) if sole_t.get("timing_base_value") is not None else cur_ent_bid
                timing_ent_desc = f" [领星分时策略运行中: 模板'{t_name}', 当前分时基准值:${t_base:.2f}]"
                timing_ent_rec = f" 【领星分时联动提示】：当前投放项已绑定分时策略'{t_name}'，请同步在领星后台将分时基准竞价(timing_base_value)更新为 ${{rec_val:.2f}}。"

        if c_assessment == "STOCKOUT_SUSPENDED":
            base_val = cur_ent_bid
            all_final_adjustments.append({
                "action_type": "Stockout_Conserve_Hold",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${base_val:.2f}",
                "reason": "Listing断货应急休眠：FBA可售库存已归零，失去购物车并由亚马逊系统停推。严禁在无货状态下盲目提价回踩探底，维持当前基础竞价稳态，等待补货入仓。",
            })
            continue

        if c_assessment == "LOW_INVENTORY_THROTTLE":
            base_val = cur_ent_bid
            dos_val = num(profile.get("days_of_supply")) if profile else 0
            all_final_adjustments.append({
                "action_type": "Low_Inventory_Conserve_Hold",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${base_val:.2f}",
                "reason": f"极低库存告急控流：可售天数仅剩 {dos_val:.0f} 天，维持基础竞价稳态控速，平滑过渡至补货上架防断货断崖。",
            })
            continue

        if c_assessment in ("TOP_POSITION_MARKET_COOLDOWN", "DEAL_TRANSITION_COOLDOWN"):
            base_val = cur_ent_bid
            reason_txt = (
                f"广告位首屏黄金位保护：核心词广告排名高居首屏顶部（SP #{c.get('best_sp_rank', 1)}），近3天曝光变动 ({c.get('delta_imp_pct', 0):+.1f}%) 属于自然大盘冷却，绝非基础竞价过低脱靶。保持基础竞价 ${base_val:.2f} 稳态运行。"
                if c_assessment == "TOP_POSITION_MARKET_COOLDOWN"
                else f"秒杀异动保护：商品近期刚结束秒杀/促销活动，近3天曝光变动 ({c.get('delta_imp_pct', 0):+.1f}%) 属于促销红利消退后的正常均值回归，保持基础竞价 ${base_val:.2f} 稳态运行。"
            )
            t_rec = timing_ent_rec.format(rec_val=base_val) if timing_ent_rec else ""
            hierarchy_note = "【竞价生效明确】：单投放活动中按具体投放项独立竞价执行。" if is_single_target else ""
            all_final_adjustments.append({
                "action_type": "Top_Rank_Cooldown_Hold",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${base_val:.2f}",
                "reason": f"{reason_txt}{t_rec} {hierarchy_note}".strip(),
            })
            continue

        if c_assessment == "CIRCUIT_BREAKER_SHOCK":
            base_val = cur_ent_bid
            baseline_cpc = c.get("cpc") or 0.50
            current_tos = c.get("tos", 0)
            target_clearing_bid = round(baseline_cpc / (1.0 + (current_tos / 100.0)), 2)
            probe_val = max(round(base_val * 1.15, 2), round(base_val + 0.05, 2))
            if target_clearing_bid > probe_val:
                probe_val = min(target_clearing_bid, round(base_val * 1.60, 2), round(baseline_cpc, 2))
            is_target_camp = any(x in c["name"].lower() for x in ["asin", "竞品", "b0", "target", "同类", "关联"]) or c.get("targeting") == "TARGETING" or bool(c_targets)
            is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 12.0)
            target_bid_floor = 0.42 if (is_fragile or is_high_refund) else 0.36
            camp_floor = target_bid_floor if is_target_camp else (0.30 if (is_fragile or is_high_refund) else 0.25)
            probe_val = max(camp_floor, probe_val)
            t_rec = timing_ent_rec.format(rec_val=probe_val) if timing_ent_rec else ""
            hierarchy_note = "【竞价生效明确】：请直接修改该投放项基础竞价（及领星分时基准值），广告组默认竞价同步对齐。" if is_single_target else ""
            all_final_adjustments.append({
                "action_type": "Probe_Bid_Recovery",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${probe_val:.2f}",
                "reason": f"AI 熔断纠偏：调后休克断流（近3天日均曝光暴跌 {c.get('delta_imp_pct', 0):+.1f}%），基于基线CPC ${baseline_cpc:.2f}与当前TOS {current_tos:.0f}%倒推回提基础竞价至 ${probe_val:.2f} (底价保护 ${camp_floor:.2f}) 探底拉回展示池。{t_rec} {hierarchy_note}".strip(),
            })
            continue

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
                        "reason": f"观察期紧急熔断：活动持续失血（花费 ${c['spend']:.1f}, 出单 {c['ord']}），紧急下调顶展溢价至 TOS=60%。",
                    })
                    campaigns_with_placement_change.add(c["name"])
                    camp_new_tos_map[c["name"]] = 60.0
                elif c["pp"] >= 80:
                    all_final_adjustments.append({
                        "action_type": "Emergency_Placement_Throttle",
                        "campaign": c["name"],
                        "target_entity": "PlacementProductPage",
                        "match_type": "placement",
                        "current_value": f"PP={int(c['pp'])}%",
                        "recommended_value": "PP=20%",
                        "reason": f"观察期紧急熔断：商品页 PP 溢价过高持续失血（花费 ${c['spend']:.1f}, ACOS {c['acos']:.1f}%），下调至 PP=20%。",
                    })
                    campaigns_with_placement_change.add(c["name"])
            continue

        # Bidding Strategy 2x Amplification Guard (Dynamic Up & Down + High TOS)
        if c_assessment == "UP_AND_DOWN_2X_AMPLIFICATION" or (
            c.get("is_up_and_down")
            and c.get("tos", 0) >= 30
            and c.get("spend", 0) >= 15.0
            and (c.get("ord", 0) == 0 or c.get("acos", 0) > target_acos * 1.25)
        ):
            mult_val = 2.0 * (1.0 + c["tos"] / 100.0)
            all_final_adjustments.append({
                "action_type": "Update_Bidding_Strategy",
                "campaign": c["name"],
                "target_entity": "BiddingStrategy",
                "match_type": "strategy",
                "current_value": f"{c.get('bidding_strategy', '动态竞价-提高和降低')} (TOS={int(c['tos'])}%, 最高叠乘 {mult_val:.2f}x)",
                "recommended_value": "Dynamic Bids - Down Only",
                "reason": f"[竞价策略2倍叠乘止血] 当前开启“提高和降低”叠加 TOS={int(c['tos'])}%，首屏实际最高竞价被放大 {mult_val:.2f} 倍导致高 CPC (${c['cpc']:.2f}) 与高 ACOS ({c['acos']:.1f}%)；切换为“动态竞价-仅降低”消除溢价叠乘。",
            })

        # Velocity-Shrinkage Guard: do NOT slash placement modifiers on converting campaigns within acceptable ACOS!
        if shrinkage_guard and c.get("ord", 0) >= 2 and 0 < c.get("acos", 999.0) <= target_acos * 1.35:
            continue

        c_lower = c["name"].lower()
        is_auto = "自动" in c["name"] or c.get("targeting") == "auto" or "auto" in c_lower
        is_exact = "exact" in c_lower or "精准" in c["name"]
        is_comp = any(x in c_lower for x in ["substitute", "asin", "同类", "关联", "comp", "defense"]) or c.get("targeting") == "targets"
        is_broad = "广泛" in c["name"] or "broad" in c_lower
        real_tos_30 = (c.get("real_placements_30d") or {}).get("TOS") or {}
        real_tos_profitable = (real_tos_30.get("orders", 0) >= 3 and 0 < real_tos_30.get("acos", 999.0) <= target_acos * 1.05)

        if c_assessment == "REAL_TOS_BLEED" and not is_auto and not is_broad:
            new_tos_val = max(20, int(round(c["tos"] * 0.65 / 5.0) * 5))
            rec_pp, rec_ros, p_note = _recommended_pp_ros(c, int(min(c["pp"], 15)), int(min(c["ros"], 15)), target_acos)
            all_final_adjustments.append({
                "action_type": "Placement_TOS_PP_ROS",
                "campaign": c["name"],
                "target_entity": "CampaignPlacement",
                "match_type": "placement",
                "current_value": f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%",
                "recommended_value": f"TOS={new_tos_val}% / PP={rec_pp}% / ROS={rec_ros}%",
                "reason": f"[真实广告位TOS止血] 真实位置报告确认 TOS 花费 ${real_tos_30.get('spend', 0):.2f} ({real_tos_30.get('orders', 0)}单, ACOS {real_tos_30.get('acos', 0):.1f}%) 严重超标，阶梯下调 TOS 至 {new_tos_val}%{p_note}。",
            })
            campaigns_with_placement_change.add(c["name"])
            camp_new_tos_map[c["name"]] = float(new_tos_val)
        elif is_auto and not real_tos_profitable and (c["tos"] >= 100 or c["pp"] >= 30 or c["ros"] >= 30):
            rec_pp, rec_ros, p_note = _recommended_pp_ros(c, 0, 15, target_acos)
            target_tos = max(40, int(c["tos"] * 0.70)) if (is_fragile or shrinkage_guard) else 20
            rec_str = f"TOS={target_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(target_tos)
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "reason": f"自动探索仓均衡位置溢价：下调虚高溢价至 {rec_str}，防范展位漂移至低效商品页失血{p_note}。",
                })
                if not shrinkage_guard:
                    drift_target_name = target_ent_name if is_single_target else "CampaignDefaultBid"
                    drift_match = target_ent_match if is_single_target else "bidding"
                    base_bid = cur_ent_bid if is_single_target else camp_base_bid_map.get(c["name"], 0.35)
                    if base_bid > 0.28 and c.get("acos", 999.0) > target_acos:
                        new_base_bid = max(0.30 if is_fragile else 0.25, round(base_bid * 0.85, 2))
                        drift_rec = timing_ent_rec.format(rec_val=new_base_bid) if timing_ent_rec else ""
                        cur_val_str = f"{single_ent_type_label} (当前基础竞价 ${base_bid:.2f}){timing_ent_desc}" if is_single_target else f"${base_bid:.2f}"
                        all_final_adjustments.append({
                            "action_type": "Anti_Drift_Base_Bid_Throttle",
                            "campaign": c["name"],
                            "target_entity": drift_target_name,
                            "match_type": drift_match,
                            "current_bid_num": base_bid,
                            "current_value": cur_val_str,
                            "recommended_value": f"${new_base_bid:.2f}",
                            "reason": f"自动仓防展位漂移联动控价：削减 TOS 溢价时同步微调基础竞价至 ${new_base_bid:.2f}（降幅15%），防止预算向低效商品页（PP）泄洪。{drift_rec}",
                        })
        elif is_exact and (c["tos"] < 40 or c["pp"] > 15 or c["ros"] > 30):
            # Only boost exact TOS to 50% if exact campaign is NOT bleeding heavily on real TOS
            if real_tos_30.get("spend", 0) >= 15.0 and (real_tos_30.get("orders", 0) == 0 or real_tos_30.get("acos", 0) > target_acos * 1.6):
                target_tos = max(20, min(int(c["tos"]), 35))
            else:
                target_tos = max(50, int(c["tos"])) if c["tos"] < 40 else int(c["tos"])
            rec_pp, rec_ros, p_note = _recommended_pp_ros(c, 0, 10, target_acos)
            rec_str = f"TOS={target_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "reason": f"精准收割仓聚焦首屏顶部促转化，同时基于真实广告位表现精细约束非顶展溢价{p_note}。",
                })
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(target_tos)
        elif is_comp and (c["tos"] > 20 or c["pp"] < 20 or c["ros"] > 20):
            real_pp_30 = (c.get("real_placements_30d") or {}).get("PP") or {}
            pp_bleeding = (real_pp_30.get("spend", 0) >= 15.0 and (real_pp_30.get("orders", 0) == 0 or real_pp_30.get("acos", 0) > target_acos * 1.6))
            rec_pp = 0 if pp_bleeding else 20
            rec_tos = int(min(c["tos"], 20)) if real_tos_profitable else 0
            rec_str = f"TOS={rec_tos}% / PP={rec_pp}% / ROS=0%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "reason": "竞品防御仓基于真实广告位表现收敛无效溢价，聚焦高性价比商品页拦截。",
                })
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(rec_tos)
        elif is_broad and not real_tos_profitable and (c["tos"] >= 120 or c["pp"] >= 40 or c["ros"] >= 40):
            rec_pp, rec_ros, p_note = _recommended_pp_ros(c, 0, 15, target_acos)
            target_tos = max(50, int(c["tos"] * 0.70)) if (is_fragile or shrinkage_guard) else 30
            rec_str = f"TOS={target_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(target_tos)
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "reason": f"广泛拓展仓拉平畸形溢价至 {rec_str}，中低TOS网罗长尾词流防CPC虚高{p_note}。",
                })
                if not shrinkage_guard:
                    drift_target_name = target_ent_name if is_single_target else "CampaignDefaultBid"
                    drift_match = target_ent_match if is_single_target else "bidding"
                    base_bid = cur_ent_bid if is_single_target else camp_base_bid_map.get(c["name"], 0.35)
                    if base_bid > 0.28 and c.get("acos", 999.0) > target_acos:
                        new_base_bid = max(0.30 if is_fragile else 0.25, round(base_bid * 0.85, 2))
                        drift_rec = timing_ent_rec.format(rec_val=new_base_bid) if timing_ent_rec else ""
                        cur_val_str = f"{single_ent_type_label} (当前基础竞价 ${base_bid:.2f}){timing_ent_desc}" if is_single_target else f"${base_bid:.2f}"
                        all_final_adjustments.append({
                            "action_type": "Anti_Drift_Base_Bid_Throttle",
                            "campaign": c["name"],
                            "target_entity": drift_target_name,
                            "match_type": drift_match,
                            "current_bid_num": base_bid,
                            "current_value": cur_val_str,
                            "recommended_value": f"${new_base_bid:.2f}",
                            "reason": f"广泛仓防展位漂移联动控价：削减 TOS 溢价时同步微调基础竞价至 ${new_base_bid:.2f}（降幅15%），封堵长尾预算失控泄洪。{drift_rec}",
                        })
        elif c["tos"] >= 150 and not real_tos_profitable:
            all_final_adjustments.append({
                "action_type": "Placement_TOS",
                "campaign": c["name"],
                "target_entity": "PlacementTop",
                "match_type": "placement",
                "current_value": f"TOS={int(c['tos'])}%",
                "recommended_value": "TOS=60%",
                "reason": "TOS 溢价 >=150% 且顶展投产偏弱，阶梯回调至 TOS=60% 消除点击成本通胀。",
            })
            campaigns_with_placement_change.add(c["name"])
            camp_new_tos_map[c["name"]] = 60.0
        elif c["pp"] >= 80 and c_assessment == "INEFFICIENT_PP":
            all_final_adjustments.append({
                "action_type": "Placement_PP",
                "campaign": c["name"],
                "target_entity": "PlacementProductPage",
                "match_type": "placement",
                "current_value": f"PP={int(c['pp'])}%",
                "recommended_value": "PP=10%",
                "reason": f"商品页 PP 高耗低效（活动 ACOS={c['acos']:.1f}% vs 目标 {target_acos:.0f}%），下调至 PP=10% 止血。",
            })
            campaigns_with_placement_change.add(c["name"])

    # 11.2 Keyword misfit pauses
    all_final_adjustments.extend(kw_misfit_adjustments)

    # 11.3 Mistaken negative recovery actions
    all_final_adjustments.extend(unnegative_adjustments)

    # 11.4 Search terms & ASIN targets adjustments with Double-Damping Mutex & Compensated Floor
    downward_bid_actions = {"Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid"}
    filtered_bulk_adjustments = []
    camp_lookup_all = {c["name"]: c for c in camps if c.get("name")}
    for b_item in bulk_adjustments:
        c_target = b_item.get("campaign", "")
        b_act = b_item.get("action_type", "")
        if c_target in campaigns_with_placement_change and b_act in downward_bid_actions:
            cur_val = b_item.get("current_value", "")
            cur_num = b_item.get("current_bid_num")
            if cur_num is None:
                m = re.search(r"(?:当前基础竞价|当前出价|当前竞价|当前基价=?)\s*\$?([\d.]+)", str(cur_val))
                cur_num = float(m.group(1)) if m else num(cur_val)

            camp_info = camp_lookup_all.get(c_target, {})
            camp_cpc = camp_info.get("cpc", 0.50)
            old_tos = float(camp_info.get("tos", 0.0))
            new_tos = float(camp_new_tos_map.get(c_target, old_tos))
            # Only apply double-damping mutex when TOS was actually reduced!
            if new_tos < old_tos:
                compensated_floor_bid = max(0.30, round(camp_cpc / (1.0 + (new_tos / 100.0)), 2))
                if cur_num < compensated_floor_bid:
                    timing_note = " 建议同步更新领星分时基准值(timing_base_value)。" if "领星分时策略运行中" in str(cur_val) else ""
                    filtered_bulk_adjustments.append({
                        "action_type": "Compensated_Base_Bid_Floor",
                        "campaign": c_target,
                        "target_entity": b_item.get("target_entity", ""),
                        "match_type": b_item.get("match_type", ""),
                        "current_bid_num": cur_num,
                        "current_value": str(cur_val),
                        "recommended_value": f"${compensated_floor_bid:.2f}",
                        "reason": f"【防双重抑制与断流休克】活动 TOS 下调至 {int(new_tos)}%，为防有效竞价跌穿拍卖底线断流，基础竞价托底补偿至 ${compensated_floor_bid:.2f}（拦截原建议 {b_act}）。{timing_note}",
                    })
                else:
                    timing_note = " 保持领星分时基准值不变。" if "领星分时策略运行中" in str(cur_val) else ""
                    filtered_bulk_adjustments.append({
                        "action_type": "Anti_Double_Damping_Hold",
                        "campaign": c_target,
                        "target_entity": b_item.get("target_entity", ""),
                        "match_type": b_item.get("match_type", ""),
                        "current_bid_num": cur_num,
                        "current_value": str(cur_val),
                        "recommended_value": f"${cur_num:.2f}",
                        "reason": f"【双重抑制互斥锁定】本期已将该活动 TOS 下调至 {int(new_tos)}%，基础竞价保持 ${cur_num:.2f} 观察（拦截原建议 {b_act}），严禁同周期既砍溢价又砍基础竞价。{timing_note}",
                    })
                continue
        filtered_bulk_adjustments.append(b_item)

    all_final_adjustments.extend(filtered_bulk_adjustments)

    # 11.45 Fragmented Single-Keyword Campaign Convergence (suppressed during Velocity-Shrinkage Alert on converting islands)
    kw_camp_counts = Counter(k.get("campaign_name") or k.get("campaign") for k in (kws or []))
    exact_prove_camp = find_exact_harvest_campaign(camps, kws or [])
    enabled_exact_texts_in_prove = {
        (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        for k in (kws or [])
        if (k.get("campaign_name") == exact_prove_camp and (k.get("match_type") or "").lower() == "exact" and str(k.get("state") or "enabled").lower() == "enabled")
    }

    for c in camps:
        c_name = c.get("name") or c.get("campaign_name")
        if c.get("state") != "enabled":
            continue
        if c_name == exact_prove_camp or any(x in c_name.lower() for x in ["exact-prove", "手动-广泛", "自动", "auto", "broad", "research", "asin", "target", "竞品"]):
            continue

        kw_cnt = kw_camp_counts.get(c_name, 0)
        sp_30d = num(c.get("spend_30d") or c.get("spend"))
        ord_30d = num(c.get("ord_30d") or c.get("ord"))
        cpc_30d = num(c.get("cpc_30d") or c.get("cpc"))
        if shrinkage_guard and ord_30d >= 1:
            continue

        if kw_cnt in (1, 2) and ord_30d <= 1 and 5.0 <= sp_30d <= 35.0 and c.get("acos", 999.0) > target_acos:
            camp_kws = [k for k in (kws or []) if (k.get("campaign_name") or k.get("campaign")) == c_name]
            kw_text = (camp_kws[0].get("keyword_text") or camp_kws[0].get("text") or "").strip() if camp_kws else ""
            target_bid = max(0.28, round(cpc_30d / 1.40, 2)) if cpc_30d > 0 else 0.35

            if exact_prove_camp and ord_30d >= 1 and kw_text and kw_text.lower() not in enabled_exact_texts_in_prove:
                all_final_adjustments.append({
                    "action_type": "Converge_Harvest_To_Exact",
                    "campaign": exact_prove_camp,
                    "target_entity": kw_text,
                    "match_type": "exact",
                    "current_bid_num": target_bid,
                    "current_value": f"孤岛活动 {c_name} (30d=${sp_30d:.2f}, {int(ord_30d)}单, CPC=${cpc_30d:.2f})",
                    "recommended_value": f"${target_bid:.2f}",
                    "reason": f"【碎片化活动收敛·移入主仓】单活动孤岛分散预算；将出单词迁移至精准主仓 ({exact_prove_camp}) 统一控费基础竞价 ${target_bid:.2f}。",
                })

            all_final_adjustments.append({
                "action_type": "Archive_Fragmented_Campaign",
                "campaign": c_name,
                "target_entity": c_name,
                "match_type": "campaign",
                "current_bid_num": num(c.get("budget")),
                "current_value": f"日预算 ${num(c.get('budget')):.2f}, 30d=${sp_30d:.2f}, {int(ord_30d)}单, ACOS={c.get('acos', 0):.1f}%",
                "recommended_value": "Archive Campaign",
                "reason": f"【碎片化低效孤岛归档】活动仅投放 {kw_cnt} 个词且低产高耗（30d仅 {int(ord_30d)} 单, ACOS {c.get('acos', 0):.1f}%），迁移有效词后归档聚合预算至主仓。",
            })

    # 11.5 Dayparting schedule recommendations
    if dayparting_records:
        total_active_budget = sum(num(c.get("budget")) for c in camps if c.get("state") == "enabled")
        total_daily_spend = sum(num(c.get("spend_30d") or c.get("spend")) / 30.0 for c in camps if c.get("state") == "enabled")
        budget_utilization = (total_daily_spend / total_active_budget) if total_active_budget > 0 else 0.0

        morning_peak = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR" and 6 <= r["hour"] <= 11]
        evening_peak = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR" and 15 <= r["hour"] <= 21]
        bleed_hours = [r["hour"] for r in dayparting_records if r["classification"] == "BLEEDING_OFFPEAK_HOUR"]

        if budget_utilization >= 0.80:
            if morning_peak:
                m_str = ", ".join(f"{h:02d}:00" for h in sorted(morning_peak))
                all_final_adjustments.append({
                    "action_type": "Dayparting_Budget_Pacing",
                    "campaign": "Portfolio/All_Active_SP",
                    "target_entity": f"Morning Peak [{m_str}]",
                    "match_type": "schedule",
                    "current_value": "Baseline Budget/Bid (100%)",
                    "recommended_value": "+0% ~ +5% Neutral Hold",
                    "reason": f"【预算早竭防御】：大盘日预算利用率达 {budget_utilization*100:.1f}% (>=80%)；早间保持平价续航，防止下午前打空预算错失晚间高峰。",
                })
            if evening_peak:
                e_str = ", ".join(f"{h:02d}:00" for h in sorted(evening_peak))
                all_final_adjustments.append({
                    "action_type": "Dayparting_Boost",
                    "campaign": "Portfolio/All_Active_SP",
                    "target_entity": f"Evening Prime Peak [{e_str}]",
                    "match_type": "schedule",
                    "current_value": "Baseline Budget/Bid (100%)",
                    "recommended_value": "+15% ~ +20% Bid Schedule",
                    "reason": "晚间高转化黄金窗口（出单密集且投产优异），重点加注 +15%~+20% 抢占首屏红利。",
                })
        else:
            peak_hours = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR"]
            if peak_hours:
                peak_str = ", ".join(f"{h:02d}:00" for h in sorted(peak_hours))
                all_final_adjustments.append({
                    "action_type": "Dayparting_Boost",
                    "campaign": "Portfolio/All_Active_SP",
                    "target_entity": f"Peak Hours [{peak_str}]",
                    "match_type": "schedule",
                    "current_value": "Baseline Budget/Bid (100%)",
                    "recommended_value": "+15% ~ +20% Bid Schedule",
                    "reason": "高转化黄金时段，分时策略配置 +15%~+20% 溢价放大首屏单量。",
                })

        if bleed_hours:
            bleed_str = ", ".join(f"{h:02d}:00" for h in sorted(bleed_hours))
            rec_sched = "-15% ~ -20% Bid Throttle" if shrinkage_guard else "-25% ~ -30% Bid Throttle"
            all_final_adjustments.append({
                "action_type": "Dayparting_Throttle",
                "campaign": "Portfolio/All_Active_SP",
                "target_entity": f"Bleeding Hours [{bleed_str}]",
                "match_type": "schedule",
                "current_value": "Baseline Budget/Bid (100%)",
                "recommended_value": rec_sched,
                "reason": "低转化高耗散时段（仅浏览少购买），分时策略降权控费，基础竞价折后底价不低于 $0.25。",
            })

    # High Refund Unit-Economics Hard Brake
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= 15.0)
    if is_high_refund and not shrinkage_guard:
        ref_rate = financial_prof.get("refund_rate", 0)
        for c in camps:
            c_n = c.get("name", "")
            if c.get("state") == "enabled" and c.get("acos", 0) > target_acos and any(x in c_n.lower() for x in ["自动", "auto", "broad", "广泛", "拓展"]):
                all_final_adjustments.append({
                    "action_type": "High_Refund_Throttle_Budget",
                    "campaign": c_n,
                    "target_entity": "DailyBudget",
                    "match_type": "budget",
                    "current_bid_num": num(c.get("budget")),
                    "current_value": f"日预算 ${num(c.get('budget')):.2f}",
                    "recommended_value": f"${max(10.0, round(num(c.get('budget')) * 0.70, 2)):.2f}",
                    "reason": f"【高退款率极限制冷】退款率高达 {ref_rate:.1f}% (>=15%) 吞噬毛利；压缩超标泛流预算 30% 收敛至高转化精准词，同步排查退货主因。",
                })

    # 11.6 Unified Dual-Track Rank Synergy Adjustments (gated by 7d/3d freshness, inherited bid, & velocity_shrinkage_guard!)
    if xydc_map and kws:
        camp_lookup_all = {c["name"]: c for c in camps if c.get("name")}
        for k in kws:
            c_name = k.get("campaign_name") or ""
            camp_obj = camp_lookup_all.get(c_name, {})
            if camp_obj.get("state") != "enabled" or c_name in locked_campaigns:
                continue
            kw_text = (k.get("keyword_text") or k.get("targeting_text") or "").strip()
            mt_k = (k.get("match_type") or "exact").lower().strip()
            tl_k = kw_text.lower()
            if not tl_k:
                continue

            k7 = kw_7d_lu.get((c_name, mt_k, tl_k), {})
            k3 = kw_3d_lu.get((c_name, mt_k, tl_k), {})
            kw_state = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()
            if kw_state != "enabled":
                continue

            k_spend = num(k.get("spends"))
            k_sales = num(k.get("sales"))
            k_orders = int(num(k.get("orders")))
            k_clicks = int(num(k.get("clicks")))
            k_bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
            if num(k3.get("bid") or k7.get("bid")) > 0:
                k_bid = num(k3.get("bid") or k7.get("bid"))

            sp7 = num(k7.get("spends") or k7.get("spend") or k7.get("cost")) if k7 else 0.0
            od7 = int(num(k7.get("orders"))) if k7 else 0
            ac7 = acos(sp7, num(k7.get("sales"))) if k7 and num(k7.get("sales")) > 0 else (999.0 if sp7 > 0 else 0.0)
            im7 = int(num(k7.get("impressions"))) if k7 else 0
            sp3 = num(k3.get("spends") or k3.get("spend") or k3.get("cost")) if k3 else 0.0
            od3 = int(num(k3.get("orders"))) if k3 else 0
            im3 = int(num(k3.get("impressions"))) if k3 else 0

            synergy = evaluate_rank_synergy(
                term=kw_text,
                xydc_map=xydc_map,
                spend=k_spend,
                sales=k_sales,
                orders=k_orders,
                clicks=k_clicks,
                target_acos=target_acos or 25.0,
                current_bid=k_bid,
            )
            act_type = synergy.get("action_type")
            if act_type == "Throttle_Cannibalization_Bid":
                # Block during velocity shrinkage, double-damping campaign, zero recent traffic, or already-healthy 7d ACOS!
                if shrinkage_guard or c_name in campaigns_with_placement_change:
                    continue
                if (im3 == 0 and sp7 <= 0.5 and im7 == 0) or (od7 >= 2 and 0 < ac7 <= target_acos * 1.05):
                    continue
            elif act_type == "Boost_Ascender_TOS":
                # Never boost an ascender keyword that is actively bleeding in recent 3d/7d!
                if (sp3 >= 5.0 and od3 == 0) or (sp7 >= 10.0 and od7 == 0) or (od7 >= 1 and ac7 > target_acos * 1.35):
                    continue
            else:
                continue

            timing_note = ""
            if k.get("is_apply_time"):
                app_t = k.get("applied_templates") or []
                tn = app_t[0].get("name", "分时策略") if app_t else "分时策略"
                timing_note = f" 【领星分时联动提示】：已绑定分时模板'{tn}'，请同步更新领星分时基准值(timing_base_value)。"

            all_final_adjustments.append({
                "action_type": act_type,
                "campaign": c_name,
                "target_entity": kw_text,
                "match_type": mt_k,
                "current_bid_num": k_bid,
                "current_value": f"当前基础竞价 ${k_bid:.2f} (自然位#{synergy['organic_rank']}, 广告位#{synergy['sp_rank'] or '--'}, 30d=${k_spend:.1f}/{k_orders}单, 7d=${sp7:.1f}/{od7}单)",
                "recommended_value": synergy["recommended_value"],
                "reason": f"西柚洞察双轨协同：{synergy['reason']}{timing_note}",
            })

    # 11.7 Full-Context Entity Memory Gate (Rank-Slide Veto + Anti-Flip-Flop Continuity + Historical Context Enrichment)
    entities_mem = (entity_memory or {}).get("entities") or {}
    recent_ledger_by_ent = {}
    for l_item in (ledger or []):
        lc = str(l_item.get("campaign") or "").strip()
        ek = str(l_item.get("entity_key") or "")
        parts = ek.split("::")
        ent_txt = parts[-1].lower().strip() if len(parts) >= 3 else ""
        if not ent_txt:
            en = str(l_item.get("entity_name") or "").strip()
            if "[" in en:
                ent_txt = en.split("[")[0].lower().strip()
            else:
                ent_txt = en.lower().strip()
        if lc and ent_txt:
            recent_ledger_by_ent[(lc, ent_txt)] = l_item
            m_asin = re.search(r"(b0[0-9a-z]{8})", ent_txt)
            if m_asin:
                recent_ledger_by_ent[(lc, m_asin.group(1))] = l_item

    memory_vetted_adjustments = []
    for raw_adj in all_final_adjustments:
        adj = dict(raw_adj)
        act = adj.get("action_type", "")
        camp = str(adj.get("campaign") or "").strip()
        ent = str(adj.get("target_entity") or "").strip()
        ent_low = ent.lower()
        mt = str(adj.get("match_type") or "").lower().strip()

        rk = _compute_rank_deltas(ent_low, xydc_map, sr_map, entity_memory, today_str)
        kw_30 = kw_30d_by_camp_ent.get((camp, ent_low)) or {}
        ord_30 = int(num(kw_30.get("orders")))
        k3 = kw_3d_lu.get((camp, mt, ent_low)) or {}
        sp_3 = num(k3.get("spends") or k3.get("spend") or k3.get("cost")) if k3 else 0.0
        od_3 = int(num(k3.get("orders"))) if k3 else 0
        k7 = kw_7d_lu.get((camp, mt, ent_low)) or {}
        od_7 = int(num(k7.get("orders"))) if k7 else 0
        sp_7 = num(k7.get("spends") or k7.get("spend") or k7.get("cost")) if k7 else 0.0
        ac_7 = acos(sp_7, num(k7.get("sales"))) if k7 and num(k7.get("sales")) > 0 else 999.0

        # A) Rank-Slide Protection Veto: Never cut bids on a Top-20 converting keyword whose organic rank dropped >= 4 positions!
        if act in DOWNWARD_BID_ACTIONS and rk["organic_rank"] is not None and rk["organic_rank"] <= 20 and rk["delta_or"] is not None and rk["delta_or"] <= -4 and ord_30 >= 1:
            cur_b = adj.get("current_bid_num") or num(adj.get("current_value")) or 0.35
            adj["action_type"] = "Rank_Slide_Protection_Hold"
            adj["recommended_value"] = f"${cur_b:.2f}"
            adj["reason"] = (
                f"【自然排名滑坡保护锁】监测到该出单词自然排名由 #{rk['prev_organic_rank']} 下滑至 #{rk['organic_rank']} (ΔOR {rk['delta_or']:+d})，"
                f"历史累计出单 {ord_30} 单；严禁在自然位滑坡期叠砍广告基础竞价加速权重崩塌（已拦截原建议 {act}），维持 ${cur_b:.2f} 稳位护盘。"
            )
            act = adj["action_type"]

        # B) Anti-Flip-Flop Continuity & Observation Cooldown Lock
        ekey = f"{camp}::{mt}::{ent_low}"
        prev_e = entities_mem.get(ekey) or {}
        last_act = prev_e.get("last_action_type", "")
        last_dt_str = prev_e.get("last_action_date", "")
        days_since_mem = 999
        if last_dt_str and last_dt_str != today_str:
            try:
                days_since_mem = max(0, (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(last_dt_str[:10], "%Y-%m-%d")).days)
            except Exception:
                days_since_mem = 999

        l_rec = recent_ledger_by_ent.get((camp, ent_low))
        days_since_ledger = 999
        ledger_cut_bid = False
        ledger_raised_bid = False
        if l_rec:
            adj_dt = str(l_rec.get("change_date") or l_rec.get("changed_at") or l_rec.get("adjusted_at") or "")[:10]
            if adj_dt:
                try:
                    days_since_ledger = max(0, (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(adj_dt, "%Y-%m-%d")).days)
                except Exception:
                    days_since_ledger = 999
            if l_rec.get("field") == "bid":
                old_v = num(l_rec.get("old_val"))
                new_v = num(l_rec.get("new_val"))
                if new_v < old_v:
                    ledger_cut_bid = True
                elif new_v > old_v:
                    ledger_raised_bid = True

        effective_cooldown = getattr(args, "cooldown_days", 7)
        mem_in_cooldown = (0 <= days_since_mem <= effective_cooldown)
        ledger_in_cooldown = (0 <= days_since_ledger <= effective_cooldown)
        in_cooldown = mem_in_cooldown or ledger_in_cooldown

        if in_cooldown:
            # 1. Anti-Flip-Flop: Downward -> Upward forbidden unless strongly converting
            if ((last_act in DOWNWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_cut_bid and ledger_in_cooldown)) and act in ("Elevate_To_TOS", "Boost_Ascender_TOS"):
                if not (od_7 >= 2 and ac_7 <= target_acos * 0.85):
                    continue

            # 2. Anti-Flip-Flop: Upward -> Downward forbidden unless severe bleeding
            elif ((last_act in UPWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_raised_bid and ledger_in_cooldown)) and act in DOWNWARD_BID_ACTIONS:
                if not (sp_3 >= 8.0 and od_3 == 0):
                    continue

            # 3. Rule 24 Cooldown Lock (Downward): Never cut bids repeatedly within cooldown window unless entity is actively hemorrhaging!
            elif act in DOWNWARD_BID_ACTIONS and ((last_act in DOWNWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_cut_bid and ledger_in_cooldown)):
                if not (sp_3 >= 8.0 and od_3 == 0):
                    continue

            # 4. Rule 24 Cooldown Lock (Upward): Never repeatedly hike bids within cooldown window when already hiked recently!
            elif act in ("Elevate_To_TOS", "Boost_Ascender_TOS") and ((last_act in UPWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_raised_bid and ledger_in_cooldown)):
                continue

        # C) Enrich reason with longitudinal rank delta & prior entity history so NO prior consideration is ever forgotten
        ctx_badges = []
        if rk["delta_or"] is not None and rk["delta_or"] != 0:
            arrow = "↑" if rk["delta_or"] > 0 else "↓"
            ctx_badges.append(f"自然位#{rk['prev_organic_rank']}→#{rk['organic_rank']}({arrow}{abs(rk['delta_or'])})")
        if rk["delta_sp"] is not None and rk["delta_sp"] != 0:
            arrow_sp = "↑" if rk["delta_sp"] > 0 else "↓"
            ctx_badges.append(f"广告位#{rk['prev_sp_rank']}→#{rk['sp_rank']}({arrow_sp}{abs(rk['delta_sp'])})")
        if rk["aba_rank"] is not None and "ABA" not in str(adj.get("reason", "")):
            ctx_badges.append(f"ABA#{rk['aba_rank']}")

        l_rec = recent_ledger_by_ent.get((camp, ent_low))
        l_old_v = l_rec.get("old_val") if (l_rec and l_rec.get("old_val") is not None) else (l_rec.get("old_value") if l_rec else None)
        l_new_v = l_rec.get("new_val") if (l_rec and l_rec.get("new_val") is not None) else (l_rec.get("new_value") if l_rec else None)
        if l_rec and l_old_v is not None and l_new_v is not None:
            l_dt_s = str(l_rec.get("change_date") or l_rec.get("changed_at") or l_rec.get("adjusted_at") or "")[:10]
            ctx_badges.append(f"前次实调({l_dt_s}):{l_old_v}→{l_new_v}[{l_rec.get('effect_verdict', '')[:6]}]")
        elif last_act and last_dt_str and last_dt_str != today_str:
            ctx_badges.append(f"前次建议({last_dt_str[:10]}):{last_act}→{prev_e.get('last_recommended_value', '')}")

        if ctx_badges:
            adj["reason"] = f"【全量追踪: {' | '.join(ctx_badges)}】{adj.get('reason', '')}"

        memory_vetted_adjustments.append(adj)

    # 11.8 Deterministic Single-Winner Conflict Resolution (0 intra-TSV entity contradictions!)
    deduped_adjustments = resolve_entity_action_conflicts(memory_vetted_adjustments)

    # Fragile Listing Floor Protection SOP
    if is_fragile:
        for item in deduped_adjustments:
            act_i = item.get("action_type", "")
            mt_i = item.get("match_type", "")
            floor_v = 0.42 if mt_i == "asinSameAs" else 0.30
            if ("Bid" in act_i or mt_i in ("bidding", "exact", "phrase", "broad", "asinSameAs")) and act_i not in ("Negative_Exact", "Negative_Phrase", "Negative_Target", "Pause_Keyword", "Remove_Negative"):
                m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", str(item.get("recommended_value", "")))
                if m_bid:
                    bid_val = float(m_bid.group(1))
                    if bid_val < floor_v and "state" not in str(item.get("recommended_value", "")):
                        item["recommended_value"] = f"${floor_v:.2f}"
                        item["reason"] += f" (脆弱品 ${floor_v:.2f} 底价保护)"

    bulk_lines = []
    sanitized_adjustments = []
    for raw_item in deduped_adjustments:
        item = dict(raw_item)
        act = item.get("action_type", "")
        rec = str(item.get("recommended_value", "")).strip()
        cur = str(item.get("current_value", "")).strip()
        rsn = str(item.get("reason", "")).strip()

        # TSV Zero-Placeholder & Pure Value Sanitizer
        if any(x in act for x in ("Bid", "Throttle", "Elevate", "Boost", "Harvest_ASIN")) and "Schedule" not in rec and "Schedule" not in act and "Placement" not in act and "Budget" not in act:
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bid:
                pure_val = f"${float(m_bid.group(1)):.2f}"
                if rec != pure_val:
                    rsn = f"[{rec}] " + rsn
                    item["recommended_value"] = pure_val

        if act == "Unpause_Keyword":
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bid:
                item["recommended_value"] = f"state=enabled (bid=${float(m_bid.group(1)):.2f})"
            else:
                item["recommended_value"] = "state=enabled"
        elif act == "Pause_Keyword":
            item["recommended_value"] = "state=paused"

        if act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact"):
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bid:
                item["recommended_value"] = f"${float(m_bid.group(1)):.2f}"
                if "[Add Exact Target" not in rsn:
                    rsn = f"[Add Exact Target ${float(m_bid.group(1)):.2f}] " + rsn

        if "Current Lowered Bid" in cur:
            item["current_value"] = "$0.30"
        if act == "Probe_Bid_Recovery" and not re.search(r"\$[0-9]", item["recommended_value"]):
            item["recommended_value"] = "$0.45"
            rsn = f"[{rec}] " + rsn

        # Strip any accidental tabs/newlines inside field strings so TSV column count is strictly 7 on every row!
        for fld in ("action_type", "campaign", "target_entity", "match_type", "current_value", "recommended_value"):
            item[fld] = re.sub(r"[\t\r\n]+", " ", str(item.get(fld) or "")).strip()
        rsn = re.sub(r"[\t\r\n]+", " ", rsn).strip()
        item["reason"] = rsn
        sanitized_adjustments.append(item)

        bulk_lines.append("\t".join([
            item["action_type"], item["campaign"], item["target_entity"],
            item["match_type"], item["current_value"], item["recommended_value"], item["reason"]
        ]))
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

    return sanitized_adjustments

def write_observation_status(active_cooldowns, camps, dest):
    """observation_status.tsv -- what is still inside its cooling-off window."""
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
            str((cd.get("changed_at") or "")[:10]),
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


def write_ledger_files(args, dest, gen_time_str, ledger, ledger_dir, meta, profile, snap_key):
    """Persist the historical ledger into the db, the analysis dir, and the export dir."""
    asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
    country_str = str(meta.get("country") or "US").upper()
    file_prefix = f"{asin_str}-{country_str}"
    exp_dir_p = Path(args.export_dir) if args.export_dir else None
    save_ledger_files(ledger, ledger_dir, snap_key, dest, exp_dir_p, file_prefix, gen_time_str)


def write_summary(
    active_cooldowns,
    active_neg_dict,
    asin_campaigns,
    audit_records,
    bleeding_ng,
    camps,
    cannibal_lines,
    dayparting_records,
    deduped_adjustments,
    dest,
    diff_events,
    financial_prof,
    gen_date_str,
    gen_time_str,
    kw_misfit_adjustments,
    ledger,
    locked_campaigns,
    meta,
    placement_diagnoses,
    profile,
    snap_key,
    sr_map,
    traffic_prof,
    xydc_map=None,
    deal_info=None,
    kws=None,
    targets=None,
    macro_scorecard=None,
    real_placement_records=None,
    rules=None,
    xydc_traffic=None,
    xydc_blindspot_count=0,
):
    """summary.txt -- the headline numbers the report template reads."""
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

    if macro_scorecard:
        r7 = macro_scorecard.get("recent_7d") or {}
        p7 = macro_scorecard.get("prior_7d") or {}
        d7 = macro_scorecard.get("deltas_7d_vs_prior7d") or {}
        summary.append(f"macro_north_star_verdict\t{macro_scorecard.get('verdict')}\t{macro_scorecard.get('verdict_cn')}\tshrinkage_guard={macro_scorecard.get('velocity_shrinkage_guard')}")
        summary.append(
            f"macro_7d_vs_prior7d\tsales=${r7.get('daily_sales', 0):.2f}/d(vs ${p7.get('daily_sales', 0):.2f}/d, {d7.get('sales_delta_pct', 0):+.1f}%)"
            f"\tunits={r7.get('daily_units', 0):.2f}/d({d7.get('units_delta_pct', 0):+.1f}%)"
            f"\torganic_units={r7.get('daily_organic_units', 0):.2f}/d({r7.get('organic_unit_share_pct', 0):.1f}%)"
            f"\treal_tacos={r7.get('real_tacos_pct', 0):.2f}%({d7.get('tacos_delta_pp', 0):+.2f}pp)"
        )
        n_exp = macro_scorecard.get("next_7d_expectations") or {}
        summary.append(
            f"macro_next_7d_expectations\tsales_min=${n_exp.get('expected_daily_sales_min', 0):.2f}/d"
            f"\tsales_floor=${n_exp.get('circuit_breaker_daily_sales_floor', 0):.2f}/d"
            f"\ttacos_max={n_exp.get('expected_tacos_max', 0):.1f}%"
            f"\tacos_max={n_exp.get('expected_ad_acos_max', 0):.1f}%"
        )
        if macro_scorecard.get("confounders"):
            summary.append("macro_confounders\t" + " | ".join(f"{c['code']}:{c['detail']}" for c in macro_scorecard["confounders"]))

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
    summary.append(f"real_placement_rows_loaded\t{len(real_placement_records or [])}")
    up_down_camps = [c["name"] for c in enabled if c.get("is_up_and_down")]
    if up_down_camps:
        summary.append("up_and_down_strategy_campaigns\t" + " | ".join(up_down_camps[:12]))
    high_tos = [c["name"] for c in enabled if c["tos"] >= 180]
    summary.append("high_tos>=180\t" + " | ".join(high_tos[:12]))
    if rules and rules.get("_quarantined_is_not"):
        summary.append("quarantined_self_contradictory_rules\t" + ", ".join(str(x) for x in rules["_quarantined_is_not"]))
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
    summary.append(f"database_entity_memory_saved\tdb/entity_memory/{snap_key}_memory.json")
    summary.append(f"database_expectations_saved\tdb/expectations/{snap_key}_expectations.json")
    summary.append(f"searchrank_keywords_tracked\t{len(sr_map)}")
    trapped_count = sum(1 for d in placement_diagnoses if d.get("strategy_tag") == "TRAPPED_CORE_HERO")
    failed_tos_count = sum(1 for d in placement_diagnoses if d.get("strategy_tag") == "FAILED_AT_TOS")
    summary.append(f"placement_diagnosed_queries\t{len(placement_diagnoses)}")
    summary.append(f"trapped_core_heroes_elevate\t{trapped_count}")
    summary.append(f"premier_tos_failed_throttle\t{failed_tos_count}")
    protected_strong_count = sum(1 for a in deduped_adjustments if a.get("action_type") in ("Refine_Bid_Placement", "Elevate_To_TOS", "Throttle_TOS_Bid", "Rank_Slide_Protection_Hold"))
    summary.append(f"strong_relevance_queries_protected\t{protected_strong_count}")
    competitor_actions_count = sum(1 for a in deduped_adjustments if a.get("match_type") == "asinSameAs")
    summary.append(f"competitor_asin_actions\t{competitor_actions_count}")
    if financial_prof:
        summary.append(f"financial_gross_profit\t${financial_prof.get('gross_profit', 0):.2f}\tmargin={financial_prof.get('gross_margin', 0):.1f}%\tbreak_even_acos={financial_prof.get('break_even_acos', 0):.1f}%\ttotal_ad_cost=${financial_prof.get('total_ad_cost', 0):.2f}")
        summary.append(f"financial_cost_structure\tcogs=${financial_prof.get('cogs_per_unit', 0):.2f}\tfreight=${financial_prof.get('logistics_per_unit', 0):.2f}\tfba=${financial_prof.get('fba_per_unit', 0):.2f}\tcomm=${financial_prof.get('comm_per_unit', 0):.2f}\tstorage=${financial_prof.get('storage_fee_total', 0):.2f}")
        if financial_prof.get("is_cross_shop_asin") and financial_prof.get("omni_shop_summary"):
            omni = financial_prof["omni_shop_summary"]
            summary.append(f"cross_shop_omni_summary\tshops={omni.get('shop_count')}\tomni_sales=${omni.get('omni_total_sales', 0):.2f}\tomni_gp=${omni.get('omni_gross_profit', 0):.2f}")
    if traffic_prof:
        tot_sess = traffic_prof.get("sessions_total", 0)
        mob_sess = traffic_prof.get("sessions_mobile", 0)
        mob_share = (mob_sess / tot_sess * 100) if tot_sess > 0 else 0.0
        summary.append(f"listing_traffic_macro\tsessions={tot_sess}\tpageviews={traffic_prof.get('page_views_total', 0)}\tmobile_share={mob_share:.1f}%\tbuy_box={traffic_prof.get('buy_box_percentage', 0):.1f}%\tattribution_lag={traffic_prof.get('attribution_lag', False)}")
        summary.append(f"listing_cvr_comparison\tad_cvr={traffic_prof.get('ad_cvr', 0):.2f}%\tnature_cvr={traffic_prof.get('nature_cvr', 0):.2f}%\ttacos={traffic_prof.get('tacos', 0):.2f}%\tadv_sales_share={traffic_prof.get('adv_sales_share', 0):.2f}%")
    if dayparting_records:
        peak_hrs = [f"{r['hour']:02d}:00" for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR"]
        bleed_hrs = [f"{r['hour']:02d}:00" for r in dayparting_records if r["classification"] == "BLEEDING_OFFPEAK_HOUR"]
        summary.append(f"dayparting_peak_hours\t{', '.join(peak_hrs) if peak_hrs else 'None'}")
        summary.append(f"dayparting_bleeding_hours\t{', '.join(bleed_hrs) if bleed_hrs else 'None'}")
    if kws:
        timing_kws = [k for k in kws if k.get("is_apply_time")]
        summary.append(f"dayparting_keywords_active\t{len(timing_kws)} / {len(kws)}")
    if targets:
        timing_targets = [t for t in targets if t.get("is_apply_time")]
        summary.append(f"dayparting_targets_active\t{len(timing_targets)} / {len(targets)}")
    all_timing_templates = set()
    for item in (kws or []) + (targets or []):
        if item.get("is_apply_time"):
            for t in item.get("applied_templates") or []:
                if t.get("name"):
                    all_timing_templates.add(t.get("name"))
    if all_timing_templates:
        summary.append(f"dayparting_active_templates\t{', '.join(sorted(all_timing_templates))}")
    if diff_events:
        summary.append(f"diff_events_detected\t{' ; '.join(diff_events[:5])}")
    if xydc_map:
        t1_cnt = sum(1 for v in xydc_map.values() if v.get("organic_rank") and v.get("organic_rank") <= 3)
        t2_cnt = sum(1 for v in xydc_map.values() if v.get("organic_rank") and 4 <= v.get("organic_rank") <= 15)
        t3_cnt = sum(1 for v in xydc_map.values() if v.get("organic_rank") and 16 <= v.get("organic_rank") <= 50)
        t4_cnt = sum(1 for v in xydc_map.values() if (v.get("organic_rank") is None or v.get("organic_rank") > 50) and (v.get("sp_rank") and v.get("sp_rank") <= 10))
        summary.append(f"xydc_ranks_total_tracked\t{len(xydc_map)}")
        summary.append(f"xydc_tier1_dominance_count\t{t1_cnt}")
        summary.append(f"xydc_tier2_ascender_count\t{t2_cnt}")
        summary.append(f"xydc_tier3_breakthrough_count\t{t3_cnt}")
        summary.append(f"xydc_tier4_disconnect_count\t{t4_cnt}")
        summary.append(f"xydc_tier5_blindspot_count\t{int(xydc_blindspot_count or 0)}")
    if xydc_traffic and isinstance(xydc_traffic, dict):
        st7 = xydc_traffic.get("self_traffic_7d") or {}
        if st7:
            summary.append(
                f"xydc_self_traffic_7d\ttotal_score={num(st7.get('total_traffic_score')):.1f}"
                f"\torg_score={num(st7.get('organic_traffic_score')):.1f}({num(st7.get('organic_share_pct')):.1f}%)"
                f"\tad_score={num(st7.get('ad_traffic_score')):.1f}({num(st7.get('ad_share_pct')):.1f}%)"
                f"\torg_growth={num(st7.get('organic_growth_pct')):+.1f}%"
                f"\tad_growth={num(st7.get('ad_growth_pct')):+.1f}%"
                f"\torg_kw_cnt={int(num(st7.get('organic_keyword_count')))}"
                f"\tad_kw_cnt={int(num(st7.get('ad_keyword_count')))}"
            )
        p_ov = xydc_traffic.get("parent_ad_overview_7d") or {}
        if p_ov:
            ch = p_ov.get("channel_distribution") or {}
            summary.append(
                f"xydc_parent_channel_split\tparent={p_ov.get('parent_asin') or '--'}"
                f"\tvariations={p_ov.get('advertised_variation_count', 0)}/{p_ov.get('total_variation_count', 0)}"
                f"\torganic={num((ch.get('organic') or {}).get('share_pct')):.1f}%"
                f"\tsp={num((ch.get('sp') or {}).get('share_pct')):.1f}%"
                f"\tsb={num((ch.get('sb') or {}).get('share_pct')):.1f}%"
                f"\tsbv={num((ch.get('sbv') or {}).get('share_pct')):.1f}%"
            )
        kc_tr = xydc_traffic.get("keyword_count_trend") or {}
        if kc_tr:
            snap_kc = kc_tr.get("latest_snapshot") or {}
            summary.append(
                f"xydc_keyword_count_14d_trend\torg_7d_avg={num(kc_tr.get('recent_7d_avg_organic_keywords')):.1f}"
                f"({num(kc_tr.get('organic_keywords_delta_pct')):+.1f}%)"
                f"\tad_7d_avg={num(kc_tr.get('recent_7d_avg_ad_keywords')):.1f}"
                f"({num(kc_tr.get('ad_keywords_delta_pct')):+.1f}%)"
                f"\thead={snap_kc.get('head_count', 0)}\tmid={snap_kc.get('mid_tail_count', 0)}\tlong={snap_kc.get('long_tail_count', 0)}"
            )
        bsr_tr = xydc_traffic.get("bsr_trend") or {}
        leaf_b = bsr_tr.get("leaf_category") if isinstance(bsr_tr, dict) else None
        if isinstance(leaf_b, dict) and leaf_b.get("latest_rank"):
            summary.append(
                f"xydc_bsr_leaf_14d_trend\tcategory={leaf_b.get('category_name')}"
                f"\tlatest=#{leaf_b.get('latest_rank')}\tavg_7d=#{leaf_b.get('recent_7d_avg_rank')}"
                f"\tprior_7d=#{leaf_b.get('prior_7d_avg_rank')}\tchange={num(leaf_b.get('rank_change_7d')):+.1f}"
            )
    if deal_info:
        summary.append(f"deal_status_badge\t{deal_info.get('summary_badge', 'NONE')}")
        summary.append(f"deal_status_desc\t{deal_info.get('summary_desc', '无近期秒杀/促销')}")
        summary.append(f"deal_is_transition\t{deal_info.get('is_deal_transition', False)}")
        summary.append(f"deal_last_date\t{deal_info.get('last_deal_date') or '--'}")
    summary.append(f"generation_timestamp\t{gen_time_str}")
    dest.joinpath("summary.txt").write_text("\n".join(summary), encoding="utf-8")

    return summary


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
    parser.add_argument("--shop", default=None, help="Optional shop alias/name for multi-shop portfolio isolation.")
    args = parser.parse_args()

    src = Path(args.indir)
    dest = Path(args.outdir)
    dest.mkdir(parents=True, exist_ok=True)

    known_negatives = load_known_negatives(args=args, src=src)
    dayparting_records, dayparting_table_lines, financial_prof, gen_time_str, meta, profile, rules, target_acos, traffic_prof, deal_info = load_product_context(
        args=args,
        dest=dest,
        src=src,
    )
    asin_campaigns, camps, kws, kws_3d, kws_7d, targets, targets_3d, targets_7d = load_report_frames(
        meta=meta,
        profile=profile,
        src=src,
    )
    group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(src=src, asin_campaigns=asin_campaigns)
    real_placement_records = load_real_placement_report(src=src, camps=camps, dest=dest)
    sr_map = load_searchrank_map(src=src)
    xydc_map = load_xydc_ranks(src=src)
    xydc_traffic = load_xydc_traffic(src=src)

    active_cooldowns, daily_records_dir, db_root, diff_events, gen_date_str, ledger, ledger_dir, legacy_key, locked_campaigns, snap_key = run_state_tracking(
        args=args,
        camps=camps,
        dayparting_records=dayparting_records,
        financial_prof=financial_prof,
        kws=kws,
        meta=meta,
        profile=profile,
        src=src,
        targets=targets,
        traffic_prof=traffic_prof,
        kws_3d=kws_3d,
        kws_7d=kws_7d,
        targets_3d=targets_3d,
        targets_7d=targets_7d,
        xydc_map=xydc_map,
        deal_info=deal_info,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
    )
    entity_memory = load_entity_memory(db_root=db_root, snap_key=snap_key, legacy_key=legacy_key)
    macro_scorecard = evaluate_macro_scorecard(
        src=src,
        dest=dest,
        export_dir=getattr(args, "export_dir", None),
        db_root=db_root,
        snap_key=snap_key,
        legacy_key=legacy_key,
        gen_time_str=gen_time_str,
        meta=meta,
        profile=profile,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        camps=camps,
        deal_info=deal_info,
        target_acos=target_acos,
    )
    write_campaigns_tsv(camps=camps, dest=dest)
    assess_and_write_placements(
        active_cooldowns=active_cooldowns,
        args=args,
        camps=camps,
        dest=dest,
        locked_campaigns=locked_campaigns,
        target_acos=target_acos,
        traffic_prof=traffic_prof,
        kws=kws,
        xydc_map=xydc_map,
        deal_info=deal_info,
        financial_prof=financial_prof,
        profile=profile,
    )
    now_iso = datetime.now().isoformat()
    archive_daily_record(
        daily_records_dir, snap_key, gen_date_str, now_iso,
        meta, profile, financial_prof, traffic_prof, camps, dayparting_records
    )
    write_keywords_tsv(
        dest=dest,
        kws=kws,
        sr_map=sr_map,
        xydc_map=xydc_map,
        kws_7d=kws_7d,
        kws_3d=kws_3d,
        target_acos=target_acos,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
    )
    active_neg_dict, all_neg_queries, audit_records, bucket_stats, bulk_adjustments, camp_lookup, cannibal_map, harvest, kw_misfit_adjustments, ngram_stats, placement_diagnoses, st_lines, unnegative_adjustments, waste = analyze_search_terms(
        asin_campaigns=asin_campaigns,
        camps=camps,
        known_negatives=known_negatives,
        kws=kws,
        kws_3d=kws_3d,
        kws_7d=kws_7d,
        profile=profile,
        rules=rules,
        sr_map=sr_map,
        src=src,
        target_acos=target_acos,
        xydc_map=xydc_map,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        targets=targets,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        macro_scorecard=macro_scorecard,
        entity_memory=entity_memory,
    )
    write_search_term_outputs(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        harvest=harvest,
        meta=meta,
        placement_diagnoses=placement_diagnoses,
        profile=profile,
        st_lines=st_lines,
        waste=waste,
    )
    _, blindspot_rows = write_rank_synergy_tsv(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        kws=kws,
        meta=meta,
        profile=profile,
        target_acos=target_acos,
        xydc_map=xydc_map,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        camps=camps,
        rules=rules,
        sr_map=sr_map,
        entity_memory=entity_memory,
        gen_date_str=gen_date_str,
    )
    write_xydc_traffic_and_competitor_tsvs(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        meta=meta,
        profile=profile,
        target_acos=target_acos,
        targets=targets,
        xydc_traffic=xydc_traffic,
        camp_lookup=camp_lookup,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        src=src,
        asin_campaigns=asin_campaigns,
    )
    write_buckets_tsv(bucket_stats=bucket_stats, dest=dest)
    bleeding_ng = write_ngram_outputs(
        all_neg_queries=all_neg_queries,
        bulk_adjustments=bulk_adjustments,
        dest=dest,
        known_negatives=known_negatives,
        ngram_stats=ngram_stats,
        profile=profile,
        rules=rules,
    )
    cannibal_lines = write_cannibalization_tsv(cannibal_map=cannibal_map, dest=dest)
    write_groups_tsv(asin_campaigns=asin_campaigns, dest=dest, src=src)
    write_targets_tsv(
        bulk_adjustments=bulk_adjustments,
        camp_lookup=camp_lookup,
        dest=dest,
        profile=profile,
        target_acos=target_acos,
        targets=targets,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        targets_7d=targets_7d,
        targets_3d=targets_3d,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        macro_scorecard=macro_scorecard,
        locked_campaigns=locked_campaigns,
    )
    write_negative_tsvs(active_neg_dict=active_neg_dict, audit_records=audit_records, dest=dest)
    deduped_adjustments = assemble_and_write_adjustments(
        args=args,
        bulk_adjustments=bulk_adjustments,
        camps=camps,
        dayparting_records=dayparting_records,
        dayparting_table_lines=dayparting_table_lines,
        dest=dest,
        financial_prof=financial_prof,
        gen_time_str=gen_time_str,
        kw_misfit_adjustments=kw_misfit_adjustments,
        locked_campaigns=locked_campaigns,
        meta=meta,
        profile=profile,
        target_acos=target_acos,
        traffic_prof=traffic_prof,
        unnegative_adjustments=unnegative_adjustments,
        kws=kws,
        kws_7d=kws_7d,
        kws_3d=kws_3d,
        xydc_map=xydc_map,
        sr_map=sr_map,
        deal_info=deal_info,
        targets=targets,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        macro_scorecard=macro_scorecard,
        entity_memory=entity_memory,
        ledger=ledger,
    )
    sync_and_write_entity_full_context(
        db_root=db_root,
        dest=dest,
        snap_key=snap_key,
        gen_date_str=gen_date_str,
        entity_memory=entity_memory,
        kws=kws,
        kws_7d=kws_7d,
        kws_3d=kws_3d,
        targets=targets,
        targets_7d=targets_7d,
        targets_3d=targets_3d,
        xydc_map=xydc_map,
        sr_map=sr_map,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        ledger=ledger,
        deduped_adjustments=deduped_adjustments,
    )
    write_observation_status(active_cooldowns=active_cooldowns, camps=camps, dest=dest)
    write_ledger_files(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        ledger=ledger,
        ledger_dir=ledger_dir,
        meta=meta,
        profile=profile,
        snap_key=snap_key,
    )
    summary = write_summary(
        active_cooldowns=active_cooldowns,
        active_neg_dict=active_neg_dict,
        asin_campaigns=asin_campaigns,
        audit_records=audit_records,
        bleeding_ng=bleeding_ng,
        camps=camps,
        cannibal_lines=cannibal_lines,
        dayparting_records=dayparting_records,
        deduped_adjustments=deduped_adjustments,
        dest=dest,
        diff_events=diff_events,
        financial_prof=financial_prof,
        gen_date_str=gen_date_str,
        gen_time_str=gen_time_str,
        kw_misfit_adjustments=kw_misfit_adjustments,
        ledger=ledger,
        locked_campaigns=locked_campaigns,
        meta=meta,
        placement_diagnoses=placement_diagnoses,
        profile=profile,
        snap_key=snap_key,
        sr_map=sr_map,
        traffic_prof=traffic_prof,
        xydc_map=xydc_map,
        deal_info=deal_info,
        kws=kws,
        targets=targets,
        macro_scorecard=macro_scorecard,
        real_placement_records=real_placement_records,
        rules=rules,
        xydc_traffic=xydc_traffic,
        xydc_blindspot_count=len(blindspot_rows),
    )

    print(f"Analysis complete. Tables saved to {dest}")
    print("\n".join(summary[:18]))


if __name__ == "__main__":
    main()
