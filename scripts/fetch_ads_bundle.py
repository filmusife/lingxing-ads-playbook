# -*- coding: utf-8 -*-
"""Fetch Lingxing listing, stock, ads reports, dayparting, traffic, and profit intelligence for one ASIN + country."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lx_client as lx

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


def get_multi_windows(macro_days: int = 30) -> dict[str, str]:
    """Calculate date windows for macro 30-day baseline, medium 7-day trend, and micro 3-day latest slice."""
    yesterday = date.today() - timedelta(days=1)
    return {
        "macro_30d": f"{(yesterday - timedelta(days=macro_days - 1)).isoformat()} - {yesterday.isoformat()}",
        "recent_7d": f"{(yesterday - timedelta(days=6)).isoformat()} - {yesterday.isoformat()}",
        "recent_3d": f"{(yesterday - timedelta(days=2)).isoformat()} - {yesterday.isoformat()}",
    }


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


def fetch_reports(
    profile_id: str,
    window: str,
    outdir: Path,
    sku: str | None = None,
    asin: str | None = None,
    portfolio_id: str | None = None,
    windows_dict: dict[str, str] | None = None,
) -> dict:
    pid = str(profile_id)
    bundle = {}
    extra = {"sku": [sku]} if sku else {}
    if portfolio_id:
        extra["portfolio_id"] = str(portfolio_id)

    # 1. Campaigns (30-day Macro Baseline)
    camp_params = base_report_params(pid, window, extra)
    campaigns = lx.paginate("ad_campaign_report", camp_params)
    save(outdir / "campaign_report.json", campaigns)
    bundle["campaigns"] = len(campaigns)

    # Multi-window fresh slices for dynamic delta and trend detection
    w_7d = (windows_dict.get("recent_7d") if windows_dict else None) or report_window(7)
    w_3d = (windows_dict.get("recent_3d") if windows_dict else None) or report_window(3)

    try:
        camp_params_7d = base_report_params(pid, w_7d, extra)
        campaigns_7d = lx.paginate("ad_campaign_report", camp_params_7d)
        save(outdir / "campaign_report_7d.json", campaigns_7d)
        bundle["campaigns_7d"] = len(campaigns_7d)
    except Exception as exc:
        print(f"Warning: could not fetch campaign_report_7d: {exc}", file=sys.stderr)

    try:
        camp_params_3d = base_report_params(pid, w_3d, extra)
        campaigns_3d = lx.paginate("ad_campaign_report", camp_params_3d)
        save(outdir / "campaign_report_3d.json", campaigns_3d)
        bundle["campaigns_3d"] = len(campaigns_3d)
    except Exception as exc:
        print(f"Warning: could not fetch campaign_report_3d: {exc}", file=sys.stderr)

    # 2. Ad Groups
    grp_extra = {"with_ring": 0, **({"sku": [sku]} if sku else {})}
    if portfolio_id:
        grp_extra["portfolio_id"] = [str(portfolio_id)]
    groups = lx.paginate(
        "ad_campaign_group_report",
        base_report_params(pid, window, grp_extra),
    )
    save(outdir / "ad_groups.json", groups)
    bundle["groups"] = len(groups)

    # 3. Keywords (30d macro, 7d trend, 3d micro)
    kw_extra = dict(extra)
    if portfolio_id:
        kw_extra["portfolio_id"] = [str(portfolio_id)]
    keywords = lx.paginate("ad_campaign_keyword_report", base_report_params(pid, window, kw_extra))
    save(outdir / "keywords.json", keywords)
    bundle["keywords"] = len(keywords)

    try:
        kw_params_7d = base_report_params(pid, w_7d, kw_extra)
        keywords_7d = lx.paginate("ad_campaign_keyword_report", kw_params_7d)
        save(outdir / "keywords_7d.json", keywords_7d)
        bundle["keywords_7d"] = len(keywords_7d)
    except Exception as exc:
        print(f"Warning: could not fetch keywords_7d: {exc}", file=sys.stderr)

    try:
        kw_params_3d = base_report_params(pid, w_3d, kw_extra)
        keywords_3d = lx.paginate("ad_campaign_keyword_report", kw_params_3d)
        save(outdir / "keywords_3d.json", keywords_3d)
        bundle["keywords_3d"] = len(keywords_3d)
    except Exception as exc:
        print(f"Warning: could not fetch keywords_3d: {exc}", file=sys.stderr)

    # 4. Search Terms (with automatic translation)
    st_extra = {"length": 100, "with_translation": 1, **({"sku": sku} if sku else {})}
    if portfolio_id:
        st_extra["portfolio_id"] = [str(portfolio_id)]
    search_terms = lx.paginate(
        "ad_campaign_search_term_report",
        base_report_params(pid, window, st_extra),
        max_pages=40,
    )
    save(outdir / "search_terms.json", search_terms)
    bundle["search_terms"] = len(search_terms)

    # 5. Product Targets (30d macro, 7d trend, 3d micro)
    tg_extra = {"with_ring": 0, "length": "100"}
    if portfolio_id:
        tg_extra["portfolio_id"] = [str(portfolio_id)]
    targets = lx.paginate(
        "ad_campaign_targeting_report",
        base_report_params(pid, window, tg_extra),
        max_pages=20,
    )
    save(outdir / "targets.json", targets)
    bundle["targets"] = len(targets)

    try:
        tg_params_7d = base_report_params(pid, w_7d, tg_extra)
        targets_7d = lx.paginate("ad_campaign_targeting_report", tg_params_7d, max_pages=20)
        save(outdir / "targets_7d.json", targets_7d)
        bundle["targets_7d"] = len(targets_7d)
    except Exception as exc:
        print(f"Warning: could not fetch targets_7d: {exc}", file=sys.stderr)

    try:
        tg_params_3d = base_report_params(pid, w_3d, tg_extra)
        targets_3d = lx.paginate("ad_campaign_targeting_report", tg_params_3d, max_pages=20)
        save(outdir / "targets_3d.json", targets_3d)
        bundle["targets_3d"] = len(targets_3d)
    except Exception as exc:
        print(f"Warning: could not fetch targets_3d: {exc}", file=sys.stderr)

    # 6. Product Ads
    prod_params = {
        "report_date": window,
        "profile_id": int(pid) if str(pid).isdigit() else pid,
        "page": 1,
        "length": 100,
        "sort_field": "spends",
        "sort_type": "desc",
        "with_ring": False,
    }
    if portfolio_id:
        prod_params["portfolio_id"] = [str(portfolio_id)]
    products = lx.paginate(
        "ad_campaign_product_report",
        prod_params,
        max_pages=15,
    )
    save(outdir / "product_ads.json", products)
    bundle["product_ads"] = len(products)

    # 7. Portfolios
    portfolios = lx.paginate(
        "ad_portfolio_report_shop",
        base_report_params(pid, window),
    )
    save(outdir / "portfolios.json", portfolios)
    bundle["portfolios"] = len(portfolios)

    # 8. Keyword Analysis (ABA search rank / search volume)
    try:
        parts = [p.strip() for p in window.split(" - ")]
        if len(parts) >= 2:
            kw_analysis = lx.paginate(
                "advertising_ad_analyze_keyword",
                {
                    "start_date": parts[0],
                    "end_date": parts[1],
                    "asin": [asin] if asin else None,
                    "profile_id": int(pid) if str(pid).isdigit() else pid,
                    "limit": 100,
                },
                max_pages=20,
            )
            save(outdir / "keyword_analysis.json", kw_analysis)
            bundle["keyword_analysis"] = len(kw_analysis)
    except Exception as exc:
        print(f"Warning: could not fetch keyword_analysis: {exc}", file=sys.stderr)

    return bundle


def fetch_advanced_intelligence(sid: str, sku: str | None, asin: str, window: str, outdir: Path) -> dict:
    """Fetch Dayparting (Hourly/Weekly), Product Traffic Performance (Sessions/CVR), and Real Financial Profit."""
    adv_bundle = {}
    parts = [p.strip() for p in window.split(" - ")]
    if len(parts) < 2:
        return adv_bundle
    start_date, end_date = parts[0], parts[1]

    # 1. Dayparting: 24-hour and Day-of-Week Order Analysis
    if sku:
        try:
            h_res = lx.action("advertising_order_analysis_list", {
                "sid": str(sid),
                "sku": [sku],
                "start_date": start_date,
                "end_date": end_date,
                "group_type": "hourly",
            })
            hourly_rows = lx.rows(h_res)
            save(outdir / "order_analysis_hourly.json", hourly_rows)
            adv_bundle["hourly_dayparting"] = len(hourly_rows)
        except Exception as exc:
            print(f"Warning: hourly order analysis failed: {exc}", file=sys.stderr)

        try:
            w_res = lx.action("advertising_order_analysis_list", {
                "sid": str(sid),
                "sku": [sku],
                "start_date": start_date,
                "end_date": end_date,
                "group_type": "weekly",
            })
            weekly_rows = lx.rows(w_res)
            save(outdir / "order_analysis_weekly.json", weekly_rows)
            adv_bundle["weekly_dayparting"] = len(weekly_rows)
        except Exception as exc:
            print(f"Warning: weekly order analysis failed: {exc}", file=sys.stderr)

    # 2. Product Traffic Performance (Sessions, Pageviews, Organic vs Ad CVR, TACOS)
    try:
        p_res = lx.action("query_product_performance_asin_lists", {
            "sids": str(sid),
            "search_field": "asin",
            "search_value": [asin],
            "start_date": start_date,
            "end_date": end_date,
            "offset": 0,
            "length": 10,
        })
        perf_rows = lx.rows(p_res)
        save(outdir / "product_performance.json", perf_rows)
        adv_bundle["product_performance"] = len(perf_rows)
    except Exception as exc:
        print(f"Warning: product performance query failed: {exc}", file=sys.stderr)

    # 3. Real Profit Report (COGS, Logistics, Gross Profit, Selling & FBA Fees, Break-Even ACOS)
    try:
        profit_res = lx.action("query_order_profit_list", {
            "start_date": start_date,
            "end_date": end_date,
            "currency_type": "USD",
            "summary_field": "asin",
            "turn_on_summary": "0",
            "sort_type": "desc",
            "length": "20",
            "offset": "0",
            "source_service": "mcp",
            "external_service_mark": 1,
            "search_field": "asin",
            "search_value": [asin],
        })
        profit_rows = lx.rows(profit_res)
        save(outdir / "order_profit.json", profit_rows)
        adv_bundle["order_profit"] = len(profit_rows)
    except Exception as exc:
        print(f"Warning: order profit query failed: {exc}", file=sys.stderr)

    return adv_bundle


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
    parser.add_argument("--portfolio", default=None, help="Optional portfolio name or portfolio_id filter")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--db-dir", default=None, help="Optional path to database directory (default: workspace db/)")
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

    # Resolve portfolio_id if --portfolio passed
    portfolio_id = None
    if args.portfolio:
        pf_text = args.portfolio.strip()
        if pf_text.isdigit():
            portfolio_id = pf_text
        else:
            try:
                pfs = lx.paginate("ad_portfolio_report_shop", base_report_params(str(shop.get("profile_id")), window))
                # Try exact match first
                for p in pfs:
                    pname = str(p.get("name") or p.get("portfolio_name") or "").strip()
                    if pf_text.lower() == pname.lower():
                        portfolio_id = str(p.get("portfolio_id") or p.get("key"))
                        break
                # Try substring match fallback
                if not portfolio_id:
                    for p in pfs:
                        pname = str(p.get("name") or p.get("portfolio_name") or "").strip()
                        if pf_text.lower() in pname.lower():
                            portfolio_id = str(p.get("portfolio_id") or p.get("key"))
                            break
                # Fallback: find portfolio_id from ad_campaign_report
                if not portfolio_id:
                    try:
                        c_res = lx.paginate("ad_campaign_report", {
                            "profile_ids": [str(shop.get("profile_id"))],
                            "report_date": window,
                            "page": 1,
                            "length": 100,
                            "sort_field": "spends",
                            "sort_type": "desc",
                        })
                        for c in c_res:
                            c_pname = str(c.get("portfolio_name") or "").strip()
                            if pf_text.lower() == c_pname.lower() or pf_text.lower() in c_pname.lower():
                                if c.get("portfolio_id"):
                                    portfolio_id = str(c["portfolio_id"])
                                    break
                    except Exception:
                        pass
            except Exception:
                pass

    multi_win = get_multi_windows(args.days)
    window = multi_win["macro_30d"]

    try:
        counts = fetch_reports(str(shop.get("profile_id")), window, outdir, sku=msku, asin=asin, portfolio_id=portfolio_id, windows_dict=multi_win)
        if counts.get("campaigns", 0) <= 1 and msku:
            counts = fetch_reports(str(shop.get("profile_id")), window, outdir, sku=None, asin=asin, portfolio_id=portfolio_id, windows_dict=multi_win)
    except Exception:
        counts = fetch_reports(str(shop.get("profile_id")), window, outdir, portfolio_id=portfolio_id, windows_dict=multi_win)

    # Advanced Intelligence: Dayparting, Traffic performance, Real profit
    adv_counts = fetch_advanced_intelligence(str(shop.get("sid")), msku, asin, window, outdir)
    counts.update(adv_counts)

    product_ads = json.loads((outdir / "product_ads.json").read_text(encoding="utf-8"))
    asin_keys = filter_asin_campaigns(product_ads, asin)
    meta = {
        "asin": asin,
        "country": country,
        "shop_alias": shop.get("alias"),
        "sid": shop.get("sid"),
        "profile_id": shop.get("profile_id"),
        "portfolio_id": portfolio_id,
        "portfolio_name": args.portfolio,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": window,
        "days": args.days,
        "windows": multi_win,
        "listing_found": bool(listing),
        "asin_campaign_keys": sorted(asin_keys),
        "counts": counts,
        "note": resolved.get("note"),
    }
    save(outdir / "meta.json", meta)

    # Optional DB record tracking
    db_root = Path(args.db_dir) if args.db_dir else (Path("db") if Path("db").exists() else (outdir.parent / "db" if (outdir.parent / "db").exists() else None))
    if db_root and db_root.exists():
        daily_rec_dir = db_root / "daily_records"
        daily_rec_dir.mkdir(parents=True, exist_ok=True)
        try:
            log_line = json.dumps({
                "fetched_at": meta["fetched_at"],
                "asin": asin,
                "country": country,
                "portfolio_id": portfolio_id,
                "outdir": str(outdir),
                "counts": counts,
            }, ensure_ascii=False)
            with open(daily_rec_dir / "fetch_history.jsonl", "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception:
            pass

    print(json.dumps({k: v for k, v in meta.items() if k != "asin_campaign_keys"}, ensure_ascii=False, indent=2))
    print("campaign keys", len(asin_keys))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except lx.LingxingError as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(2)
