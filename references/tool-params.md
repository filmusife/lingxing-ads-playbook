# 领星广告工具参数

取数失败先 `search` 当前 toolId，再对照下面的已知坑。类型以 schema 为准，但领星 schema 经常和能跑通的值不一致。

## 公共

- `profile_ids`: **字符串数组**，如 `["1234567890123456"]`
- `report_date`: `"2026-09-07 - 2026-09-20"`
- `sort_field`: `spends`；`sort_type`: `desc`
- `page`: 从 1 开始

## 各工具

### ad_auth_shops
无必填。

### ad_portfolio_report_shop / ad_campaign_report
必填：`report_date, profile_ids, page, length, sort_field, sort_type`。
`portfolio_id` schema 写 string；若筛选失败，改试单元素数组。

### ad_campaign_group_report
必填含 `with_ring`。传 **整数 `0`**，不要传 `false`。
`portfolio_id` 用数组。

### ad_campaign_keyword_report
必填：`report_date, profile_ids, page, length, sort_field, sort_type`。
`portfolio_id` 用数组。关键词可能 300+ 行，必须翻页。

### ad_campaign_search_term_report
必填：`report_date, profile_ids`。仍要带 `page, length, sort_field, sort_type`。
搜索词经常 800–2000 行，必须翻页。

### ad_campaign_targeting_report
必填含 `with_ring`（整数 `0`）以及 `page, length, sort_field, sort_type`。
注意：`length` 必须传字符串 `"100"`，`portfolio_id` 传字符串数组。

### ad_campaign_product_report
必填：`report_date, profile_id`（必须是单数整数 `int(pid)`）。
**特别注意**：绝不能传 `profile_ids`（数组），否则报 HTTP 422 参数错误；`with_ring` 传 `False`。用来确认哪些活动投放了该 ASIN/MSKU。

### erp_listing
必填：`offset, length, pvi_ids`。
可用组合：

```
offset=0, length=20, pvi_ids="0", sids="<sid>", search_field="asin",
search_value=["B0XXXXXXXX"], exact_search="1"
```

`pvi_ids=""` 有时也能过。返回里要拿：标题、价格、stars、reviews、大类/小类 rank、父体、MSKU、可售、近 7/14/30 天销量与广告花费。

### get_fba_stock_list
`sid`（字符串）、`search_field="asin"`、`search_value`（字符串不是数组）、`offset=0`、`length=20`、`is_hide_zero_stock="0"`。
关注：`afn_fulfillable_quantity`、`available_total`、库龄、`short_term_historical_days_of_supply`、下月仓储费。

### advertising_order_analysis_list (分时转化与出单洞察)
按 24 小时或按周拆解转化与出单，用于分时调价与分时预算策略。
- 必填：
  - `sid`: 店铺 ID（字符串，如 `"<SID>"`)
  - `sku`: SKU 列表（**必须是数组**，如 `["DEMO-SKU-001"]`）
  - `start_date`, `end_date`: 日期字符串（如 `"2026-08-27"`）
  - `group_type`: 枚举，`"hourly"` (24 小时分时) 或 `"weekly"` (按星期一至日)
- 关键输出：`spend, ad_orders, store_orders, cvr, acos, cpc, impression, clicks`。

### query_product_performance_asin_lists (Listing 宏观全域流量大盘)
获取 Listing 宏观自然流量、总访客与转化承接力。
- 必填：
  - `sids`: 店铺 ID（**字符串**，如 `"<SID>"`，不要传数组）
  - `search_field`: `"asin"`
  - `search_value`: ASIN 数组，如 `["B0XXXXXXXX"]`
  - `start_date`, `end_date`: 日期字符串
  - `offset`: 0, `length`: 10
- 关键输出：
  - `sessions_total` (总访客 Sessions), `sessions_mobile` (移动端访客)
  - `page_views_total` (总浏览量 Pageviews), `page_views_mobile`
  - `ad_cvr` (广告转化率) vs `nature_cvr` (自然转化率)
  - `adv_rate` (广告销量占比), `acoas` (全店 TACOS), `return_goods_rate` (退货率)

### query_order_profit_list (真实财务利润与保本 ACOS 核算)
精准获取产品单均采购成本、运费、FBA配送费与毛利，用于科学锚定保本 ACOS 天花板。
- 必填（参数类型要求极其严格）：
  - `start_date`, `end_date`: 日期字符串
  - `currency_type`: `"USD"`
  - `summary_field`: `"asin"`
  - `turn_on_summary`: `"0"` (字符串)
  - `sort_type`: `"desc"`
  - `length`: `"20"` (字符串), `offset`: `"0"` (字符串)
  - `source_service`: `"mcp"`, `external_service_mark`: `1` (数字)
  - `search_field`: `"asin"`, `search_value`: `["B0XXXXXXXX"]` (数组)
- 关键输出：
  - `net_amount` (净销售额), `volume` (销量), `gross_profit` (毛利润), `gross_margin` (毛利率)
  - `purchase_costs` (总采购成本 COGS) / `avg_purchase_costs`
  - `logistics_costs` (头程头运费) / `avg_logistics_costs`
  - `fba_fulfillment_fee` (FBA 配送费), `selling_fee` (平台佣金), `refund_amount_rate`
  - **保本 ACOS 计算公式**：$\text{Break-Even ACOS} = \frac{\text{客单价} - \text{单均COGS} - \text{单均头程} - \text{单均FBA} - \text{单均佣金}}{\text{客单价}} \times 100\%$

### ad_campaign_search_term_report (增强高级参数)
- `with_translation`: 设为 `1`，自动对西语、德语等小语种搜索词提供英语/中文翻译。
- `category`: 设为 `"HasClickNoDeal"`，直接精准过滤出高花费 0 转化、急需止血的出血词。
- `targeted_type`: 设为 `"not_targeted"`，过滤出全账户尚未建立投放、急需收割的高潜转化词。

## 关键字段（分析时不要丢）

对用户写「基础竞价」，不要写 BID。API 字段仍是 `bid` / `default_bid`。

活动：`name, state, targeting_type, sponsored_type, daily_budget, bidding, placement_top, placement_product_page, placement_rest_of_search, default_bid, spends, sales, acos, cpc, cvr, ctr, impressions, clicks, orders, direct_orders, indirect_orders, top_of_search_impression_share`

`bidding.strategy`：`manual` 固定竞价。`adjustments[].predicate`：`placementTop` / `placementProductPage` / `placementRestOfSearch`。

关键词：`campaign_name, keyword_text, match_type, bid, default_bid, state, spends, sales, orders, clicks, cvr, cpc, ctr`

搜索词：`query, campaign_name, match_type/target_match_type, target_text/keyword_text, spends, sales, orders, clicks, cvr, cpc, is_asin, kw_negative, st_negative`

投放：`exp_type` / `targeting_text`（紧密/宽泛/同类/关联/asinSameAs）、`bid`

Listing & 财务：`item_name, listing_price, stars, reviews_num, small_rank, rank, parent_asin, msku, afn_fulfillable_quantity, seven/fourteen/thirty_volume, seven/fourteen/thirty_spend, seven/fourteen/thirty_amount, gross_margin, cogs, break_even_acos, sessions_total, ad_cvr, nature_cvr, tacos`
