---
name: lingxing-ads-playbook
description: 用领星 MCP (LingXing-MCP) + 西柚洞察 MCP (xydc-mcp) 按 ASIN+站点拉取 30d/14d/7d/3d 广告、真实四展位、财务毛利、库存、自然/广告双轨排名与竞品流量活数据，先定产品阶段与经营目标，再做产品匹配优先的 SP/SB/SBV/SD 深度诊断，输出 13 节广告调整手册（含 0.0 阶段目标卡）与零冲突批量表。用户给 ASIN+国家/站点、提到领星广告、广告组合、SP 优化、搜索词否定、基础竞价、位置溢价、分时、或输入 /lingxing-ads、/广告调整 时使用。不用于写 Listing 文案、选品，或没有活数据的空泛广告理论。
metadata:
  short-description: Stage-aware Lingxing + Xiyou Amazon ads audit and adjustment playbook (v2)
  version: 2.0
---

# 领星广告调整手册 v2（阶段驱动 · 利润约束 · 自然排名导向）

## 0. 使命与总原则

**广告是为商品经营服务的杠杆，不是独立 KPI。** 本技能的目标函数按优先级为：

1. **L1 经营结果**：广告后全店贡献利润（美元/天）与总销量（总单量）；
2. **L2 增长质量**：核心词自然排名、自然单占比、TACOS、小类 BSR；
3. **L3 广告效率**：ACOS、CPC、CVR、CTR、展位效率。

L3 只是约束，任何 L3 优化不得以牺牲 L1/L2 为代价。不要把机制写成“A9/A10/亚马逊官方公式”，只用可观察证据解释：搜索词转化、相关流量、自然排位进退、小类 BSR、TOS 展示份额。

五条总原则（所有模块都服从）：

- **先阶段后目标**：先判定产品所处阶段（§1），阶段决定目标 ACOS/TACOS 与允许动作，禁止对所有商品套同一个保本系数。
- **先产品后数据**：先判本品 `IS` / `IS NOT`，再看 ACOS；严禁只看组合/活动 ACOS 就否定或加码。
- **证据够了才动**：阈值按目标 CPA 与基准 CVR 缩放（§3），近 3 天转化数据受归因滞后影响，不单独作为“0 单”判据。
- **一次只拧一个旋钮**：同一活动同一周期只改一类杠杆，且有统一步长上限与全局变更预算（§5），保证调后效果可归因、可回滚。
- **只读默认、记忆闭环**：默认只出建议不写回；每条动作都带杠杆类型、多期路径（如有）、预期效果、验证日与回滚条件并入账，下期必须对账。

---

## 1. 阶段判定与经营目标（第一步必做）

### 1.1 阶段模式（Mode）

由 `scripts/v2_policy.py` 先根据活数据自动判别；用户通过 `--mode` 或 `rules.json` 显式指定时以显式模式为准（若财务处于零广告即亏 / 保本 `< 8%` 不可持续区，仍会标出警告并拦截盲目扩量）。

| 模式 | 自动触发条件（按自上而下优先级） | 经营目标 | 目标 ACOS（BE = 决策口径全口径保本，见 §2） | TACOS 护栏 |
|---|---|---|---|---|
| `STOP_LOSS` 止损 | `CM ≤ 0`（零广告即亏）或全口径保本 `< 8%` | 止血，优先修复价格/COGS/退货结构 | **广告不可持续区**：不硬压无意义 3% 地板，也不虚设 12% 目标；仅以 `8%` 作极简防守参考，保留品牌防守与已验证盈利精准词，冻结全部扩量 | `0% ~ min(8%, CM%)`，广告仅做最小防守 |
| `CLEARANCE` 清仓 | `rules.json` 声明 `clearance=true`，或 `> 180` 天库龄占比 `≥ 50%` 且 `DOS ≥ 180` 天 | 尽快去化库存、降低长期仓储/移除损失 | 放宽至 `≤ CM%`（可接受至保本），须满足单件清仓损失 `<` 继续持有/移除成本 | `≤ CM%` |
| `LAUNCH` 新品/冲排名 | 评论 `< 30`（或显式指定 `--mode LAUNCH`） | 核心词自然位进首页、积累评论与转化样本 | `≤ BE × 1.30`（可由 `launch_acos_multiplier` 覆盖），**必须设置投资周期（默认 6 周）与累计投资上限 `launch_budget_cap`** | `≤ CM% + 10pp`，超上限自动转 `GROWTH` |
| `DEFEND` 护盘 | 北极星缩量防线激活，或底盘为 `ORGANIC_FRAGILE` | 守住单量地板、稳住自然位 | 维持可持续参考线（`BE × 0.78 ~ 0.85` 结合当前 30d ACOS），不以压低 ACOS 为目标 | `≤ CM% × 0.85`（允许短期上浮，但不得击穿 L1 止损线） |
| `PROFIT` 成熟收割 | `ORGANIC_FLYWHEEL` 且头部词自然位 `≤ #10`（或无西柚数据时满足飞轮条件） | 守住自然位 + 最大化广告后贡献利润 | `clamp(边际保本 × 0.68, 12%, 35%)`，再受全口径 `BE × 0.85` 硬封顶 | `≤ CM% − target_net_margin`（默认净利率 10%） |
| `GROWTH` 成长 | 其余健康均衡态 | 扩量 + 抬升核心词自然位 | `BE × 0.85`（`growth_acos_coef` 默认 `0.85`，与 §2.1 `BE × 0.85` 护栏一致） | `≤ CM% − 5pp` |

**叠加态 `PEAK`（大促期，可与上表任一模式叠加）**：见 §7.9。脚本依据数据窗口锚定日期（`meta.json` 结束日）与 `deal_info.json` 自动识别黑五/网一（11 月中下旬至 12 月上旬）、Prime Day（7 月 / 10 月）及进行中秒杀，也可通过 `--peak` / `--no-peak` 或 `--mode GROWTH+PEAK` 显式开关。

### 1.2 本期目标卡（写入报告 `### 0.0` 与 `db/expectations/`）

每期必须明确并持久化到 `db/expectations/{KEY}_expectations.json` 与 `macro_scorecard.json`：阶段模式及理由、是否叠加 `PEAK` 及相位、目标 ACOS、目标 TACOS、目标获客成本 TCPA、日均销额地板、日均贡献利润目标、核心词作战表（§6.3）目标 OR、本期全局变更预算上限与占用率（§5.4）。下期首先对账这张卡。

---

## 2. 财务模型（双口径保本 · TCPA 缩放锚点）

### 2.1 单件贡献与双口径保本

```
单件贡献毛利 CM_unit = (adjusted_gross_profit + total_ad_cost) / 净销量
                     = 客单价 − COGS − 头程 − FBA 配送费 − 佣金 − 退货损耗分摊 − 仓储/销毁分摊 − 促销折让分摊
单件贡献毛利率 CM%   = true_all_in_break_even_tacos = (adjusted_gross_profit + total_ad_cost) / total_sales × 100%
单件保本 ACOS  BE    = CM%                      （决策口径：一单广告单不赚不亏的真实 ACOS）
目标 CPA       TCPA  = 客单价 × max(目标ACOS, 8%) （全手册所有转化/流血/熔断阈值的缩放基准）
保本 TACOS           = CM%                      （全店广告费 / 总销售额上限）
目标 TACOS           = 按 §1.1 阶段公式推导（PROFIT 阶段为 CM% − target_net_margin）
```

- **双口径并列披露**：`marginal_break_even_acos`（不含退货/仓储/促销的**乐观口径**）与 `true_all_in_break_even_tacos`（全口径**决策口径**）必须在 `### 0.3` 并列披露，严禁混用 ACOS 与 TACOS 口径。
- **全口径硬封顶**：除 `LAUNCH` / `CLEARANCE`（须显式注明投资/清仓额度）或命令行显式 `--target-acos` 外，最终目标 ACOS 不得高于 `true_all_in_break_even_tacos × 0.85`（即使旧版 `product_rules.json` 中存在静态 `target_acos` 也会受此护栏与 `STOP_LOSS` 约束）。
- **广告不可持续区（`< 8%` 或 `CM ≤ 0`）**：若全口径保本 `< 8%` 或 `pre_ad_net_contribution ≤ 0`，直接判定 `ad_unsustainable_zone = True` 并转 `STOP_LOSS`——绝不硬压至无意义的 `3%` 物理地板，也绝不虚设 `12%` 兜底目标；重点把建议导向售价、COGS、物流、退货与仓储结构整改。

### 2.2 数据口径与 COGS 缺失/异常处理

- 多店共卖同一 ASIN：优先取 `sids == [当前店铺 sid]` 的单店利润行；全店汇总 `omni_shop_summary` 另列对照。
- 广告费全口径计入 `SP + SB + SBV + SD`；仓储费穿透读取 `abs(total_stock_fee)` 与分摊仓储/销毁费；保留负毛利符号。
- 比率与币种以财务利润表美元口径为唯一真实基准（`TACOS = total_ad_cost / total_sales`），自动识别已带 `%` 的比率防止百倍虚增。
- **COGS 异常或缺失**（`cogs_per_unit > 客单价 × 1.05` 或有销量但 `cogs_per_unit ≤ 0`）：
  1. 优先用领星实发采购成本 `afn_cogs_per_unit = afn_purchase_costs / afn_volume`（在 `(0, 客单价 × 0.80]` 合理区间内）修正；
  2. 若实发口径仍缺失或异常，**显式标记 `DATA_MISSING: COGS`（`cogs_data_missing = True`, `cogs_is_estimated = True`）**，仅以 `客单价 × 28%` 作临时保守估算并扣减账面虚高毛利，同时**强制冻结一切加码/扩量类动作**，提醒运营先补齐 ERP 采购成本。

### 2.3 退款率两阶硬约束（可在 `rules.json` 按类目覆盖）

- 默认阈值：一级 `refund_lv1 ≥ 12.0%`、二级 `refund_lv2 ≥ 15.0%`（服装鞋包等高退货类目可在 `rules.json` 覆盖）。
- **一级警戒**：首页 `[CRITICAL_REFUND_ALERT]`；目标 ACOS `× 0.82`；停止宽泛拓词；启用高退款硬底价档；第 9 节强制排查质量/预期偏差。
- **二级制冷**：未触发缩量防线且非 `PEAK_DAY` 时，超标自动/广泛仓日预算 `−30%`（保底 `$10`），预算收敛到低退款精准词；已触发缩量防线则不砍预算，转供应链与页面治理。

---

## 3. 证据纪律（归因滞后防护与统计显著性）

### 3.1 必须覆盖的数据层

店铺/Profile、Listing 与属性、FBA 库存（含 `>180` 天库龄与 `days_of_supply`）、活动（竞价策略、日预算、TOS/PP/ROS/OFF AMAZON、OOB、人群溢价 `Cohort%`）、广告组默认基础竞价、关键词（匹配方式 + 基础竞价 / `timing_base_value` + CPC + 亚马逊建议竞价 + 直接/间接 ACOS）、搜索词、自动/ASIN 投放、广告商品、财务利润、业务报告（Sessions/购物车率）、西柚 6 接口、促销记录、Listing 改动日志。

缺哪层就标 `DATA_MISSING` 并降低该层建议强度；绝不编造数字；领星连不上时指导配置，不用旧数据冒充现状。

### 3.2 时间窗口分工（修正归因滞后）

- 所有窗口截止 `T-1`，自动剔除当天未完结半日残缺切片；报告开头与每个活动注明时间戳。
- `30d`：IS/IS NOT 分桶、N-Gram 累计损耗、移词漏斗与长期基线。
- `14d / 7d`：**转化类判断**（CVR、ACOS、0 单失败测试、收割、预算再分配）的主窗口（`fetch_ads_bundle.py` 支持拉取可选 `*_14d.json`，历史无 14d 包时自动无缝回退 7d/30d）。
- `3d`：**只用于曝光、点击、CPC 与调后轨迹**（无归因滞后）。SP 归因最长 7 天、SB/SD 为 14 天，**严禁仅凭 `ord_3d == 0` 判定失败、取消提价或触发止血**，除非 3d 花费已达极端熔断线（`≥ max($15, 3 × TCPA)`）。
- SB/SBV/SD（14 天归因）与 SP（7 天归因）不得直接横向对比 ACOS。

### 3.3 TCPA 与基准 CVR 缩放阈值 + 贝叶斯平滑 CVR

所有阈值由 `v2_policy.compute_scaled_thresholds` 按 `TCPA` 与组合基准 `CVR_base` 动态缩放，并保留历史固定值为严格下限保护（取较严者）：

| 判定场景 | v2 缩放规则（可在 `rules.json` 覆盖系数） | 说明 |
|---|---|---|
| 失败测试（0 单） | `clicks ≥ max(25, max(15, ⌈3 / CVR_base⌉))` 或 `spend ≥ max($20, 2.0 × TCPA)`（14d/30d 0 单） | 约 95% 统计置信度区分“真不转化”与“样本不足” |
| 活跃流血（降价前提） | `spend_7d ≥ max($3, 0.5 × TCPA)` 且 7d 0 单/高 ACOS，辅以近 3d 有活跃花费 `≥ max($1.50, 0.2 × TCPA)` | 彻底消除 3d 归因滞后误杀 |
| 中度 / 重度流血 | 中度：`< max($15, 1.5 × TCPA)`（`-15%`）；重度：`≥ max($15, 1.5 × TCPA)`（`-20%`） | 高客单与低客单差异化控价 |
| 失血熔断（穿透冷却） | 7d `spend ≥ max($15, 2.5 × TCPA)` 且 0 单；或 3d `spend ≥ max($15, 3.0 × TCPA)` 且 0 单；或 7d `ACOS ≥ 目标 × 2` 且 `spend ≥ max($25, 2.0 × TCPA)`；或实体 `ΔCPC ≥ +40%` 且超标；或半天 OOB 且 7d/3d 0 单 | 仅极端值才允许 3d 穿透 |
| 贝叶斯平滑 CVR | `CVR_adj = (orders + k × CVR_base) / (clicks + k)`，`k` 默认 `20`（`cvr_prior_k`） | 防止 1 单 3 点击算出 33% 虚假高 CVR 导致出价暴涨 |

### 3.4 七大非广告外部干扰前置剥离

`CONF_STOCKOUT`（断货）、`CONF_LOW_INVENTORY`（`DOS ≤ 7`）、`CONF_BUY_BOX_LOSS`（`< 85%`）、`CONF_PRICE_HIKE`（近 7d 涨价 `≥ +8%`）、`CONF_DEAL_COOLDOWN`（秒杀结束 10 天内回落）、`CONF_PEAK_POST_COOLDOWN`（大促后 10~14 天回落）、`CONF_LISTING_CONTENT_CHANGE`（标题/图片/五点/类目改动），以及西柚竞品降价/流量异动。命中干扰时先归因外部因素，不误判广告出价失误。

### 3.5 排名数据的定位

西柚 `OR/SP` 排名是抽样快照，作方向性证据；判断“是否已占满顶展”时必须与领星 **TOS 展示份额 (`tos_share`)** 交叉验证。

---

## 4. 决策优先级（仅用于动作冲突裁决）

同一实体命中多条规则时按 **P0 → P5** 裁决，高层无条件覆盖低层。**交付检查（13 节完整性、TSV 格式、校验器）不参与动作裁决，单列于 §9。**

| 级别 | 内容 | 覆盖效果 |
|---|---|---|
| **P0 合规与生存** | 违禁词（含 `rules.json.restricted_terms`）零豁免暂停；断货/低库存控流；确凿 `IS NOT` 0 单即停；流血 N-Gram 词组否定；外部干扰剥离 | 拦截一切提价/收割/复活/回踩；可穿透 P2 冷却与 P3 锁 |
| **P1 经营底线** | 阶段目标与 TACOS 护栏；`STOP_LOSS` / 估算 COGS 冻结全部扩量；缩量防线（利润正轨除外）；退款两阶约束；`PEAK_DAY` 冻结非 P0 砍价 | 止损/估算 COGS 冻结加码；缩量态冻结对核心转化仓大幅砍流 |
| **P2 状态与记忆** | 排除已暂停实体；意向暂停/否定不判断流；实体级冷却（零连坐、双向防连加）；未执行建议三态继承；自然位滑坡保护锁；首屏冷却 | 拦截对已停实体调价、假休克回踩、滑坡核心词降价、冷却期重复调价 |
| **P3 变更纪律** | 双向单杠杆原则、统一步长上限、全局变更预算、TOS 托底（E3）与防泄洪联动（E4/E5） | 拦截同周期多杠杆叠加与超步长/超变更预算动作 |
| **P4 豁免与门禁** | 跨界英雄词豁免（`MULTI_PURPOSE_PROVEN_HERO`）；提权/收割四道门禁 | 豁免正则误杀；一票否决不合格提权 |
| **P5 战术优化** | 贝叶斯保本竞价、真实展位矩阵、双轨五阶、跨活动预算再分配、分时、结构治理 | 在上层约束内执行 |

---

## 5. 变更纪律（单杠杆 · 统一步长 · 全局变更预算）

### 5.1 双向单杠杆原则

杠杆分四类：① `STRATEGY_STATE`（竞价策略/状态）② `PLACEMENT`（活动级 TOS/PP/ROS）③ `BUDGET`（活动日预算）④ `BID`（关键词/ASIN 基础竞价）。**同一活动同一周期只能动一类杠杆（升降双向都适用）**，优先级 `STRATEGY_STATE > PLACEMENT > BUDGET > BID`，低优先杠杆自动转 `Anti_Double_Damping_Hold` 或排入 `next_cycle_queue.tsv` 顺延下期。

五类法定例外（代码自动在 `reason` 中标注编号，报告必须如实呈现）：
- **E1**：P0 违禁/错品暂停与否定；
- **E2**：失血/休克熔断纠偏（`Probe_Bid_Recovery`、`Emergency_Placement_Throttle`）；
- **E3** `Compensated_Base_Bid_Floor`：本期确实下调 TOS 时同步托底基础竞价，保持有效顶展竞价不跌穿拍卖底线（若因 `STRATEGY_STATE` 占先而未动 TOS，E3 一并顺延下期）；
- **E4** `Anti_Drift_Base_Bid_Throttle`（自动/广泛仓）：高 ACOS 且非缩量期下调 TOS 时，PP/ROS 封顶 `≤ 15%` 并联动微降默认基础竞价 `-15%` 防详情页泄洪（未动 TOS 时一并顺延下期）；
- **E5** `Anti_Drift_Base_Bid_Throttle`（站外）：非缩量活动 `OFF AMAZON ≥ 50%` 站外严重泄洪时联动微降默认基础竞价 `-15%`。

### 5.2 以“有效竞价”为被控变量与统一步长上限

```
有效顶展竞价   = 基础竞价 × (1 + TOS%)          （策略为“提高和降低”时上限再 × 2.0；其余位 × 1.5）
有效其余位竞价 = 基础竞价 × (1 + max(PP%, ROS%))
```

| 对象 | 下调上限 | 上调上限 |
|---|---|---|
| 关键词/ASIN 基础竞价（普通） | `-20%` | `+20%` |
| 脆弱品核心支柱词（30d ≥ 6 单或 7d ≥ 2 单） | `-10%`，且有效顶展竞价 ≥ 近期成交 CPC × 85% | `+15%` |
| TOS / PP / ROS 位置溢价 | 有效系数降幅 `≤ 30%`（即 `× 0.70` 取 5pp 整） | `+30pp` |
| 活动日预算 | `-30%`（保底 `$10`） | `+50%`（OOB 高效活动） |

**大幅回归必须分期推进**：例如 TOS `250% → 60%` 严禁一步到位，由 `calc_placement_step` 自动拆解为多期路径（如 `250% → 175% → 120% → 85% → 60%`），路径写入 `planned_path`，`validate_playbook_report.py` 强制拦截超步长行。

### 5.3 冷却与零连坐

- 关键词/ASIN 改动仅锁定该 `(campaign, match_type, entity)` 三元组；仅活动级变更才锁定整组活动。
- 冷却期默认 `7` 天（`cooldown_days` 可配），双向防连砍/连加/横跳；调后 3d `ΔCPC ≤ -15%` 且曝光稳定（`ΔImp > -60%`）→ `【调后生效·见效保持】`，禁止二次砍价。

### 5.4 全局变更预算上限

除 P0/P1 安全与熔断动作外，单期所有被调整实体对应的 30d 日均花费合计 **≤ 组合在跑日均花费的 35%**（`ORGANIC_FRAGILE` 脆弱品 **≤ 20%**；单活动独大占超上限的极简组合启用 `SINGLE_CAMPAIGN_DOMINANCE_ALLOW_ONE` 防死锁仅放行最高优 1 笔）。超额动作自动排入 `next_cycle_queue.tsv` 并写入 `entity_memory` 以 `INHERITED_PENDING` 继承至下期。

### 5.5 展位溢价是活动级杠杆

TOS/PP/ROS 作用于整个活动内全部词。针对单个核心词的展位策略（如双轨 Tier 1/2 调 TOS），仅当该词位于**专属精准活动**（`≤ 3` 个启用 exact 词或 `*-hero-exact` / `*-exact-prove` 窄仓）时才可执行；否则本期只调该词基础竞价，并在第 6 节建议拆建专属仓。

---

## 6. 产品画像与核心词作战表

### 6.1 产品画像（以属性与后台 Listing 为准）

完整交代本品 `IS` / `IS NOT`、价格带、评分评论、小类 BSR、FBA 库存与 `> 180` 天库龄、底盘承接力分级（§7.1）与阶段模式（§1）。

### 6.2 四级合规与豁免阶梯

- **Level 0 违禁词（零豁免）**：内置高危词 + `rules.json["restricted_terms"]`，无论出单多少一律暂停/否定，正向动作恒为 `0`。
- **Level 1 确凿物理硬冲突（`IS NOT`）**：0 单零冷静期清理；偶发出单禁止加码/收割/复活；加载规则时 `sanitize_product_rules` 自动剥离与本品标题正向核心词自相矛盾的错误规则。
- **Level 2 跨界英雄词（`MULTI_PURPOSE_PROVEN_HERO`）**：非违禁且 `orders ≥ 1` 免正则自动暂停；满足 `orders ≥ 2`（或平滑 `CVR_adj ≥ 5%` 且 `spend ≥ 1 × TCPA`）且 ACOS 达标时迁入独立精准主题仓放大。
- **Level 3 相邻可转化词**：低预算低出价观察，不扩词不冲顶展。

### 6.3 核心词作战表（`core_keyword_battlecard.tsv`，5~15 词）

每期自动筛选 **5~15 个核心强相关词**（融通西柚排名、ABA 梯队、30d/14d/7d 关键词与搜索词转化），记录：ABA 梯队、当前 `OR/SP` 与跨期 `ΔOR/ΔSP`、近 14d/7d 转化、平滑 `CVR_adj`、所在活动及是否专属仓、角色（守位/冲刺/补盲/防守/测试）、**目标 OR、验收期及上期目标达成对账**。报告 `### 1.5` 与第 10 节验收以此表为准，并持久化写入 `db/expectations/`。

---

## 7. 诊断与决策模块

### 7.1 盘面、漏斗与缩量防线

- **北极星对账 (`macro_scorecard.tsv`)**：近 7d（`T-7~T-1`）vs 前 7d vs 上期目标卡；前 7d 含大促日自动标记 `DEAL_POST_COOLDOWN` 豁免假缩量。
- **利润优化正轨 (`PROFIT_OPTIMIZATION_ON_TRACK`)**：日均花费 `≤ -8%` 或 TACOS `≤ -1.5pp`、销额 `-25%~0%`、日均预估毛利 `≥ +$3/d` 或 `≥ +10%` → 不触发缩量锁。
- **缩量防线 (`VELOCITY_SHRINKAGE_ALERT` / `VOLUME_AND_EFFICIENCY_DRIFT`)**：非外部干扰、非利润正轨下，总单跌 `≥ 15%` 或跌破销额地板，或销额跌 `≥ 10%` 且 TACOS 上升 / 自然单跌 `≥ 25%` → 冻结核心转化仓大幅降价/砍溢价/砍预算。
- **底盘分级**（默认值，可由 `rules.json` 覆盖）：`ORGANIC_FLYWHEEL`（`Nature CVR ≥ 5%`、`Adv Share ≤ 75%`、30d 自然单 `≥ 45`、访客 `≥ 1500`）；`ORGANIC_FRAGILE`（`Nature CVR ≤ 1%` 或 `Adv Share ≥ 90%` 或自然单 `< 15`）；其余 `ORGANIC_BALANCED`。

### 7.2 库存与促销

- 断货（`fulfillable == 0`）→ `Stockout_Conserve_Hold`（屏蔽休克回踩并给出补货后先精准后泛流的恢复顺序）；库存告急（`DOS ≤ 7` 或 `fulfillable ≤ 25`）→ 泛流预算 `-30%~-50%` 仅保精准主粮词；秒杀结束 10 天内 → `DEAL_TRANSITION_COOLDOWN`。

### 7.3 竞价精调（统一贝叶斯平滑保本公式）

- **有单词保本锚定**：`Target Base Bid = max(底价, round(目标ACOS × 客单价 × CVR_adj / (1 + TOS%), 2))`，单步受 §5.2 限制（`-10%~-20%`；脆弱支柱词 `≤ -10%`）。
- **无单词两阶渐降**：仅对 7d 活跃流血实体执行，中度 `-15%` / 重度 `-20%`；已零曝光零花费不二次砍价。
- **竞品 ASIN 投放**：先对标价格、评分评论与西柚 7d 流量护城河；被碾压/错形态暂停否定；有平替优势按 `max(ASIN底价, min(当前×0.88, CPC×0.85/(1+PP%)))` 防守。
- **分时实体**：`is_apply_time == True` 且 `timing_base_value > 0` 时以 `timing_base_value` 为唯一基准，严禁拿高峰瞬时抓包出价算降价，并提醒同步修改领星分时基准值。

### 7.4 展位溢价与竞价策略

- 优先以 30d/7d 真实全展位报表（`real_placement_performance.tsv`，单活动 100% 归一化）为准：盈利 `PP/ROS/OFF AMAZON`（`orders ≥ 2` 且 `ACOS ≤ 目标 × 1.15`）严禁机械清零；高耗低效 `OFF AMAZON`（占比 `≥ 20%` 或花费 `≥ max($15, 1×TCPA)`）触发站外止血（`≥ 50%` 叠加 E5）。
- `提高和降低` 叠加高 TOS 超标时优先切 `仅降低`（本周期不再动该活动其他杠杆）。
- 搜索词展位指纹（`TRAPPED_CORE_HERO` / `FAILED_AT_TOS` / `FAILED_EXTENDED_TEST`）绑定真实 `ad_groups`/`targets` 默认竞价，提权严格过四道门禁（合规与 N-Gram、未达失败测试、非扩量冻结、强相关低 CTR）。

### 7.5 跨活动预算再分配与分时（`budget_reallocation.tsv`）

- **预算再分配**：按 14d → 7d → 30d 边际效率排序，从 `ACOS > 目标 × 1.3` 且非 OOB 活动回收预算（`-25%~-30%`，保底 `$10`），向 OOB 且 `ACOS ≤ 目标` 的高效活动扩容（步长 `≤ +50%`）；遵守单杠杆、缩量防线与多品共享保护。
- **分时 60 单样本门槛**：全组合近 30d 在跑广告订单 **≥ 60 单**（`daypart_min_orders_30d`）才输出分时调整策略；样本不足时跳过分时并在报告注明，避免噪声调价。
- **分时良性豁免**：必须同时满足 `ord_7d > 0`、`acos_7d ≤ 目标 × 1.35` 且 `ΔCPC ≤ +25%` 才豁免降价；早竭防御（利用率 `≥ 80%`）稳早间保晚高峰，凌晨降幅 `≤ 35%` 守底价兼顾跨时区平权。

### 7.6 西柚双轨五阶矩阵（单杠杆合规版）

前置保护：`SP #1` 且高 TOS 份额禁止继续提价（转 `Defensive_Dominance_Maintain`）；`SP #1~#3` 曝光回落转 `Top_Rank_Cooldown_Hold`；扩量冻结态（`STOP_LOSS`/估算 COGS）冻结 Tier 2 冲刺；自然位 `OR ≤ 20` 且有单的词下滑 `≥ 4` 名触发 `Rank_Slide_Protection_Hold`。

| 层级 | 条件 | 单杠杆合规动作（每期只动一个杠杆） |
|---|---|---|
| Tier 1 王者守位 | `OR 1~3` | 禁止盲关广告。ACOS ≤ 目标 × 1.15 → 守流；ACOS > 目标 × 1.2 → **本期先降基础竞价 `15%~20%`**，3~5 天观察总单量（跌 `≥ 15%` 立即回调），下期再视专属仓情况压 TOS |
| Tier 2 临界冲刺 | `OR 4~15` | 非 `SP #1`、非冻结、`CVR_adj ≥ 5%` 或有单且 ACOS 受控 → **本期先提基础竞价 `+15%~+20%`**，下期确认效果后再在专属仓加 TOS（目标 `60%~75%`） |
| Tier 3 腰部攻坚 | `OR 16~50` | 转化优异 → 隔离进精准仓冲首页；高耗无转化 → 柔性降价 `-15%~-20%` |
| Tier 4 自然脱节 | `OR > 50` 或未上榜且 `SP ≤ 10` | 严禁高溢价抢顶展；降价控频并联动第 9 节补埋词 |
| Tier 5 自然盲区 | `OR ≤ 45`（或 `≤ 60` 高流量）、强相关、无在跑广告 | 按倒推起步基础竞价纳入精准仓补盲（`xydc_blindspot_opportunities.tsv`） |

### 7.7 搜索词、否定与两阶段移词

- 意图分桶 + N-Gram 出血词根词组否定（TCPA/CVR 缩放阈值，P0 零冷静期，保护核心功能词根）。
- 两阶段移词软交接：第 1 阶段按倒推竞价入精准仓（已存在但暂停则 `Unpause_Keyword`），源仓保留 3~5 天软过渡；第 2 阶段精准仓出单稳态后再在源仓 `Negative_Exact`。
- 同步 SP 静态否定（`ad_sp_negative_target_list`，不传 `state`）与 HSA 静态否定，新否定 100% 去重；严格执行四态否定审计。

### 7.8 熔断、观察期与台账闭环

- **休克熔断**：近 3d 日均曝光跌 `≥ 70%` 且 0 单，先依次排除 5 类假休克（断货/低库存、主动暂停/否定、首屏 `SP #1~#3`、秒杀/大促后冷却、全类目季节性回落），确诊后按倒推公式回踩，严格隔离 `entity_field`。
- **台账与动作全元数据**：每条建议自动附带 `lever_type`、`planned_path`、`expected_effect`、`verify_date`、`rollback_rule`（输出至 `action_plan_details.tsv`、`next_cycle_queue.tsv` 并持久化至 `db/ledger/` 与 `db/entity_memory/`，100% 兼容历史旧台账/旧记忆文件）。

### 7.9 大促模式 `PEAK`

- **预热期 (`PRE_HEAT`，大促前 7~14 天)**：仅对已验证出单词小步加码、提前评估预算上限，不做结构性大改；库存不足时不加码。
- **大促当天 (`PEAK_DAY`)**：ACOS 容忍度放宽至 `目标 × 1.20`；优先盯 OOB 与核心精准/Tier 2 词预算；**禁止非 P0 常规否定与降价**（仅允许违禁、`IS NOT` 与极端熔断）。
- **大促后 (`POST_COOLDOWN`，大促后 10~14 天)**：自动置入 `CONF_PEAK_POST_COOLDOWN` 并屏蔽休克回踩 (`block_shock_recovery`)，按 §5.2 步长平滑回收大促加码。

### 7.10 结构治理与 SB/SBV/SD 独立体检

- **死活动五步重生**：归档老活动 → 提纯 `IS` 出单词 → 预置出血词根与 `IS NOT` 否定 → 倒推起步竞价 → 1~3 词精准仓密集监控 3~5 天。
- **碎片化孤岛合并豁免**：投放词 `≤ 2`、30d `≤ 1` 单且低效的启用活动可并入主精准仓后归档；**硬豁免**：上线 `< 21` 天新活动、核心词作战表专属活动（`*-hero-exact`/`*-exact-prove`）、跨品共享品牌活动、`LAUNCH` / `PEAK` / 缩量防线期间——彻底杜绝“刚建单词仓又被合并”的死循环。
- **SB/SBV/SD 独立体检 (`brand_display_health.tsv`)**：独立按 14 天归因核算（绝不与 SP 7d ACOS 直比），逐项检查品牌词防守覆盖、SBV 对核心词作战表覆盖率、新客指标 NTB（接口未返回标 `DATA_MISSING: NTB`）及跨组合多品共享保护 (`is_shared_multi_asin_campaign`)。

---

## 8. 必做流程

连接与参数见 [references/mcp-connection.md](references/mcp-connection.md) 与 [references/tool-params.md](references/tool-params.md)；方法论细则见 [references/analysis-framework.md](references/analysis-framework.md)；成稿模板见 [references/report-template.md](references/report-template.md)。

1. **接通双 MCP**：按优先级读取 `LINGXING_MCP_KEY` → `~/.gemini/config/mcp_config.json` → `~/.codex/config.toml`；同步校验 `xydc-mcp`。连不上时指导配置，绝不拿旧数据冒充。
2. **定位组合工作区**：根目录 `<WORKSPACE_DIR>/<广告组合名>/`（默认 `./lingxing-ads/<广告组合名>/`，可通过环境变量 `LINGXING_ADS_WORKSPACE` 或 `--base-dir` 指定）；不存在则初始化 `outputs/`、`db/{daily_records,ledger,snapshots,expectations,entity_memory}/`、`work/lx-ads/`、`README.md`。
3. **一键取数与全量分析**：
   ```bash
   python ~/.gemini/config/skills/lingxing-ads-playbook/scripts/run_portfolio_audit.py \
       --portfolio <广告组合名> --asin <ASIN> --country <US|UK|DE> --shop <SHOP> \
       [--mode AUTO|LAUNCH|GROWTH|PROFIT|DEFEND|STOP_LOSS|CLEARANCE] [--peak|--no-peak] \
       [--rules <rules.json>] [--target-acos <float>] [--concurrency 1]
   ```
   脚本自动向下兼容既有 `db/` 全部历史快照、台账、预期与实体记忆文件。
4. **对账上期 → 阶段定标 → 作战表 → 全量诊断**：核验 `summary.txt`、`macro_scorecard.tsv`、`core_keyword_battlecard.tsv`、`budget_reallocation.tsv`、`brand_display_health.tsv`、`action_plan_details.tsv`、`next_cycle_queue.tsv`、`ledger_calibration.json` 与 `bulk_adjustments.tsv`。
5. **成稿与三方对齐**：先定第 4 节总表，再把完全一致的数值逐字写入第 5 节与 `bulk_adjustments.tsv`；全部启用活动逐一覆盖。
6. **强制硬门禁校验**：运行 §9.3 `validate_playbook_report.py` 直至 `[PASS]`，随后按 §9.1 在对话窗交付。

---

## 9. 交付规范

### 9.1 对话窗口 5 段式摘要

1. **阶段与盘面结论**：本期模式（及 `PEAK` 相位）+ 一句话定性；7d 环比日均销额、日均贡献利润、总单量、自然单占比、TACOS、双口径保本 → 目标 ACOS/TACOS/TCPA、可售天数与库龄、外部干扰、上期目标卡达成情况。
2. **3~5 个核心病灶**（数据 + 商业因果），直指利润黑洞或核心词自然位瓶颈。
3. **按顺位行动指南**（每条写明实体 + 改前 → 改后纯美元值 + 理由 + 验证点/回滚条件）：P0 止血消雷 → P1 策略/溢价/预算再分配（注明单杠杆锁与多期 `planned_path`）→ P2 基础竞价与分时基准同步 → P3 收割/复活/盲区拓词 → **今日不碰清单** → **下期队列**（变更预算或单杠杆顺延项）。
4. **2~3 天熔断巡检点 + 7 天目标卡验收线**（日均销额地板、自然单占比、TACOS、核心词目标 OR）。
5. **交付物完整路径 + `validate_playbook_report.py` `[PASS]` 确认**。

### 9.2 落盘 13 节手册（`## 0` ~ `## 12`，含必选 `### 0.0`，结构零删减）

严格遵循 [references/report-template.md](references/report-template.md)：
- 报告头 + **首屏复核速览卡**（含阶段模式、PEAK、TCPA、全局变更预算占用率）；
- `## 0`（`0.0 阶段模式与本期目标卡`、`0.1`、`0.2`、`0.3`、`0.4`）→ `## 1`（`1.1`~`1.9.3`，含 `1.5` 核心词作战表）→ `## 2`（`2.1` 含预期/验证/回滚与 §10 校准率、`2.2`）→ `## 3`（含多期路径与红线）→ `## 4`（唯一真相总表）→ `## 5`（全活动逐一展开 + 预算再分配 + SB/SBV/SD 独立体检，四行卡片末行附 `验证：…；回滚：…`）→ `## 6` → `## 7` → `## 8` → `## 9` → `## 10` → `## 11`（含下期队列）→ `## 12`（全套带时间戳 TSV/JSON 索引）。

### 9.3 交付硬门禁（不参与动作裁决）

- 第 4 节、第 5 节与 `bulk_adjustments.tsv` 三方 100% 一致；`bulk_adjustments.tsv` 严格 7 列、无制表符污染、竞价与预算为纯美元绝对值、零同实体冲突、零单杠杆违规、零超步长溢价跳变、零违禁正向词。
- 交付前必跑校验器直至返回码 `0`：
  ```bash
  python ~/.gemini/config/skills/lingxing-ads-playbook/scripts/validate_playbook_report.py \
      --report <手册路径> --analysis-dir <analysis目录> --bulk-tsv <批量表路径> [--rules <rules.json>]
  ```
  （审计历史 v1 旧报告时可附加 `--legacy-v1` 兼容不含 `0.0` 的旧版结构。）
- 所有输出写入组合 `outputs/`，文件名带 `YYYYMMDD-HHmmss`，禁止覆盖历史文件。

### 9.4 安全底线

- 默认只读，只输出手册与批量表；仅当用户明确要求写后台时才调用写接口，且执行前须列出完整变更清单请用户确认（暂停/归档活动、单次预算变动 `> 30%` 需二次确认）。
- 永不打印或回显 `X-Mcp-Key`、西柚 Token、Open API Secret 或任何认证凭据。

---

## 10. 持续校准

- 脚本每次运行通过 `v2_policy.compute_ledger_calibration` 按动作类型（降价、提价、溢价调整、预算调整、竞价策略、暂停/状态）汇总历史台账的预期达成率并输出 `ledger_calibration.json`。
- 任一动作类型累计追踪 `≥ 3` 笔且达成率 `< 50%` 时，在 `summary.txt` 与报告 `### 2.1` 显式提出 `rules.json` 参数校准建议（需人工确认后写入）。

---

## 附录 A：可配置参数（`rules.json` / `product_rules.json` 可覆盖，见 `references/product-rules-template.json`）

| 参数键名 | 默认值 | 说明 |
|---|---|---|
| `mode` | `AUTO` | `AUTO/LAUNCH/GROWTH/PROFIT/DEFEND/STOP_LOSS/CLEARANCE`（可加 `+PEAK`） |
| `peak_mode` | `AUTO` | `AUTO / ON / OFF` |
| `clearance` | `false` | 显式声明进入 `CLEARANCE` 清仓模式 |
| `launch_acos_multiplier` / `launch_weeks` / `launch_budget_cap` | `1.30` / `6` / `null` | `LAUNCH` 投资倍数、周期（周）与累计投资美元上限 |
| `growth_acos_coef` | `0.85` | `GROWTH` 保本系数（受 `BE × 0.85` 护栏约束） |
| `profit_acos_coef` / `profit_acos_min` / `profit_acos_max` | `0.68` / `12.0` / `35.0` | `PROFIT` 成熟收割系数与夹紧区间 |
| `target_net_margin` | `10.0` | 目标净利率（%），用于推导 `PROFIT` 目标 TACOS |
| `refund_lv1` / `refund_lv2` | `12.0` / `15.0` | 退款率一级警戒 / 二级制冷阈值（%） |
| `restricted_terms` | `[]`（叠加内置列表） | 类目自定义 Level 0 违禁词列表 |
| `bid_floor_kw` / `bid_floor_kw_fragile` | `0.25` / `0.30` | 关键词普通 / 脆弱高退款硬底价（USD） |
| `bid_floor_asin` / `bid_floor_asin_fragile` | `0.36` / `0.42` | ASIN 商品投放普通 / 脆弱高退款硬底价（USD） |
| `cvr_prior_k` | `20.0` | 贝叶斯平滑先验点击强度 `k` |
| `fail_test_clicks_factor` / `fail_test_clicks_min` / `fail_test_clicks_legacy_floor` | `3.0` / `15` / `25` | 0 单失败测试点击门槛缩放系数与保护下限 |
| `change_budget_share` / `change_budget_share_fragile` | `0.35` / `0.20` | 单期全局变更预算占在跑日均花费上限 |
| `cooldown_days` | `7` | 实体级/活动级观察冷却期（天） |
| `daypart_min_orders_30d` | `60` | 启用分时调价的最低 30d 在跑广告订单数 |
| `organic_flywheel_cvr` / `organic_flywheel_adv_share` / `organic_flywheel_orders` / `organic_flywheel_sessions` | `5.0` / `75.0` / `45` / `1500` | `ORGANIC_FLYWHEEL` 判定门槛 |
| `organic_fragile_cvr` / `organic_fragile_adv_share` / `organic_fragile_orders` | `1.0` / `90.0` / `15` | `ORGANIC_FRAGILE` 判定门槛 |
| `low_stock_buffer_days` / `lead_time_days` | `14` / `null` | 低库存禁加码缓冲天数；若设置 `lead_time_days`，有效预警线为 `max(low_stock_buffer_days, lead_time_days + 7)` |

## 附录 B：脚本实现契约（代码底层保障，报告严禁违背）

- 0 单有花费 API 极值 `99999999.0` → TSV 清洗为 `999.0`，报告正文与批量表统一显示 `-- (0单)`；历史台账加载时同步清洗。
- 活动按 `campaign_id` 或完整精确名称绑定，严禁字符串后缀/包含匹配；关键词 `bid: None` 时继承所属广告组真实 `default_bid`。
- ASIN 实体用 `b0[0-9a-z]{8}` 双向索引纯 ASIN 与投放表达式；`entity_memory` 与唯一胜出仲裁主键完整包含 `match_type`。
- 西柚优先读取线上新键名（`totalTraffic`/`trafficDistribution`），向下兼容旧键名；本品自然/广告流量占比（`0~100%`）与类目获取率严格分列。
- 业务报告 API 返回空或 0 Sessions 时自动回退 `db/daily_records/` 最近有效快照，并以财务口径交叉核算 TACOS，杜绝 `0%` 假数据。
- 同 `(campaign, entity, field)` 存在更新记录时旧记录归档 `SUPERSEDED_ARCHIVED`；超 14 天未复现休克归档；暂停或调后 3d 0 曝光时 `curr_imp = 0.0`；`curr_cpc ≤ 0` 时 `delta_cpc = 0.0`；分时波峰波谷漂移归档 `DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED`。
- `bulk_adjustments.tsv` 严格保持 7 列（100% 向下兼容既有上传流程）；新增扩展元数据写入配套 `action_plan_details.tsv`、`next_cycle_queue.tsv`、`core_keyword_battlecard.tsv`、`budget_reallocation.tsv`、`brand_display_health.tsv`、`ledger_calibration.json`，台账 `historical_ledger.tsv` 与 `db/ledger/*.json` 向下兼容旧记录并自动补齐 `lever_type`/`planned_path`/`expected_effect`/`verify_date`/`rollback_rule`。
- 排版硬约束：中文用户全程中文；对外一律写「基础竞价 / 默认基础竞价」，严禁裸词 `bid`/`BID`；全部美元金额用反引号包裹（负号在反引号内，如 `` `-$113.96` ``）；箭头统一用 Unicode `→ ↑ ↓`，严禁 `\to`/`\rightarrow`；报告外层严禁包裹三反引号代码围栏。
