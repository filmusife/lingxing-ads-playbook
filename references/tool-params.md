# 领星 MCP 与西柚洞察 (`xydc-mcp`) 工具参数与避坑指南

取数失败先 `search` 当前 toolId 或读取 MCP schema，再对照下面的已知实战规范。对外向用户一律写「基础竞价」与「默认基础竞价」，不要写 BID；API 内部字段仍为 `bid` / `default_bid`。

---

## 一、领星 OpenMCP (`LingXing-MCP`) 工具参数

### 公共约定
- `profile_ids`: **字符串数组**，如 `["1234567890123456"]`
- `report_date`: `"2026-09-07 - 2026-09-20"`（默认拉取 30 天宏观基线 + 近 14 天 + 近 7 天 + 近 3 天四个鲜活切片）
- `sort_field`: `spends`；`sort_type`: `desc`
- `page`: 从 `1` 开始，循环翻页直至返回空页或不足一页；第一行若无 `name`/`campaign_name`/`keyword_text` 为合计行，分析时必须剔除。

### 1. `ad_auth_shops`
无必填。返回 `alias`（店铺名）、`country`（注意英国是 `UK` 而非 `GB`）、`sid`、`profile_id`、`real_status`。

### 2. `ad_portfolio_report_shop` / `ad_campaign_report`
- 必填：`report_date, profile_ids, page, length, sort_field, sort_type`。
- `portfolio_id` schema 写 string；若筛选失败，改试单元素字符串数组。
- 关键输出：`name, campaign_id, state, targeting_type, sponsored_type, daily_budget, bidding, bidding_type, placement_top, placement_product_page, placement_rest_of_search, spends, sales, acos, cpc, cvr, ctr, impressions, clicks, orders, direct_orders, indirect_orders, top_of_search_impression_share, is_apply_time, timing_base_value, applied_templates, is_apply_rule, optimization_rule_desc, avg_over_budget_time, last_over_budget_at, campaign_budget_suggestion_percent_time_in_budget, suggestion_budget`。
- 注意 `bidding.strategy`：`manual` 为固定竞价，`autoForSales` 为动态竞价-提高和降低（顶展最高可放大 `2.0x`），`legacyForSales` 为动态竞价-仅降低；同时提取 `bidding.shopperCohortBidding`（受众人群溢价比率 `Cohort%`）。

### 3. `get_sp_campaign_placement_report` (真实全广告位表现报表 — 30d & 7d，含 `OFF AMAZON` 站外)
直接获取每个活动在 `搜索结果顶部（首页）`、`商品页面`、`其余搜索位置` 及 **`OFF AMAZON`（亚马逊站外）** 的真实花费、直接/间接订单、CPC、CVR 与 ACOS（落盘为 `placement_report.json` 与 `placement_report_7d.json` → `real_placement_performance.tsv`）。
- 必填：
  - `report_date`: `"YYYY-MM-DD - YYYY-MM-DD"`
  - `profile_ids`: `["<profile_id>"]`
  - `portfolio_id`: `["<portfolio_id>"]`（可选，字符串数组）
  - `with_ring`: 整数 `0`（不要传布尔 `false`）
  - `page`: `1`, `length`: `200`, `sort_field`: `"spends"`, `sort_type`: `"desc"`
- 注意：首行常为 `campaign_id == None` 的全盘汇总行，解析时必须跳过，按 `campaign_id` 或 `campaign_name` 精确绑定各活动；各展位花费占比 (`spend_share_pct`) 必须按**单活动内部总花费 100% 归一化重算**（切勿直接将全店汇总 `spend_rate` 乘 `100`），并按单活动投产差异化审计 `OFF AMAZON` 站外流量。

### 4. `ad_campaign_group_report`
- 必填含 `with_ring`（传 **整数 `0`**，不要传 `false`），`portfolio_id` 传字符串数组。
- 核心用途：提取每个 `ad_group_id` 的真实 `default_bid`，用于关键词/商品投放 `bid: None` 及搜索词展位指纹推断时的真实继承基础竞价还原（消除假 `$0.60` 兜底竞价）。

### 5. `ad_campaign_keyword_report`
- 必填：`report_date, profile_ids, page, length, sort_field, sort_type`；`portfolio_id` 传数组。关键词常超 300+ 行，必须完整翻页。
- 关键字段：`campaign_name, ad_group_id, ad_group_name, keyword_id, keyword_text, match_type, bid, default_bid, state, spends, sales, orders, clicks, impressions, cvr, cpc, ctr, is_apply_time, timing_base_value, applied_templates, recommends, direct_acos, indirect_acos`（注意：凡 `is_apply_time == True` 且 `timing_base_value > 0` 时，自变量快照与基础竞价计算一律锚定 `timing_base_value`）。

### 6. `ad_campaign_search_term_report`
- 必填：`report_date, profile_ids, page, length, sort_field, sort_type`（常达 800~2000+ 行，必须翻页）。
- 关键字段：`query, campaign_name, match_type/target_match_type, target_text/keyword_text, spends, sales, orders, clicks, impressions, cvr, cpc, is_asin, kw_negative, st_negative`。
- 可选增强参数：`with_translation: 1`（小语种翻译）、`category: "HasClickNoDeal"`、`targeted_type: "not_targeted"`。

### 7. `ad_campaign_targeting_report`
- 必填含 `with_ring: 0` 以及 `page, length, sort_field, sort_type`。
- **踩坑注意**：`length` 必须传字符串 `"100"`，`portfolio_id` 传字符串数组。同时返回竞品 `asin_title, asin_price, asin_stars, asin_review_count` 以及分时字段 `is_apply_time, timing_base_value, applied_templates`。

### 8. `ad_campaign_product_report`
- 必填：`report_date, profile_id`（**必须是单个整数 `int(pid)`**，绝不能传数组 `profile_ids`，否则报 HTTP 422；`with_ring` 传 `False`）。用于确认哪些活动实际推广了目标 ASIN/MSKU。

### 9. `erp_listing` & `get_fba_stock_list`
- `erp_listing`：`offset=0, length=20, pvi_ids="0", sids="<sid>", search_field="asin", search_value=["B0XXXXXXXX"], exact_search="1"`。提取标题、价格、星级、评论数、大类/小类 BSR、父体、MSKU、可售库存及近 7/14/30 天销量与销售额。
- `get_fba_stock_list`：`sid`（字符串）、`search_field="asin"`、`search_value`（**单字符串而非数组**）、`offset=0, length=20, is_hide_zero_stock="0"`。关注 `afn_fulfillable_quantity`、`short_term_historical_days_of_supply`、库龄。

### 10. `advertising_order_analysis_list` (24h 与星期分时转化洞察)
- 必填：`sid`（字符串）、`sku`（**MSKU 字符串数组**）、`start_date`、`end_date`、`group_type`（`"hourly"` 或 `"weekly"`）。

### 11. `query_product_performance_asin_lists` (30d 汇总漏斗 + 逐日 USD 北极星时间序列)
- **30 天汇总漏斗 (`product_performance.json`)**：`sids="<sid>"`（单字符串）、`search_field="asin"`、`search_value=["B0XXXXXXXX"]`、`start_date`、`end_date`、`offset=0, length=10`。注意所有比率字段（`ad_cvr, nature_cvr, cvr, adv_rate, acoas`）领星统一返回小数比率（如 `'0.2928'` = `29.28%`，`'1.3370'` = `133.70%` 跨期归因回溯），须先判别 `>100%` 归因滞后再夹紧至 `[0, 100%]`。
- **逐日 USD 北极星序列 (`product_performance_daily.json`)**：传入 `summary_field="asin"`、`is_recently_enum=False`（按日展开 `rdate`）、`currency_code="USD"`、`length=100`，用于 `macro_scorecard.tsv` 精确计算近 7 天 vs 前 7 天日均总销售额、日均预估毛利 (`predict_gross_profit`)、总单量、自然单 (`nature_order_items`，负值用 `max(0, vol - ad_direct_order_quantity)` 还原)、自然转化率 (`nature_cvr`)、广告转化率 (`ad_cvr`)、Sessions、购物车率 (`buy_box_percentage`) 与小类 BSR（注意剔除拉取当日未完结切片及最近 1-2 天亚马逊业务报告尚未出数的 `0-session` 延迟尾部）。

### 12. `query_order_profit_list` (真实财务毛利、全口径保本 TACOS 与跨店铺对账)
- 必填（参数类型严格）：`start_date, end_date, currency_type="USD", summary_field="asin", turn_on_summary="0", sort_type="desc", length="20", offset="0", source_service="mcp", external_service_mark=1, search_field="asin", search_value=["B0XXXXXXXX"]`。
- **多店铺共享 ASIN 与全口径保本核算关键规范**：
  1. 返回列表中既可能有单店行 `sids: [1001]`，也可能有跨店汇总行 `sids: [1001, 1002]`。代码必须优先精确匹配单元素 `[target_sid]`，保留负毛利率符号，并单独汇总跨店 `omni_shop_summary`；
  2. 发货量优先读取 `afn_volume`（fallback `afn_quantity` / `volume`）；当触发 COGS 录入异常（`cogs_per_unit > avg_price * 1.05`）时，基于 `afn_purchase_costs / afn_volume` 实发单均成本同步修正经营毛利 `adjusted_gross_profit`；
  3. 全量提取 `SP + SB + SBV + SD` 广告费、月度/长期仓储费 (`total_stock_fee` / `fba_storage_fee` / `long_term_storage_fee`)、退款额 (`refund_amount`)、件数退货量 (`fba_return_goods_count` → `unit_return_rate_pct`)、退货不可售损耗 (`return_unsellable_purchase_costs`)、促销折让 (`promotion_discount`) 与手续费 (`promotion_fee`)、销毁及调整费 (`shared_fba_disposal_fee` / `adjustments_fee`)，按严格会计恒等式计算 `true_all_in_break_even_tacos = (adjusted_gross_profit + total_ad_cost) / total_sales * 100` 及零广告亏损熔断 (`zero_ad_loss_circuit_breaker`)。

### 13. `sales_detail_sec_kill` / `sales_detail_prime_discount` / `analytics_log_list_v2` / `sales_operate_log_list`
- `sales_detail_sec_kill` & `sales_detail_prime_discount`：排查领星后台秒杀与 Prime 专享折扣记录；
- `analytics_log_list_v2`：拉取广告后台真实调价/改溢价/改状态操作审计日志；
- `sales_operate_log_list`：拉取 Listing 标题、五点、图片、价格等运营修改日志（用于 `macro_scorecard` 剥离 `CONF_LISTING_CONTENT_CHANGE` 非广告外部干扰；注意过滤 `small_image_url` 等系统缩略图元数据字段防误报）。

### 14. `ad_sp_negative_target_list` (SP 静态否定关键词库与否定商品库)
- 核心用途：按本广告组合下 SP 活动的 `campaign_id` / `campaign_ids` 游标分页拉取后台已配置的静态否定词与否定商品，分别落盘为 `sp_negative_keywords.json` 与 `sp_negative_targets.json`（客户端按返回行过滤 `state == "enabled"`），并入 `known_negatives` 与 `active_negatives_audit.tsv` 确保 100% 去重。
- **关键参数防坑红线**：
  - 必填 `target_type`：枚举值严格为 `"keyword"`（否定关键词）或 `"target"`（否定商品/分类），须分别调用拉取；
  - 接口 Schema 声明 `additionalProperties: false` 且使用游标分页——**严禁传入 `state` 或越界 `page` 参数，否则接口直接报错失败！**
  - **严禁不加活动 ID 过滤直接拉取全店数据**（单店全量可达数万条无关废数据）。

### 15. 跨组合共享品牌广告探测与 `ad_hsa_negative_keywords` / `ad_hsa_negative_targets` / `ad_hsa_campaign_placement_reports`
- **跨组合共享 SB / SBV / SD 穿透探测 (`shared_brand_campaigns.json`)**：突破单一 `portfolio_id` 限制，扫描当前店铺下的 `hsa`/`sb`/`sd` 活动，筛选 `creative_asins`（或关联推广 ASIN）包含当前目标 `ASIN` 但位于当前 `portfolio_id` 之外的跨品共享品牌广告，标记 `"is_shared_multi_asin_campaign": True`、`"shared_asin_count"`、`"creative_asins"`，防止共享品牌声量池隐身或被单品视角误停。
- **HSA / SBV 静态否定与展位报表**：
  - 当本组合或跨组合共享存在 HSA/SBV 活动时，按需拉取 `ad_hsa_negative_keywords`（`hsa_negative_keywords.json`）与 `ad_hsa_negative_targets`（`hsa_negative_targets.json`）；
  - `ad_hsa_campaign_placement_reports`（`hsa_placement_report_7d.json`）：**踩坑注意**——该接口 `report_date` 要求传**单日日期字符串 `"YYYY-MM-DD"`**（非日期区间），且花费字段名为 **`cost`**（非 `spends`），应并发拉取近 7 天单日切片聚合或保留明细，异常时优雅降级不阻塞主流程；
  - 严禁将 `ad_aba_report` 塞入单组合日常抓取循环（该接口返回全美站点数百 MB COS 压缩文件下载链接）。

---

## 二、西柚洞察 (`xydc-mcp`) 六大核心工具参数（`scripts/xydc_client.py` 自动集成）

所有 `xydc-mcp` 工具调用均需附带语义清晰的 `user_task` 与 `intent_summary` 字符串参数，国家码传大写（如 `"US"`）。

### 1. `get_asin_keywords` (`xydc_ranks.json` → `rank_synergy.tsv` & `xydc_blindspot_opportunities.tsv`)
查询目标 ASIN 在目标站点下收录的全部自然搜索词与广告词排名及流量指标（支持 `page` + `page_size=100` 翻页拉取最高 1500 词）：
- 参数：`asin`（字符串）、`country`、`page`、`page_size`、`sort_by="totalTrafficScore"`、`sort_order="desc"`。
- **关键字段语义防坑**：
  - `ranks[]`：按 `position` 区分 `"or"`（自然位 `totalRank` / `page`）、`"sp"`（SP 广告位 `totalRank` / `page`）、`"sb"`（SB 品牌广告位 `totalRank`，**严禁误读 `page`**！）；
  - `organicTrafficScore` / `advertisingTrafficScore` / `totalTrafficScore`（兼容无 `Score` 键名）：该词贡献给本品的自然流量得分、广告流量得分与总流量得分。**本品在该词下的真实自然/广告流量占比 (`organic_share` / `ad_share`) 必须用 `organicTrafficScore / totalTrafficScore × 100%` 计算（两者之和 = `100%`）**；
  - `organicTrafficAcquisitionRate` / `advertisingTrafficAcquisitionRate`：该词相对于类目流量天花板的获取效率比率（两者相加可超过 `100%`，单独输出为 `org_acq_rate` / `ad_acq_rate`，切勿当作本品内部占比！）。

### 2. `get_asin_traffic` (`xydc_traffic.json` → `xydc_competitor_benchmark.tsv`)
单次批量查询本品 ASIN + Top 8 核心投放竞品 ASIN（共最多 9 个 ASIN 一次请求，仅耗 2 积分）近 7 天及前 7 天的自然流量、广告流量、流量结构占比、增速与收录词总数：
- 参数：`asins: ["<SELF_ASIN>", "<COMP_1>", ...]`、`country`。
- **线上真实返回键名防坑（无 `Score` 后缀优先，双兼容 fallback）**：
  - 西柚线上接口真实返回键名为：`totalTraffic`、`previous7DaysTotalTraffic`、`totalTrafficGrowthRate`、`organicTraffic`、`previous7DaysOrganicTraffic`、`organicTrafficGrowthRate`、`organicTrafficRatio`、`organicSearchTermCount`、`advertisingTraffic`、`previous7DaysAdvertisingTraffic`、`advertisingTrafficGrowthRate`、`advertisingTrafficRatio`、`advertisingSearchTermCount`（无 `Score` 后缀！）；
  - 客户端代码必须**优先读取上述真实无 `Score` 键名**，并 fallback 兼容旧版 `*TrafficScore*` 键名，杜绝映射失配导致流量得分为 `0`。

### 3. `get_parent_asin_ad_overview` (`xydc_traffic.json`)
查询父体变体结构与近 7 天 `organic / sp / sb / sbv` 四渠道流量占比、头腰尾词流量分布及各子体广告流量贡献占比：
- 参数：`asin: "<SELF_ASIN>"`、`country`、`top_n: 5`。
- **线上真实返回键名防坑（`trafficDistribution` 双兼容）**：
  - 顶层返回：`scope.parentAsin`、`scope.totalVariationCount`、`advertisedVariationCount`；
  - 四渠道分布线上真实键名为 **`trafficDistribution`**（非旧名 `trafficScoreDistribution`），各渠道子项真实字段名为 **`traffic`**（非 `trafficScore`）、**`shareOfTotalTraffic`**（非 `shareOfTotalScore`）、**`shareOfAdTraffic`**（非 `shareOfAdScore`），配合 `keywordTiers` (`head/midTail/longTail`) 与 `topVariations`，客户端须新旧键名双兼容以确保 `channel_distribution`、`keyword_tier_distribution`、`top_variations` 100% 非空还原。

### 4. `get_asin_keyword_count_trends` (`xydc_traffic.json`)
查询近 14 天每日自然收录关键词数、广告关键词数、`or/sp/sb/sbv/ac` 各展位词数及头腰尾词数量变化趋势：
- 参数：`asins: ["<SELF_ASIN>"]`、`country`、`start_date`、`end_date`（注意返回日期可能逆序，代码必须按 `cycleDate.startDate` 升序排列后再切分前 7 天与近 7 天！）。

### 5. `get_asin_bsr_trends` (`xydc_traffic.json`)
查询近 14 天大类目与小类目每日 BSR 排名轨迹：
- 参数：`asin: "<SELF_ASIN>"`、`country`、`start_date`、`end_date`。提取 `categoryTree`（区分 `root == False` 叶子小类与 `root == True` 一级大类）及每日 `values[].rank`，按日期升序计算近 7 天 vs 前 7 天排名进退。

### 6. `get_asin_info_trends` (`deal_info.json`)
查询近 30 天每日价格分布（`display` 日常挂牌价、`deal` 秒杀价、`prime` 专享价）与优惠标记（`coupon`、`promotion`）：
- 参数：`asin: "<SELF_ASIN>"`、`country`、`start_date`、`end_date`（必须按 `date` 升序排序并以数据窗口 `anchor_date` 锚定近 7 天价格涨幅 `price_delta_7d_pct` 与秒杀结束过渡期 `is_deal_transition`）。
