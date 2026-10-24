# -*- coding: utf-8 -*-
"""Xiyou Dongcha (西柚洞察) MCP client for Amazon keyword & rank intelligence."""

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

    # Check ~/.gemini/config/mcp_config.json
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

    # Handle SSE data: lines if returned as event-stream
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


def fetch_asin_keyword_ranks(
    asin: str,
    country: str = "US",
    max_keywords: int = 1000,
) -> dict[str, dict]:
    """Fetch ASIN keyword ranks and traffic distribution from xydc-mcp.

    Returns a normalized dictionary keyed by lowercase search term:
    {
        "coffee grinder": {
            "search_term": "coffee grinder",
            "organic_rank": 108,
            "organic_page": 3,
            "sp_rank": 9,
            "sp_page": 1,
            "sb_rank": 1,
            "organic_traffic": 28259,
            "ad_traffic": 32874,
            "total_traffic": 61133,
            "organic_rate": 0.2136,
            "ad_rate": 0.2486,
            "organic_growth": -0.3136,
            "ad_growth": -0.3085,
        },
        ...
    }
    """
    page_size = min(max_keywords, 500)
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

    data_block = res.get("data", {})
    raw_list = data_block.get("list", [])
    if not raw_list and isinstance(res.get("list"), list):
        raw_list = res.get("list", [])

    rank_map: dict[str, dict] = {}
    for item in raw_list:
        term = (item.get("searchTerm") or "").strip().lower()
        if not term:
            continue

        ranks = item.get("ranks", [])
        or_info = next((r for r in ranks if r.get("position") == "or"), None)
        sp_info = next((r for r in ranks if r.get("position") == "sp"), None)
        sb_info = next((r for r in ranks if r.get("position") == "sb"), None)

        traf_block = item.get("trafficSummary", {})
        traf = traf_block.get("traffic", {})
        acq_rate = traf_block.get("trafficAcquisitionRate", {})

        def _num(x):
            try:
                return float(x) if x is not None and str(x) != "--" else 0.0
            except Exception:
                return 0.0

        rank_map[term] = {
            "search_term": term,
            "organic_rank": or_info.get("totalRank") if or_info else None,
            "organic_page": or_info.get("page") if or_info else None,
            "sp_rank": sp_info.get("totalRank") if sp_info else None,
            "sp_page": sp_info.get("page") if sp_info else None,
            "sb_rank": sb_info.get("page") if sb_info else None,
            "organic_traffic": _num(traf.get("organic")),
            "ad_traffic": _num(traf.get("advertising")),
            "total_traffic": _num(traf.get("total")),
            "organic_rate": _num(acq_rate.get("organic")),
            "ad_rate": _num(acq_rate.get("advertising")),
            "organic_growth": _num(traf.get("organicGrowthRate")),
            "ad_growth": _num(traf.get("advertisingGrowthRate")),
        }

    return rank_map


def fetch_asin_deal_trends(
    asin: str,
    country: str = "US",
    days: int = 30,
) -> dict:
    """Fetch ASIN daily deal, coupon, and price promotion trends from xydc-mcp (get_asin_info_trends)."""
    end_date = (date.today() - timedelta(days=1)).isoformat()
    start_date = (date.today() - timedelta(days=days)).isoformat()
    args = {
        "asin": asin,
        "country": country.upper(),
        "start_date": start_date,
        "end_date": end_date,
        "user_task": f"查询商品 {asin} 近期秒杀、Deal与优惠券活动日趋势",
        "intent_summary": "查询价格、Deal及促销变动日趋势，识别近期是否有秒杀活动及其结束时间",
    }
    res = call_xydc_tool("get_asin_info_trends", args)
    if not res or "error" in res:
        return {}

    data_block = res.get("data", {})
    raw_trends = data_block.get("trends", [])
    if not raw_trends and isinstance(res.get("trends"), list):
        raw_trends = res.get("trends", [])

    deal_days = []
    coupon_days = []
    prime_days = []
    promo_days = []

    for t in raw_trends:
        d_str = t.get("date", "")
        p_dist = t.get("priceDistribution", {})
        deal_val = str(p_dist.get("deal") or "").strip()
        coupon_val = p_dist.get("coupon") or []
        prime_val = str(p_dist.get("prime") or "").strip()
        promo_val = p_dist.get("promotion") or []
        display_price = str(p_dist.get("display") or "")

        if deal_val:
            deal_days.append({"date": d_str, "deal_price": deal_val, "display_price": display_price})
        if coupon_val:
            coupon_days.append({"date": d_str, "coupon": coupon_val, "display_price": display_price})
        if prime_val:
            prime_days.append({"date": d_str, "prime_price": prime_val, "display_price": display_price})
        if promo_val:
            promo_days.append({"date": d_str, "promotion": promo_val, "display_price": display_price})

    has_deal_history = bool(deal_days or prime_days or coupon_days or promo_days)
    last_deal_date = deal_days[-1]["date"] if deal_days else (prime_days[-1]["date"] if prime_days else None)
    
    # Calculate days since last deal ended
    is_deal_transition = False
    days_since_deal = None
    if last_deal_date:
        try:
            d_obj = datetime.strptime(last_deal_date, "%Y-%m-%d").date()
            diff_days = (date.today() - d_obj).days
            days_since_deal = diff_days
            # If deal ended within last 10 days, current period is considered deal transition
            if diff_days <= 10:
                is_deal_transition = True
        except Exception:
            pass

    return {
        "asin": asin,
        "country": country.upper(),
        "query_window": f"{start_date} - {end_date}",
        "has_deal_history": has_deal_history,
        "is_deal_transition": is_deal_transition,
        "days_since_last_deal": days_since_deal,
        "last_deal_date": last_deal_date,
        "deal_days": deal_days,
        "coupon_days": coupon_days,
        "prime_days": prime_days,
        "promotion_days": promo_days,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python xydc_client.py <ASIN> [COUNTRY]")
        sys.exit(1)
    asin = sys.argv[1]
    country = sys.argv[2] if len(sys.argv) > 2 else "US"
    res = fetch_asin_keyword_ranks(asin, country=country)
    print(f"Total ranks mapped for {asin}: {len(res)}")
    for k in list(res.keys())[:10]:
        v = res[k]
        print(f"  {k:30s} | OR: {str(v['organic_rank']):4s} (p{str(v['organic_page'])}) | SP: {str(v['sp_rank']):4s} (p{str(v['sp_page'])}) | OrTraf: {v['organic_traffic']} | AdTraf: {v['ad_traffic']}")
