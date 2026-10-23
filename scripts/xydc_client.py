# -*- coding: utf-8 -*-
"""Xiyou Dongcha (西柚洞察) MCP client for Amazon keyword & rank intelligence."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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
