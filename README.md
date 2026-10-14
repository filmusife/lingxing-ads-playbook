# lingxing-ads-playbook

亚马逊广告智能诊断与实操调优 Skill：通过领星 MCP 拉取多时间切片活数据（30天宏观基线 + 近7天中期走向 + 近3天最新微观切片），结合单位经济保本 ACOS、Listing 全域流量大盘、ABA 搜索排名梯队与 24 小时分时出单指纹，驱动 **双窗口差分纠偏与 AI 动态熔断体系**，并基于本地数据库持久化台账实现跨会话效果复盘，输出极具专业操盘深度的完整可执行手册与批量操作表。

本仓库仅包含 Skill 源代码、诊断框架与模版规范，**绝不包含**：
- 任何真实的领星 MCP Key / Open API Secret / Token / 认证凭据
- 任何特定店铺的私有数据、真实销售额、真实采购成本或历史快照
- 任何本地运行产物（`work/`、`db/`、`outputs/` 均已被 `.gitignore` 严格忽略）

---

## 核心特性

1. **多切片双轨保鲜引擎 (Multi-Window Fresh Slices)**：
   - 同步拉取截至昨天（$T-1$）的 **30天宏观基线**（用于大样本意图分桶与 N-Gram 累积词根挖掘）、**近7天走向** 与 **近3天微观切片**（用于锁定当前实际有效 CPC 与流速），彻底告别数据时效黑盒。
2. **双窗口动态差分纠偏 (Dual-Window Dynamic Delta Engine)**：
   - 彻底打破“30天历史均值稀释陷阱”。系统实时计算 $\Delta \text{CPC}\%$ 与 $\Delta \text{Imp}\%$，依据调后最新变化轨迹纠偏，严防对已生效降价的活动做“二次误杀”。
3. **AI 动态弹性观察期与异常熔断体系 (AI Circuit Breakers)**：
   - 告别死等 7 天：
     - **调后生效·见效保持 (`ADJUSTMENT_EFFECTIVE`)**：$\Delta \text{CPC} \le -15\%$ 且曝光健康，强制拦截降价，保持稳态；
     - **调后休克断流 (`CIRCUIT_BREAKER_SHOCK`)**：$\Delta \text{Imp} \le -70\%$ 且近3天 0 单，立即于第 2-3 天执行梯级回踩探底（+10%~+15%）；
     - **调后失血暴冲 (`CIRCUIT_BREAKER_BLEED`)**：$\Delta \text{CPC} \ge +40\%$ 或严重超标无单，立即切断溢价/限价；
     - **稳态监控 (`NORMAL_MONITORING`)**：指标在正常统计公差内波动，保持冷静观察。
4. **本地持久化数据库与跨会话自动对账 (Local DB & Persistent Ledger)**：
   - 在项目目录维护 `db/daily_records/`（每日指标档案）与 `db/ledger/`（调优历史账本）。
   - 无论何时新开对话窗口或重启会话，系统自动穿透读取历史账本，在报告第 2 节呈现《历史调控台账与调后效果复盘对账总表》，彻底打通跨窗口记忆断层。
5. **死活动净水器式重生协议 (Dead Campaign Rebirth Protocol)**：
   - 确立“展示死锁、负向饱和、架构畸变”三大死活动确诊指纹，提供标准化的“老活动彻底归档 + 基因清洗 + 预置词组否定防火墙 + 倒推起步竞价 + 单组 1-3 词精准收割仓”重生 SOP。
6. **全域业务闭环与财务保本 ACOS 约束**：
   - 自动核算单均采购成本 (COGS)、头程运费、FBA 配送费、平台佣金，精准推导真实保本 ACOS 并科学锚定目标投产线。
7. **全美 ABA 搜索量梯队与多维广告位指纹诊断**：
   - 自动对齐全美 ABA 搜索排名（Top 5K 超大词 / Top 30K 头部大词 / Top 150K 腰部特征词 / 长尾词）。
   - 结合 CTR/CPC 指纹推断真实展示位，精准拯救被困在商品页/低效位的潜质好词（`TRAPPED_CORE_HERO`），抑制顶部饱和耗散词（`FAILED_AT_TOS`）。
8. **停运实体绝对隔离与鲜活断流防误杀 (Paused Guardrail & Traffic-Floor Protection)**：
   - 严禁向处于 `paused` 状态或所属活动已暂停的实体提出调低出价或重复暂停建议；
   - 调低基础竞价必须穿透校验近 3 天/7 天鲜活表现（`imp_3d`, `spend_3d`），对调后已零曝光止血的词坚决保持观察期，严禁二次打压导致彻底丧失权重；增设 $0.20 竞价保底防线。

---

## 安装

### 方式 1：安装至 Antigravity Skills 目录（推荐）

```bash
# Linux / macOS:
git clone https://github.com/filmusife/lingxing-ads-playbook.git ~/.gemini/config/skills/lingxing-ads-playbook

# Windows PowerShell:
git clone https://github.com/filmusife/lingxing-ads-playbook.git "$env:USERPROFILE\.gemini\config\skills\lingxing-ads-playbook"
```

### 方式 2：安装至 Codex Skills 目录

```bash
# Linux / macOS:
git clone https://github.com/filmusife/lingxing-ads-playbook.git ~/.codex/skills/lingxing-ads-playbook

# Windows PowerShell:
git clone https://github.com/filmusife/lingxing-ads-playbook.git "$env:USERPROFILE\.codex\skills\lingxing-ads-playbook"
```

---

## 领星 MCP 配置

配置支持三种方式（按优先级自动识别，**绝不要把真实 Key 提交到 Git**）：

1. **环境变量配置（跨平台通用）**：
   ```bash
   export LINGXING_MCP_KEY="<YOUR_KEY>"
   # 可选自定义 URL: export LINGXING_MCP_URL="https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"
   ```

2. **Antigravity 全局配置 (`~/.gemini/config/mcp_config.json`)**：
   ```json
   {
     "mcpServers": {
       "lingxing-mcp": {
         "url": "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp",
         "headers": {
           "X-Mcp-Key": "<YOUR_KEY>"
         }
       }
     }
   }
   ```

3. **Codex 全局配置 (`~/.codex/config.toml`)**：
   ```toml
   [mcp_servers.lingxing-mcp]
   enabled = true
   url = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"

   [mcp_servers.lingxing-mcp.http_headers]
   X-Mcp-Key = "<YOUR_KEY>"
   ```

---

## 使用方法

提供 ASIN 与国家/站点后，在对话窗口中触发本 Skill（输入 `/lingxing-ads` 或 `/广告调整`）。系统默认只输出调优建议手册与批量导入表，绝不擅自写回广告后台。

### 脚本取数与证据生成

在工作目录中执行：

```bash
# 1. 从领星拉取多切片活数据与 Listing/财务档案
python scripts/fetch_ads_bundle.py --asin <ASIN> --country <US|UK|DE> --outdir work/lx-ads

# 2. 深度分析与证据生成（自动维护本地 db/ 数据库与账本，输出批量调整表）
python scripts/analyze_ads_bundle.py --indir work/lx-ads --outdir work/lx-ads/analysis --export-dir outputs
```

### 常用可选参数
- `--shop <SHOP_ALIAS>`：多店铺时指定店铺别名
- `--portfolio <PORTFOLIO_ID_OR_NAME>`：筛选指定广告组合
- `--target-acos <FLOAT>`：手动指定目标 ACOS 阈值（默认自动基于保本 ACOS 的 68% 科学锚定）
- `--rules <PATH>`：自定义精准产品特征分桶规则（参考 `references/product-rules-template.json`）
- `--known-negatives <PATH_OR_LIST>`：指定已否定词列表或文件路径，防止重复推荐已知否定词

---

## 目录结构

```
lingxing-ads-playbook/
├── SKILL.md                          # 24 条一线操盘核心铁律与端到端标准化流程
├── README.md                         # 仓库说明与部署使用指南
├── agents/
│   └── openai.yaml                   # MCP 工具依赖与代理声明
├── references/
│   ├── analysis-framework.md         # 深度分析框架（多切片差分、熔断逻辑、归因模型、重生SOP）
│   ├── mcp-connection.md             # 领星 MCP 连接与鉴权最佳实践
│   ├── product-rules-template.json   # 全品类通用自定义分桶规则模版
│   ├── report-template.md            # 专业交付手册标准化模版（含第2节历史对账总表）
│   └── tool-params.md                # 领星 MCP 各接口参数踩坑与避错指南
└── scripts/
    ├── fetch_ads_bundle.py           # 多窗口切片取数与元数据档案生成
    ├── analyze_ads_bundle.py         # 智能分析引擎（差分、熔断、ABA、溢价、持久化账本）
    └── lx_client.py                  # 健壮的轻量级 JSON-RPC 客户端（无原生 MCP 工具时自动回退）
```

---

## 许可证

MIT License
