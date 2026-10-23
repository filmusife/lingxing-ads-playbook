# -*- coding: utf-8 -*-
"""Lingxing OpenMCP client. Never print the API key."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"


class LingxingError(RuntimeError):
    pass


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_HOME") or (Path.home() / ".gemini"))


def _resolve_mcp_config() -> dict[str, str]:
    # 1. Check environment variables
    url = os.environ.get("LINGXING_MCP_URL") or DEFAULT_URL
    key = os.environ.get("LINGXING_MCP_KEY") or ""
    if key:
        return {"url": url, "key": key}

    # 2. Check Antigravity / Gemini global and local mcp_config.json
    gemini_mcp_paths = [
        _gemini_home() / "config" / "mcp_config.json",
        Path.cwd() / ".gemini" / "mcp_config.json",
        Path.cwd() / ".agents" / "mcp_config.json",
    ]
    for p in gemini_mcp_paths:
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    data = json.loads(content)
                    servers = data.get("mcpServers") or data.get("mcp_servers") or {}
                    # Try matching variations of lingxing
                    srv = None
                    for name in ("lingxing-mcp", "LingXing-MCP", "lingxing", "lingxing_mcp"):
                        if name in servers:
                            srv = servers[name]
                            break
                    if srv and isinstance(srv, dict):
                        url = srv.get("url") or srv.get("serverUrl") or url
                        headers = srv.get("headers") or srv.get("http_headers") or {}
                        key = (
                            headers.get("X-Mcp-Key")
                            or headers.get("x-mcp-key")
                            or (srv.get("env") or {}).get("LINGXING_MCP_KEY")
                            or key
                        )
                        if key:
                            return {"url": url, "key": key}
            except Exception:
                pass

    # 3. Check Codex config.toml. tomllib is stdlib only on Python 3.11+, so this
    #    source is optional -- never let it break the env-var or Gemini paths above.
    cfg_path = _codex_home() / "config.toml"
    if cfg_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            tomllib = None
        if tomllib is not None:
            try:
                with cfg_path.open("rb") as f:
                    cfg = tomllib.load(f)
                server = (cfg.get("mcp_servers") or {}).get("lingxing-mcp") or {}
                url = server.get("url") or url
                headers = server.get("http_headers") or {}
                key = headers.get("X-Mcp-Key") or headers.get("x-mcp-key") or key
                if key:
                    return {"url": url, "key": key}
            except Exception:
                pass

    raise LingxingError(
        "Missing Lingxing MCP key. Please configure it via one of:\n"
        "1. Environment variable: set LINGXING_MCP_KEY=<your_key>\n"
        "2. Antigravity: add to ~/.gemini/config/mcp_config.json under mcpServers.lingxing-mcp.headers.X-Mcp-Key\n"
        "3. Codex: add to ~/.codex/config.toml under [mcp_servers.lingxing-mcp.http_headers] X-Mcp-Key\n"
        "   (requires Python 3.11+ for TOML support)"
    )


_config_cache: dict[str, str] | None = None
_config_lock = threading.Lock()


def load_mcp_config() -> dict[str, str]:
    """Resolve the MCP endpoint once and reuse it for every subsequent RPC."""
    global _config_cache
    if _config_cache is None:
        with _config_lock:
            if _config_cache is None:
                _config_cache = _resolve_mcp_config()
    return _config_cache


def rpc(method: str, params: dict | None = None, timeout: int = 120, max_retries: int = 3) -> dict[str, Any]:
    cfg = load_mcp_config()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "X-Mcp-Key": cfg["key"],
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json; charset=utf-8",
    }
    
    last_err = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            cfg["url"],
            data=req_data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            if isinstance(exc, urllib.error.HTTPError):
                raise LingxingError(f"HTTP {exc.code} calling {method}") from exc
            raise LingxingError(f"Network error calling {method}: {exc}") from exc
    else:
        raise LingxingError(f"Failed calling {method} after {max_retries} attempts: {last_err}")

    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        trimmed = body.strip()
        if trimmed.startswith("data:"):
            lines = [l[5:].strip() for l in trimmed.splitlines() if l.strip().startswith("data:")]
            try:
                obj = json.loads("".join(lines))
            except Exception:
                raise LingxingError("Lingxing MCP returned non-JSON") from exc
        else:
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


_searched_tools: set[str] = set()
_searched_lock = threading.Lock()


def _needs_schema_refresh(res: Any) -> bool:
    """A 422 means the server rejected our params; re-fetching the tool schema may fix it."""
    msg = res.get("msg", "") if isinstance(res, dict) else ""
    raw_str = str(res)
    return "status: 422" in raw_str or "422" in str(msg) or "工具参数" in raw_str


def action(tool_id: str, params: dict | None = None) -> Any:
    res = call_tool("action", {"toolId": tool_id, "params": params or {}})
    if not _needs_schema_refresh(res):
        return res
    with _searched_lock:
        already_searched = tool_id in _searched_tools
        if not already_searched:
            _searched_tools.add(tool_id)
    if already_searched:
        return res
    search(tool_id)
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


def _declared_total(payload: Any) -> int | None:
    """Pull the server's row count out of a response so pagination can stop early."""
    cur = payload
    for _ in range(4):
        if not isinstance(cur, dict):
            return None
        for key in ("total", "total_count", "totalCount", "total_size", "count"):
            val = cur.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) and val >= 0:
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)
        cur = cur.get("data")
    return None


def paginate(tool_id: str, base_params: dict, page_key: str = "page", start: int = 1, max_pages: int = 20) -> list[dict]:
    all_rows: list[dict] = []
    try:
        length = int(base_params.get("length") or base_params.get("page_size") or base_params.get("limit") or 100)
    except (TypeError, ValueError):
        length = 100
    if length <= 0:
        length = 100

    page = start
    while page < start + max_pages:
        params = dict(base_params)
        params[page_key] = page
        payload = action(tool_id, params)
        chunk = rows(payload)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(chunk) < length:
            break
        # Stop without burning a round trip on an empty final page when the
        # server told us how many rows exist in total.
        total = _declared_total(payload)
        if total is not None and len(all_rows) >= total:
            break
        page += 1
    return all_rows
