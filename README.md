# lingxing-ads-playbook

亚马逊广告全链路智能诊断与实操调优 Skill：深度融合 **领星 OpenMCP (`LingXing-MCP`)** 多时间切片活数据（30天宏观基线 + 近7天中期走向 + 近3天最新微观切片、真实三广告位报表、订单级财务毛利、全域业务报告流量与后台操作审计日志）与 **西柚洞察 (`xydc-mcp`)** 六维全域搜索情报（自然位 vs 广告位排名、本品与竞品 7d 自然/广告流量护城河、父体四渠道占比、14d 收录词数趋势、大小类 BSR 走势与每日秒杀/价格轨迹），驱动 **北极星总览预期闭环对账、双窗口动态差分纠偏、跨期实体全量记忆与 AI 动态熔断体系**，输出完整 **13 节（`## 0` ~ `## 12`）专业操盘手册** 与 **零冲突可直接上传的批量操作表**。

本仓库仅包含 Skill 源代码、诊断框架与模版规范，**绝不包含**：
- 任何真实的领星 MCP Key / 西柚洞察 Token / Open API Secret / 认证凭据
- 任何特定店铺的私有数据、真实销售额、真实采购成本或历史快照
- 任何本地运行产物（`work/`、`db/`、`outputs/` 均已被 `.gitignore` 严格忽略）

---

## 核心特性

1. **北极星总览预期闭环与外部干扰剥离 (`macro_scorecard.tsv`)**：
   - 自动按日追踪全店真实美元销售额、总单量、自然单量与占比、真实全店 TACOS、访客 Sessions、购物车率与小类 BSR，逐期核验是否达成上期持久化预期 (`db/expectations/`)；
   - 前置剥离断货、低库存、购物车丢失、日常涨价、秒杀结束降温与 Listing 文案改动 6 类非广告干扰；一旦监测到砍广告引发大盘单量萎缩，立即激活 `VELOCITY_SHRINKAGE_ALERT` 缩量防线。
2. **西柚洞察 (`xydc-mcp`) 六接口全栈深度协同**：
   - 接入 `get_asin_keywords`、`get_asin_traffic`、`get_parent_asin_ad_overview`、`get_asin_keyword_count_trends`、`get_asin_bsr_trends`、`get_asin_info_trends`；
   - 构建五阶自然排名 vs 广告排名双轨协同矩阵 (`rank_synergy.tsv`，精确核算 `0~100%` 本品自然/广告流量占比与跨期 `ΔOR`/`ΔSP`)、对标竞品 7d 自然流量护城河表 (`xydc_competitor_benchmark.tsv`) 以及 **Tier 5 自然流量盲区高潜拓词表 (`xydc_blindspot_opportunities.tsv`)**。
3. **多切片双轨保鲜与真实三广告位 + 竞价策略穿透**：
   - 同步拉取截至昨天（`T-1`）的 30d / 7d / 3d 鲜活切片及 `get_sp_campaign_placement_report` 真实 `TOS / PP / ROS` 三展位表现 (`real_placement_performance.tsv`)，保护高投产真实商品页/其余位，精准识别 `动态竞价 - 提高和降低` 的 `2.0x` 顶展叠加失血。
4. **跨会话实体全量记忆与防滑坡/防左右横跳 (`db/ledger/` & `db/entity_memory/`)**：
   - 永久追踪每个活动、关键词与 ASIN 投放的前次调整动作、调后 `ΔCPC%` / `ΔImp%` 以及跨期自然位/广告位进退 (`entity_full_context.tsv`)；核心出单词自然位下滑 `≥ 4` 名时自动触发 `Rank_Slide_Protection_Hold` 严禁顺周期叠砍竞价。
5. **两阶段移词闭环与零冲突批量表仲裁 (`resolve_entity_action_conflicts`)**：
   - 完整覆盖第 1 阶段倒推竞价软交接与第 2 阶段源仓精准否定隔离，代码底层对同一 `(campaign, target_entity)` 严格执行优先级唯一胜出仲裁，确保 `bulk_adjustments.tsv` 零重复冲突、严格 7 列纯绝对美元数值。
6. **完整 13 节 (`## 0` ~ `## 12`) 高信息密度手册交付**：
   - 严格保持 `## 0` 产品对标与财务北极星底线 → `## 1` 多窗口大盘与西柚全域双轨协同 → `## 2` 历史调控台账对账 → `## 3` 执行时序 → `## 4` 位置溢价一套数总表 → `## 5` 全活动分仓逐词四行卡片 → `## 6` 目标架构 → `## 7` 否定词全生命周期 → `## 8` 因果逻辑 → `## 9` Listing 协同 → `## 10` 熔断与 7 天量化验收 → `## 11` 今日可勾选清单 → `## 12` 配套批量表索引。

---

## 安装与配置

### 1. 安装路径
放置于 `~/.gemini/config/skills/lingxing-ads-playbook`（Windows 为 `%USERPROFILE%\.gemini\config\skills\lingxing-ads-playbook`）。

### 2. MCP 配置 (`~/.gemini/config/mcp_config.json`)
同时配置 `lingxing-mcp`（`headers.X-Mcp-Key`）与 `xydc-mcp`（详见 `references/mcp-connection.md`）。

---

## 使用方法

提供 ASIN 与国家/站点后触发 `/lingxing-ads` 或 `/广告调整`，或运行一体化广告组合调度脚本：

```bash
# 一键完成组合工作区建档、领星+西柚双引擎取数、全量分析与跨期对账：
python ~/.gemini/config/skills/lingxing-ads-playbook/scripts/run_portfolio_audit.py \
    --portfolio <广告组合名> --asin <ASIN> --country <US|UK|DE> --shop <SHOP_ALIAS>
```

---

## 目录结构

```
lingxing-ads-playbook/
├── SKILL.md                          # 70 条一线操盘核心铁律与端到端标准化流程
├── README.md                         # 仓库说明与部署使用指南
├── agents/
│   └── openai.yaml                   # LingXing-MCP + xydc-mcp 双引擎依赖声明
├── references/
│   ├── analysis-framework.md         # 37 节深度分析框架（北极星对账、双轨排名、真实三展位、熔断与重生SOP）
│   ├── mcp-connection.md             # 领星 MCP 与西柚洞察 MCP 连接与鉴权规范
│   ├── product-rules-template.json   # 全品类通用自定义分桶规则模版
│   ├── report-template.md            # 13 节（## 0 ~ ## 12）全量深度交付手册标准模版
│   └── tool-params.md                # 领星 MCP 与西柚洞察 6 大接口参数与避坑指南
└── scripts/
    ├── run_portfolio_audit.py        # 广告组合专属工作区一键调度入口
    ├── fetch_ads_bundle.py           # 领星多窗口切片 + 真实展位 + 西柚6接口全量取数器
    ├── analyze_ads_bundle.py         # 智能分析引擎（北极星对账、双轨五阶协同、跨期记忆、零冲突TSV仲裁）
    ├── lx_client.py                  # 领星 OpenMCP JSON-RPC 客户端
    └── xydc_client.py                # 西柚洞察 (xydc-mcp) 6 接口客户端
```

---

## 许可证

MIT License
