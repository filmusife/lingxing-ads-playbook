# 领星 MCP (`LingXing-MCP`) 与西柚洞察 (`xydc-mcp`) 双引擎连接指南

本 Skill 同时联动 **领星 OpenMCP (`LingXing-MCP`)**（拉取真实广告多切片报表、三展位报告、订单财务毛利、业务报告流量与操作审计日志）与 **西柚洞察 (`xydc-mcp`)**（拉取真实自然排名 vs 广告位排名、本品与竞品 7d 自然/广告流量护城河、父体四渠道流量拆解、14d 收录词数趋势、BSR 排名走势与每日价格/秒杀轨迹）。

> ⚠️ **安全红线**：永远不要在终端、报告或聊天窗口打印、复述任何 `X-Mcp-Key`、`Authorization` Token 或 Open API Secret！

---

## 一、配置位置（`lx_client.py` 与 `xydc_client.py` 自动按序识别）

### 1. Antigravity 全局配置（推荐：`~/.gemini/config/mcp_config.json`）

```json
{
  "mcpServers": {
    "lingxing-mcp": {
      "url": "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp",
      "headers": {
        "X-Mcp-Key": "<用户自己的领星 Key，不要写进 skill 或聊天>"
      }
    },
    "xydc-mcp": {
      "url": "<西柚洞察 MCP Streamable HTTP Endpoint>",
      "headers": {
        "Authorization": "Bearer <用户自己的西柚洞察 Token，不要写进 skill 或聊天>"
      }
    }
  }
}
```

### 2. 环境变量（轻量 / CI / 定时自动化脚本支持）

```powershell
# Windows PowerShell
$env:LINGXING_MCP_KEY="<你的领星_Key>"
# 可选：$env:LINGXING_MCP_URL="https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"
# 可选：$env:XYDC_MCP_URL="<西柚_URL>" ; $env:XYDC_MCP_KEY="<西柚_Token>"
```

### 3. Codex 配置（兼容 `~/.codex/config.toml`）

```toml
[mcp_servers.lingxing-mcp]
enabled = true
url = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"

[mcp_servers.lingxing-mcp.http_headers]
X-Mcp-Key = "<用户自己的领星 Key，不要写进 skill 或聊天>"
```

---

## 二、调用机制与客户端回退

### 1. 领星 OpenMCP (`scripts/lx_client.py`)
领星 OpenMCP 对外提供三个标准入口：

| 入口 | 用途 |
|---|---|
| `help` | 列出所有可用 `toolId` |
| `search` | `{ "toolId": "ad_campaign_report" }` 获取指定接口 `inputSchema` |
| `action` | `{ "toolId": "...", "params": { ... } }` 真正执行数据拉取 |

当会话无原生 MCP 工具或由后台定时任务直接执行 Python 脚本时，`scripts/lx_client.py` 自动通过 HTTP JSON-RPC (`tools/call`) 直连，支持 SSE / JSON 双响应解析与 `lx_client.unwrap()` 多层嵌套解包。

### 2. 西柚洞察 MCP (`scripts/xydc_client.py`)
西柚洞察每个功能为独立 MCP Tool，`fetch_ads_bundle.py` 在取数阶段自动通过 `scripts/xydc_client.py` 顺序调用 6 个高价值只读接口并缓存至工作目录：
1. `get_asin_keywords` → `work/lx-ads/xydc_ranks.json`（自然位 `OR`、广告位 `SP`/`SB`、词级流量得分与增速）
2. `get_asin_info_trends` → `work/lx-ads/deal_info.json`（融合领星秒杀/专享折与西柚近 30 天每日挂牌价、秒杀价、Coupon、Prime 专享折轨迹）
3. `get_asin_traffic` → `work/lx-ads/xydc_traffic.json`（本品 + Top 8 投放竞品 ASIN 近 7 天自然/广告流量得分、自然占比与收录词数）
4. `get_parent_asin_ad_overview` → `work/lx-ads/xydc_traffic.json`（父体变体结构与 `organic / sp / sb / sbv` 四渠道流量占比）
5. `get_asin_keyword_count_trends` → `work/lx-ads/xydc_traffic.json`（近 14 天每日自然词与广告词收录趋势）
6. `get_asin_bsr_trends` → `work/lx-ads/xydc_traffic.json`（近 14 天大小类目 BSR 每日走势）

---

## 三、店铺与组合自动定位流程（ASIN + 国家）

1. `ad_auth_shops`：按国家过滤（注意英国在领星里为 `UK`，西柚洞察里同样传 `US/UK/DE/...`）；用户指定店铺名时大小写不敏感匹配 `alias`。
2. 同一国家多店铺时：对候选 `sid` 调用 `erp_listing`（`search_field="asin"`, `search_value=[ASIN]`, `exact_search="1"`）或 `ad_campaign_product_report`（`profile_id=int(pid)`）精准锁定在投店铺与所属广告组合（Portfolio）。
3. 广告报表使用 `profile_ids: ["<profile_id>"]`；财务利润表 `query_order_profit_list` 返回多店共享 ASIN 时，优先精确匹配单店 `sids == [sid]` 行核算当前组合保本线，同时保留跨店全盘汇总。

---

## 四、默认窗口与只读安全边界

- **多切片鲜活窗口**：默认以昨天（`T-1`）为锚点同步拉取 **30 天宏观基线 + 近 7 天中期走向 + 近 3 天微观切片**，严禁拿几个月前的陈旧缓存冒充当前实时表现。
- **默认严格只读**：本 Skill 默认仅拉取只读分析数据并生成 `.md` 操盘手册与 `.tsv` 批量调整表，绝不自动调用 `put_campaigns_sp`、`put_adGroups_sp`、`put_targets_sp`、`advertising_add_negative_keywords` 等写接口。仅在用户明确指令“帮我写回后台”且逐项确认活动与金额后，方可先 `search` schema 再执行受控写入。
