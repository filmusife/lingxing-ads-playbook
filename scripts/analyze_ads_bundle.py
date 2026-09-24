# -*- coding: utf-8 -*-
"""Turn fetched Lingxing JSON into product-fit analysis tables.

User-facing copy should say 基础竞价, never BID. Default evidence window is 30 days.
Query buckets are a weak prior from title/brand/ASIN overlap only.
The handbook must re-bucket from that product's feature card for ANY category.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from config_snapshot import (
    diffs_to_cooldowns,
    diff_independent_vars,
    extract_config,
    keep_active_cooldowns,
    load_snapshot,
    merge_locks,
    save_snapshot,
    snapshot_path,
)
import product_scope as scope

# Weak prior only. Category-specific IS NOT belongs in the handbook feature card,
# never here. Optional indir/product_rules.json may add extra mismatch regexes.


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
    return json.loads(path.read_text(encoding="utf-8"))


def num(x, default=0.0):
    if x is None or x == "" or x == "--":
        return default
    try:
        return float(str(x).replace("%", "").replace(",", ""))
    except Exception:
        return default


def acos(spend, sales):
    if sales <= 0:
        return 9999.0 if spend > 0 else 0.0
    return spend / sales * 100


def skip_total(row: dict) -> bool:
    name = str(row.get("name") or row.get("campaign_name") or row.get("keyword_text") or row.get("query") or "").strip()
    return not name and not row.get("campaign_id") and not row.get("keyword_id")


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
    return {
        "asin": item.get("asin") or s0.get("asin"),
        "parent_asin": item.get("parent_asin"),
        "msku": item.get("msku") or s0.get("seller_sku"),
        "local_sku": item.get("local_sku") or s0.get("sku"),
        "title": title,
        "brand": brand,
        "price": item.get("listing_price") or item.get("landed_price") or item.get("price"),
        "stars": item.get("stars"),
        "reviews": item.get("reviews_num"),
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
        "open_date": item.get("open_date") or item.get("open_date_time"),
        "first_order_time": item.get("first_order_time"),
        "landed_price": item.get("landed_price"),
        "listing_price": item.get("listing_price"),
        "regular_price": item.get("regular_price") or item.get("list_price"),
        "history_price": item.get("history_price"),
        "amz_product_type": item.get("amz_product_type"),
        "category_text": item.get("category_text"),
        "inbound_working": item.get("afn_inbound_working_quantity") or s0.get("afn_inbound_working_quantity"),
        "inbound_shipped": item.get("afn_inbound_shipped_quantity") or s0.get("afn_inbound_shipped_quantity"),
        "inbound_receiving": item.get("afn_inbound_receiving_quantity") or s0.get("afn_inbound_receiving_quantity"),
        "unsellable": item.get("afn_unsellable_quantity") or s0.get("afn_unsellable_quantity"),
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
        strategy = bidding.get("strategy") if isinstance(bidding, dict) else r.get("bidding_strategy")
        out.append({
            "name": r.get("name"),
            "id": r.get("campaign_id"),
            "state": r.get("state"),
            "targeting": r.get("targeting_type"),
            "sponsored": r.get("sponsored_type"),
            "strategy": strategy or "",
            "budget": num(r.get("daily_budget") or r.get("budget")),
            "tos": tos, "pp": pp, "ros": ros,
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
        })
    return sorted(out, key=lambda x: -x["spend"])


def load_mismatch_rules(indir: Path) -> list[tuple[str, str]]:
    path = indir / "product_rules.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rules = raw.get("mismatch") or raw.get("is_not") or []
    out = []
    for item in rules:
        if isinstance(item, dict) and item.get("pattern"):
            out.append((str(item["pattern"]), str(item.get("label") or "mismatch")))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), str(item[1])))
    return out


def classify_query(q: str, profile: dict, extra_rules: list[tuple[str, str]] | None = None) -> str:
    ql = (q or "").lower()
    brand = str(profile.get("brand") or "").lower()
    if re.search(r"b0[0-9a-z]{8}", ql):
        return "asin"
    if brand and len(brand) >= 3 and brand in ql:
        return "brand"
    for pat, label in extra_rules or []:
        try:
            if re.search(pat, ql, re.I):
                return label
        except re.error:
            continue
    title = str(profile.get("title") or "").lower()
    title_tokens = set(re.findall(r"[a-z0-9]+", title))
    q_tokens = set(re.findall(r"[a-z0-9]+", ql))
    if title_tokens and len(q_tokens & title_tokens) >= 2:
        return "core_or_title"
    return "other"


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load_keyword_analysis(src: Path) -> dict[str, dict]:
    rows = as_rows(load_json(src / "keyword_analysis.json"))
    out: dict[str, dict] = {}
    for row in rows:
        key = norm_text(row.get("keyword_text") or row.get("key") or row.get("query") or "")
        if not key:
            continue
        prev = out.get(key)
        if prev is None:
            out[key] = row
            continue
        prev_rank = num(prev.get("searchrank"), 0)
        cur_rank = num(row.get("searchrank"), 0)
        if prev_rank <= 0 < cur_rank:
            out[key] = row
        elif num(row.get("impressions")) > num(prev.get("impressions")):
            out[key] = row
    return out


def volume_cuts(index: dict[str, dict]) -> tuple[float, float] | None:
    ranks = sorted(num(r.get("searchrank"), 0) for r in index.values() if num(r.get("searchrank"), 0) > 0)
    if len(ranks) < 4:
        return None
    return ranks[max(0, len(ranks) // 3 - 1)], ranks[max(0, (2 * len(ranks)) // 3 - 1)]


def volume_band(searchrank: float, impressions: float, cuts: tuple[float, float] | None) -> str:
    if searchrank and searchrank > 0:
        if cuts:
            p33, p66 = cuts
            if searchrank <= p33:
                return "head"
            if searchrank <= p66:
                return "torso"
            return "tail"
        if searchrank <= 8000:
            return "head"
        if searchrank <= 40000:
            return "torso"
        return "tail"
    if impressions >= 50000:
        return "head_proxy"
    if impressions >= 10000:
        return "torso_proxy"
    if impressions > 0:
        return "tail_proxy"
    return "unknown"


def lookup_volume(index: dict[str, dict], text: str, cuts: tuple[float, float] | None) -> dict:
    row = index.get(norm_text(text)) or {}
    rank = num(row.get("searchrank"), 0)
    impr = num(row.get("impressions"), 0)
    return {
        "searchrank": int(rank) if rank > 0 else "",
        "ka_impr": int(impr) if impr else "",
        "ka_ctr": round(num(row.get("ctr")), 2) if row else "",
        "ka_cvr": round(num(row.get("cvr")), 1) if row else "",
        "vol_band": volume_band(rank, impr, cuts),
        "row": row,
    }


def related_bucket(bucket: str) -> bool:
    return bucket in {"core_or_title", "brand"}


def diagnosis_hint(bucket: str, vol: dict, clk: float, ordn: float, spend: float, ctr: float, cvr: float, head_median_impr: float) -> str:
    band = vol.get("vol_band") or "unknown"
    ka_impr = num(vol.get("ka_impr"), 0)
    is_head = band in {"head", "head_proxy", "torso", "torso_proxy"}
    under_exposed = False
    if band in {"head", "head_proxy"} and ka_impr:
        if ka_impr < 8000:
            under_exposed = True
        elif head_median_impr > 0 and ka_impr < head_median_impr * 0.15:
            under_exposed = True
    if bucket == "asin":
        return "asin_win_matrix_first"
    if related_bucket(bucket) and is_head and under_exposed:
        return "underbid_or_placement_do_not_negate"
    if related_bucket(bucket) and ctr >= 0.4 and clk >= 15 and ordn == 0:
        return "listing_or_feature_visibility_do_not_negate"
    if related_bucket(bucket) and clk >= 20 and ordn == 0:
        return "pause_observe_not_negate"
    if related_bucket(bucket) and is_head and spend >= 8 and ordn == 0:
        return "head_term_fix_bid_or_listing_not_negate"
    if (not related_bucket(bucket)) and spend >= 8 and ordn == 0 and clk >= 20:
        return "negate_only_after_feature_card_confirms_is_not"
    if related_bucket(bucket):
        return "keep_is_related"
    return ""




def tsv(path: Path, header: str, lines: list[str]) -> None:
    path.write_text(header + "\n" + "\n".join(lines), encoding="utf-8")



ADS_LOG_TYPES = {
    501: ("campaign", "placement_budget_state"),
    502: ("ad_group", "default_bid"),
    503: ("product_ad", "product_ad_state"),
    504: ("targeting", "keyword_target_bid"),
    505: ("negative", "negatives"),
}


def find_auto_log_maps(obj):
    found = []
    stack = [obj]
    seen = 0
    while stack and seen < 40:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            if isinstance(cur.get("auto_log_data"), dict):
                found.append(cur["auto_log_data"])
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for x in cur[:20]:
                if isinstance(x, (dict, list)):
                    stack.append(x)
    return found


def parse_ops_ads_events(raw) -> list[dict]:
    events = []
    for mapping in find_auto_log_maps(raw):
        for day, payload in mapping.items():
            logs = (payload or {}).get("log_data") if isinstance(payload, dict) else None
            if not logs:
                continue
            for item in logs:
                if not isinstance(item, dict):
                    continue
                type_id = item.get("type_id")
                try:
                    type_id = int(type_id)
                except (TypeError, ValueError):
                    continue
                if type_id not in ADS_LOG_TYPES:
                    continue
                layer, lever = ADS_LOG_TYPES[type_id]
                remark = str(item.get("remark") or "")
                names = []
                for chunk in re.split(r"[、,;；]", remark):
                    chunk = chunk.strip()
                    chunk = re.sub(r"^\[.*?\]\s*(修改|新建)", "", chunk).strip(" ，,;")
                    if chunk and ("UL-" in chunk or "US-" in chunk or "SP-" in chunk or "SB" in chunk):
                        names.append(chunk)
                events.append({
                    "date": str(day)[:10],
                    "type_id": type_id,
                    "layer": layer,
                    "lever": lever,
                    "remark": remark,
                    "campaigns": names,
                })
    events.sort(key=lambda x: (x["date"], x["type_id"]))
    return events


def flatten_daily_campaigns(raw) -> list[dict]:
    days = raw if isinstance(raw, list) else []
    out = []
    for item in days:
        if not isinstance(item, dict):
            continue
        day = item.get("date")
        for row in item.get("rows") or []:
            if not isinstance(row, dict) or skip_total(row):
                continue
            name = row.get("name") or row.get("campaign_name")
            if not name:
                continue
            out.append({
                "date": day,
                "name": name,
                "state": row.get("state"),
                "tos": num(row.get("placement_top")),
                "budget": num(row.get("daily_budget") or row.get("budget")),
                "spend": num(row.get("spends")),
                "cpc": num(row.get("cpc")),
                "clicks": num(row.get("clicks")),
                "orders": num(row.get("orders")),
                "impr": num(row.get("impressions")),
            })
    return out


def detect_daily_breaks(daily_rows: list[dict]) -> list[dict]:
    by_name = defaultdict(list)
    for row in daily_rows:
        by_name[row["name"]].append(row)
    breaks = []
    for name, series in by_name.items():
        series = sorted(series, key=lambda x: x["date"] or "")
        for prev, cur in zip(series, series[1:]):
            reasons = []
            if prev["spend"] >= 5:
                if cur["spend"] <= prev["spend"] * 0.45:
                    reasons.append(f"spend {prev['spend']:.1f}->{cur['spend']:.1f}")
                elif cur["spend"] >= prev["spend"] * 2.2:
                    reasons.append(f"spend {prev['spend']:.1f}->{cur['spend']:.1f}")
            if prev["clicks"] >= 8 and cur["clicks"] >= 8 and prev["cpc"] > 0:
                if cur["cpc"] <= prev["cpc"] * 0.7 or cur["cpc"] >= prev["cpc"] * 1.35:
                    reasons.append(f"cpc {prev['cpc']:.2f}->{cur['cpc']:.2f}")
            if reasons:
                breaks.append({
                    "date": cur["date"],
                    "name": name,
                    "reasons": "; ".join(reasons),
                    "prev_spend": prev["spend"],
                    "spend": cur["spend"],
                    "prev_cpc": prev["cpc"],
                    "cpc": cur["cpc"],
                })
    return breaks


def build_cooldowns(events: list[dict], breaks: list[dict], today: date | None = None, fallback_campaigns: list[str] | None = None) -> list[dict]:
    today = today or date.today()
    locks = {}
    fallback = [n for n in (fallback_campaigns or []) if n]

    def add(campaign, lever, source, change_date, days, note):
        try:
            d0 = date.fromisoformat(change_date)
        except ValueError:
            return
        until = d0 + timedelta(days=days)
        key = (campaign or "*", lever)
        rec = {
            "campaign": campaign or "*",
            "lever": lever,
            "source": source,
            "changed_on": change_date,
            "lock_until": until.isoformat(),
            "status": "cooling" if today <= until else "expired",
            "note": note,
        }
        old = locks.get(key)
        if not old or rec["changed_on"] > old["changed_on"]:
            locks[key] = rec

    for ev in events:
        if ev["lever"] == "negatives":
            continue
        days = 7
        if ev["campaigns"]:
            names = ev["campaigns"]
        elif ev["lever"] == "keyword_target_bid":
            names = ["*"]
        else:
            names = fallback or ["*"]
        for name in names:
            add(name, ev["lever"], "ops_log", ev["date"], days, ev["remark"][:180])

    return sorted(locks.values(), key=lambda x: (x["lock_until"], x["campaign"], x["lever"]), reverse=True)


def write_change_detection(
    src: Path,
    dest: Path,
    camp_names: set[str] | None = None,
    camps: list[dict] | None = None,
    groups: list[dict] | None = None,
    kws: list[dict] | None = None,
    targets: list[dict] | None = None,
    meta: dict | None = None,
) -> dict:
    events = parse_ops_ads_events(load_json(src / "ops_log.json"))
    daily_rows = flatten_daily_campaigns(load_json(src / "daily_campaigns.json"))
    if camp_names:
        daily_rows = [r for r in daily_rows if r["name"] in camp_names]
        filtered = []
        for ev in events:
            names = [n for n in ev["campaigns"] if n in camp_names]
            if names or not ev["campaigns"]:
                ev = dict(ev)
                if names:
                    ev["campaigns"] = names
                filtered.append(ev)
        events = filtered
    breaks = detect_daily_breaks(daily_rows)
    today = date.today()
    ops_locks = build_cooldowns(events, [], today=today, fallback_campaigns=sorted(camp_names or []))

    meta = meta or {}
    curr_cfg = extract_config(camps or [], groups or [], kws or [], targets or [])
    snap_file = snapshot_path(str(meta.get("profile_id") or ""), str(meta.get("asin") or ""))
    prev_snap = load_snapshot(snap_file)
    diffs = diff_independent_vars(prev_snap, curr_cfg) if prev_snap.get("updated_at") else []
    diff_locks = diffs_to_cooldowns(diffs, prev_snap, events, today)
    kept = keep_active_cooldowns(prev_snap, today)
    cooldowns = merge_locks(kept, ops_locks, diff_locks)

    new_snap = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "asin": meta.get("asin"),
        "profile_id": meta.get("profile_id"),
        "campaigns": curr_cfg["campaigns"],
        "groups": curr_cfg["groups"],
        "keywords": curr_cfg["keywords"],
        "targets": curr_cfg["targets"],
        "active_cooldowns": cooldowns,
    }
    save_snapshot(snap_file, new_snap)
    dest.joinpath("config_snapshot.json").write_text(
        json.dumps(new_snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    event_lines = []
    for ev in events:
        event_lines.append("\t".join(str(x) for x in [
            ev["date"], ev["type_id"], ev["layer"], ev["lever"],
            " | ".join(ev["campaigns"][:8]), ev["remark"][:200],
        ]))
    tsv(dest / "ops_ads_events.tsv", "date\ttype_id\tlayer\tlever\tcampaigns\tremark", event_lines)

    break_lines = []
    for br in breaks:
        break_lines.append("\t".join(str(x) for x in [
            br["date"], br["name"], round(br["prev_spend"], 2), round(br["spend"], 2),
            round(br["prev_cpc"], 2), round(br["cpc"], 2), br["reasons"],
        ]))
    tsv(dest / "daily_breaks.tsv", "date\tcampaign\tprev_spend\tspend\tprev_cpc\tcpc\treasons", break_lines)

    daily_lines = []
    for row in daily_rows:
        daily_lines.append("\t".join(str(x) for x in [
            row["date"], row["name"], row["state"], int(row["tos"]), int(row["budget"]),
            round(row["spend"], 2), round(row["cpc"], 2), int(row["clicks"]), int(row["orders"]), int(row["impr"]),
        ]))
    tsv(
        dest / "daily_campaigns.tsv",
        "date\tname\tstate\tTOS_snapshot\tbudget_snapshot\tspend\tcpc\tclk\tord\timpr",
        daily_lines,
    )

    cool_lines = []
    for rec in cooldowns:
        cool_lines.append("\t".join(str(x) for x in [
            rec["campaign"], rec["lever"], rec["source"], rec["changed_on"],
            rec["lock_until"], rec["status"], rec["note"],
        ]))
    tsv(dest / "cooldowns.tsv", "campaign\tlever\tsource\tchanged_on\tlock_until\tstatus\tnote", cool_lines)

    cooling = [c for c in cooldowns if c["status"] == "cooling"]
    diff_lines = []
    for d in diffs:
        diff_lines.append("\t".join(str(x) for x in [
            d.get("campaign"), d.get("lever"), d.get("field"), d.get("old"), d.get("new"), d.get("note"),
        ]))
    tsv(dest / "config_diff.tsv", "campaign\tlever\tfield\told\tnew\tnote", diff_lines)

    return {
        "ops_events": len(events),
        "daily_breaks": len(breaks),
        "config_diffs": len(diffs),
        "cooling_locks": len(cooling),
        "latest_ads_change": events[-1]["date"] if events else "",
        "snapshot_path": str(snap_file),
        "had_previous_snapshot": bool(prev_snap.get("updated_at")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()
    src = Path(args.indir)
    dest = Path(args.outdir)
    dest.mkdir(parents=True, exist_ok=True)

    meta = load_json(src / "meta.json") or {}
    listing = load_json(src / "listing.json")
    stock = load_json(src / "fba_stock.json")
    profile = listing_profile(listing, stock)
    profile["meta"] = meta
    dest.joinpath("product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    product_ads = as_rows(load_json(src / "product_ads.json"))
    asin = str((meta.get("asin") or profile.get("asin") or "")).upper()
    asin_campaigns = set(meta.get("scoped_campaigns") or meta.get("asin_campaign_keys") or [])
    tokens = list(meta.get("belonging_tokens") or []) or scope.belonging_tokens(asin, listing or {})
    for row in product_ads:
        row_asin = str(row.get("asin") or row.get("advertised_asin") or "").upper()
        if asin and row_asin == asin and row.get("campaign_name"):
            asin_campaigns.add(row.get("campaign_name"))

    camps = campaign_rows(as_rows(load_json(src / "campaign_report.json")))
    if asin_campaigns:
        camps = [c for c in camps if c["name"] in asin_campaigns or str(c.get("name")) in asin_campaigns]
    elif tokens:
        camps = [c for c in camps if scope.campaign_belongs(c.get("name"), tokens)]
        asin_campaigns = {c["name"] for c in camps}
    camp_lines = []
    for c in camps:
        camp_lines.append(
            "\t".join(str(x) for x in [
                c["state"], c["targeting"], c["sponsored"], c.get("strategy") or "", int(c["budget"]),
                int(c["tos"]), int(c["pp"]), int(c["ros"]),
                round(c["spend"], 2), round(c["sales"], 2), round(c["acos"], 1),
                round(c["cpc"], 2), round(c["cvr"], 1), round(c["ctr"], 2),
                int(c["clk"]), int(c["ord"]), int(c["direct_ord"]), int(c["indirect_ord"]),
                round(c["tos_share"], 2), c["name"],
            ])
        )
    tsv(
        dest / "campaigns.tsv",
        "state\ttype\tsponsored\tstrategy\tbudget\tTOS\tPP\tROS\tspend\tsales\tacos\tcpc\tcvr\tctr\tclk\tord\tdir\tindir\tshare\tname",
        camp_lines,
    )

    ka_index = load_keyword_analysis(src)
    cuts = volume_cuts(ka_index)
    head_imprs = [
        num(r.get("impressions"), 0)
        for r in ka_index.values()
        if volume_band(num(r.get("searchrank"), 0), num(r.get("impressions"), 0), cuts) in {"head", "head_proxy"}
        and num(r.get("impressions"), 0) > 0
    ]
    head_median_impr = sorted(head_imprs)[len(head_imprs) // 2] if head_imprs else 0.0
    ka_lines = []
    for key, row in sorted(ka_index.items(), key=lambda kv: num(kv[1].get("searchrank") or 9e9)):
        rank = num(row.get("searchrank"), 0)
        impr = num(row.get("impressions"), 0)
        ka_lines.append("\t".join(str(x) for x in [
            key, row.get("_sponsored_type") or "",
            int(rank) if rank else "",
            volume_band(rank, impr, cuts),
            int(impr), int(num(row.get("clicks"))), int(num(row.get("orders"))),
            round(num(row.get("spends")), 2), round(num(row.get("sales")), 2),
            round(num(row.get("acos")), 1), round(num(row.get("cvr")), 1), round(num(row.get("ctr")), 2),
            row.get("localize_keyword_text") or "",
        ]))
    tsv(
        dest / "keyword_analysis.tsv",
        "keyword\tsponsored\tsearchrank\tvol_band\timpr\tclk\tord\tspend\tsales\tacos\tcvr\tctr\tzh",
        ka_lines,
    )

    extra_rules = load_mismatch_rules(src)
    kws = [r for r in as_rows(load_json(src / "keywords.json")) if not skip_total(r)]
    if asin_campaigns:
        kws = [r for r in kws if r.get("campaign_name") in asin_campaigns]
    kw_lines = []
    hints = []
    for r in sorted(kws, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 1 and num(r.get("orders")) < 1:
            continue
        text_kw = r.get("keyword_text") or r.get("targeting_text") or ""
        vol = lookup_volume(ka_index, text_kw, cuts)
        bucket = classify_query(text_kw, profile, extra_rules)
        hint = diagnosis_hint(
            bucket, vol, num(r.get("clicks")), num(r.get("orders")), spend,
            num(r.get("ctr")), num(r.get("cvr")), head_median_impr,
        )
        kw_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("match_type"), text_kw,
            r.get("bid"), r.get("default_bid"), r.get("state"),
            round(spend, 2), round(num(r.get("sales")), 2), round(num(r.get("acos")), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))),
            round(num(r.get("cvr")), 1), round(num(r.get("cpc")), 2), round(num(r.get("ctr")), 2),
            vol["searchrank"], vol["vol_band"], vol["ka_impr"], hint,
        ]))
        if hint:
            hints.append("\t".join(str(x) for x in [
                "keyword", r.get("campaign_name"), text_kw, bucket,
                vol["searchrank"], vol["vol_band"], hint,
                round(spend, 2), int(num(r.get("orders"))), int(num(r.get("clicks"))),
            ]))
    tsv(
        dest / "keywords.tsv",
        "campaign\tmatch\ttext\tbid\tdefbid\tstate\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tctr\tsearchrank\tvol_band\tka_impr\thint",
        kw_lines,
    )

    sts = [r for r in as_rows(load_json(src / "search_terms.json")) if r.get("query") or r.get("campaign_name")]
    if asin_campaigns:
        sts = [r for r in sts if r.get("campaign_name") in asin_campaigns]
    bucket_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0})
    st_lines = []
    waste = []
    harvest = []
    for r in sorted(sts, key=lambda x: -num(x.get("spends"))):
        q = (r.get("query") or "").strip()
        if not q:
            continue
        spend = num(r.get("spends"))
        sales = num(r.get("sales"))
        ordn = num(r.get("orders"))
        clk = num(r.get("clicks"))
        bucket = classify_query(q, profile, extra_rules)
        vol = lookup_volume(ka_index, q, cuts)
        if not vol.get("row"):
            vol = lookup_volume(ka_index, r.get("keyword_text") or r.get("target_text") or "", cuts)
        hint = diagnosis_hint(bucket, vol, clk, ordn, spend, num(r.get("ctr")), num(r.get("cvr")), head_median_impr)
        bucket_stats[bucket]["spend"] += spend
        bucket_stats[bucket]["sales"] += sales
        bucket_stats[bucket]["ord"] += ordn
        bucket_stats[bucket]["clk"] += clk
        if spend >= 3 or ordn >= 1:
            st_lines.append("\t".join(str(x) for x in [
                bucket, r.get("campaign_name"), q, r.get("match_type") or r.get("target_match_type"),
                r.get("keyword_text") or r.get("target_text"),
                round(spend, 2), round(sales, 2), round(num(r.get("acos")), 1),
                int(ordn), int(clk), round(num(r.get("cvr")), 1), round(num(r.get("cpc")), 2),
                vol["searchrank"], vol["vol_band"], vol["ka_impr"], hint,
            ]))
            if hint:
                hints.append("\t".join(str(x) for x in [
                    "search_term", r.get("campaign_name"), q, bucket,
                    vol["searchrank"], vol["vol_band"], hint,
                    round(spend, 2), int(ordn), int(clk),
                ]))
        if spend >= 8 and ordn == 0 and clk >= 20:
            waste.append(
                f"{q}\t{r.get('campaign_name')}\tspend={spend:.2f}\tclk={int(clk)}"
                f"\tbucket={bucket}\tsearchrank={vol['searchrank']}\tvol={vol['vol_band']}\thint={hint}"
            )
        if ordn >= 5 and acos(spend, sales) <= 22 and spend >= 12:
            harvest.append(
                f"{q}\t{r.get('campaign_name')}\tspend={spend:.2f}\tacos={acos(spend,sales):.1f}"
                f"\tord={int(ordn)}\tbucket={bucket}\tsearchrank={vol['searchrank']}\tvol={vol['vol_band']}"
            )

    tsv(
        dest / "search_terms.tsv",
        "bucket\tcampaign\tquery\tmatch\ttarget\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tsearchrank\tvol_band\tka_impr\thint",
        st_lines,
    )
    tsv(
        dest / "diagnosis_hints.tsv",
        "layer\tcampaign\ttext\tbucket\tsearchrank\tvol_band\thint\tspend\tord\tclk",
        hints,
    )
    dest.joinpath("waste.txt").write_text("\n".join(waste), encoding="utf-8")
    dest.joinpath("harvest.txt").write_text("\n".join(harvest), encoding="utf-8")

    b_lines = ["bucket\tspend\tsales\tacos\tord\tclk\tcvr"]
    for b, v in sorted(bucket_stats.items(), key=lambda kv: -kv[1]["spend"]):
        b_lines.append("\t".join(str(x) for x in [
            b, round(v["spend"], 1), round(v["sales"], 1), round(acos(v["spend"], v["sales"]), 1),
            int(v["ord"]), int(v["clk"]),
            round((v["ord"] / v["clk"] * 100) if v["clk"] else 0, 1),
        ]))
    dest.joinpath("buckets.tsv").write_text("\n".join(b_lines), encoding="utf-8")

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

    targets = as_rows(load_json(src / "targets.json"))
    if asin_campaigns:
        targets = [r for r in targets if r.get("campaign_name") in asin_campaigns]
    t_lines = []
    for r in sorted(targets, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 0.5 and num(r.get("orders")) < 1:
            continue
        t_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("exp_type_zh") or r.get("exp_type_text") or r.get("match_type"),
            r.get("targeting_text") or r.get("expression") or r.get("keyword_text"),
            r.get("bid"), r.get("default_bid"), r.get("state"),
            round(spend, 2), round(num(r.get("sales")), 2), round(num(r.get("acos")), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(num(r.get("cvr")), 1),
            round(num(r.get("cpc")), 2),
        ]))
    tsv(
        dest / "targets.tsv",
        "campaign\ttype\ttext\tbid\tdefbid\tstate\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        t_lines,
    )

    enabled = [c for c in camps if c["state"] == "enabled"]
    total_spend = sum(c["spend"] for c in camps)
    total_sales = sum(c["sales"] for c in camps)
    total_ord = sum(c["ord"] for c in camps)
    summary = []
    summary.append(f"window\t{meta.get('window')}")
    summary.append(f"shop\t{meta.get('shop_alias')}\tsid={meta.get('sid')}\tprofile={meta.get('profile_id')}")
    summary.append(f"title\t{profile.get('title')}")
    summary.append(f"price/stars/reviews\t{profile.get('price')}\t{profile.get('stars')}\t{profile.get('reviews')}")
    summary.append(f"price_stack\tlanded={profile.get('landed_price')}\tlisting={profile.get('listing_price')}\tregular={profile.get('regular_price')}\thistory={profile.get('history_price')}")
    summary.append(f"lifecycle_signals\topen_date={profile.get('open_date')}\tfirst_order={profile.get('first_order_time')}\treviews={profile.get('reviews')}\tvol7={profile.get('seven_volume')}\tvol14={profile.get('fourteen_volume')}\tvol30={profile.get('thirty_volume')}")
    summary.append(f"product_type\t{profile.get('amz_product_type')}\t{profile.get('category_text')}")
    summary.append(f"small_rank\t{profile.get('small_rank')}")
    summary.append(f"stock\tfulfillable={profile.get('fulfillable')}\tdos={profile.get('days_of_supply')}\tinbound_shipped={profile.get('inbound_shipped')}\tunsellable={profile.get('unsellable')}")
    summary.append(f"portfolio_like_totals\tspend={total_spend:.2f}\tsales={total_sales:.2f}\tacos={acos(total_spend,total_sales):.1f}\tord={int(total_ord)}")
    summary.append(f"enabled_campaigns\t{len(enabled)} / {len(camps)}")
    summary.append(f"asin_campaigns_filtered\t{len(asin_campaigns)}")
    matched_rank = sum(1 for row in ka_index.values() if num(row.get("searchrank"), 0) > 0)
    summary.append(f"keyword_analysis\t{len(ka_index)}\twith_searchrank={matched_rank}\thead_median_impr={int(head_median_impr)}")
    if not ka_index:
        summary.append("keyword_analysis_status\tDATA_MISSING")
    elif matched_rank == 0:
        summary.append("keyword_analysis_status\tsearchrank DATA_MISSING; using impression proxies")
    else:
        summary.append("keyword_analysis_status\tsearchrank=ABA SFR proxy; no weekly search volume")
    high_tos = [c["name"] for c in enabled if c["tos"] >= 180]
    summary.append("high_tos>=180\t" + " | ".join(high_tos[:12]))
    change_info = write_change_detection(
        src, dest, {c["name"] for c in camps} if camps else None,
        camps=camps, groups=groups, kws=kws, targets=targets, meta=meta,
    )
    summary.append(f"ops_ads_events\t{change_info.get('ops_events')}")
    summary.append(f"config_diffs\t{change_info.get('config_diffs')}")
    summary.append(f"daily_breaks_corroboration_only\t{change_info.get('daily_breaks')}")
    summary.append(f"cooling_locks\t{change_info.get('cooling_locks')}")
    summary.append(f"latest_ads_change\t{change_info.get('latest_ads_change')}")
    summary.append(f"snapshot\t{change_info.get('snapshot_path')}")
    dest.joinpath("summary.txt").write_text("\n".join(summary), encoding="utf-8")
    print("wrote", dest)
    print("\n".join(summary[:8]))


if __name__ == "__main__":
    main()
