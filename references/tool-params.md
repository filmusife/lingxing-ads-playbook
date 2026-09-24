# 领星广告工具参数

取数失败先 `search` 当前 toolId，再对照下面的已知坑。类型以 schema 为准，但领星 schema 经常和能跑通的值不一致。

## 公共

- `profile_ids`: **字符串数组**，如 `["<profile_id>"]`
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

### advertising_ad_analyze_keyword
这是目前能接到**搜索量代理**的只读工具。显示名「广告分析-关键词分析」，描述是搜索词分析。
必填：`start_date, end_date`（最大 31 天）。`asin` 与 `msku` 二选一；`sid` 与 `profile_id` 二选一。
实测更稳：`sid`（字符串）+ `asin` 数组 + `sponsored_type`（`sp`/`sb`）+ `page` + `limit`（最大 1000，脚本用 200）。
不要用 `report_date`。`order.column=searchrank` 会报「排序字段异常」，默认按 spends 降序即可。
关键返回字段：
- `keyword_text` / `key`：用于和投放词、搜索词 join
- `searchrank`：**ABA 搜索频率排名**，数字越小搜索量越大。这不是周搜索量绝对值。有的词没有该字段
- `impressions, clicks, ctr, cvr, spends, orders, acos`：该词跨活动汇总
- `localize_keyword_text`：中文对照，可辅助功能卡，不能代替属性表
没有 searchrank 时标 `DATA_MISSING`，用曝光作代理。禁止把 searchrank 当成每周搜索次数。
`query_erp_keyword_ranking_asin` / `_keyword` 只是已创建的排名监控目录，**不含搜索量**。对应写入工具 `create_erp_keyword` 默认不要调用。

### ad_campaign_targeting_report
必填含 `with_ring`（number/int `0`）以及 `page, length, sort_field, sort_type`。
`length` schema 可能是 string，传 `"100"` 或 `100` 都可以。
`portfolio_id` 用数组。

### ad_campaign_product_report
必填：`report_date, profile_id`（注意是单数 integer）。
同时传 `profile_ids` 更稳。用来确认哪些活动在投该 ASIN/MSKU。

### erp_listing
必填：`offset, length, pvi_ids`。
可用组合：

```
offset=0, length=20, pvi_ids="0", sids="<sid>", search_field="asin",
search_value=["B0XXXX"], exact_search="1"
```

`pvi_ids=""` 有时也能过。返回里要拿：标题、价格栈、stars、reviews、大类/小类 rank、父体、MSKU、可售、近 7/14/30 天销量与广告花费、**上架/首单、产品类型、在途**。

阶段卡必看字段：`open_date` / `open_date_time`、`first_order_time`、`reviews_num`、`stars`、`average_seven_volume`、`average_fourteen_volume`、`average_thirty_volume`。

价格栈必看：`landed_price`（成交/券后）、`listing_price`、`regular_price` / `list_price`、`history_price`。有券时不要用成交价 CVR 去扩发现。

功能卡辅助：`item_name`、`amz_product_type`、`category_text`、`title_differentiation`、`variant_text`。这些仍不能替代属性表；没有的功能不要从标题里脑补成“有”。

库存辅助：`afn_fulfillable_quantity`、`afn_inbound_working_quantity`、`afn_inbound_shipped_quantity`、`afn_inbound_receiving_quantity`、`afn_unsellable_quantity`、`reserved_customerorders`、`fba_fee`、`estimated_referral_fee`。

### get_fba_stock_list
`sid`（字符串）、`search_field="asin"`、`search_value`（字符串不是数组）、`offset=0`、`length=20`、`is_hide_zero_stock="0"`。
关注：`afn_fulfillable_quantity`、`available_total`、库龄、`short_term_historical_days_of_supply`、下月仓储费。

## 关键字段（分析时不要丢）

对用户写「基础竞价」，不要写 BID。API 字段仍是 `bid` / `default_bid`。

活动：`name, state, targeting_type, sponsored_type, daily_budget, bidding, placement_top, placement_product_page, placement_rest_of_search, default_bid, spends, sales, acos, cpc, cvr, ctr, impressions, clicks, orders, direct_orders, indirect_orders, top_of_search_impression_share`

`bidding.strategy`：`manual` 固定竞价。动态提高+降低会叠在位置溢价上，发现仓和新品不要用。`adjustments[].predicate`：`placementTop` / `placementProductPage` / `placementRestOfSearch`。

关键词：`campaign_name, keyword_text, match_type, bid, default_bid, state, spends, sales, orders, clicks, cvr, cpc, ctr`，分析表另接 `searchrank, vol_band, ka_impr, hint`

搜索词：`query, campaign_name, match_type/target_match_type, target_text/keyword_text, spends, sales, orders, clicks, cvr, cpc, is_asin, kw_negative, st_negative`，分析表另接 `searchrank, vol_band, ka_impr, hint`

投放：`exp_type` / `targeting_text`（紧密/宽泛/同类/关联/asinSameAs）、`bid`

Listing：`item_name, open_date, open_date_time, first_order_time, landed_price, listing_price, regular_price, history_price, stars, reviews_num, small_rank, rank, parent_asin, msku, amz_product_type, category_text, afn_fulfillable_quantity, inbound 数量, seven/fourteen/thirty_volume, seven/fourteen/thirty_spend, seven/fourteen/thirty_amount`

### analytics_log_list_v2
必填：`start_date, end_date, summary_type, page_size`。
`summary_type="asin"`，`search_field="asin"`，`search_value=[ASIN]`，`sids=[sid]`。
广告改动在 `auto_log_data[日期].log_data`：
- 501 `[广告活动] 修改...` → 锁这些活动的 TOS/PP/ROS/日预算 7 天
- 502 `[广告组] 修改...` → 锁默认基础竞价 7 天
- 504 `[投放] 修改N个` → 锁关键词/ASIN 基础竞价 7 天
- 505 `[否定投放] 新建N个` → 不锁溢价
拿不到 before/after 数值，只拿得到改动日和对象。历史 TOS/基础竞价不要从逐日报表的 placement/bid 字段读，那些是当前快照。

### 逐日活动报告（冷却佐证）
同一套 `ad_campaign_report`，`report_date` 改成单日 `YYYY-MM-DD - YYYY-MM-DD`，连续拉 7 天。
历史可用：spends, cpc, impressions, clicks, orders。
不可当历史：placement_top/product_page/rest_of_search, daily_budget, bid, default_bid, updated_at。

### 配置快照差分
分析脚本会把当前自变量写到 `~/.codex/skills/lingxing-ads-playbook/state/{profile_id}_{ASIN}.json`。
比对字段：TOS/PP/ROS、daily_budget、state、广告组 default_bid、关键词/投放 bid。
不比对 CPC、花费、曝光。那些是结果，不是控制变量。
第一次运行没有旧快照，只写入；第二次开始才产出 `config_diff.tsv`。
快照按 profile+ASIN 分文件，任意品类共用这套逻辑，互不串锁。
