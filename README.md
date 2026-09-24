# lingxing-ads-playbook

Codex skill：用领星 MCP 按 ASIN + 站点拉取最近 30 天广告/Listing 活数据，按产品功能差、上架阶段、搜索频率排名和配置快照冷却锁做 SP/SBV 诊断，输出可执行的完整或 delta 验收手册。

本仓库只包含 skill 源码与说明，**不包含**：

- 领星 MCP Key / Open API secret / token
- 店铺、ASIN、广告活动、库存等历史快照
- 本地 `state/` 运行产物

## 安装

把本仓库放到 Codex skills 目录：

```bash
git clone https://github.com/filmusife/lingxing-ads-playbook.git ~/.codex/skills/lingxing-ads-playbook
```

Windows：

```powershell
git clone https://github.com/filmusife/lingxing-ads-playbook.git "$env:USERPROFILE\.codex\skills\lingxing-ads-playbook"
```

## 领星 MCP 配置

在 `~/.codex/config.toml` 自行填写，**不要把 key 提交进仓库或发到聊天里**：

```toml
[mcp_servers.lingxing-mcp]
enabled = true
url = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"

[mcp_servers.lingxing-mcp.http_headers]
X-Mcp-Key = "<YOUR_LINGXING_MCP_KEY>"
```

也可使用环境变量 `LINGXING_MCP_KEY`、`LINGXING_MCP_URL`。

## 使用

用户提供 ASIN + 国家/站点后，在对话中调用本 skill（`/lingxing-ads` 或 `/广告调整`）。默认只出建议，不写回广告后台。

取数脚本：

```bash
python ~/.codex/skills/lingxing-ads-playbook/scripts/fetch_ads_bundle.py --asin {ASIN} --country {US|UK|DE} --outdir work/lx-ads
python ~/.codex/skills/lingxing-ads-playbook/scripts/analyze_ads_bundle.py --indir work/lx-ads --outdir work/lx-ads/analysis
```

`analyze_ads_bundle.py` 只产出证据表；最终手册按 `references/report-template.md` 手写。

## 目录

- `SKILL.md`：流程、铁律、交付标准
- `references/`：取数、分析框架、冷却锁、报告模板
- `scripts/`：领星取数与证据表脚本
- `agents/openai.yaml`：Codex 展示与 MCP 依赖声明

本地运行后会出现 `state/{profile_id}_{ASIN}.json`，该目录已被 `.gitignore` 忽略，请勿提交。
