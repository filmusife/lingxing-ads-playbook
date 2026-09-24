# -*- coding: utf-8 -*-
"""Fetch Lingxing listing, stock, and ads reports for one ASIN + country."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lx_client as lx
import product_scope as scope

COUNTRY_ALIASES = {
    "美国": "US", "usa": "US", "us": "US", "amazon.com": "US",
    "英国": "UK", "gb": "UK", "uk": "UK", "amazon.co.uk": "UK",
    "德国": "DE", "de": "DE",
    "法国": "FR", "fr": "FR",
    "意大利": "IT", "it": "IT",
    "西班牙": "ES", "es": "ES",
    "日本": "JP", "jp": "JP",
    "加拿大": "CA", "ca": "CA",
    "墨西哥": "MX", "mx": "MX",
    "澳洲": "AU", "澳大利亚": "AU", "au": "AU",
    "巴西": "BR", "br": "BR",
    "荷兰": "NL", "nl": "NL",
    "瑞典": "SE", "se": "SE",
    "波兰": "PL", "pl": "PL",
    "土耳其": "TR", "tr": "TR",
}


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_country(raw: str) -> str:
    key = (raw or "").strip()
    return COUNTRY_ALIASES.get(key.lower(), key.upper())


def report_window(days: int) -> str:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return f"{start.isoformat()} - {end.isoformat()}"


def shops_for_country(country: str) -> list[dict]:
    payload = lx.action("ad_auth_shops", {})
    out = []
    for row in lx.rows(payload):
        if str(row.get("country") or "").upper() != country:
            continue
        if row.get("real_status") not in (1, "1", None, True):
            # keep unknown status; skip explicit dead
            if row.get("real_status") in (0, "0", False):
                continue
        out.append(row)
    return out


def listing_lookup(sid, asin: str) -> list[dict]:
    last = None
    for pvi in ("0", ""):
        payload = lx.action("erp_listing", {
            "offset": 0,
            "length": 20,
            "pvi_ids": pvi,
            "sids": str(sid),
            "search_field": "asin",
            "search_value": [asin],
            "exact_search": "1",
        })
        last = payload
        found = lx.rows(payload)
        if found:
            return found
    return lx.rows(last) if last else []


def resolve_shop(asin: str, country: str, shop: str | None) -> dict:
    candidates = shops_for_country(country)
    if not candidates:
        raise lx.LingxingError(f"No Lingxing ads shops for country {country}")
    if shop:
        needle = shop.strip().lower()
        matched = [r for r in candidates if needle in str(r.get("alias") or "").lower()]
        if matched:
            candidates = matched
    hits = []
    for row in candidates:
        items = listing_lookup(row.get("sid"), asin)
        if items:
            hits.append((row, items[0]))
    if len(hits) == 1:
        shop_row, listing = hits[0]
        return {"shop": shop_row, "listing": listing, "candidates": len(candidates)}
    if len(hits) > 1:
        if shop:
            shop_row, listing = hits[0]
            return {"shop": shop_row, "listing": listing, "candidates": len(hits), "note": "multiple hits, used first alias match"}
        aliases = [str(h[0].get("alias")) for h in hits]
        raise lx.LingxingError(
            f"ASIN {asin} exists in multiple {country} shops: {aliases}. Pass --shop."
        )
    if shop and candidates:
        # listing miss but user named the shop; still use it for ads
        return {"shop": candidates[0], "listing": None, "candidates": len(candidates), "note": "listing miss, used named shop"}
    aliases = [str(r.get("alias")) for r in candidates]
    raise lx.LingxingError(
        f"ASIN {asin} not found via erp_listing in {country} shops {aliases}. Pass --shop if it is advertised."
    )


def base_report_params(profile_id: str, window: str, extra: dict | None = None) -> dict:
    params = {
        "report_date": window,
        "profile_ids": [str(profile_id)],
        "page": 1,
        "length": 100,
        "sort_field": "spends",
        "sort_type": "desc",
    }
    if extra:
        params.update(extra)
    return params


def fetch_reports(profile_id: str, window: str, outdir: Path, sku: str | None = None, asin: str | None = None) -> dict:
    pid = str(profile_id)
    bundle = {}
    extra = {"sku": [sku]} if sku else None

    campaigns = lx.paginate("ad_campaign_report", base_report_params(pid, window, extra))
    save(outdir / "campaign_report.json", campaigns)
    bundle["campaigns"] = len(campaigns)

    groups = lx.paginate(
        "ad_campaign_group_report",
        base_report_params(pid, window, {"with_ring": 0, **({} if not sku else {"sku": [sku]})}),
    )
    save(outdir / "ad_groups.json", groups)
    bundle["groups"] = len(groups)

    keywords = lx.paginate("ad_campaign_keyword_report", base_report_params(pid, window, extra if sku else None))
    save(outdir / "keywords.json", keywords)
    bundle["keywords"] = len(keywords)

    search_terms = lx.paginate(
        "ad_campaign_search_term_report",
        base_report_params(pid, window, {"length": 100, **({"sku": sku} if sku else {})}),
        max_pages=30,
    )
    save(outdir / "search_terms.json", search_terms)
    bundle["search_terms"] = len(search_terms)

    targets = lx.paginate(
        "ad_campaign_targeting_report",
        base_report_params(pid, window, {"with_ring": 0, "length": "100"}),
    )
    save(outdir / "targets.json", targets)
    bundle["targets"] = len(targets)

    products = lx.paginate(
        "ad_campaign_product_report",
        {
            "report_date": window,
            "profile_id": int(pid) if str(pid).isdigit() else pid,
            "profile_ids": [pid],
            "page": 1,
            "length": 100,
            "sort_field": "spends",
            "sort_type": "desc",
            "with_ring": 0,
        },
    )
    save(outdir / "product_ads.json", products)
    bundle["product_ads"] = len(products)

    portfolios = lx.paginate(
        "ad_portfolio_report_shop",
        base_report_params(pid, window),
    )
    save(outdir / "portfolios.json", portfolios)
    bundle["portfolios"] = len(portfolios)
    return bundle


def split_window(window: str) -> tuple[str, str]:
    if " - " not in (window or ""):
        raise lx.LingxingError(f"Bad report window: {window}")
    start, end = [x.strip() for x in window.split(" - ", 1)]
    return start, end


def fetch_keyword_analysis(sid, profile_id, asin: str | None, msku: str | None, window: str) -> list[dict]:
    """ABA search-frequency rank lives here as searchrank. No weekly search-volume number exists."""
    start, end = split_window(window)
    all_rows: list[dict] = []
    pid = None
    if profile_id is not None and str(profile_id).isdigit():
        pid = int(profile_id)
    # sponsored_type=sb currently returns the same SP payload; do not double-fetch.
    for sponsored_type in ("sp",):
        params = {
            "start_date": start,
            "end_date": end,
            "page": 1,
            "limit": 200,
            "sponsored_type": sponsored_type,
        }
        if asin:
            params["asin"] = [asin]
        elif msku:
            params["msku"] = [msku]
        if sid is not None:
            params["sid"] = str(sid)
        elif pid is not None:
            params["profile_id"] = pid
        else:
            continue
        try:
            rows = lx.paginate("advertising_ad_analyze_keyword", params, max_pages=20)
        except Exception:
            rows = []
        for row in rows:
            if isinstance(row, dict):
                row["_sponsored_type"] = sponsored_type
                all_rows.append(row)
    return all_rows


def fetch_ops_log(sid, asin: str, days: int = 14) -> dict:

    end = date.today()
    start = end - timedelta(days=days - 1)
    payload = lx.action("analytics_log_list_v2", {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "summary_type": "asin",
        "page_size": 50,
        "offset": 0,
        "sids": [str(sid)] if sid is not None else [],
        "search_field": "asin",
        "search_value": [asin],
        "turn_on_summary": False,
        "is_dimension_display": True,
    })
    return payload


def fetch_daily_campaigns(profile_id: str, days: int = 7, portfolio_id=None) -> list[dict]:
    """One page per day. Placement/bid fields are CURRENT snapshots; spend/CPC/impressions are historical."""
    pid = str(profile_id)
    out = []
    end = date.today() - timedelta(days=1)
    for offset in range(days):
        day = end - timedelta(days=offset)
        window = f"{day.isoformat()} - {day.isoformat()}"
        params = {
            "report_date": window,
            "profile_ids": [pid],
            "page": 1,
            "length": 100,
            "sort_field": "spends",
            "sort_type": "desc",
        }
        if portfolio_id:
            params["portfolio_id"] = str(portfolio_id)
        payload = lx.action("ad_campaign_report", params)
        rows = lx.rows(payload)
        out.append({"date": day.isoformat(), "rows": rows})
    out.sort(key=lambda x: x["date"])
    return out


def filter_asin_campaigns(product_ads: list[dict], asin: str) -> set[str]:
    names = set()
    needle = asin.strip().upper()
    for row in product_ads:
        row_asin = str(row.get("asin") or row.get("advertised_asin") or "").upper()
        if needle and needle == row_asin:
            if row.get("campaign_name"):
                names.add(row["campaign_name"])
            if row.get("campaign_id"):
                names.add(str(row["campaign_id"]))
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asin", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--shop", default=None)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    asin = args.asin.strip().upper()
    country = normalize_country(args.country)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    window = report_window(args.days)

    resolved = resolve_shop(asin, country, args.shop)
    shop = resolved["shop"]
    listing = resolved.get("listing")
    save(outdir / "shop.json", shop)
    if listing:
        save(outdir / "listing.json", listing)

    stock = lx.action("get_fba_stock_list", {
        "sid": str(shop.get("sid")),
        "search_field": "asin",
        "search_value": asin,
        "offset": 0,
        "length": 20,
        "is_hide_zero_stock": "0",
    })
    save(outdir / "fba_stock.json", stock)

    msku = None
    if listing:
        msku = listing.get("msku") or listing.get("seller_sku")
    try:
        counts = fetch_reports(str(shop.get("profile_id")), window, outdir, sku=msku, asin=asin)
        if counts.get("campaigns", 0) <= 1 and msku:
            counts = fetch_reports(str(shop.get("profile_id")), window, outdir, sku=None, asin=asin)
    except Exception:
        counts = fetch_reports(str(shop.get("profile_id")), window, outdir)
    product_ads = json.loads((outdir / "product_ads.json").read_text(encoding="utf-8"))
    asin_keys = filter_asin_campaigns(product_ads, asin)
    tokens = scope.belonging_tokens(asin, listing or {})
    try:
        camps = json.loads((outdir / "campaign_report.json").read_text(encoding="utf-8"))
    except Exception:
        camps = []
    scoped = [r for r in camps if isinstance(r, dict) and r.get("name") and scope.campaign_belongs(r.get("name"), tokens)]
    name_keys = {str(r.get("name")) for r in scoped}
    asin_keys |= name_keys
    pids = {str(r.get("portfolio_id")) for r in scoped if r.get("portfolio_id")}
    portfolio_id = next(iter(pids)) if len(pids) == 1 else None

    try:
        ka_rows = fetch_keyword_analysis(shop.get("sid"), shop.get("profile_id"), asin, msku, window)
        save(outdir / "keyword_analysis.json", ka_rows)
        counts["keyword_analysis"] = len(ka_rows)
        counts["keyword_analysis_with_searchrank"] = sum(
            1 for r in ka_rows if r.get("searchrank") not in (None, "", 0, "0")
        )
    except Exception as exc:
        save(outdir / "keyword_analysis.json", {"error": str(exc)})
        counts["keyword_analysis"] = 0
        counts["keyword_analysis_with_searchrank"] = 0

    try:
        ops_log = fetch_ops_log(shop.get("sid"), asin, days=14)
        save(outdir / "ops_log.json", ops_log)
        counts["ops_log"] = 1
    except Exception as exc:
        save(outdir / "ops_log.json", {"error": str(exc)})
        counts["ops_log"] = 0
    try:
        daily = fetch_daily_campaigns(str(shop.get("profile_id")), days=7, portfolio_id=portfolio_id)
        save(outdir / "daily_campaigns.json", daily)
        counts["daily_campaign_days"] = len(daily)
    except Exception as exc:
        save(outdir / "daily_campaigns.json", {"error": str(exc)})
        counts["daily_campaign_days"] = 0

    meta = {
        "asin": asin,
        "country": country,
        "shop_alias": shop.get("alias"),
        "sid": shop.get("sid"),
        "profile_id": shop.get("profile_id"),
        "window": window,
        "days": args.days,
        "listing_found": bool(listing),
        "asin_campaign_keys": sorted(asin_keys),
        "belonging_tokens": tokens,
        "scoped_campaigns": sorted(name_keys),
        "counts": counts,
        "note": resolved.get("note"),
        "portfolio_id": portfolio_id,
        "change_detection": "config_snapshot + ops_log; placement/bid in reports are live snapshots",
    }
    save(outdir / "meta.json", meta)
    print(json.dumps({k: v for k, v in meta.items() if k != "asin_campaign_keys"}, ensure_ascii=False, indent=2))
    print("campaign keys", len(asin_keys))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except lx.LingxingError as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(2)
