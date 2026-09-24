# -*- coding: utf-8 -*-
"""Lingxing OpenMCP client. Never print the API key."""

from __future__ import annotations

import json
import os
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"


class LingxingError(RuntimeError):
    pass


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def load_mcp_config() -> dict[str, str]:
    url = os.environ.get("LINGXING_MCP_URL") or DEFAULT_URL
    key = os.environ.get("LINGXING_MCP_KEY") or ""
    cfg_path = _codex_home() / "config.toml"
    if cfg_path.exists():
        with cfg_path.open("rb") as f:
            cfg = tomllib.load(f)
        server = (cfg.get("mcp_servers") or {}).get("lingxing-mcp") or {}
        url = server.get("url") or url
        headers = server.get("http_headers") or {}
        key = headers.get("X-Mcp-Key") or headers.get("x-mcp-key") or key
    if not key:
        raise LingxingError(
            "Missing Lingxing MCP key. Add [mcp_servers.lingxing-mcp.http_headers] "
            "X-Mcp-Key in ~/.codex/config.toml or set LINGXING_MCP_KEY."
        )
    return {"url": url, "key": key}


def rpc(method: str, params: dict | None = None, timeout: int = 120) -> dict[str, Any]:
    cfg = load_mcp_config()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-Mcp-Key": cfg["key"],
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise LingxingError(f"HTTP {exc.code} calling {method}") from exc
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LingxingError("Lingxing MCP returned non-JSON") from exc
    if obj.get("error"):
        raise LingxingError(json.dumps(obj["error"], ensure_ascii=False))
    return obj.get("result") or {}


def call_tool(name: str, arguments: dict | None = None) -> Any:
    result = rpc("tools/call", {"name": name, "arguments": arguments or {}})
    texts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(item.get("text") or "")
        elif isinstance(item, dict):
            texts.append(json.dumps(item, ensure_ascii=False))
    raw = "\n".join(texts).strip()
    if not raw:
        return result
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def help(query: str | None = None) -> Any:
    args = {"query": query} if query else {}
    return call_tool("help", args)


def search(tool_id: str) -> Any:
    return call_tool("search", {"toolId": tool_id})


def action(tool_id: str, params: dict | None = None) -> Any:
    return call_tool("action", {"toolId": tool_id, "params": params or {}})


def unwrap(obj: Any) -> Any:
    cur = obj
    for _ in range(6):
        if not isinstance(cur, dict):
            return cur
        if isinstance(cur.get("list"), list):
            return cur["list"]
        data = cur.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            cur = data
            continue
        for key in ("records", "rows", "items"):
            if isinstance(cur.get(key), list):
                return cur[key]
        return cur
    return cur


def rows(obj: Any) -> list[dict]:
    data = unwrap(obj)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "rows", "items", "data"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def paginate(tool_id: str, base_params: dict, page_key: str = "page", start: int = 1, max_pages: int = 20) -> list[dict]:
    all_rows: list[dict] = []
    page = start
    while page <= max_pages:
        params = dict(base_params)
        params[page_key] = page
        payload = action(tool_id, params)
        chunk = rows(payload)
        if not chunk:
            break
        all_rows.extend(chunk)
        try:
            length = int(params.get("length") or params.get("limit") or params.get("page_size") or 100)
        except (TypeError, ValueError):
            length = 100
        if len(chunk) < length:
            break
        page += 1
    return all_rows
