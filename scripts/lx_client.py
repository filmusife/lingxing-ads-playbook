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


def _gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_HOME") or (Path.home() / ".gemini"))


def load_mcp_config() -> dict[str, str]:
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

    # 3. Check Codex config.toml
    cfg_path = _codex_home() / "config.toml"
    if cfg_path.exists():
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
        "3. Codex: add to ~/.codex/config.toml under [mcp_servers.lingxing-mcp.http_headers] X-Mcp-Key"
    )


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


def action(tool_id: str, params: dict | None = None) -> Any:
    res = call_tool("action", {"toolId": tool_id, "params": params or {}})
    raw_str = str(res)
    if ("status: 422" in raw_str or "422" in str(res.get("msg", "")) or "工具参数" in raw_str) and tool_id not in _searched_tools:
        search(tool_id)
        _searched_tools.add(tool_id)
        res = call_tool("action", {"toolId": tool_id, "params": params or {}})
    return res


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
            length = int(params.get("length") or params.get("page_size") or params.get("limit") or 100)
        except (TypeError, ValueError):
            length = 100
        if len(chunk) < length:
            break
        page += 1
    return all_rows
