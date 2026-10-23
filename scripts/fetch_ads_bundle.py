# -*- coding: utf-8 -*-
"""Fetch Lingxing listing, stock, ads reports, dayparting, traffic, and profit intelligence for one ASIN + country."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Callable

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

DEFAULT_CONCURRENCY = 6


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


def split_window(window: str) -> tuple[str, str] | None:
    parts = [p.strip() for p in (window or "").split(" - ")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


# ==============================================================================
# PARALLEL FETCH ENGINE
# ==============================================================================

@dataclass
class FetchSpec:
    """One independent endpoint pull: what to call, where to save it, is it fatal if it fails."""
    key: str
    filename: str
    fetch: Callable[[], list[dict]]
    required: bool = False


def run_specs(specs: list[FetchSpec], outdir: Path, max_workers: int = DEFAULT_CONCURRENCY) -> dict[str, int]:
    """Run independent endpoint pulls concurrently, then save each result in the main thread.

    Every spec hits a different endpoint, so they only contend on the network. Pagination
    inside a single spec stays sequential because page N+1 depends on page N.
    """
    counts: dict[str, int] = {}
    if not specs:
        return counts

    workers = max(1, min(max_workers, len(specs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(spec, pool.submit(spec.fetch)) for spec in specs]
        for spec, fut in futures:
            try:
                rows = fut.result()
            except Exception as exc:
                if spec.required:
                    raise
                print(f"Warning: could not fetch {spec.key}: {exc}", file=sys.stderr)
                continue
            save(outdir / spec.filename, rows)
            counts[spec.key] = len(rows)
    return counts


def shops_for_country(country: str) -> list[dict]:
    payload = lx.action("ad_auth_shops", {})
    # Keep rows whose status is unknown; drop only the explicitly dead ones.
    return [
        row for row in lx.rows(payload)
        if str(row.get("country") or "").upper() == country
        and row.get("real_status") not in (0, "0", False)
    ]


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


def probe_campaign_count(profile_id: str, window: str, extra: dict | None = None) -> int:
    """One cheap single-page call to decide whether the SKU filter is too narrow.

    Cheaper than fetching every report twice just to discover the filter was wrong.
    """
    params = base_report_params(profile_id, window, extra)
    params["length"] = 20
    rows = lx.rows(lx.action("ad_campaign_report", params))
    return len([r for r in rows if r.get("name") or r.get("campaign_id")])


def build_report_specs(
    profile_id: str,
    windows: dict[str, str],
    sku: str | None = None,
    asin: str | None = None,
    portfolio_id: str | None = None,
) -> list[FetchSpec]:
    """Describe every ads report pull as data so they can run in parallel."""
    pid = str(profile_id)
    w30 = windows["macro_30d"]
    w7 = windows["recent_7d"]
    w3 = windows["recent_3d"]

    sku_extra = {"sku": [sku]} if sku else {}
    pid_int = int(pid) if pid.isdigit() else pid

    def paginated(tool_id: str, params: dict, max_pages: int = 20) -> Callable[[], list[dict]]:
        return lambda: lx.paginate(tool_id, params, max_pages=max_pages)

    camp_extra = dict(sku_extra)
    if portfolio_id:
        camp_extra["portfolio_id"] = str(portfolio_id)

    grp_extra = {"with_ring": 0, **sku_extra}
    if portfolio_id:
        grp_extra["portfolio_id"] = [str(portfolio_id)]

    kw_extra = dict(sku_extra)
    if portfolio_id:
        kw_extra["portfolio_id"] = [str(portfolio_id)]

    st_extra = {"length": 100, "with_translation": 1, **({"sku": sku} if sku else {})}
    if portfolio_id:
        st_extra["portfolio_id"] = [str(portfolio_id)]

    tg_extra = {"with_ring": 0, "length": "100"}
    if portfolio_id:
        tg_extra["portfolio_id"] = [str(portfolio_id)]

    prod_params = {
        "report_date": w30,
        "profile_id": pid_int,
        "page": 1,
        "length": 100,
        "sort_field": "spends",
        "sort_type": "desc",
        "with_ring": False,
    }
    if portfolio_id:
        prod_params["portfolio_id"] = [str(portfolio_id)]

    specs = [
        # (key, filename, tool, params, max_pages, required)
        ("campaigns", "campaign_report.json", "ad_campaign_report", base_report_params(pid, w30, camp_extra), 20, True),
        ("campaigns_7d", "campaign_report_7d.json", "ad_campaign_report", base_report_params(pid, w7, camp_extra), 20, False),
        ("campaigns_3d", "campaign_report_3d.json", "ad_campaign_report", base_report_params(pid, w3, camp_extra), 20, False),
        ("groups", "ad_groups.json", "ad_campaign_group_report", base_report_params(pid, w30, grp_extra), 20, True),
        ("keywords", "keywords.json", "ad_campaign_keyword_report", base_report_params(pid, w30, kw_extra), 20, True),
        ("keywords_7d", "keywords_7d.json", "ad_campaign_keyword_report", base_report_params(pid, w7, kw_extra), 20, False),
        ("keywords_3d", "keywords_3d.json", "ad_campaign_keyword_report", base_report_params(pid, w3, kw_extra), 20, False),
        ("search_terms", "search_terms.json", "ad_campaign_search_term_report", base_report_params(pid, w30, st_extra), 40, True),
        ("targets", "targets.json", "ad_campaign_targeting_report", base_report_params(pid, w30, tg_extra), 20, True),
        ("targets_7d", "targets_7d.json", "ad_campaign_targeting_report", base_report_params(pid, w7, tg_extra), 20, False),
        ("targets_3d", "targets_3d.json", "ad_campaign_targeting_report", base_report_params(pid, w3, tg_extra), 20, False),
        ("product_ads", "product_ads.json", "ad_campaign_product_report", prod_params, 15, True),
        ("portfolios", "portfolios.json", "ad_portfolio_report_shop", base_report_params(pid, w30), 20, True),
    ]
    out = [
        FetchSpec(key=key, filename=fname, fetch=paginated(tool, params, pages), required=req)
        for key, fname, tool, params, pages, req in specs
    ]

    # Keyword Analysis (ABA search rank / search volume) takes explicit dates, not a window string.
    bounds = split_window(w30)
    if bounds:
        out.append(FetchSpec(
            key="keyword_analysis",
            filename="keyword_analysis.json",
            fetch=paginated("advertising_ad_analyze_keyword", {
                "start_date": bounds[0],
                "end_date": bounds[1],
                "asin": [asin] if asin else None,
                "profile_id": pid_int,
                "limit": 100,
            }, 20),
        ))
    return out


def build_advanced_specs(sid: str, sku: str | None, asin: str, window: str) -> list[FetchSpec]:
    """Dayparting (hourly/weekly), product traffic performance, and real financial profit."""
    bounds = split_window(window)
    if not bounds:
        return []
    start_date, end_date = bounds

    def single(tool_id: str, params: dict) -> Callable[[], list[dict]]:
        return lambda: lx.rows(lx.action(tool_id, params))

    specs: list[FetchSpec] = []
    if sku:
        for key, fname, group in (
            ("hourly_dayparting", "order_analysis_hourly.json", "hourly"),
            ("weekly_dayparting", "order_analysis_weekly.json", "weekly"),
        ):
            specs.append(FetchSpec(key=key, filename=fname, fetch=single("advertising_order_analysis_list", {
                "sid": str(sid),
                "sku": [sku],
                "start_date": start_date,
                "end_date": end_date,
                "group_type": group,
            })))

    specs.append(FetchSpec(
        key="product_performance",
        filename="product_performance.json",
        fetch=single("query_product_performance_asin_lists", {
            "sids": str(sid),
            "search_field": "asin",
            "search_value": [asin],
            "start_date": start_date,
            "end_date": end_date,
            "offset": 0,
            "length": 10,
        }),
    ))
    specs.append(FetchSpec(
        key="order_profit",
        filename="order_profit.json",
        fetch=single("query_order_profit_list", {
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
        }),
    ))
    return specs


def resolve_portfolio_id(portfolio: str, profile_id: str, window: str) -> str | None:
    """Resolve a portfolio name to its id, falling back to the campaign report."""
    pf_text = portfolio.strip()
    if pf_text.isdigit():
        return pf_text
    needle = pf_text.lower()

    try:
        pfs = lx.paginate("ad_portfolio_report_shop", base_report_params(profile_id, window))
    except Exception:
        pfs = []
    names = [(str(p.get("name") or p.get("portfolio_name") or "").strip(), p) for p in pfs]
    for exact in (True, False):
        for pname, p in names:
            hit = (needle == pname.lower()) if exact else (needle in pname.lower())
            if pname and hit:
                return str(p.get("portfolio_id") or p.get("key"))

    try:
        campaigns = lx.paginate("ad_campaign_report", base_report_params(profile_id, window))
    except Exception:
        return None
    for c in campaigns:
        c_pname = str(c.get("portfolio_name") or "").strip().lower()
        if c_pname and needle in c_pname and c.get("portfolio_id"):
            return str(c["portfolio_id"])
    return None


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


def log_fetch_history(db_dir: str | None, outdir: Path, meta: dict) -> None:
    db_root = Path(db_dir) if db_dir else None
    if db_root is None:
        for candidate in (Path("db"), outdir.parent / "db"):
            if candidate.exists():
                db_root = candidate
                break
    if db_root is None or not db_root.exists():
        return
    daily_rec_dir = db_root / "daily_records"
    try:
        daily_rec_dir.mkdir(parents=True, exist_ok=True)
        log_line = json.dumps({
            "fetched_at": meta["fetched_at"],
            "asin": meta["asin"],
            "country": meta["country"],
            "portfolio_id": meta["portfolio_id"],
            "outdir": str(outdir),
            "counts": meta["counts"],
        }, ensure_ascii=False)
        with open(daily_rec_dir / "fetch_history.jsonl", "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asin", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--shop", default=None)
    parser.add_argument("--portfolio", default=None, help="Optional portfolio name or portfolio_id filter")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--db-dir", default=None, help="Optional path to database directory (default: workspace db/)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Parallel report fetches (default: {DEFAULT_CONCURRENCY}; use 1 to serialize)")
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    asin = args.asin.strip().upper()
    country = normalize_country(args.country)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    multi_win = get_multi_windows(args.days)
    window = multi_win["macro_30d"]

    resolved = resolve_shop(asin, country, args.shop)
    shop = resolved["shop"]
    listing = resolved.get("listing")
    save(outdir / "shop.json", shop)
    if listing:
        save(outdir / "listing.json", listing)

    profile_id = str(shop.get("profile_id"))
    sid = str(shop.get("sid"))

    stock = lx.action("get_fba_stock_list", {
        "sid": sid,
        "search_field": "asin",
        "search_value": asin,
        "offset": 0,
        "length": 20,
        "is_hide_zero_stock": "0",
    })
    save(outdir / "fba_stock.json", stock)

    msku = (listing.get("msku") or listing.get("seller_sku")) if listing else None

    portfolio_id = resolve_portfolio_id(args.portfolio, profile_id, window) if args.portfolio else None

    # Decide the SKU filter with one cheap probe instead of fetching everything twice.
    sku = msku
    if msku:
        probe_extra = {"sku": [msku]}
        if portfolio_id:
            probe_extra["portfolio_id"] = str(portfolio_id)
        try:
            if probe_campaign_count(profile_id, window, probe_extra) <= 1:
                sku = None
        except Exception as exc:
            print(f"Warning: campaign probe failed, fetching without SKU filter: {exc}", file=sys.stderr)
            sku = None

    specs = build_report_specs(profile_id, multi_win, sku=sku, asin=asin, portfolio_id=portfolio_id)
    try:
        counts = run_specs(specs, outdir, max_workers=args.concurrency)
    except Exception as exc:
        print(f"Warning: report fetch failed ({exc}); retrying unfiltered", file=sys.stderr)
        specs = build_report_specs(profile_id, multi_win, sku=None, asin=asin, portfolio_id=portfolio_id)
        counts = run_specs(specs, outdir, max_workers=args.concurrency)

    # Advanced Intelligence: Dayparting, Traffic performance, Real profit
    counts.update(run_specs(
        build_advanced_specs(sid, msku, asin, window), outdir, max_workers=args.concurrency
    ))

    # Xiyou Dongcha (西柚洞察) Rank & Traffic Intelligence
    try:
        import xydc_client as xydc
        print(f"[xydc-mcp] Probing keyword rank distribution for ASIN {asin} ({country})...")
        xydc_ranks = xydc.fetch_asin_keyword_ranks(asin, country=country, max_keywords=1000)
        if xydc_ranks:
            save(outdir / "xydc_ranks.json", xydc_ranks)
            counts["xydc_ranks"] = len(xydc_ranks)
            print(f"[xydc-mcp] Successfully mapped {len(xydc_ranks)} keywords with organic & ad ranks.")
        else:
            print("[xydc-mcp] No rank records returned or xydc-mcp not responding.")
    except Exception as exc:
        print(f"[xydc-mcp] Optional rank intelligence skipped: {exc}", file=sys.stderr)

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
        "sku_filter": sku,
        "asin_campaign_keys": sorted(asin_keys),
        "counts": counts,
        "note": resolved.get("note"),
    }
    save(outdir / "meta.json", meta)
    log_fetch_history(args.db_dir, outdir, meta)

    print(json.dumps({k: v for k, v in meta.items() if k != "asin_campaign_keys"}, ensure_ascii=False, indent=2))
    print("campaign keys", len(asin_keys))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except lx.LingxingError as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(2)
