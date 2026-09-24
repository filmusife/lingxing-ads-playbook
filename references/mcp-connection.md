# 领星 MCP 连接

## 配置位置

`%USERPROFILE%\.codex\config.toml`（Linux/macOS 为 `~/.codex/config.toml`）：

```toml
[mcp_servers.lingxing-mcp]
enabled = true
url = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"

[mcp_servers.lingxing-mcp.http_headers]
X-Mcp-Key = "<用户自己的 key，不要写进 skill 或聊天>"
```

环境变量备选：`LINGXING_MCP_KEY`、`LINGXING_MCP_URL`。

用户若粘贴 JSON（`mcpServers.LingXing-MCP`），转换成上面的 toml，**不要把 key 复述出来**。Open API IP 白名单是另一条路，默认继续用 MCP。

## 调用面

领星 OpenMCP 不是“每个报表一个 MCP tool”。对外三个入口：

| 入口 | 用途 |
|---|---|
| `help` | 列 toolId |
| `search` | `{ "toolId": "ad_campaign_report" }` 拿 inputSchema |
| `action` | `{ "toolId": "...", "params": { ... } }` 真正取数 |

HTTP 回退（当前会话经常没有原生 MCP tools）：

- URL: `https://openmcp.lingxing.com/mcp-servers/lingxing-mcp`
- JSON-RPC `tools/call`
- Header: `X-Mcp-Key`, `Accept: application/json, text/event-stream`, `Content-Type: application/json; charset=utf-8`
- 返回 `result.content[].text`，再 `json.loads`

用 `scripts/lx_client.py`，不要手写一堆含 key 的临时脚本。

## 店铺解析（ASIN + 国家）

1. `ad_auth_shops`：字段 `alias`（店铺名）、`country`（US/UK/DE…）、`sid`、`profile_id`、`real_status`。
2. 按国家过滤；用户给了店铺名就匹配 `alias`（大小写不敏感）。
3. 同一国家多个店铺：对每个 `sid` 调 `erp_listing`（`search_field=asin`, `search_value=[ASIN]`, `exact_search=1`），命中谁用谁。
4. 广告报表用 `profile_ids: ["<profile_id>"]`；Listing/库存用 `sid`。
5. 可选：用户点名广告组合时，用 `ad_portfolio_report_shop` 或活动名/portfolio_id 收窄。没点名就先按 ASIN 的 `ad_campaign_product_report` 找到所有在投活动，再反查组合。

英国在领星里是 `UK` 不是 `GB`。

## 日期窗口

默认：昨天往前 **30 天**（否定词需要更长样本）。

```
report_date = "YYYY-MM-DD - YYYY-MM-DD"
```

用户明确要求 7/14 天才改短窗口。不要把仓库里几个月前的 JSON 当当前表现。

## 分页与外壳

- 报表几乎都是 `page` + `length`（建议 100）。一直翻到空页或不足一页。
- 返回经常套两层 `{code, data:{code, data:{list: [...]}}}`。用 `lx_client.unwrap()`。
- **第一行常常是合计**：`name` / `campaign_name` / `keyword_text` 为空。分析时丢掉。

## 只读 vs 写入

本 skill 默认只读。写入工具（用户明确要求才用）：

- `put_campaigns_sp` / `put_adGroups_sp` / `put_targets_sp`
- `advertising_add_negative_keywords` / `advertising_add_negative_targets`
- `advertising_sp_add_keywords` / `post_keywords`

写之前必须先 `search` schema，并且只改用户确认过的活动。
