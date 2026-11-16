# -*- coding: utf-8 -*-
"""Xiyou Dongcha (西柚洞察) MCP client for Amazon keyword, rank & deal/price intelligence."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_XYDC_URL = "https://mcp.xydc.com/mcp"


def _gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_HOME") or (Path.home() / ".gemini"))


def resolve_xydc_config() -> dict[str, str]:
    """Resolve the endpoint URL and Authorization token for xydc-mcp."""
    url = os.environ.get("XYDC_MCP_URL") or DEFAULT_XYDC_URL
    token = os.environ.get("XYDC_MCP_TOKEN") or ""

    if token:
        auth_header = token if token.startswith("Bearer ") else f"Bearer {token}"
        return {"url": url, "auth": auth_header}

    paths = [
        _gemini_home() / "config" / "mcp_config.json",
        Path.cwd() / ".gemini" / "mcp_config.json",
    ]
    for p in paths:
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                servers = cfg.get("mcpServers") or cfg.get("mcp_servers") or {}
                srv = servers.get("xydc-mcp") or servers.get("xydc") or servers.get("xiyou")
                if srv and isinstance(srv, dict):
                    url = srv.get("url") or srv.get("serverUrl") or url
                    headers = srv.get("headers") or {}
                    auth = headers.get("Authorization") or headers.get("authorization") or ""
                    if auth:
                        return {"url": url, "auth": auth}
            except Exception:
                pass

    return {"url": url, "auth": ""}


def call_xydc_tool(tool_name: str, arguments: dict, timeout: int = 40) -> dict[str, Any]:
    """Direct JSON-RPC call to xydc-mcp endpoint."""
    cfg = resolve_xydc_config()
    if not cfg.get("auth"):
        return {"error": "Missing xydc-mcp authorization token in config"}

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": cfg["auth"],
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except Exception as exc:
        return {"error": f"Network error calling xydc-mcp {tool_name}: {exc}"}

    trimmed = body.strip()
    if trimmed.startswith("data:"):
        lines = [l[5:].strip() for l in trimmed.splitlines() if l.strip().startswith("data:")]
        trimmed = "".join(lines)

    try:
        res = json.loads(trimmed)
    except Exception as exc:
        return {"error": f"xydc-mcp returned non-JSON: {exc}"}

    if res.get("error"):
        return {"error": res["error"]}

    content = res.get("result", {}).get("content", [])
    if content and isinstance(content, list) and isinstance(content[0], dict):
        text = content[0].get("text", "")
        if text:
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}

    return res.get("result") or {}


def _safe_float(x: Any) -> float:
    if x is None:
        return 0.0
    s = str(x).strip().replace("$", "").replace(",", "").replace("%", "")
    if not s or s == "--":
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def fetch_asin_keyword_ranks(
    asin: str,
    country: str = "US",
    max_keywords: int = 1000,
) -> dict[str, dict]:
    """Fetch ASIN keyword ranks and traffic distribution from xydc-mcp.

    Returns a normalized dictionary keyed by lowercase search term.
    """
    page_size = max(1, min(int(max_keywords or 1000), 2000))
    args = {
        "asin": asin,
        "country": country.upper(),
        "page": 1,
        "page_size": page_size,
        "user_task": f"拉取 ASIN {asin} 在 {country} 的自然排名与广告排名分布",
        "intent_summary": "查询关键词自然位与广告位双轨排名及获流比",
    }

    res = call_xydc_tool("get_asin_keywords", args)
    if not res or "error" in res:
        err_msg = res.get("error") if isinstance(res, dict) else "Unknown error"
        print(f"[xydc-mcp] Warning: Could not fetch ranks for {asin}: {err_msg}")
        return {}

    data_block = res.get("data", {}) if isinstance(res.get("data"), dict) else {}
    raw_list = data_block.get("list", [])
    if not raw_list and isinstance(res.get("list"), list):
        raw_list = res.get("list", [])

    rank_map: dict[str, dict] = {}
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        term = (item.get("searchTerm") or "").strip().lower()
        if not term:
            continue

        ranks = item.get("ranks", []) or []
        or_info = next((r for r in ranks if isinstance(r, dict) and r.get("position") == "or"), None)
        sp_info = next((r for r in ranks if isinstance(r, dict) and r.get("position") == "sp"), None)
        sb_info = next((r for r in ranks if isinstance(r, dict) and r.get("position") == "sb"), None)

        traf_block = item.get("trafficSummary", {}) or {}
        traf = traf_block.get("traffic", {}) or {}
        acq_rate = traf_block.get("trafficAcquisitionRate", {}) or {}

        rank_map[term] = {
            "search_term": term,
            "organic_rank": or_info.get("totalRank") if or_info else None,
            "organic_page": or_info.get("page") if or_info else None,
            "sp_rank": sp_info.get("totalRank") if sp_info else None,
            "sp_page": sp_info.get("page") if sp_info else None,
            "sb_rank": (sb_info.get("totalRank") if sb_info.get("totalRank") is not None else sb_info.get("rank")) if sb_info else None,
            "sb_page": sb_info.get("page") if sb_info else None,
            "organic_traffic": _safe_float(traf.get("organic")),
            "ad_traffic": _safe_float(traf.get("advertising")),
            "total_traffic": _safe_float(traf.get("total")),
            "organic_rate": _safe_float(acq_rate.get("organic")),
            "ad_rate": _safe_float(acq_rate.get("advertising")),
            "organic_growth": _safe_float(traf.get("organicGrowthRate")),
            "ad_growth": _safe_float(traf.get("advertisingGrowthRate")),
        }

    return rank_map


def fetch_asin_deal_trends(
    asin: str,
    country: str = "US",
    days: int = 30,
    anchor_date: str | None = None,
) -> dict:
    """Fetch ASIN daily deal, coupon, and price promotion trends from xydc-mcp (get_asin_info_trends).

    Properly sorts trend days chronologically and distinguishes an active in-flight deal
    (diff_days == 0 relative to anchor_date) from a post-deal cooldown transition (1 <= diff_days <= 10).
    """
    if anchor_date:
        try:
            anchor_dt = datetime.strptime(anchor_date[:10], "%Y-%m-%d").date()
        except Exception:
            anchor_dt = date.today() - timedelta(days=1)
    else:
        anchor_dt = date.today() - timedelta(days=1)

    end_date = anchor_dt.isoformat()
    start_date = (anchor_dt - timedelta(days=max(1, days - 1))).isoformat()
    args = {
        "asin": asin,
        "country": country.upper(),
        "start_date": start_date,
        "end_date": end_date,
        "user_task": f"查询商品 {asin} 近期秒杀、Deal、优惠券及价格变动日趋势",
        "intent_summary": "查询价格、Deal及促销变动日趋势，识别当前是否处于秒杀期或秒杀结束后冷却期",
    }
    res = call_xydc_tool("get_asin_info_trends", args)
    if not res or "error" in res:
        return {}

    data_block = res.get("data", {}) if isinstance(res.get("data"), dict) else {}
    raw_trends = data_block.get("trends", [])
    if not raw_trends and isinstance(res.get("trends"), list):
        raw_trends = res.get("trends", [])

    # Sort strictly in ascending chronological order so [-1] is guaranteed to be the latest date
    sorted_trends = sorted(
        [t for t in raw_trends if isinstance(t, dict) and t.get("date")],
        key=lambda t: str(t.get("date"))[:10],
    )

    deal_days = []
    coupon_days = []
    prime_days = []
    promo_days = []
    price_series = []

    for t in sorted_trends:
        d_str = str(t.get("date", ""))[:10]
        p_dist = t.get("priceDistribution", {}) or {}
        deal_val = str(p_dist.get("deal") or "").strip()
        coupon_val = p_dist.get("coupon") or []
        prime_val = str(p_dist.get("prime") or "").strip()
        promo_val = p_dist.get("promotion") or []
        display_price = str(p_dist.get("display") or "").strip()
        disp_num = _safe_float(display_price)
        if disp_num > 0:
            price_series.append({"date": d_str, "price": disp_num})

        if deal_val:
            deal_days.append({"date": d_str, "deal_price": deal_val, "display_price": display_price})
        if coupon_val:
            coupon_days.append({"date": d_str, "coupon": coupon_val, "display_price": display_price})
        if prime_val:
            prime_days.append({"date": d_str, "prime_price": prime_val, "display_price": display_price})
        if promo_val:
            promo_days.append({"date": d_str, "promotion": promo_val, "display_price": display_price})

    has_deal_history = bool(deal_days or prime_days or coupon_days or promo_days)
    major_promo_dates = sorted({x["date"] for x in (deal_days + prime_days) if x.get("date")})
    last_deal_date = major_promo_dates[-1] if major_promo_dates else None

    is_active_deal = False
    is_deal_transition = False
    days_since_deal = None
    if last_deal_date:
        try:
            d_obj = datetime.strptime(last_deal_date, "%Y-%m-%d").date()
            diff_days = max(0, (anchor_dt - d_obj).days)
            days_since_deal = diff_days
            if diff_days == 0:
                is_active_deal = True
                is_deal_transition = False
            elif 1 <= diff_days <= 10:
                is_active_deal = False
                is_deal_transition = True
        except Exception:
            pass

    price_summary: dict[str, Any] = {}
    if price_series:
        prices = [p["price"] for p in price_series]
        recent_7 = [p["price"] for p in price_series[-7:]]
        prior_7 = [p["price"] for p in price_series[-14:-7]]
        avg_r7 = sum(recent_7) / len(recent_7) if recent_7 else prices[-1]
        avg_p7 = sum(prior_7) / len(prior_7) if prior_7 else avg_r7
        price_delta_pct = round(((avg_r7 - avg_p7) / avg_p7) * 100.0, 2) if avg_p7 > 0 else 0.0
        price_summary = {
            "latest_price": prices[-1],
            "min_price_30d": min(prices),
            "max_price_30d": max(prices),
            "avg_price_recent_7d": round(avg_r7, 2),
            "avg_price_prior_7d": round(avg_p7, 2),
            "price_delta_7d_pct": price_delta_pct,
        }

    return {
        "asin": asin,
        "country": country.upper(),
        "anchor_date": end_date,
        "query_window": f"{start_date} - {end_date}",
        "has_deal_history": has_deal_history,
        "is_active_deal": is_active_deal,
        "is_deal_transition": is_deal_transition,
        "days_since_last_deal": days_since_deal,
        "last_deal_date": last_deal_date,
        "deal_days": deal_days,
        "coupon_days": coupon_days,
        "prime_days": prime_days,
        "promotion_days": promo_days,
        "price_summary": price_summary,
    }


def fetch_asin_traffic_intelligence(
    asin: str,
    country: str = "US",
    competitor_asins: list[str] | None = None,
    anchor_date: str | None = None,
    days: int = 14,
) -> dict[str, Any]:
    """Fetch comprehensive ASIN & competitor traffic, parent variation ad split, keyword indexation trend,
    and daily BSR trend from xydc-mcp (get_asin_traffic, get_parent_asin_ad_overview,
    get_asin_keyword_count_trends, get_asin_bsr_trends).
    """
    asin_up = asin.strip().upper()
    ctry = country.strip().upper()
    if anchor_date:
        try:
            anchor_dt = datetime.strptime(anchor_date[:10], "%Y-%m-%d").date()
        except Exception:
            anchor_dt = date.today() - timedelta(days=1)
    else:
        anchor_dt = date.today() - timedelta(days=1)

    end_date = anchor_dt.isoformat()
    start_date = (anchor_dt - timedelta(days=max(7, days - 1))).isoformat()
    user_task = f"深度诊断 ASIN {asin_up} 在 {ctry} 站点的自然流量、广告流量结构、竞品护城河及排名趋势"

    comp_clean: list[str] = []
    seen_asins = {asin_up}
    for c_asin in (competitor_asins or []):
        ca = str(c_asin or "").strip().upper()
        if len(ca) == 10 and ca.startswith("B0") and ca not in seen_asins:
            seen_asins.add(ca)
            comp_clean.append(ca)
        if len(comp_clean) >= 8:
            break

    batch_asins = [asin_up] + comp_clean
    result: dict[str, Any] = {
        "asin": asin_up,
        "country": ctry,
        "anchor_date": end_date,
        "trend_window": f"{start_date} - {end_date}",
        "self_traffic_7d": {},
        "competitor_traffic_7d": {},
        "parent_ad_overview_7d": {},
        "keyword_count_trend": {},
        "bsr_trend": {},
    }

    # 1. get_asin_traffic (Main ASIN + up to 8 top competitor ASINs in one batched call)
    traf_res = call_xydc_tool("get_asin_traffic", {
        "asins": batch_asins,
        "country": ctry,
        "user_task": user_task,
        "intent_summary": "查询本品与核心对标竞品近7天自然流量得分、广告流量得分、流量占比及环比增速",
    })
    if isinstance(traf_res, dict) and "error" not in traf_res:
        t_data = traf_res.get("data", {}) if isinstance(traf_res.get("data"), dict) else {}
        entities = t_data.get("entities", []) if isinstance(t_data.get("entities"), list) else []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            e_asin = str(ent.get("asin") or "").strip().upper()
            if not e_asin:
                continue
            parsed_ent = {
                "asin": e_asin,
                "total_traffic_score": _safe_float(ent.get("totalTrafficScore")),
                "prev_7d_total_traffic_score": _safe_float(ent.get("previous7DaysTotalTrafficScore")),
                "total_growth_pct": round(_safe_float(ent.get("totalTrafficScoreGrowthRate")) * 100.0, 2),
                "organic_traffic_score": _safe_float(ent.get("organicTrafficScore")),
                "prev_7d_organic_traffic_score": _safe_float(ent.get("previous7DaysOrganicTrafficScore")),
                "organic_growth_pct": round(_safe_float(ent.get("organicTrafficScoreGrowthRate")) * 100.0, 2),
                "organic_share_pct": round(_safe_float(ent.get("organicTrafficScoreRatio")) * 100.0, 2),
                "organic_keyword_count": int(_safe_float(ent.get("organicSearchTermCount"))),
                "ad_traffic_score": _safe_float(ent.get("advertisingTrafficScore")),
                "prev_7d_ad_traffic_score": _safe_float(ent.get("previous7DaysAdvertisingTrafficScore")),
                "ad_growth_pct": round(_safe_float(ent.get("advertisingTrafficScoreGrowthRate")) * 100.0, 2),
                "ad_share_pct": round(_safe_float(ent.get("advertisingTrafficScoreRatio")) * 100.0, 2),
                "ad_keyword_count": int(_safe_float(ent.get("advertisingSearchTermCount"))),
            }
            if e_asin == asin_up:
                result["self_traffic_7d"] = parsed_ent
            else:
                result["competitor_traffic_7d"][e_asin] = parsed_ent

    # 2. get_parent_asin_ad_overview (Parent ASIN variations & Organic/SP/SB/SBV 4-channel split)
    parent_res = call_xydc_tool("get_parent_asin_ad_overview", {
        "asin": asin_up,
        "country": ctry,
        "top_n": 5,
        "user_task": user_task,
        "intent_summary": "查询父体变体结构及近7天自然搜索与SP/SB/SBV四渠道流量得分结构占比",
    })
    if isinstance(parent_res, dict) and "error" not in parent_res:
        p_data = parent_res.get("data", {}) if isinstance(parent_res.get("data"), dict) else {}
        scope = p_data.get("scope", {}) if isinstance(p_data.get("scope"), dict) else {}
        ch_dist = {}
        for item in (p_data.get("trafficScoreDistribution") or []):
            if isinstance(item, dict) and item.get("source"):
                ch_dist[str(item["source"])] = {
                    "traffic_score": _safe_float(item.get("trafficScore")),
                    "share_pct": round(_safe_float(item.get("shareOfTotalScore")) * 100.0, 2),
                }
        tier_dist = {}
        for item in (p_data.get("keywordTiers") or []):
            if isinstance(item, dict) and item.get("tier"):
                tier_dist[str(item["tier"])] = {
                    "traffic_score": _safe_float(item.get("trafficScore")),
                    "share_pct": round(_safe_float(item.get("ratio")) * 100.0, 2),
                }
        variations = []
        for item in (p_data.get("topVariations") or []):
            if isinstance(item, dict) and item.get("asin"):
                variations.append({
                    "asin": str(item.get("asin")).upper(),
                    "ad_traffic_score": _safe_float(item.get("trafficScore")),
                    "share_of_ad_score_pct": round(_safe_float(item.get("shareOfAdScore")) * 100.0, 2),
                })
        if scope or ch_dist:
            result["parent_ad_overview_7d"] = {
                "parent_asin": scope.get("parentAsin"),
                "total_variation_count": int(_safe_float(scope.get("totalVariationCount"))),
                "advertised_variation_count": int(_safe_float(p_data.get("advertisedVariationCount"))),
                "campaign_count_detected": int(_safe_float(p_data.get("campaignCount"))),
                "keyword_count_detected": int(_safe_float(p_data.get("keywordCount"))),
                "period": p_data.get("period") or {},
                "channel_distribution": ch_dist,
                "keyword_tier_distribution": tier_dist,
                "top_variations": variations,
            }

    # 3. get_asin_keyword_count_trends (14d daily indexed organic vs ad keyword count & position breakdown)
    kw_cnt_res = call_xydc_tool("get_asin_keyword_count_trends", {
        "asins": [asin_up],
        "country": ctry,
        "start_date": start_date,
        "end_date": end_date,
        "user_task": user_task,
        "intent_summary": "查询近14天每日收录自然关键词数、广告关键词数及头部/腰部/长尾词结构趋势",
    })
    if isinstance(kw_cnt_res, dict) and "error" not in kw_cnt_res:
        kc_data = kw_cnt_res.get("data", {}) if isinstance(kw_cnt_res.get("data"), dict) else {}
        kc_entities = kc_data.get("entities", []) if isinstance(kc_data.get("entities"), list) else []
        if kc_entities and isinstance(kc_entities[0], dict):
            raw_kc_trends = sorted(
                [
                    t for t in (kc_entities[0].get("trends") or [])
                    if isinstance(t, dict) and isinstance(t.get("cycleDate"), dict)
                ],
                key=lambda t: str(t["cycleDate"].get("startDate") or ""),
            )
            daily_kc = []
            for t in raw_kc_trends:
                d_str = str(t["cycleDate"].get("startDate") or "")[:10]
                sum_map = {
                    str(x.get("position")): int(_safe_float(x.get("count")))
                    for x in (t.get("summary") or []) if isinstance(x, dict)
                }
                pos_map = {
                    str(x.get("position")): int(_safe_float(x.get("count")))
                    for x in (t.get("positions") or []) if isinstance(x, dict)
                }
                tier_map = {
                    str(x.get("tier")): int(_safe_float(x.get("count")))
                    for x in (t.get("rankTier") or []) if isinstance(x, dict)
                }
                daily_kc.append({
                    "date": d_str,
                    "organic_count": sum_map.get("organic", pos_map.get("or", 0)),
                    "advertising_count": sum_map.get("advertising", pos_map.get("sp", 0)),
                    "or_count": pos_map.get("or", 0),
                    "sp_count": pos_map.get("sp", 0),
                    "sb_count": pos_map.get("sb", 0),
                    "sbv_count": pos_map.get("sbv", 0),
                    "ac_count": pos_map.get("ac", 0),
                    "head_count": tier_map.get("head", 0),
                    "mid_tail_count": tier_map.get("midTail", 0),
                    "long_tail_count": tier_map.get("longTail", 0),
                })
            if daily_kc:
                r7 = daily_kc[-7:]
                p7 = daily_kc[-14:-7]
                avg_org_r7 = round(sum(x["organic_count"] for x in r7) / len(r7), 1)
                avg_org_p7 = round(sum(x["organic_count"] for x in p7) / len(p7), 1) if p7 else avg_org_r7
                avg_ad_r7 = round(sum(x["advertising_count"] for x in r7) / len(r7), 1)
                avg_ad_p7 = round(sum(x["advertising_count"] for x in p7) / len(p7), 1) if p7 else avg_ad_r7
                org_delta_pct = round(((avg_org_r7 - avg_org_p7) / avg_org_p7) * 100.0, 2) if avg_org_p7 > 0 else 0.0
                ad_delta_pct = round(((avg_ad_r7 - avg_ad_p7) / avg_ad_p7) * 100.0, 2) if avg_ad_p7 > 0 else 0.0
                result["keyword_count_trend"] = {
                    "latest_date": daily_kc[-1]["date"],
                    "latest_snapshot": daily_kc[-1],
                    "recent_7d_avg_organic_keywords": avg_org_r7,
                    "prior_7d_avg_organic_keywords": avg_org_p7,
                    "organic_keywords_delta_pct": org_delta_pct,
                    "recent_7d_avg_ad_keywords": avg_ad_r7,
                    "prior_7d_avg_ad_keywords": avg_ad_p7,
                    "ad_keywords_delta_pct": ad_delta_pct,
                    "daily_series": daily_kc,
                }

    # 4. get_asin_bsr_trends (14d daily BSR category rank trajectory)
    bsr_res = call_xydc_tool("get_asin_bsr_trends", {
        "asin": asin_up,
        "country": ctry,
        "start_date": start_date,
        "end_date": end_date,
        "user_task": user_task,
        "intent_summary": "查询近14天大类与小类 BSR 排名每日走势",
    })
    if isinstance(bsr_res, dict) and "error" not in bsr_res:
        b_data = bsr_res.get("data", {}) if isinstance(bsr_res.get("data"), dict) else {}
        cat_tree = b_data.get("categoryTree") or []
        cat_map = {
            str(c.get("categoryId")): {
                "name": str(c.get("name") or ""),
                "is_root": bool(c.get("root")),
            }
            for c in cat_tree if isinstance(c, dict) and c.get("categoryId")
        }
        raw_b_trends = sorted(
            [t for t in (b_data.get("trends") or []) if isinstance(t, dict) and t.get("date")],
            key=lambda t: str(t.get("date"))[:10],
        )
        by_cat: dict[str, list[tuple[str, int]]] = {}
        for t in raw_b_trends:
            d_str = str(t.get("date"))[:10]
            for v in (t.get("values") or []):
                if isinstance(v, dict) and v.get("categoryId") and v.get("rank") is not None:
                    cid = str(v["categoryId"])
                    rk = int(_safe_float(v["rank"]))
                    if rk > 0:
                        by_cat.setdefault(cid, []).append((d_str, rk))
        cat_summaries = []
        for cid, pts in by_cat.items():
            c_meta = cat_map.get(cid, {"name": cid, "is_root": False})
            r7_pts = [rk for _, rk in pts[-7:]]
            p7_pts = [rk for _, rk in pts[-14:-7]]
            avg_r7 = round(sum(r7_pts) / len(r7_pts), 1) if r7_pts else pts[-1][1]
            avg_p7 = round(sum(p7_pts) / len(p7_pts), 1) if p7_pts else avg_r7
            cat_summaries.append({
                "category_id": cid,
                "category_name": c_meta["name"],
                "is_root": c_meta["is_root"],
                "latest_date": pts[-1][0],
                "latest_rank": pts[-1][1],
                "best_rank_14d": min(rk for _, rk in pts),
                "recent_7d_avg_rank": avg_r7,
                "prior_7d_avg_rank": avg_p7,
                "rank_change_7d": round(avg_r7 - avg_p7, 1),  # negative means rank improved (moved closer to #1)
            })
        cat_summaries.sort(key=lambda x: (1 if x["is_root"] else 0, x["latest_rank"]))
        if cat_summaries:
            result["bsr_trend"] = {
                "leaf_category": next((c for c in cat_summaries if not c["is_root"]), cat_summaries[0]),
                "root_category": next((c for c in cat_summaries if c["is_root"]), None),
                "categories": cat_summaries,
            }

    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python xydc_client.py <ASIN> [COUNTRY]")
        sys.exit(1)
    asin_arg = sys.argv[1]
    country_arg = sys.argv[2] if len(sys.argv) > 2 else "US"
    res = fetch_asin_keyword_ranks(asin_arg, country=country_arg)
    print(f"Total ranks mapped for {asin_arg}: {len(res)}")
    for k in list(res.keys())[:10]:
        v = res[k]
        print(
            f"  {k:30s} | OR: {str(v['organic_rank']):4s} (p{str(v['organic_page'])}) "
            f"| SP: {str(v['sp_rank']):4s} (p{str(v['sp_page'])}) "
            f"| OrTraf: {v['organic_traffic']} | AdTraf: {v['ad_traffic']}"
        )
