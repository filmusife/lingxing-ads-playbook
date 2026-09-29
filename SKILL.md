---
name: lingxing-ads-playbook
description: 用领星 MCP 按 ASIN+站点拉取最近30天真实广告与 Listing 数据，按产品匹配做深度 SP/SBV 分析，并输出可执行的完整广告调整手册。在用户给 ASIN 和国家/站点、提到领星广告、广告组合、SP 广告优化、搜索词否定、基础竞价、位置溢价、或输入 /lingxing-ads、/广告调整 时使用。不要用于写 Listing 文案、品类选品、或没有活数据的空泛广告理论。
metadata:
  short-description: Deep Lingxing ads audit and full adjustment playbook
---

# 领星广告调整手册

给 ASIN + 国家后，必须用领星活数据做**产品匹配优先**的广告诊断，输出一份别人能直接照着改的手册。禁止只看组合/活动 ACOS 就否定或加码。

**目的不是把 ACOS 做低，而是提升 Listing 搜索权重和自然流量。** ACOS 只是约束。不要把机制写成“A9”或“亚马逊最新官方公式”；用可观察的 query 转化、相关流量、小类 BSR 来解释。

## 何时用

- 用户给了 ASIN，并给了国家/站点（美国站、US、UK、DE…）或店铺名
- 要看广告组合、广告活动、投放词、搜索词、自动/商品投放、位置溢价、基础竞价
- 要的是调整建议，不是“ACOS 高就否”

不要用来写标题五点（转 `amazon-listing-agent` / `amazon-listing-penalty-audit`），也不要在领星连不上时编造报表数字。

## 铁律

1. **先产品，后 ACOS。** 从 Listing 属性判断本品 IS / IS NOT，再决定加码、保留、降价、否定。错品词即使出过单也优先否定。
2. **看全层数据。** 至少覆盖：店铺/Profile、Listing、FBA 库存、活动（含 TOS/PP/ROS、预算、竞价策略）、广告组默认基础竞价、关键词（匹配方式 + 基础竞价 + CPC）、搜索词、自动/ASIN 投放、广告商品。缺哪层就标 `DATA_MISSING`，不要用活动汇总代替。
3. **先改位置溢价，再改基础竞价。** 低基础竞价 + 超高 TOS 会让 CPC 远高于基础竞价。同一天不要既砍词又大幅加薪。
4. **长短双轨制与鲜活切片数据（宏观 30 天定词性与漏斗，微观 7 天/3 天定调价与动态纠偏，拒绝拿历史平均当现状）。**
   - **数据时效与鲜活切片**：必须拉取截至昨天的最新鲜活切片，包含 30 天全量基线（`macro_30d`）、近 7 天走势（`recent_7d`）与近 3 天调后切片（`recent_3d`）。在报告开头和每个活动明确展示数据时间戳与分段切片，严禁提供来源不明或模糊过时的平均数。
   - **分工边界**：
     - **30 天宏观基线**：用于意图分桶（IS / IS NOT）、N-Gram 词根累积损耗（高置信度词组否定）、移词漏斗穿透审计；
     - **近 7 天 / 近 3 天微观切片**：用于衡量当前实际有效 CPC、最新转化率与曝光流速。
   - **动态差分纠偏（Delta Metrics）**：
     - 给出竞价与溢价建议时，必须对比 $\Delta \text{CPC}$ 与 $\Delta \text{Imp}$，依据调整后的最新变化轨迹纠偏，绝不拿被稀释的历史 30 天均值覆盖当前现状！
     - 若前期调价后近 3 天 CPC 已显著下降（$\Delta \text{CPC} \le -15\%$）且曝光稳定，判定为**【调后生效·见效保持】**，严禁再次砍价，保持稳态；
     - 若调价后近 3 天日均曝光断崖跌 >70%，判定为**【调后休克断流】**，立即触发熔断并回踩探底（+10%~+15%）。
5. **位置溢价只允许一套数。** 先写总表，后面每个活动的 TOS/PP/ROS 必须与总表完全相同。写完后逐行对账，禁止总表降、详情里又留高溢价。
6. **对外一律写「基础竞价」。** 不要对用户说 BID/bid。广告组层写「默认基础竞价」，关键词层写「基础竞价」。API 字段仍是 `bid`，只留在脚本内部。
7. **默认只出建议，不写回广告。** 领星有 `put_campaigns_sp`、`advertising_add_negative_keywords` 等写接口；只有用户明确说“帮我改后台/提交否定词”才调用写工具。
8. **永远不要打印或回显** `X-Mcp-Key`、Open API secret、config 里的 token。
9. 用户用中文就出中文手册。
10. **AI 动态弹性观察期与异常熔断纠偏体系（告别死等 7 天，拒绝天天乱调，AI 判别理想出入与即时熔断纠偏）。** 
    - **打破死板硬性时间锁**：死守 7 天在出现严重失血（预算秒空、CPC 暴涨）或断流休克（曝光归零、自然排位断崖）时会导致毁灭性损失。
    - **基准观察期（Baseline Window）**：默认保持 3-7 天观察期，防人工手痒、等 24-48h 归因滞后并给算法留出稳态重敛时间。
    - **AI 动态理想出入判别模型（AI Discrepancy & Trajectory Evaluation）**：AI 模型每次复盘自动深度比对「调整后实际运行轨迹 vs 预期理想基准」：
      - **失血暴冲熔断 (`CIRCUIT_BREAKER_BLEED`)**：花费超标、实际 CPC 异常暴涨超预期 >40% 或单日花费击穿保本且 0 单，AI 立即穿透观察期，执行紧急限价/下调溢价止血；
      - **休克断流熔断 (`CIRCUIT_BREAKER_SHOCK`)**：曝光断崖跌 >70%-80%、点击归零，导致销售速度（Sales Velocity）与自然排名濒临休克，AI 立即穿透锁定，于第 2-3 天执行「梯级微调回踩探底（Step-up Bid Probe +10%~15%）」，拒绝死等 7 天导致权重猝死；
      - **毒性错品泄漏熔断 (`CIRCUIT_BREAKER_TOXIC`)**：出现确凿物理冲突词或出血词根，执行**否定零冷静期（Zero-Cooldown Rule）**，见毒即清；
      - **正常公差稳态 (`NORMAL_OBSERVATION`)**：各项指标在正常统计波动范围内（小样本波动、轻微出单起伏），保持观察锁定，静待算法稳态。
11. **强相关功能词保护（禁止盲目否定核心词）。** 必须深入理解 Listing 功能点与卖点。强相关词数据差通常是因为展示在商品页/其余位未打中转化点，或是宽泛匹配被稀释。严禁将强相关核心功能词一刀切否定（会打断自然权重），只做基础竞价调控、独立精准仓可控测试或 Listing 页面承接优化。
12. **竞品功能与竞争力对标（降竞价防守 vs 暂停否定）。** 投放竞品 ASIN 或打竞品打不过时，先对标双方功能点、售价与评价数壁垒。遇断层评价优势或知名大牌压制，果断暂停/否定；若价格接近且我品具备功能点平替优势，严禁盲目否定，应降低 25-35% 基础竞价或削减商品页溢价，转为低成本防守/捡漏收割。
13. **接入 ABA 搜索量与排名梯队体系。** 自动同步亚马逊官方搜索频率排名（`searchrank`），分为 Top 5K 超大词、Top 30K 头部大词、Top 150K 腰部特征词与长尾词。头部大词竞争白热化、TOS 成本高，表现差多因位置被挤压到底部，严禁一刀切全盘否定，结合 TOS 占有率制定独立精准或低价防守策略。
14. **已有否定词跨活动穿透审计与移词漏斗保护（杜绝孤立断言，严防破坏移词架构或全账户断流）。** 每次分析必须提取全量已生效否定词，并穿透整个广告组合（Portfolio）核验每个词在其他活动的投放状态：
    - **正常移词防内耗 (`PROMOTED_MIGRATED_PROTECTED`)**：若搜索词在广泛仓已否定，但在精准仓/专用活动已正常开启且出单，属于成熟移词漏斗的标准防内耗操作，**严禁在广泛仓建议解否（否则会引发内耗、拉高 CPC）**，保持广泛否定！
    - **移词后断流误杀 (`MIGRATED_BUT_PAUSED_IN_EXACT`)**：若搜索词已移至精准仓，但在精准仓处于 `paused` 暂停状态，同时在广泛仓被否定，导致该词在全账户彻底断流。**绝对不可在广泛仓解否，而是在目标精准仓生成 `Unpause_Keyword`（依原成交 CPC 与目标 TOS 倒推基础竞价）重新唤醒流量**！
    - **全账户未投放孤儿词 (`TRUE_ORPHAN_NEGATIVE`)**：若转化核心词在广泛仓被否，且全账户没有任何活动投放，输出 `Harvest_To_Exact` 移入精准活动收割！
    - **物理硬伤与竞品排除 (`SAFE_HARD_NEGATIVE`)**：物理冲突与竞品品牌保持永久否定；若在投放词中误混入，输出 `Pause_Keyword` 紧急暂停。
    - **新建议 100% 去重**：已生效的否定词绝不再重复建议，杜绝无效重复推荐。
15. **报告与调整表命名必须追加生成时间戳（禁止覆盖历史文件，便于多期对比）。** 每次生成的调整手册与批量调整表文件名末尾，必须追加精确的生成时间戳（统一格式：`YYYYMMDD-HHmmss`，如 `outputs/{ASIN}-{country}-ads-playbook-20260926-104901.md` 与 `outputs/{ASIN}-{country}-bulk-adjustments-20260926-104901.tsv`）。严禁直接覆盖历史源文件，确保运营可以依据时间戳对比历史各期建议，回溯调整前后转化率与 ACOS 走势。
16. **跨活动移词与断流复活基础竞价动态测算（严禁拍脑袋出价，杜绝二次暂停）。** 移词或复活关键词时，严禁使用千篇一律的固定值或直接照搬原出价。必须参考原活动成交 CPC（Anchor Converting CPC）与目标活动位置溢价（TOS/PP/ROS），使用倒推公式 $\text{建议基础竞价} = \text{round}\left(\frac{CPC_{\text{历史出单}}}{1 + \frac{TOS_{\text{目标}}}{100}}, 2\right)$ 设定，保证加成后顶部实际竞价精准咬合历史出单成本，彻底规避因出价过高叠加 TOS 后导致 CPC 暴涨（$1.10~$1.30）而被二次误暂停的恶性循环。
17. **多维广告位指纹诊断与动态提权决策体系（打破死板程序规则，拒绝孤立盲目否定）。** 搜索词报表没有直接位置标签，必须综合曝光量、点击量、点击率（CTR 指纹：TOS ≥1.2%~3%+ vs 商品页 ≤0.40%）、扣费 CPC 与基础竞价及非 TOS 最高出价上限 $MaxBid_{\text{non-tos}} = \text{基础竞价} \times (1 + \max(PP\%, ROS\%)/100)$ 的数学关系、以及活动宏观广告位出单比，反推其真实出单与耗损位置，执行动态决策：
    - **困在商品页/低效位的潜质核心词 (`TRAPPED_CORE_HERO`)**：若核心主粮词/功能词曝光量高但 CTR 偏低（≤0.60%）、CPC 贴近基础竞价，累积点击但未出单，绝不代表词不好，而是流量被困在转化极低的商品详情页或页面底部！**绝对禁止盲目一刀切否定（会斩断自然搜索根基）**；应执行**动态提权拯救 (`Elevate_To_TOS`)**：提高基础竞价 15%-20% 或提高活动 TOS% 至 60%-80% 将流量抽调到首屏顶部，或移入独立精准仓专打 TOS 独享预算测试真实转化！
    - **首页顶部黄金位已饱和耗散词 (`FAILED_AT_TOS`)**：若 CTR 极高（≥1.5%）且实际 CPC 达到或超过顶部溢价天花板，说明已获得最顶级首屏黄金流量但仍未转化，绝不可继续盲目加价或推高 TOS；若为边缘弱相关词果断精准否定（`Negative_Exact`）止血；若为核心主粮词则执行**防守降价 (`Throttle_TOS_Bid`)**，下调基础竞价 25%-30% 退守防守位，同时重点排查 Listing 首屏视觉、主图痛点与价格对比竞争力。
    - **结构畸形受抑词 (`HIGH_TOS_ASPHYXIATION`)**：极低基础竞价 + 虚高 TOS（≥150%）导致其余位置零曝光，执行**结构拉平重塑**（下调 TOS 至 50%-70%，基础竞价回升至 $0.45-$0.55）。
18. **全域业务闭环与财务保本 ACOS 约束 (Financial Unit Economics & Break-Even Anchor)。** 拒绝只看孤立广告表象。自动调用 `query_order_profit_list` 调取真实财务数据（单均采购成本 COGS、头程运费 Freight、FBA 配送费、佣金、实现毛利与毛利率），精准核算真实保本 ACOS：$\text{Break-Even ACOS} = \frac{\text{客单价} - \text{单均COGS} - \text{单均头程} - \text{单均FBA} - \text{单均佣金}}{\text{客单价}} \times 100\%$。若用户未显式指定目标 ACOS，系统自动将目标 ACOS 科学锚定在保本 ACOS 的 65%~70%，确保每一分广告花费都有健康的正向毛利沉淀。
19. **Listing 全域流量大盘与自然承接力对比 (Listing Funnel & Organic vs Ad CVR)。** 自动调用 `query_product_performance_asin_lists` 获取 Listing 宏观全域访客（总 Sessions、Pageviews、移动端占比）。深度比对**广告转化率 (Ad CVR) vs 自然转化率 (Nature CVR)**，计算广告销量占比 (Adv Sales Share) 与全店 TACOS。若广告转化率数倍于自然转化率，说明自然位承接力脱节或重度依赖广告推力，应重点打磨自然搜索主粮词权重，严禁盲目粗暴砍广告导致自然流飞轮失速。
20. **24 小时与周度分时转化决策 (24-Hour & Weekly Dayparting Intelligence)。** 自动调用 `advertising_order_analysis_list` 获取 24 小时与星期一至日的真实花费、订单、CVR、ACOS 与 CPC 曲线。系统自动识别**黄金出单峰值窗口 (`PEAK_GOLDEN_HOUR`)**（如 CVR ≥15%、ACOS ≤18%）与**低效流血窗口 (`BLEEDING_OFFPEAK_HOUR`)**（如 CVR ≤11%、ACOS ≥24%）。在调整手册与批量导入表中直接生成分时预算与竞价调度动作（联动领星广告定时调价规则，高峰时段 +15%~+25% 溢价抢占首屏，低谷时段 -20%~-30% 降价避损）。
21. **死活动判定标准与净水器式重生协议 (Dead Campaign Rebirth Protocol)。** 
    - **算法本质认知**：亚马逊底层质量得分（Ad Quality Score）对活动具有长期记忆性。若活动长期跑偏积压巨量低劣点击，系统会将活动打入冷宫；新建活动则享有历史负向清零与 3-7 天冷启动探索红利（Cold Start Exploration）。
    - **触发「弃老开新」的三大死活动指纹**：
      1. **展示死锁**：经过调价与回踩探底后，有效竞价已高于市场建议上限，但日曝光依然严重锁死（算法惩罚冷宫）；
      2. **负向沉淀饱和**：历史累计数百美金、数千点击且 CVR < 3%，当期微调预算已无法稀释历史负分；
      3. **架构畸形不可逆**：混杂几十个词的大杂烩活动，词流彻底失控。
    - **净水器式重生 SOP（严禁无脑照搬重开）**：
      1. 彻底归档老活动（Archive Old Campaign），严禁新老双开内耗；
      2. 基因清洗与预置免疫防火墙（仅提取验证能出单的 IS 核心词，开启前直接将历史出血词根预置为词组否定）；
      3. 架构进化为单组 1-3 词独立收割仓（`*-exact-prove`）；
      4. 动态倒推科学起步竞价（Base Bid = 历史成交 CPC / (1 + TOS%)）；
      5. 冷启动前 3-5 天紧密跟踪首屏 TOS 占有率与转化，防止再次跑偏。
22. **本地持久化数据库台账与跨会话自动效果归因体系 (Persistent Local DB Ledger & Cross-Session Effect Attribution)。**
    - **彻底告别聊天窗口记忆依赖**：大模型对话窗口是瞬态终端，不可作为业务记忆数据库。系统在工作区根目录下统一维护 `db/` 本地数据库（包含 `db/daily_records/` 每日档案、`db/ledger/` 历史调优台账、`db/snapshots/` 状态快照）。
    - **每日自动归档截面**：分析脚本运行后，自动在 `db/daily_records/{KEY}_{YYYYMMDD}.json` 中存入当日全域数据截面（包含店铺、ASIN、大盘流量、各活动当日与切片指标）。
    - **持久化调控台账**：系统在 `db/ledger/{KEY}_ledger.json` 与 `.tsv` 中永久追加记录每一笔调整事件（活动名、调整对象、改前值 $\to$ 改后值、发生时的基线 CPC/曝光/ACOS）。
    - **跨会话自动归因对账**：在新开任何会话窗口体检时，脚本自动读取该台账，并将调前基线与今日最新 3 天/7 天切片指标进行对账，自动算出 $\Delta \text{CPC}\%$、$\Delta \text{Imp}\%$ 与调后状态（`【调后生效·见效保持】` / `【断流休克熔断】` / `【失血暴冲熔断】` / `【动态观察期】`）。
    - **报告强制第 2 节对账**：交付的体检手册第 2 节必须完整展示《历史调控台账与调后效果复盘对账总表》，彻底打通跨窗口的历史记忆断层！

## 必做流程

连接与取数细节读 [references/mcp-connection.md](references/mcp-connection.md) 和 [references/tool-params.md](references/tool-params.md)。判定标准读 [references/analysis-framework.md](references/analysis-framework.md)。成稿结构用 [references/report-template.md](references/report-template.md)。

### 1. 接通领星

- 配置支持三种方式（按优先级自动识别）：
  1. 环境变量：`LINGXING_MCP_KEY`（以及可选的 `LINGXING_MCP_URL`）
  2. Antigravity 全局配置：`~/.gemini/config/mcp_config.json`（在 `mcpServers.lingxing-mcp` 中配置 `headers.X-Mcp-Key`）
  3. Codex 配置：`~/.codex/config.toml` 的 `[mcp_servers.lingxing-mcp.http_headers]`
- 本会话若**没有**原生 MCP tools，不要停：用 `scripts/lx_client.py` 走 HTTP JSON-RPC（`help` / `search` / `action`）。
- 连不上就告诉用户怎么贴 MCP 配置（白名单以后再说），不要改用过期截图或仓库旧数冒充当前表现。

### 2. 取数与证据生成

在当前工作目录跑（把 ASIN/国家换成用户给的）。脚本在 skill 根目录：

```bash
# Windows PowerShell 或 Bash:
python ~/.gemini/config/skills/lingxing-ads-playbook/scripts/fetch_ads_bundle.py --asin <ASIN> --country <COUNTRY> --outdir work/lx-ads
python ~/.gemini/config/skills/lingxing-ads-playbook/scripts/analyze_ads_bundle.py --indir work/lx-ads --outdir work/lx-ads/analysis --export-dir outputs
```

- **全品类通用性**：脚本默认从后台 Listing 标题、品牌词、属性、小类 BSR 中自动构建画像分桶，无缝兼容任何商品、任何类目（3C、小家电、户外、服装、美妆等）。
- **自定义规则（可选）**：若需对特定商品自定义精准 IS/IS_NOT/竞品词，可传入 `--rules path/to/product_rules.json`（参考 `references/product-rules-template.json`）。
- **自定义目标 ACOS（可选）**：可通过 `--target-acos 25.0` 指定目标 ACOS；若未传入，自动由真实财务保本 ACOS 的 68% 智能推导。
- **店铺与窗口**：用户指定了店铺名就加 `--shop <SHOP_ALIAS>`。默认分析窗口是昨天往前 **30 天**。
- `analyze_ads_bundle.py` 会产出：
  - `campaigns.tsv` & `groups.tsv`：全层级明细与预算健康度
  - `keywords.tsv` & `search_terms.tsv`：**包含全美 ABA 搜索排名（searchrank）与搜索量级梯队（MEGA_HEAD / CORE_HEAD / MID_TAIL / LONG_TAIL）**
  - `targets.tsv`：**包含竞品 ASIN 标题、竞品价格、评分星级、评价数对比与竞争力裁决（Harvest / Lower_Bid / Negative_Target）**
  - `financial_profile.json`：**真实财务毛利与保本 ACOS 核算画像（单均 COGS、头程、FBA、佣金、保本线）**
  - `traffic_profile.json`：**Listing 全域宏观流量漏斗（总 Sessions、Pageviews、移动端占比、广告 CVR vs 自然 CVR、TACOS）**
  - `dayparting_analysis.tsv`：**24 小时分时转化与出单指纹明细（黄金高峰与流血低谷识别及调度建议）**
  - `historical_ledger.tsv` & `db/ledger/`：**跨会话调控历史台账与调后效果复盘（追踪改动日期、改前基线、调后现状、ΔCPC%、ΔImp%、效果裁决与指令）**
  - `buckets.tsv`：意图分桶花费与转化走势
  - `ngrams.tsv` & `bleeding_ngrams.txt`：**1-gram/2-gram 词根级高损耗分析（否定词组来源）**
  - `cannibalization.tsv`：**跨活动流量内耗与竞蚀检测（自动/广泛踩压精准）**
  - `placement_audit.tsv`：**TOS/PP/ROS 位置溢价健康度审计（含动态观察期状态、失血/休克熔断标记与死活动诊断）**
  - `observation_status.tsv`：**自变量变动差分与 AI 动态观察期状态（跟踪改动天数、正常公差稳态 NORMAL vs 熔断纠偏 CIRCUIT_BREAKER）**
  - `active_negatives_audit.tsv` & `unnegative_recommendations.tsv`：**全账户移词漏斗与已生效否定词体检（区分正常移词防内耗、移词后断流误杀、全账户孤儿词与物理冲突）**
  - `search_term_placement_diagnosis.tsv`：**多维广告位指纹诊断总表（含 CTR/CPC 指纹、推断广告位、策略标签与提权/降价/否定决策）**
  - `bulk_adjustments.tsv`：**可直接落地批量操作清单（含广告位提权拯救、顶部降竞价防守、移词断流精准重启、竞品对标降竞价/否定指令、分时调价调度，支持熔断紧急纠偏指令，过滤正常公差期活动）**
- **最终手册必须按模板手写完整建议**，不要把 tsv 当最终交付物。第 2 节必须完整输出《历史调控台账与调后效果复盘对账总表》。

### 3. 建产品画像

从 Listing + 小类 BSR + 属性 + 标题，写清：

- 本品是什么、卖点、价格带、评分/评分数、小类节点
- 本品**不是**什么（竞品形态、没有的功能、错误品类）
- 库存能否加码（可售天数、库龄、日均销量走势）
- 广告单量 vs 总销量：广告占比过高时，自然流量靠 **相关 query 的转化**，不是靠乱扩词

用户若提供属性表，以属性表和后台 Listing 为准，不要用广告词反推功能。

### 4. 诊断与深度分析

对每个启用活动同时看：
- 类型（SP/SBV/SD、手动/自动）、匹配、默认基础竞价、关键词基础竞价、TOS/PP/ROS、CPC、CVR、CTR、直接/间接订单、TOS 份额、搜索词泄漏。
- 意图分桶：核心相关、品牌、功能卖点、相邻能转（不扩）、错品、竞品品牌词、ASIN 对比。
- **词根出血点（N-Gram）**：查看 `bleeding_ngrams.txt`，发现单词消耗低但累积花费巨大、0 单的隐性无效词根，给出精准词组否定建议。
- **内部竞蚀（Cannibalization）**：查看 `cannibalization.tsv`，检查是否有广泛/自动活动在更高 CPC 下分流精准活动，建立词流隔离。
- **位置溢价失衡**：查看 `placement_audit.tsv`，排查虚高 TOS（>=150%）导致 CPC 暴涨或 ROS 被饥饿抑制的问题。

### 5. 出完整手册

聊天里给：目标、今天止血清单、明天基础竞价、核心否定词（含精准否定与词根词组否定）、3/7 天验收。

完整手册写到 `outputs/{ASIN}-{country}-ads-playbook-{YYYYMMDD-HHmmss}.md`（同时配套输出 `outputs/{ASIN}-{country}-bulk-adjustments-{YYYYMMDD-HHmmss}.tsv`）。必须按 [report-template.md](references/report-template.md) 写全，严禁覆盖历史旧文件，确保多期可比。
配套提供可直接用于系统批量上传的 `bulk_adjustments.tsv` 清单。

写位置溢价时：先定总表，再把同一行数字复制进该活动详情。不要在详情里“顺便改一版”。

## 交付标准

合格手册必须让运营不用再问就能改，至少包括：

- 产品画像与权重目标（小类节点 + 自然流量，而不是“把 ACOS 做低”）
- 组合 **30 天**盘点 + 搜索词意图拆分
- 位置溢价总表（现在 → 改成 → 原因），且与后文每个活动数字一致（标明正常观察期锁定、触发熔断纠偏或死活动判定）
- 每个活动：保留/暂停/降预算、默认基础竞价、关键词基础竞价、否定、迁词、紧急熔断干预
- 隐性出血词根（N-Gram）与精准/词组否定清单（30 天样本，确凿 IS NOT 错品零冷静期即刻执行）
- 内部流量竞蚀整改方案（自动/广泛精准否定收割赢家）
- ASIN 投放去留（按价格/评分/评分数判断能不能赢对比页）
- 操作顺序（第 0 天止血，第 1 天调基础竞价，第 2-3 天熔断扫描与探底回踩，第 7 天稳态验收或死活动归档重生）
- 死活动判定与净水器式重生方案（对确认打入算法冷宫的死活动，输出归档老活动 + 预置免疫词组否定 + 倒推竞价的新建 SOP）
- 为什么这样改（相关搜索转化和自然流量，不是 ACOS 故事，也不要写成官方算法名）
- 配套交付带时间戳的 `outputs/{ASIN}-{country}-ads-playbook-{YYYYMMDD-HHmmss}.md` 与 `bulk-adjustments-{YYYYMMDD-HHmmss}.tsv` 调整总表及历史版本

缺搜索词或关键词层时，明确写 `DATA_MISSING`，并降低建议强度。
