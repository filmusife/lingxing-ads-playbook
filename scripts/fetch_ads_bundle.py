# -*- coding: utf-8 -*-
"""Fetch Lingxing listing, stock, multi-window ads reports, real placement reports,
daily USD product performance, operation logs, dayparting, and profit intelligence for one ASIN + country."""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

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


def save(path: Path, obj: Any) -> None:
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
    """Run independent endpoint pulls concurrently, then save each result in the main thread."""
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
            counts[spec.key] = len(rows) if isinstance(rows, list) else 1
    return counts


def shops_for_country(country: str) -> list[dict]:
    payload = lx.action("ad_auth_shops", {})
    return [
        row for row in lx.rows(payload)
        if str(row.get("country") or "").upper() == country
        and row.get("real_status") not in (0, "0", False)
    ]


def listing_lookup(sid: Any, asin: str) -> list[dict]:
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


def infer_shop_hint_from_portfolio(portfolio: str | None) -> str | None:
    """Infer shop alias hint encoded in portfolio naming conventions (e.g., DL2507US -> dilu, -Q- / QF -> quafe, GD -> gdor)."""
    if not portfolio:
        return None
    p = portfolio.strip().upper()
    if re.search(r"(?:^|[-_])DL\d*", p) or "DILU" in p:
        return "dilu"
    if re.search(r"(?:^|[-_])(?:QF\d*|Q)(?:[-_]|$)", p) or "QUAFE" in p:
        return "quafe"
    if re.search(r"(?:^|[-_])GD\d*", p) or "GDOR" in p:
        return "gdor"
    return None


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


def _probe_shop_sales_usd(sid: Any, asin: str, window: str | None) -> float:
    bounds = split_window(window or report_window(30))
    if not bounds:
        return 0.0
    try:
        res = lx.action("query_product_performance_asin_lists", {
            "sids": str(sid),
            "search_field": "asin",
            "search_value": [asin],
            "start_date": bounds[0],
            "end_date": bounds[1],
            "currency_code": "USD",
            "offset": 0,
            "length": 5,
        })
        rows = lx.rows(res)
        if rows:
            return float(rows[0].get("amount") or rows[0].get("sales_amount") or 0.0)
    except Exception:
        pass
    return 0.0


def resolve_shop(
    asin: str,
    country: str,
    shop: str | None,
    portfolio: str | None = None,
    window: str | None = None,
) -> dict:
    all_country_shops = shops_for_country(country)
    if not all_country_shops:
        raise lx.LingxingError(f"No Lingxing ads shops for country {country}")

    candidates = list(all_country_shops)
    if shop:
        needle = shop.strip().lower()
        matched = [r for r in candidates if needle in str(r.get("alias") or "").lower()]
        if matched:
            candidates = matched

    hits: list[tuple[dict, dict]] = []
    for row in candidates:
        items = listing_lookup(row.get("sid"), asin)
        if items:
            # Prefer non-deleted FBA listing row when multiple SKUs share the ASIN in one shop
            best_item = sorted(
                items,
                key=lambda it: (
                    1 if str(it.get("status") or "") == "1" else 0,
                    1 if str(it.get("fulfillment_channel_type") or "").upper() == "FBA" else 0,
                    float(it.get("afn_fulfillable_quantity") or 0),
                ),
                reverse=True,
            )[0]
            hits.append((row, best_item))

    # Also check all country shops to detect cross-shop shared ASINs even if --shop was passed
    shared_shop_aliases: list[str] = []
    if len(all_country_shops) > 1:
        if shop:
            for row in all_country_shops:
                alias = str(row.get("alias") or "")
                if any(row.get("sid") == h[0].get("sid") for h in hits):
                    shared_shop_aliases.append(alias)
                else:
                    try:
                        if listing_lookup(row.get("sid"), asin):
                            shared_shop_aliases.append(alias)
                    except Exception:
                        pass
        else:
            shared_shop_aliases = [str(h[0].get("alias") or "") for h in hits]

    if len(hits) == 1:
        shop_row, listing = hits[0]
        return {
            "shop": shop_row,
            "listing": listing,
            "candidates": len(candidates),
            "shared_shops": shared_shop_aliases,
        }

    if len(hits) > 1:
        if shop:
            shop_row, listing = hits[0]
            return {
                "shop": shop_row,
                "listing": listing,
                "candidates": len(hits),
                "shared_shops": shared_shop_aliases,
                "note": "multiple hits, used explicit --shop match",
            }

        win = window or report_window(30)
        # Disambiguate 1: Which shop actually owns the requested portfolio?
        if portfolio:
            for s_row, l_row in hits:
                pf_id = resolve_portfolio_id(portfolio, str(s_row.get("profile_id")), win)
                if pf_id:
                    return {
                        "shop": s_row,
                        "listing": l_row,
                        "candidates": len(hits),
                        "shared_shops": shared_shop_aliases,
                        "resolved_portfolio_id": pf_id,
                        "note": f"multi-shop ASIN {shared_shop_aliases}; matched portfolio owner {s_row.get('alias')}",
                    }
            # Disambiguate 2: Portfolio name hint (DL -> dilu, QF/-Q- -> quafe, GD -> gdor)
            hint = infer_shop_hint_from_portfolio(portfolio)
            if hint:
                hint_hits = [h for h in hits if hint in str(h[0].get("alias") or "").lower()]
                if len(hint_hits) == 1:
                    s_row, l_row = hint_hits[0]
                    return {
                        "shop": s_row,
                        "listing": l_row,
                        "candidates": len(hits),
                        "shared_shops": shared_shop_aliases,
                        "note": f"multi-shop ASIN {shared_shop_aliases}; matched portfolio shop hint '{hint}' -> {s_row.get('alias')}",
                    }

        # Disambiguate 3: Pick shop with highest active 30d USD sales on this ASIN
        scored_hits = []
        for s_row, l_row in hits:
            sales_usd = _probe_shop_sales_usd(s_row.get("sid"), asin, win)
            fba_qty = float(l_row.get("afn_fulfillable_quantity") or 0.0)
            scored_hits.append((sales_usd, fba_qty, s_row, l_row))
        scored_hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if scored_hits[0][0] > 0 or scored_hits[0][1] > 0:
            _, _, s_row, l_row = scored_hits[0]
            return {
                "shop": s_row,
                "listing": l_row,
                "candidates": len(hits),
                "shared_shops": shared_shop_aliases,
                "note": f"multi-shop ASIN {shared_shop_aliases}; auto-selected primary revenue shop {s_row.get('alias')}",
            }

        aliases = [str(h[0].get("alias")) for h in hits]
        raise lx.LingxingError(
            f"ASIN {asin} exists in multiple {country} shops: {aliases}. Pass --shop."
        )

    if shop and candidates:
        return {
            "shop": candidates[0],
            "listing": None,
            "candidates": len(candidates),
            "shared_shops": shared_shop_aliases,
            "note": "listing miss, used named shop",
        }

    # Fallback when listing not in erp_listing (e.g. archived/cross-shop brand ads) but portfolio owner exists
    if portfolio:
        win = window or report_window(30)
        hint = infer_shop_hint_from_portfolio(portfolio)
        ordered = sorted(
            candidates,
            key=lambda r: (0 if (hint and hint in str(r.get("alias") or "").lower()) else 1),
        )
        for s_row in ordered:
            pf_id = resolve_portfolio_id(portfolio, str(s_row.get("profile_id")), win)
            if pf_id:
                return {
                    "shop": s_row,
                    "listing": None,
                    "candidates": len(candidates),
                    "shared_shops": shared_shop_aliases,
                    "resolved_portfolio_id": pf_id,
                    "note": f"listing miss, matched portfolio owner {s_row.get('alias')}",
                }

    aliases = [str(r.get("alias")) for r in candidates]
    raise lx.LingxingError(
        f"ASIN {asin} not found via erp_listing in {country} shops {aliases}. Pass --shop if it is advertised."
    )


def probe_campaign_count(profile_id: str, window: str, extra: dict | None = None) -> int:
    """One cheap single-page call to decide whether the SKU filter is too narrow."""
    params = base_report_params(profile_id, window, extra)
    params["length"] = 20
    rows = lx.rows(lx.action("ad_campaign_report", params))
    return len([r for r in rows if r.get("name") or r.get("campaign_id")])


def _fetch_sp_placement_rows(profile_id: str, window: str, portfolio_id: str | None = None) -> list[dict]:
    """Fetch ground-truth SP campaign placement performance (TOS / Detail Page / Other / Off-Amazon)."""
    params: dict[str, Any] = {
        "profile_id": str(profile_id),
        "report_date": window,
        "page": 1,
        "length": 100,
    }
    if portfolio_id:
        params["portfolio_id"] = [str(portfolio_id)]
    rows = lx.paginate("get_sp_campaign_placement_report", params, max_pages=15)
    # Drop summary rows that have no campaign_id/campaign_name
    return [r for r in rows if r.get("campaign_id") or r.get("campaign_name")]


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

    prod_params: dict[str, Any] = {
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
        ("campaigns", "campaign_report.json", "ad_campaign_report", base_report_params(pid, w30, camp_extra), 20, True),
        ("campaigns_7d", "campaign_report_7d.json", "ad_campaign_report", base_report_params(pid, w7, camp_extra), 20, False),
        ("campaigns_3d", "campaign_report_3d.json", "ad_campaign_report", base_report_params(pid, w3, camp_extra), 20, False),
        ("groups", "ad_groups.json", "ad_campaign_group_report", base_report_params(pid, w30, grp_extra), 20, True),
        ("keywords", "keywords.json", "ad_campaign_keyword_report", base_report_params(pid, w30, kw_extra), 20, True),
        ("keywords_7d", "keywords_7d.json", "ad_campaign_keyword_report", base_report_params(pid, w7, kw_extra), 20, False),
        ("keywords_3d", "keywords_3d.json", "ad_campaign_keyword_report", base_report_params(pid, w3, kw_extra), 20, False),
        ("search_terms", "search_terms.json", "ad_campaign_search_term_report", base_report_params(pid, w30, st_extra), 40, True),
        ("search_terms_7d", "search_terms_7d.json", "ad_campaign_search_term_report", base_report_params(pid, w7, st_extra), 25, False),
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

    # Real SP Campaign Placement Reports (30d macro + 7d fresh)
    out.append(FetchSpec(
        key="placement_report",
        filename="placement_report.json",
        fetch=lambda: _fetch_sp_placement_rows(pid, w30, portfolio_id),
        required=False,
    ))
    out.append(FetchSpec(
        key="placement_report_7d",
        filename="placement_report_7d.json",
        fetch=lambda: _fetch_sp_placement_rows(pid, w7, portfolio_id),
        required=False,
    ))

    # Keyword Analysis (ABA search rank / search volume)
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


def _fetch_operate_logs(sid: str, sku: str | None, asin: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch Listing & operational logs (price edits, copy edits, inventory/ad events) to isolate non-ad confounders."""
    combined: list[dict] = []
    try:
        res_a = lx.action("analytics_log_list_v2", {
            "start_date": start_date,
            "end_date": end_date,
            "summary_type": "asin",
            "search_field": "asin",
            "search_value": [asin],
            "sids": [str(sid)],
            "page_size": 100,
            "offset": 0,
        })
        for row in lx.rows(res_a):
            combined.append({"source": "analytics_log_list_v2", "payload": row})
    except Exception:
        pass

    if sku:
        try:
            res_s = lx.action("sales_operate_log_list", {
                "store_id": str(sid),
                "msku": str(sku),
                "operate_time_start": f"{start_date} 00:00:00",
                "operate_time_end": f"{end_date} 23:59:59",
                "offset": 0,
                "length": 50,
            })
            for row in lx.rows(res_s):
                combined.append({"source": "sales_operate_log_list", "payload": row})
        except Exception:
            pass
    return combined


def build_advanced_specs(sid: str, sku: str | None, asin: str, window: str) -> list[FetchSpec]:
    """Dayparting (hourly/weekly), USD product traffic performance (summary + daily), operation logs, and real profit."""
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

    # 30-day aggregate ASIN performance in explicit USD (fixes previous CNY default!)
    specs.append(FetchSpec(
        key="product_performance",
        filename="product_performance.json",
        fetch=single("query_product_performance_asin_lists", {
            "sids": str(sid),
            "search_field": "asin",
            "search_value": [asin],
            "start_date": start_date,
            "end_date": end_date,
            "currency_code": "USD",
            "summary_field": "asin",
            "offset": 0,
            "length": 10,
        }),
    ))

    # Daily USD ASIN trajectory (7d vs prior-7d sales, organic orders, sessions, Buy Box %, price, BSR, SB/SBV/SD costs)
    specs.append(FetchSpec(
        key="product_performance_daily",
        filename="product_performance_daily.json",
        fetch=single("query_product_performance_asin_lists", {
            "sids": str(sid),
            "search_field": "asin",
            "search_value": [asin],
            "start_date": start_date,
            "end_date": end_date,
            "date_view_type": "day",
            "date_view_order_type": 1,
            "currency_code": "USD",
            "summary_field": "asin",
            "offset": 0,
            "length": 120,
        }),
    ))

    # Real financial order profit (returns one row per store in `sids`, allowing both store-exact and cross-shop P&L)
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

    # Listing & operational logs (price changes, title/copy edits, ad/stock events)
    specs.append(FetchSpec(
        key="operate_logs",
        filename="operate_logs.json",
        fetch=lambda: _fetch_operate_logs(sid, sku, asin, start_date, end_date),
    ))
    return specs


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
        for candidate in (Path("db"), outdir.parent / "db", outdir.parent.parent / "db"):
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
            "sid": meta.get("sid"),
            "shop_alias": meta.get("shop_alias"),
            "portfolio_id": meta["portfolio_id"],
            "outdir": str(outdir),
            "counts": meta["counts"],
        }, ensure_ascii=False)
        with open(daily_rec_dir / "fetch_history.jsonl", "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


def _select_store_profit_row(prof_data: Any, sid: str) -> dict:
    if isinstance(prof_data, dict):
        return prof_data
    if not isinstance(prof_data, list) or not prof_data:
        return {}
    sid_str = str(sid or "").strip()
    if sid_str:
        for r in prof_data:
            if not isinstance(r, dict):
                continue
            r_sids = [str(x) for x in (r.get("sids") or [])]
            if sid_str in r_sids or str(r.get("sid") or "") == sid_str:
                return r
    return prof_data[0] if isinstance(prof_data[0], dict) else {}


def probe_deal_and_promotions(
    asin: str,
    country: str,
    sid: str,
    msku: str | None,
    window: str,
    outdir: Path,
    days: int = 30,
) -> dict:
    """Probe Deal / Lightning Deal / Promotion & Price markers across xydc-mcp and LingXing-MCP."""
    bounds = split_window(window)
    start_date, end_date = bounds if bounds else ("", "")

    deal_info: dict[str, Any] = {
        "asin": asin,
        "country": country,
        "anchor_date": end_date,
        "has_deal_history": False,
        "is_active_deal": False,
        "is_deal_transition": False,
        "days_since_last_deal": None,
        "last_deal_date": None,
        "deal_days": [],
        "coupon_days": [],
        "prime_days": [],
        "promotion_days": [],
        "price_summary": {},
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
        "summary_desc": "近期无活跃秒杀/促销活动记录",
    }

    # 1. Probe Xiyou Dongcha (xydc-mcp) get_asin_info_trends anchored to report end_date
    try:
        import xydc_client as xydc
        xydc_deal = xydc.fetch_asin_deal_trends(asin, country=country, days=days, anchor_date=end_date or None)
        if xydc_deal:
            deal_info["deal_days"] = xydc_deal.get("deal_days", [])
            deal_info["coupon_days"] = xydc_deal.get("coupon_days", [])
            deal_info["prime_days"] = xydc_deal.get("prime_days", [])
            deal_info["promotion_days"] = xydc_deal.get("promotion_days", [])
            deal_info["price_summary"] = xydc_deal.get("price_summary", {})
            deal_info["last_deal_date"] = xydc_deal.get("last_deal_date")
            deal_info["days_since_last_deal"] = xydc_deal.get("days_since_last_deal")
            if xydc_deal.get("has_deal_history"):
                deal_info["has_deal_history"] = True
            if xydc_deal.get("is_active_deal"):
                deal_info["is_active_deal"] = True
            elif xydc_deal.get("is_deal_transition"):
                deal_info["is_deal_transition"] = True
    except Exception as exc:
        print(f"[xydc-mcp] Optional deal trend probe skipped: {exc}", file=sys.stderr)

    # 2. Probe LingXing sales_detail_sec_kill & sales_detail_prime_discount
    if sid and msku and start_date and end_date:
        try:
            start_time = f"{start_date} 00:00:00"
            end_time = f"{end_date} 23:59:59"

            sk_res = lx.action("sales_detail_sec_kill", {
                "storeId": str(sid),
                "sellerSku": str(msku),
                "startTime": start_time,
                "endTime": end_time,
                "sortField": "startTime",
                "sortType": "desc",
                "pageNum": 1,
                "pageSize": 20,
            })
            if isinstance(sk_res, dict):
                data_obj = sk_res.get("data", {})
                if isinstance(data_obj, dict):
                    inner_data = data_obj.get("data", {})
                    records = inner_data.get("records") if isinstance(inner_data, dict) else None
                    if records and isinstance(records, list):
                        deal_info["sec_kill_records"] = records
                        deal_info["has_deal_history"] = True
                        for sk in records:
                            if not isinstance(sk, dict):
                                continue
                            p_start = str(sk.get("promotionStartTime") or "")[:10]
                            p_end = str(sk.get("promotionEndTime") or "")[:10]
                            st_val = str(sk.get("originStatus") or sk.get("status") or "").upper()
                            if p_start and p_end and p_start <= end_date <= p_end and st_val not in ("CANCELED", "CANCELLED", "SUPPRESSED"):
                                deal_info["is_active_deal"] = True
                                deal_info["is_deal_transition"] = False
                                deal_info["days_since_last_deal"] = 0
                                deal_info["active_deal_window"] = f"{p_start} ~ {p_end}"
                                deal_info["last_deal_date"] = end_date
                            elif p_end and p_end < end_date:
                                if not deal_info.get("last_deal_date") or str(deal_info["last_deal_date"])[:10] < p_end:
                                    deal_info["last_deal_date"] = p_end

            pd_res = lx.action("sales_detail_prime_discount", {
                "storeId": str(sid),
                "sellerSku": str(msku),
                "startTime": start_time,
                "endTime": end_time,
                "sortField": "startTime",
                "sortType": "desc",
                "pageNum": 1,
                "pageSize": 20,
            })
            if isinstance(pd_res, dict):
                data_obj = pd_res.get("data", {})
                if isinstance(data_obj, dict):
                    inner_data = data_obj.get("data", {})
                    records = inner_data.get("records") if isinstance(inner_data, dict) else None
                    if records and isinstance(records, list):
                        deal_info["prime_discount_records"] = records
                        deal_info["has_deal_history"] = True
        except Exception as exc:
            print(f"[LingXing-MCP] Optional sec_kill probe skipped: {exc}", file=sys.stderr)

    # 3. Read store-matched accounting financial discounts
    try:
        prof_file = outdir / "order_profit.json"
        if prof_file.exists():
            prof_data = json.loads(prof_file.read_text(encoding="utf-8"))
            row = _select_store_profit_row(prof_data, sid)
            deal_info["financial_promotions"]["promotion_discount"] = float(row.get("promotion_discount") or 0.0)
            deal_info["financial_promotions"]["promotion_fee"] = float(row.get("promotion_fee") or 0.0)
            deal_info["financial_promotions"]["sp_discount"] = float(row.get("sp_discount") or 0.0)

        perf_file = outdir / "product_performance.json"
        if perf_file.exists():
            perf_data = json.loads(perf_file.read_text(encoding="utf-8"))
            row = perf_data[0] if isinstance(perf_data, list) and perf_data else (perf_data if isinstance(perf_data, dict) else {})
            deal_info["financial_promotions"]["promotion_amount"] = float(row.get("promotion_amount") or 0.0)
            deal_info["financial_promotions"]["promotion_volume"] = int(float(row.get("promotion_volume") or row.get("promotion_order_items") or 0))
    except Exception:
        pass

    # Summary badge & description (ACTIVE_DEAL vs DEAL_TRANSITION vs COUPON_OR_PROMOTION)
    if deal_info["is_active_deal"]:
        deal_info["summary_badge"] = "ACTIVE_DEAL"
        act_w = deal_info.get("active_deal_window") or f"截至 {deal_info.get('last_deal_date') or end_date}"
        deal_info["summary_desc"] = f"商品当前（{act_w}）正处于活跃秒杀/Prime大促进行期，转化与曝光处于促销放大状态"
    elif deal_info["is_deal_transition"]:
        deal_info["summary_badge"] = "DEAL_TRANSITION"
        deal_info["summary_desc"] = (
            f"检测到商品于 {deal_info['last_deal_date']}（{deal_info['days_since_last_deal']} 天前）结束秒杀/大促，"
            f"当前处于促后流量自然回落冷却期（严禁将促后回落误判为竞价断流）"
        )
    elif abs(deal_info["financial_promotions"]["promotion_discount"]) > 5.0 or deal_info["coupon_days"]:
        deal_info["summary_badge"] = "COUPON_OR_PROMOTION"
        deal_info["summary_desc"] = f"近期存在优惠券/折扣活动 (财务折让额: ${abs(deal_info['financial_promotions']['promotion_discount']):.2f})"
    elif deal_info["deal_days"] or deal_info["prime_days"]:
        deal_info["summary_badge"] = "PAST_DEAL_STEADY"
        deal_info["summary_desc"] = f"历史促销已于 {deal_info['last_deal_date']} 结束（距今 {deal_info['days_since_last_deal']} 天，已回归平销稳态）"

    save(outdir / "deal_info.json", deal_info)
    return deal_info


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

    resolved = resolve_shop(asin, country, args.shop, portfolio=args.portfolio, window=window)
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

    portfolio_id = resolved.get("resolved_portfolio_id") or (
        resolve_portfolio_id(args.portfolio, profile_id, window) if args.portfolio else None
    )

    # Decide the SKU filter with one cheap probe instead of fetching everything twice.
    sku = msku
    if msku:
        probe_extra: dict[str, Any] = {"sku": [msku]}
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

    # Advanced Intelligence: Dayparting, USD Traffic performance (30d + daily), Operation Logs, Real profit
    counts.update(run_specs(
        build_advanced_specs(sid, msku, asin, window), outdir, max_workers=args.concurrency
    ))

    product_ads_path = outdir / "product_ads.json"
    product_ads = json.loads(product_ads_path.read_text(encoding="utf-8")) if product_ads_path.exists() else []
    asin_keys = filter_asin_campaigns(product_ads, asin)
    own_store_asins = {
        str(r.get("asin") or r.get("advertised_asin") or "").strip().upper()
        for r in (product_ads if isinstance(product_ads, list) else [])
        if isinstance(r, dict)
    } | {asin}

    # Xiyou Dongcha (西柚洞察) Rank, Traffic, Parent Channel Split, Keyword Count & BSR Intelligence
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

        # Extract top targeted / clicked competitor ASINs from targets.json & search_terms.json for batched get_asin_traffic benchmarking
        comp_spend: dict[str, float] = {}
        tg_path = outdir / "targets.json"
        if tg_path.exists():
            try:
                tg_rows = json.loads(tg_path.read_text(encoding="utf-8"))
                for tr in (tg_rows if isinstance(tg_rows, list) else []):
                    if not isinstance(tr, dict):
                        continue
                    if asin_keys and tr.get("campaign_name") not in asin_keys and str(tr.get("campaign_id") or "") not in asin_keys:
                        continue
                    blob = json.dumps(tr.get("expression") or tr.get("targeting_text") or tr.get("key") or "")
                    for m_asin in re.findall(r"B0[0-9A-Z]{8}", blob.upper()):
                        if m_asin not in own_store_asins:
                            comp_spend[m_asin] = comp_spend.get(m_asin, 0.0) + float(tr.get("spends") or 0.0)
            except Exception:
                pass
        if len(comp_spend) < 8:
            st_path = outdir / "search_terms.json"
            if st_path.exists():
                try:
                    st_rows = json.loads(st_path.read_text(encoding="utf-8"))
                    for sr in (st_rows if isinstance(st_rows, list) else []):
                        if not isinstance(sr, dict):
                            continue
                        if asin_keys and sr.get("campaign_name") not in asin_keys and str(sr.get("campaign_id") or "") not in asin_keys:
                            continue
                        q_up = str(sr.get("query") or "").strip().upper()
                        if re.fullmatch(r"B0[0-9A-Z]{8}", q_up) and q_up not in own_store_asins:
                            comp_spend[q_up] = comp_spend.get(q_up, 0.0) + float(sr.get("spends") or 0.0)
                except Exception:
                    pass
        top_comp_asins = [a for a, _ in sorted(comp_spend.items(), key=lambda kv: -kv[1])[:8]]
        bounds_30 = split_window(window)
        anchor_end = bounds_30[1] if bounds_30 else None
        print(f"[xydc-mcp] Probing ASIN + competitor traffic, parent channel split, keyword count & BSR trends...")
        xydc_traffic = xydc.fetch_asin_traffic_intelligence(
            asin,
            country=country,
            competitor_asins=top_comp_asins,
            anchor_date=anchor_end,
            days=14,
        )
        if xydc_traffic and (
            xydc_traffic.get("self_traffic_7d")
            or xydc_traffic.get("parent_ad_overview_7d")
            or xydc_traffic.get("keyword_count_trend")
            or xydc_traffic.get("bsr_trend")
        ):
            save(outdir / "xydc_traffic.json", xydc_traffic)
            counts["xydc_traffic"] = 1
            st7 = xydc_traffic.get("self_traffic_7d") or {}
            print(
                f"[xydc-mcp] Saved xydc_traffic.json (org_share={st7.get('organic_share_pct', 0):.1f}%, "
                f"ad_share={st7.get('ad_share_pct', 0):.1f}%, competitors_benchmarked={len(xydc_traffic.get('competitor_traffic_7d') or {})})."
            )
    except Exception as exc:
        print(f"[xydc-mcp] Optional rank/traffic intelligence skipped: {exc}", file=sys.stderr)

    # Deal, Price & Promotion Markers across xydc-mcp & LingXing-MCP
    print(f"[Deal & Promotion] Probing deals, coupons & promotions for ASIN {asin}...")
    deal_info = probe_deal_and_promotions(asin, country, sid, msku, window, outdir, days=args.days)
    counts["deal_info"] = 1
    if deal_info.get("is_active_deal") or deal_info.get("is_deal_transition"):
        print(f"[Deal & Promotion] {deal_info.get('summary_badge')}: {deal_info.get('summary_desc')}")
    elif deal_info.get("has_deal_history"):
        print(f"[Deal & Promotion] Deal/promotion history: {deal_info.get('summary_desc')}")
    meta = {
        "asin": asin,
        "country": country,
        "shop_alias": shop.get("alias"),
        "sid": shop.get("sid"),
        "profile_id": shop.get("profile_id"),
        "shared_shops": resolved.get("shared_shops") or [],
        "portfolio_id": portfolio_id,
        "portfolio_name": args.portfolio,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": window,
        "days": args.days,
        "windows": multi_win,
        "listing_found": bool(listing),
        "msku": msku,
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
