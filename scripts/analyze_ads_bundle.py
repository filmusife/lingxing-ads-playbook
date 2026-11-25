# -*- coding: utf-8 -*-
"""Turn fetched Lingxing JSON into product-fit analysis tables.

Upgraded features:
- Dynamic product-fit rule injection (decoupled category hardcoding)
- N-Gram bleeding phrase root analysis (for negative phrase discovery)
- Cross-campaign cannibalization and internal competition detection
- Campaign placement audit (TOS/PP/ROS health check)
- Actionable bulk adjustments table generation (bulk_adjustments.tsv)
- Control-variable snapshot diff & cooling-off protection (Config Snapshot Diff & Observation Lock)

User-facing copy should say 基础竞价, never BID. Default evidence window is 30 days.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lx_client import rows as lx_rows
import v2_policy as v2p


STOP_WORDS = {
    # English
    "the", "a", "an", "and", "or", "for", "with", "in", "on", "at", "to", "from",
    "by", "of", "is", "it", "as", "be", "this", "that", "are", "was", "were",
    "my", "your", "its", "into", "over", "after", "before", "between", "under",
    "all", "any", "both", "each", "few", "more", "most", "other", "some", "such",
    # German
    "der", "die", "das", "und", "oder", "fur", "für", "mit", "von", "aus", "auf", "in", "zu",
    # French
    "le", "la", "les", "et", "ou", "pour", "avec", "de", "des", "du", "en", "sur",
    # Spanish
    "el", "la", "los", "las", "y", "o", "para", "por", "con", "de", "del", "en",
    # Italian
    "il", "la", "lo", "i", "gli", "le", "e", "o", "per", "con", "di", "da", "in", "su"
}


def as_rows(obj):
    """Pull the row list out of a Lingxing payload, whatever wrapper depth it arrived in."""
    return lx_rows(obj)


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ceil_cents(val: float) -> float:
    """Round up to nearest cent so downward bid step-caps never exceed -20% / -10% due to rounding."""
    return math.ceil(float(val) * 100.0 - 1e-9) / 100.0


def _floor_cents(val: float) -> float:
    """Round down to nearest cent so upward bid step-caps never exceed +20% / +15% due to rounding."""
    return math.floor(float(val) * 100.0 + 1e-9) / 100.0


def num(x, default=0.0):
    if x is None or x == "" or x == "--":
        return default
    try:
        s = str(x).replace("%", "").replace(",", "").replace("$", "").replace("¥", "").replace("€", "").replace("£", "").strip()
        return float(s)
    except Exception:
        return default


def acos(spend, sales):
    sp = num(spend)
    sl = num(sales)
    if sl <= 0:
        return 999.0 if sp > 0 else 0.0
    return sp / sl * 100.0


def clean_api_acos(raw_acos, spend: float = 0.0, sales: float = 0.0, zero_sales_sentinel: float = 999.0) -> float:
    """Sanitize LingXing API acos where 0-order spend returns 99999999.0 or misleading 0.0."""
    sp = num(spend)
    sl = num(sales)
    if sl > 0:
        return round(sp / sl * 100.0, 2)
    if sp > 0:
        return zero_sales_sentinel
    v = num(raw_acos)
    if 0.0 < v < 5000.0:
        return round(v, 2)
    return 0.0


def clean_api_rate(raw_val, max_valid: float = 1000.0) -> float:
    """Sanitize LingXing API rate/CPC fields (cvr, ctr, cpc) that return 99999999 when denominator is 0."""
    v = num(raw_val)
    if 0.0 <= v < max_valid:
        return v
    return 0.0


def fmt_acos_text(acos_val: float, spend: float = 0.0, sales_or_orders: float = 0.0, decimals: int = 1, zero_spend_text: str = "--") -> str:
    """Human-facing ACOS formatter: 0-order spend always renders as '-- (0单)', never sentinel numbers."""
    sp = num(spend)
    so = num(sales_or_orders)
    ac = num(acos_val)
    if so > 0 and 0.0 < ac < 900.0:
        return f"{ac:.{decimals}f}%"
    if sp > 0 or ac >= 900.0:
        return "-- (0单)"
    return f"{ac:.{decimals}f}%" if ac > 0 else zero_spend_text


def calc_roas(spend: float, sales: float, raw_roas=None) -> float:
    sp = num(spend)
    sl = num(sales)
    if sp > 0 and sl >= 0:
        return round(sl / sp, 2)
    v = num(raw_roas)
    return round(v, 2) if 0.0 < v < 1000.0 else 0.0


ASIN_TERM_RE = re.compile(r"^b0[0-9a-z]{8}$", re.I)

# Global Pre-Flight Compliance & Hard Category-Conflict Firewall (Item 8)
GLOBAL_POLICY_BLOCKED_RE = re.compile(
    r"\b(?:weed|weeds|marijuana|cannabis|thc|cbd|bong|bongs|kief|herb\s+weed|kratom|psilocybin|shrooms?|tobacco|cigarette|vape|vaping|joint\s+roller|blunt\s+grinder|hash\s+grinder)\b",
    re.I,
)

GLOBAL_HARD_DOMAIN_CONFLICT_PATS = (
    (r"\b(?:angle|bench|die|stump|concrete|floor|metal|valve|chainsaw|woodworking)\s+grinders?\b", "is_not_industrial_tool"),
    (r"\b(?:meat|bone|sausage)\s+grinders?\b", "is_not_meat_grinder"),
    (r"\b(?:dog|cat|pet|baby|toe)?\s*nail\s+grinders?\b", "is_not_nail_grinder"),
    (r"\b(?:pill|tablet|medication)\s+(?:grinders?|crushers?)\b", "is_not_pill_crusher"),
)

GENERIC_APPLIANCE_HEAD_NOUNS = {
    "grinder", "grinders", "mill", "mills", "maker", "makers", "machine", "machines",
    "kettle", "kettles", "blender", "blenders", "mixer", "mixers", "chopper", "choppers",
    "crusher", "crushers", "pot", "pots", "cup", "cups", "electric", "stainless", "steel",
    "black", "white", "silver", "small", "mini", "portable", "kitchen", "home", "appliance",
}

POSITIVE_RECOMMENDATION_ACTIONS = {
    "Unpause_Keyword",
    "Harvest_To_Exact",
    "Converge_Harvest_To_Exact",
    "Elevate_To_TOS",
    "Boost_Ascender_TOS",
    "Probe_Bid_Recovery",
    "Remove_Negative",
}


def check_global_compliance_firewall(text: str, profile: dict | None = None, rules: dict | None = None) -> tuple[bool, str, str]:
    """Return (is_blocked, bucket_label, matched_reason) if text violates Amazon policy or hard physical/category gates."""
    ql = (text or "").lower().strip()
    if not ql:
        return False, "", ""
    m_pol = GLOBAL_POLICY_BLOCKED_RE.search(ql)
    if m_pol:
        return True, "is_not_policy_blocked", f"policy_blocked:{m_pol.group(0).lower()}"
    custom_pol_re = v2p.build_restricted_terms_regex(rules)
    if custom_pol_re:
        m_cpol = custom_pol_re.search(ql)
        if m_cpol:
            return True, "is_not_policy_blocked", f"policy_blocked_custom:{m_cpol.group(0).lower()}"

    raw_title = str((profile or {}).get("title") or "").lower()
    positive_title = re.sub(r"\b(?:no|not|without|non)[-\s]+\w+(?:\s+\w+)?", " ", raw_title)
    positive_title = re.sub(r"\b\w+[-\s]+free\b", " ", positive_title)

    for pat, lbl in GLOBAL_HARD_DOMAIN_CONFLICT_PATS:
        m_dom = re.search(pat, ql, re.I)
        if m_dom and (not positive_title or not re.search(pat, positive_title, re.I)):
            return True, lbl, f"domain_conflict:{m_dom.group(0).lower()}"

    if positive_title:
        # Title-gated hard mechanism/material contradictions when absent from positive title
        title_gated_conflicts = [
            (r"\b(?:manual|hand\s+crank|non[-\s]?electric)\b", "is_not_manual_mechanism", r"\b(?:manual|hand\s+crank)\b"),
            (r"\b(?:no\s+plastic|plastic[-\s]+free|zero\s+plastic|all\s+glass|all\s+metal)\b", "is_not_material_claim", r"\b(?:no\s+plastic|plastic[-\s]+free|zero\s+plastic|all\s+glass|all\s+metal)\b"),
        ]
        # Blade vs Burr strict gate: if listing title says blade and NOT burr, burr is a hard physical misfit
        if re.search(r"\bblade\b", positive_title) and not re.search(r"\bburr\b", positive_title):
            title_gated_conflicts.append((r"\b(?:burr|conical\s+burr|flat\s+burr)\b", "is_not_burr_mechanism", r"\bburr\b"))
        for q_pat, lbl, t_pat in title_gated_conflicts:
            m_tg = re.search(q_pat, ql, re.I)
            if m_tg and not re.search(t_pat, positive_title, re.I):
                return True, lbl, f"title_gated:{m_tg.group(0).lower()}"

    if isinstance(rules, dict):
        for item in rules.get("is_not_patterns", []) or []:
            pat = item if isinstance(item, str) else item.get("pattern", "")
            label = "is_not" if isinstance(item, str) else item.get("label", "is_not")
            if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
                if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", ql, re.I):
                    continue
            if pat:
                try:
                    if re.search(pat, ql, re.I):
                        return True, label, pat
                except re.error:
                    pass
    return False, "", ""


def is_asin_term(text: str) -> bool:
    return bool(ASIN_TERM_RE.match((text or "").strip()))


def skip_total(row: dict) -> bool:
    if not isinstance(row, dict):
        return True
    k_val = str(row.get("key") or row.get("key_table") or "").strip().lower()
    if k_val in ("sub_total", "total", "summary", "汇总", "合计"):
        return True
    # Campaign report summary row has name=None and campaign_id=None
    if ("placement_top" in row or "bidding_type" in row) and not row.get("name") and not row.get("campaign_id"):
        return True
    # Targets / Keywords / Search terms / Ad groups summary rows have null campaign_name and null entity fields
    has_entity_text = any(
        row.get(f) is not None and str(row.get(f)).strip() not in ("", "None", "null", "汇总", "合计")
        for f in ("name", "campaign_name", "keyword_text", "query", "targeting_text", "expression", "ad_group_name", "asin", "advertised_asin")
    )
    has_entity_id = any(
        row.get(f) is not None and str(row.get(f)).strip() not in ("", "None", "null", "0")
        for f in ("campaign_id", "keyword_id", "target_id", "ad_group_id")
    )
    return not has_entity_text and not has_entity_id


def load_product_rules(rule_path: Path | None, indir: Path) -> dict:
    candidates = []
    if rule_path and rule_path.exists():
        candidates.append(rule_path)
    candidates.extend([
        indir / "product_rules.json",
        indir / "rules.json",
    ])
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {
        "is_patterns": [],
        "is_not_patterns": [],
        "competitor_brands": [],
        "adjacent_patterns": [],
        "target_acos": None,
    }


def sanitize_product_rules(rules: dict, profile: dict) -> dict:
    """Quarantine any self-contradictory is_not_patterns that match the product's own positive title nouns.

    Prevents catastrophic false-positive Pause_Keyword / misfit classification when a rule file
    accidentally includes a primary product noun (e.g. 'grinder' on a Coffee Grinder listing),
    while preserving legitimate negated-claim exclusions (e.g. 'plastic' when title says 'plastic free').
    """
    if not isinstance(rules, dict):
        return rules
    raw_title = str(profile.get("title") or "").lower()
    if not raw_title.strip():
        return rules

    # Strip negated claims from title before checking positive self-overlap
    positive_title = re.sub(r"\b(?:no|not|without|non)[-\s]+\w+(?:\s+\w+)?", " ", raw_title)
    positive_title = re.sub(r"\b\w+[-\s]+free\b", " ", positive_title)

    safe_is_not = []
    quarantined = []
    for item in rules.get("is_not_patterns", []) or []:
        pat = item if isinstance(item, str) else item.get("pattern", "")
        if pat:
            try:
                if re.search(pat, positive_title, re.I):
                    quarantined.append(item)
                    continue
            except re.error:
                quarantined.append(item)
                continue

            # Quarantine consumable is_not patterns that conflict with explicit positive is_patterns
            is_pattern_conflict = False
            for is_item in rules.get("is_patterns", []) or []:
                is_pat = is_item if isinstance(is_item, str) else is_item.get("pattern", "")
                if is_pat:
                    is_sample = re.sub(r"[\^\$\b\\]", "", is_pat).replace(r"\s+", " ").strip()
                    try:
                        if re.search(pat, is_sample, re.I):
                            lbl = item.get("label", "") if isinstance(item, dict) else ""
                            if lbl.startswith("is_not_coffee_beans") or "beans" in lbl or "consumable" in lbl:
                                is_pattern_conflict = True
                                break
                    except re.error:
                        pass
            if is_pattern_conflict:
                quarantined.append(item)
                continue

        safe_is_not.append(item)

    out = dict(rules)
    out["is_not_patterns"] = safe_is_not
    if quarantined:
        out["quarantined_self_title_patterns"] = quarantined
    return out


def _parse_window_bounds(meta: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(meta, dict):
        return None, None
    w = meta.get("window")
    if isinstance(w, dict):
        s_dt = str(w.get("start_date") or meta.get("start_date") or "").strip()[:10] or None
        e_dt = str(w.get("end_date") or meta.get("end_date") or "").strip()[:10] or None
        return s_dt, e_dt
    w_str = str(w or "").strip()
    parts = [p.strip() for p in w_str.split(" - ") if p.strip()]
    if len(parts) == 2:
        return parts[0][:10], parts[1][:10]
    s_fb = str(meta.get("start_date") or "").strip()[:10] or None
    e_fb = str(meta.get("end_date") or "").strip()[:10] or None
    return s_fb, e_fb


def _select_store_row(rows: list[dict], sid: str | int | None) -> dict:
    if not rows:
        return {}
    sid_str = str(sid or "").strip()
    if sid_str and sid_str not in ("None", "0", ""):
        # Pass 1: Prefer an exact single-store row [sid_str] over a multi-store aggregated summary row [1001, 1002]
        for r in rows:
            if not isinstance(r, dict):
                continue
            r_sids = [str(x).strip() for x in (r.get("sids") or []) if str(x).strip()]
            r_sid_single = str(r.get("sid") or "").strip()
            if (len(r_sids) == 1 and r_sids[0] == sid_str) or (not r_sids and r_sid_single == sid_str):
                return r
        # Pass 2: Fallback to multi-store summary row containing sid_str
        for r in rows:
            if not isinstance(r, dict):
                continue
            r_sids = [str(x).strip() for x in (r.get("sids") or []) if str(x).strip()]
            if sid_str in r_sids or str(r.get("sid") or "").strip() == sid_str:
                return r
    return rows[0] if isinstance(rows[0], dict) else {}


def _extract_row_ad_costs(r: dict) -> dict[str, float]:
    sp_cost = abs(num(r.get("ads_sp_cost") if r.get("ads_sp_cost") is not None else r.get("sp_cost")))
    sb_cost = abs(num(r.get("ads_sb_cost") if r.get("ads_sb_cost") is not None else r.get("sb_cost"))) + abs(num(r.get("shared_ads_sb_cost")))
    sbv_cost = abs(num(r.get("ads_sbv_cost") if r.get("ads_sbv_cost") is not None else r.get("sbv_cost"))) + abs(num(r.get("shared_ads_sbv_cost")))
    sd_cost = abs(num(r.get("ads_sd_cost") if r.get("ads_sd_cost") is not None else r.get("sd_cost"))) + abs(num(r.get("shared_ads_sd_cost")))
    shared_other = abs(num(r.get("shared_cost_of_advertising")))
    summed = sp_cost + sb_cost + sbv_cost + sd_cost + shared_other
    explicit_spend = abs(num(r.get("spend") or r.get("cost_of_advertising")))
    total_ad = max(explicit_spend, summed) if explicit_spend > 0 else summed
    return {
        "total_ad_cost": round(total_ad, 2),
        "sp_ad_cost": round(sp_cost, 2),
        "sb_sbv_ad_cost": round(sb_cost + sbv_cost, 2),
        "sd_ad_cost": round(sd_cost, 2),
    }


def load_financial_profile(indir: Path, meta: dict | None = None) -> dict:
    rows = [r for r in as_rows(load_json(indir / "order_profit.json")) if isinstance(r, dict)]
    if not rows:
        return {}

    target_sid = (meta or {}).get("sid")
    r = _select_store_row(rows, target_sid)
    if not r:
        return {}

    total_sales = num(r.get("net_amount") or r.get("amount") or r.get("total_sales_amount"))
    gross_profit = num(r.get("gross_profit") if r.get("gross_profit") is not None else r.get("order_gross_profit"))
    raw_gm = num(r.get("gross_margin") if r.get("gross_margin") is not None else r.get("net_gross_margin"))
    # Bug fix: cross-check raw_gm against gross_profit / total_sales so decimal ratios < -1.0 (e.g. -2.7248 = -272.48%)
    # are properly scaled by 100 while percentage values (e.g. -17.3) are kept as-is!
    if total_sales > 0 and raw_gm != 0:
        expected_ratio = gross_profit / total_sales
        if abs(raw_gm - expected_ratio) <= 0.08:
            gross_margin_pct = round(raw_gm * 100.0, 2)
        elif abs(raw_gm - expected_ratio * 100.0) <= 5.0:
            gross_margin_pct = round(raw_gm, 2)
        else:
            gross_margin_pct = round(expected_ratio * 100.0, 2)
    else:
        gross_margin_pct = round(raw_gm * (100.0 if abs(raw_gm) <= 1.0 else 1.0), 2)

    purchase_costs = abs(num(r.get("purchase_costs") if r.get("purchase_costs") is not None else r.get("cg_price")))
    afn_purchase_costs = abs(num(r.get("afn_purchase_costs")))
    logistics_costs = abs(num(r.get("logistics_costs") if r.get("logistics_costs") is not None else r.get("cg_transport_costs")))
    fba_fulfillment_fee = abs(num(r.get("fba_fulfillment_fee") if r.get("fba_fulfillment_fee") is not None else r.get("fba_delivery_fee")))
    selling_fee = abs(num(r.get("selling_fee") if r.get("selling_fee") is not None else r.get("commission_amount")))
    storage_base = r.get("fba_storage_fee") if r.get("fba_storage_fee") is not None else r.get("monthly_storage_fee")
    if storage_base is None:
        storage_base = r.get("storage_fee")
    # Bug fix: LingXing order_profit stores FBA monthly & long-term storage fees in total_stock_fee / shared_fba_storage_fee
    explicit_storage = (
        abs(num(storage_base))
        + abs(num(r.get("long_term_storage_fee")))
        + abs(num(r.get("shared_fba_storage_fee")))
        + abs(num(r.get("shared_long_term_storage_fee")))
    )
    storage_fee_total = max(abs(num(r.get("total_stock_fee"))), explicit_storage)

    vol = num(r.get("volume") or r.get("order_items_quantity") or r.get("total_sales_quantity"))
    afn_vol = num(r.get("afn_volume") or r.get("afn_quantity")) or vol
    avg_price = (total_sales / vol) if vol > 0 else 0.0
    cogs_per_unit = (purchase_costs / vol) if vol > 0 else abs(num(r.get("avg_purchase_costs")))
    afn_cogs_per_unit = (afn_purchase_costs / afn_vol) if (afn_purchase_costs > 0 and afn_vol > 0) else 0.0
    logistics_per_unit = (logistics_costs / vol) if vol > 0 else abs(num(r.get("avg_logistics_costs")))
    fba_per_unit = (fba_fulfillment_fee / vol) if vol > 0 else 0.0
    comm_per_unit = (selling_fee / vol) if vol > 0 else 0.0
    storage_per_unit = (storage_fee_total / vol) if vol > 0 else 0.0

    # Complete financial deductions extraction (Item 1)
    refund_amount = round(abs(num(r.get("refund_amount"))), 2)
    fba_return_goods_count = int(abs(num(r.get("fba_return_goods_count") or r.get("return_count"))))
    raw_unit_ret = num(r.get("return_rate"))
    unit_return_rate_pct = (
        round((fba_return_goods_count / vol * 100.0), 2)
        if vol > 0 and fba_return_goods_count > 0
        else round(abs(raw_unit_ret) * (100.0 if abs(raw_unit_ret) <= 1.0 else 1.0), 2)
    )
    promotion_discount = round(abs(num(r.get("promotion_discount"))), 2)
    promotion_fee = round(abs(num(r.get("promotion_fee"))), 2)
    return_unsellable_purchase_costs = round(abs(num(r.get("return_unsellable_purchase_costs"))), 2)
    shared_fba_disposal_fee = round(abs(num(r.get("shared_fba_disposal_fee"))), 2)
    adjustments_fee = round(num(r.get("adjustments_fee")), 2)

    # Detect ERP COGS missing (<=0) or currency/entry anomalies (> avg_price * 1.05) — v2 §2.2
    effective_cogs_per_unit = cogs_per_unit
    cogs_anomaly_detected = False
    cogs_data_missing = False
    cogs_is_estimated = False
    cogs_source = "erp_actual"
    cogs_anomaly_warning = ""
    data_missing_flags: list[str] = []

    if avg_price > 0 and vol > 0 and (cogs_per_unit > avg_price * 1.05 or cogs_per_unit <= 0.0):
        is_missing_zero = (cogs_per_unit <= 0.0)
        cogs_anomaly_detected = not is_missing_zero
        if 0 < afn_cogs_per_unit <= avg_price * 0.80:
            effective_cogs_per_unit = afn_cogs_per_unit
            cogs_source = "afn_real_shipment"
            if is_missing_zero:
                cogs_anomaly_warning = (
                    f"ERP主表采购成本缺失 ($0.00/件)，已自动采用FBA实发采购成本 "
                    f"(${afn_cogs_per_unit:.2f}/件, afn_volume={int(afn_vol)}) 补齐核算"
                )
            else:
                cogs_anomaly_warning = (
                    f"ERP总采购成本异常 (${cogs_per_unit:.2f}/件 > 售价${avg_price:.2f}，疑为币种或批次录入偏差)，"
                    f"经营毛利与保本线已自动采用FBA实发采购成本 (${afn_cogs_per_unit:.2f}/件, afn_volume={int(afn_vol)}) 同步修正核算"
                )
        else:
            cogs_data_missing = True
            cogs_is_estimated = True
            cogs_source = "ESTIMATED_28PCT_FALLBACK"
            data_missing_flags.append("DATA_MISSING: COGS")
            effective_cogs_per_unit = round(avg_price * 0.28, 2)
            if is_missing_zero:
                cogs_anomaly_warning = (
                    f"DATA_MISSING: COGS（ERP采购成本缺失 $0.00/件，请用户确认真实采购价）；"
                    f"暂以估算口径 (客单价×28% = ${effective_cogs_per_unit:.2f}/件) 保守试算，已冻结一切加码类动作"
                )
            else:
                cogs_anomaly_warning = (
                    f"DATA_MISSING: COGS（ERP采购成本异常 ${cogs_per_unit:.2f}/件 > 售价${avg_price:.2f} 且无有效实发COGS，请用户确认真实采购价）；"
                    f"暂以估算口径 (客单价×28% = ${effective_cogs_per_unit:.2f}/件) 保守试算，已冻结一切加码类动作"
                )

    if (cogs_anomaly_detected or cogs_source != "erp_actual") and vol > 0:
        adjusted_gross_profit = round(gross_profit + (purchase_costs - effective_cogs_per_unit * vol), 2)
        adjusted_gross_margin = round((adjusted_gross_profit / total_sales * 100.0), 2) if total_sales > 0 else 0.0
    else:
        adjusted_gross_profit = round(gross_profit, 2)
        adjusted_gross_margin = gross_margin_pct

    break_even_profit = avg_price - effective_cogs_per_unit - logistics_per_unit - fba_per_unit - comm_per_unit
    raw_be_acos = (break_even_profit / avg_price * 100.0) if avg_price > 0 else 0.0
    marginal_break_even_acos = round(raw_be_acos, 2)
    break_even_acos = round(raw_be_acos, 1)
    break_even_acos_after_storage = round(((break_even_profit - storage_per_unit) / avg_price * 100.0), 1) if avg_price > 0 else 0.0

    raw_refund = num(r.get("refund_amount_rate") or r.get("return_rate"))
    refund_rate_pct = round(raw_refund * (100.0 if abs(raw_refund) <= 1.0 else 1.0), 2)
    ad_costs = _extract_row_ad_costs(r)

    # Strict accounting identity all-in break-even TACOS & unit contribution margin (v2 §2.1)
    pre_ad_net_contribution = round(adjusted_gross_profit + ad_costs["total_ad_cost"], 2)
    true_all_in_break_even_tacos = round(pre_ad_net_contribution / total_sales * 100.0, 2) if total_sales > 0 else 0.0
    cm_unit = round(pre_ad_net_contribution / vol, 2) if vol > 0 else round(break_even_profit, 2)
    cm_pct = true_all_in_break_even_tacos if total_sales > 0 else marginal_break_even_acos
    refund_loss_per_unit = round((refund_amount + return_unsellable_purchase_costs) / vol, 2) if vol > 0 else 0.0
    promo_per_unit = round((promotion_discount + promotion_fee) / vol, 2) if vol > 0 else 0.0
    zero_ad_loss_circuit_breaker = bool(total_sales > 0 and (pre_ad_net_contribution <= 0.0 or true_all_in_break_even_tacos <= 0.0))
    ad_unsustainable_zone = bool(total_sales > 0 and (pre_ad_net_contribution <= 0.0 or true_all_in_break_even_tacos < 8.0))

    # Cross-shop shared ASIN aggregation without double-counting multi-sid summary rows
    single_store_rows: dict[str, dict] = {}
    multi_store_rows: list[tuple[list[str], dict]] = []
    for row_item in rows:
        r_sids = sorted({str(x).strip() for x in (row_item.get("sids") or []) if str(x).strip()})
        if not r_sids and str(row_item.get("sid") or "").strip():
            r_sids = [str(row_item.get("sid")).strip()]
        if len(r_sids) == 1:
            single_store_rows.setdefault(r_sids[0], row_item)
        elif len(r_sids) > 1:
            multi_store_rows.append((r_sids, row_item))

    all_distinct_sids = set(single_store_rows.keys())
    for m_sids, _ in multi_store_rows:
        all_distinct_sids.update(m_sids)

    is_cross_shop = len(all_distinct_sids) > 1
    if len(single_store_rows) >= 2:
        agg_source = list(single_store_rows.values())
        omni_sales = sum(num(x.get("net_amount") or x.get("amount") or x.get("total_sales_amount")) for x in agg_source)
        omni_vol = int(sum(num(x.get("volume") or x.get("order_items_quantity") or x.get("total_sales_quantity")) for x in agg_source))
        omni_gp = sum(num(x.get("gross_profit") if x.get("gross_profit") is not None else x.get("order_gross_profit")) for x in agg_source)
        omni_ad = sum(_extract_row_ad_costs(x)["total_ad_cost"] for x in agg_source)
    elif multi_store_rows:
        # Only multi-sid summary row returned by API
        _, top_multi = max(multi_store_rows, key=lambda item: len(item[0]))
        omni_sales = num(top_multi.get("net_amount") or top_multi.get("amount") or top_multi.get("total_sales_amount"))
        omni_vol = int(num(top_multi.get("volume") or top_multi.get("order_items_quantity") or top_multi.get("total_sales_quantity")))
        omni_gp = num(top_multi.get("gross_profit") if top_multi.get("gross_profit") is not None else top_multi.get("order_gross_profit"))
        omni_ad = _extract_row_ad_costs(top_multi)["total_ad_cost"]
    else:
        omni_sales, omni_vol, omni_gp, omni_ad = total_sales, int(vol), adjusted_gross_profit, ad_costs["total_ad_cost"]

    real_tacos_pct = round((ad_costs["total_ad_cost"] / total_sales * 100.0), 2) if total_sales > 0 else 0.0

    result = {
        "matched_sids": r.get("sids") or ([str(target_sid)] if target_sid else []),
        "total_sales": round(total_sales, 2),
        "volume": int(vol),
        "afn_volume": int(afn_vol),
        "avg_price": round(avg_price, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_margin": gross_margin_pct,
        "adjusted_gross_profit": adjusted_gross_profit,
        "adjusted_gross_margin": adjusted_gross_margin,
        "cogs_anomaly_detected": cogs_anomaly_detected,
        "cogs_data_missing": cogs_data_missing,
        "cogs_is_estimated": cogs_is_estimated,
        "cogs_source": cogs_source,
        "data_missing_flags": data_missing_flags,
        "cogs_per_unit": round(cogs_per_unit, 2),
        "afn_cogs_per_unit": round(afn_cogs_per_unit, 2),
        "effective_cogs_per_unit": round(effective_cogs_per_unit, 2),
        "logistics_per_unit": round(logistics_per_unit, 2),
        "fba_per_unit": round(fba_per_unit, 2),
        "comm_per_unit": round(comm_per_unit, 2),
        "storage_per_unit": round(storage_per_unit, 2),
        "refund_loss_per_unit": refund_loss_per_unit,
        "promo_per_unit": promo_per_unit,
        "marginal_cm_unit": round(break_even_profit, 2),
        "cm_unit": cm_unit,
        "cm_pct": cm_pct,
        "decision_break_even_acos": cm_pct,
        "storage_fee_total": round(storage_fee_total, 2),
        "refund_amount": refund_amount,
        "refund_rate": refund_rate_pct,
        "fba_return_goods_count": fba_return_goods_count,
        "unit_return_rate_pct": unit_return_rate_pct,
        "promotion_discount": promotion_discount,
        "promotion_fee": promotion_fee,
        "return_unsellable_purchase_costs": return_unsellable_purchase_costs,
        "shared_fba_disposal_fee": shared_fba_disposal_fee,
        "adjustments_fee": adjustments_fee,
        "total_ad_cost": ad_costs["total_ad_cost"],
        "sp_ad_cost": ad_costs["sp_ad_cost"],
        "sb_sbv_ad_cost": ad_costs["sb_sbv_ad_cost"],
        "sd_ad_cost": ad_costs["sd_ad_cost"],
        "real_tacos_pct": real_tacos_pct,
        "pre_ad_net_contribution": pre_ad_net_contribution,
        "true_all_in_break_even_tacos": true_all_in_break_even_tacos,
        "marginal_break_even_acos": marginal_break_even_acos,
        "zero_ad_loss_circuit_breaker": zero_ad_loss_circuit_breaker,
        "ad_unsustainable_zone": ad_unsustainable_zone,
        "break_even_acos": round(break_even_acos, 1),
        "break_even_acos_raw": round(raw_be_acos, 1),
        "break_even_acos_after_storage": break_even_acos_after_storage,
        "is_cross_shop_asin": is_cross_shop,
    }
    if cogs_anomaly_warning:
        result["cogs_anomaly_warning"] = cogs_anomaly_warning
    if is_cross_shop:
        omni_pre_ad = round(omni_gp + omni_ad, 2)
        omni_be_tacos = round((omni_pre_ad / omni_sales * 100.0), 2) if omni_sales > 0 else 0.0
        result["omni_shop_summary"] = {
            "shop_count": len(all_distinct_sids),
            "sids": sorted(all_distinct_sids),
            "omni_total_sales": round(omni_sales, 2),
            "omni_volume": omni_vol,
            "omni_gross_profit": round(omni_gp, 2),
            "omni_total_ad_cost": round(omni_ad, 2),
            "omni_pre_ad_net_contribution": omni_pre_ad,
            "omni_true_all_in_break_even_tacos": omni_be_tacos,
            "omni_tacos": round((omni_ad / omni_sales * 100.0), 2) if omni_sales > 0 else 0.0,
        }
    return result


def load_traffic_profile(indir: Path, db_dir: Path | None = None, financial_prof: dict | None = None, rules: dict | None = None) -> dict:
    rows = as_rows(load_json(indir / "product_performance.json"))
    r = rows[0] if rows else {}
    total_sessions = int(num(r.get("sessions_total") or r.get("sessions")))
    total_pageviews = int(num(r.get("page_views_total") or r.get("page_views")))

    # Direction 4 & Rule 46: Graceful Fallback from Historical Snapshots when Traffic API Drops
    is_fallback = False
    if not r or total_sessions == 0:
        candidate_dirs = []
        if db_dir:
            candidate_dirs.append(Path(db_dir) / "daily_records")
        candidate_dirs.append(indir.parent.parent / "db" / "daily_records")
        candidate_dirs.append(indir.parent / "db" / "daily_records")

        for cdir in candidate_dirs:
            if cdir.exists():
                daily_files = sorted([f for f in cdir.glob("*.json") if not f.name.endswith("_snap.json")])
                for df in reversed(daily_files):
                    try:
                        d_data = json.loads(df.read_text(encoding="utf-8"))
                        prev_trf = d_data.get("traffic")
                        if prev_trf and isinstance(prev_trf, dict) and prev_trf.get("sessions_total", 0) > 0:
                            r = {
                                "sessions_total": prev_trf.get("sessions_total"),
                                "page_views_total": prev_trf.get("page_views_total"),
                                "sessions_mobile": prev_trf.get("sessions_mobile"),
                                "page_views_mobile": prev_trf.get("page_views_mobile"),
                                "ad_cvr": (prev_trf.get("ad_cvr") or 0) / 100.0,
                                "nature_cvr": (prev_trf.get("nature_cvr_raw") or 0) / 100.0,
                                "cvr": (prev_trf.get("total_cvr") or 0) / 100.0,
                                "adv_rate": (prev_trf.get("adv_sales_share") or 0) / 100.0,
                                "acoas": (prev_trf.get("tacos") or 0) / 100.0,
                                "return_goods_rate": (prev_trf.get("return_goods_rate") or 0) / 100.0,
                            }
                            total_sessions = int(num(r.get("sessions_total")))
                            total_pageviews = int(num(r.get("page_views_total")))
                            is_fallback = True
                            break
                    except Exception:
                        continue
                if is_fallback:
                    break

    if not r or total_sessions == 0:
        tacos_fin = 0.0
        if financial_prof and financial_prof.get("total_sales", 0) > 0:
            tacos_fin = round(financial_prof.get("total_ad_cost", 0) / financial_prof["total_sales"] * 100, 2)
        return {
            "sessions_total": 0,
            "page_views_total": 0,
            "sessions_mobile": 0,
            "page_views_mobile": 0,
            "ad_cvr": 0.0,
            "nature_cvr": 0.0,
            "nature_cvr_raw": 0.0,
            "total_cvr": 0.0,
            "adv_sales_share": 0.0,
            "attribution_lag": False,
            "attribution_interpolated": True,
            "tacos": tacos_fin,
            "return_goods_rate": financial_prof.get("refund_rate", 0.0) if financial_prof else 0.0,
            "cvr_ad_vs_nature_ratio": 0.0,
            "organic_readiness": "ORGANIC_BALANCED",
            "organic_guidance": "流量接口未返回数据，TACOS 由财务数据真实推导，已防止指标归零。",
        }

    def _perf_ratio_to_pct(raw_val) -> float:
        """LingXing query_product_performance_asin_lists always returns ratio fields as decimal (e.g. '0.2928'=29.28%, '1.3370'=133.70%).
        Only keep unscaled if explicitly formatted with a trailing '%' sign.
        """
        if raw_val is None:
            return 0.0
        s = str(raw_val).strip()
        v = num(raw_val)
        if s.endswith("%"):
            return round(v, 2)
        return round(v * 100.0, 2)

    ad_cvr = _perf_ratio_to_pct(r.get("ad_cvr"))
    nature_cvr_raw = _perf_ratio_to_pct(r.get("nature_cvr"))
    nature_cvr = max(0.0, nature_cvr_raw)
    total_cvr = _perf_ratio_to_pct(r.get("cvr"))

    nature_order_items_raw = int(num(r.get("nature_order_items")))
    order_items_total = int(num(r.get("order_items") or r.get("volume")))
    ad_order_quantity = int(num(r.get("ad_order_quantity")))
    ad_direct_order_quantity = int(num(r.get("ad_direct_order_quantity")))
    if nature_order_items_raw < 0:
        # When cross-variant/halo indirect orders push ad_order_quantity > order_items, recover direct organic floor or clamp to 0
        if ad_direct_order_quantity > 0 and order_items_total >= ad_direct_order_quantity:
            nature_order_items = order_items_total - ad_direct_order_quantity
        else:
            nature_order_items = 0
    else:
        nature_order_items = nature_order_items_raw

    adv_rate_unclamped = _perf_ratio_to_pct(r.get("adv_rate"))
    # Bug fix: evaluate attribution_lag BEFORE clamping adv_rate to [0, 100] and include negative nature_order_items_raw!
    has_attribution_lag = (adv_rate_unclamped > 100.0) or (nature_cvr_raw < 0) or (nature_order_items_raw < 0)

    tacos = _perf_ratio_to_pct(r.get("acoas"))

    # Cross-verify TACOS with authoritative financial ground truth (total_ad_cost / total_sales)
    if financial_prof and financial_prof.get("total_sales", 0) > 0:
        fin_tacos = round(financial_prof.get("total_ad_cost", 0) / financial_prof["total_sales"] * 100, 2)
        if tacos <= 0.0 or abs(tacos - fin_tacos) > 20.0:
            tacos = fin_tacos

    adv_rate = min(100.0, max(0.0, adv_rate_unclamped))

    # Assess Listing Organic Readiness Index (§7.1, configurable via rules.json Appendix A):
    rcfg = v2p.resolve_rule_config(rules)
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= rcfg["refund_lv1"])
    if (
        nature_cvr <= rcfg["organic_fragile_cvr"]
        or adv_rate >= rcfg["organic_fragile_adv_share"]
        or nature_order_items < rcfg["organic_fragile_orders"]
    ):
        organic_readiness = "ORGANIC_FRAGILE"
        organic_guidance = (
            "Listing 自然承接力脆弱（纯广告依赖型或自然单底座极薄）：自然位缓冲厚度不足，"
            "严禁执行全账户多活动同步大砍 TOS 与基础竞价的双重抑制（Double-Damping），"
            "必须执行单杠杆阶梯式微降（有效竞价降幅 <= 30%，全局变更预算 <= 20%），并将预算向细分具体卖点词置换，严防大盘失速休克！"
        )
    elif (
        nature_cvr >= rcfg["organic_flywheel_cvr"]
        and adv_rate <= rcfg["organic_flywheel_adv_share"]
        and nature_order_items >= rcfg["organic_flywheel_orders"]
        and total_sessions >= rcfg["organic_flywheel_sessions"]
    ):
        organic_readiness = "ORGANIC_FLYWHEEL"
        organic_guidance = (
            "Listing 具备成熟自然搜索飞轮（自然转化良好/自然单量充沛/BSR靠前）："
            "可按步长拉平外围畸形 TOS，清洗泛流，集中兵力于核心精准仓，安全释放自然位与利润红利。"
        )
    else:
        organic_readiness = "ORGANIC_BALANCED"
        organic_guidance = "Listing 处于标准稳态演进期，按标准单杠杆阶梯调控执行。"

    if is_high_refund:
        organic_guidance = f"[退款率高危警报 refund_rate={financial_prof.get('refund_rate')}%] {organic_guidance} 注意：启动增量毛利与销售底盘硬刹车，单靠广告调参无法扭亏，必须联动供应链/Listing排查！"

    if is_fallback:
        organic_guidance = f"[容灾平滑兜底: 沿用上期有效访客流并由财务真实数据核算] {organic_guidance}"

    raw_ret_rate = num(r.get("return_goods_rate"))
    ret_rate_pct = round(raw_ret_rate * (100.0 if abs(raw_ret_rate) <= 1.0 else 1.0), 2)

    raw_bb = num(r.get("buy_box_percentage"))
    bb_pct = round(raw_bb * 100.0, 2) if 0.0 < raw_bb <= 1.0 else round(raw_bb, 2)

    return {
        "sessions_total": total_sessions,
        "page_views_total": total_pageviews,
        "sessions_mobile": int(num(r.get("sessions_mobile"))),
        "page_views_mobile": int(num(r.get("page_views_mobile"))),
        "buy_box_percentage": bb_pct,
        "nature_order_items": nature_order_items,
        "nature_order_items_raw": nature_order_items_raw,
        "ad_order_quantity": ad_order_quantity,
        "ad_direct_order_quantity": ad_direct_order_quantity,
        "ad_cvr": ad_cvr,
        "nature_cvr": nature_cvr,
        "nature_cvr_raw": nature_cvr_raw,
        "total_cvr": total_cvr,
        "adv_sales_share": adv_rate,
        "adv_sales_share_raw": adv_rate_unclamped,
        "attribution_lag": has_attribution_lag,
        "attribution_interpolated": is_fallback,
        "tacos": tacos,
        "return_goods_rate": ret_rate_pct,
        "cvr_ad_vs_nature_ratio": round(ad_cvr / nature_cvr, 2) if nature_cvr > 0 else 0.0,
        "organic_readiness": organic_readiness,
        "organic_guidance": organic_guidance,
    }


def load_deal_info(indir: Path, meta: dict | None = None) -> dict:
    """Load deal, promotion, and discount intelligence from deal_info.json, anchored to report window end_date."""
    p = indir / "deal_info.json"
    data = None
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = None

    if not data or not isinstance(data, dict):
        data = {
            "asin": "",
            "country": "",
            "has_deal_history": False,
            "is_active_deal": False,
            "is_deal_transition": False,
            "days_since_last_deal": None,
            "last_deal_date": None,
            "deal_days": [],
            "coupon_days": [],
            "prime_days": [],
            "promotion_days": [],
            "sec_kill_records": [],
            "prime_discount_records": [],
            "financial_promotions": {
                "promotion_discount": 0.0,
                "promotion_amount": 0.0,
                "promotion_volume": 0,
                "sp_discount": 0.0,
                "promotion_fee": 0.0,
            },
            "summary_badge": "NONE",
            "summary_desc": "近30天无秒杀/促销活动记录",
        }

    _, window_end = _parse_window_bounds(meta)
    anchor_str = window_end or str(data.get("anchor_date") or "")[:10]
    try:
        ref_dt = datetime.strptime(anchor_str, "%Y-%m-%d").date() if anchor_str else datetime.now().date()
    except Exception:
        ref_dt = datetime.now().date()
    anchor_iso = ref_dt.strftime("%Y-%m-%d")

    # Enhance and validate deal dates anchored to ref_dt (prevent future promotionEndTime from polluting last_deal_date)
    deal_dates = []
    active_deal_window = None
    for d in data.get("deal_days", []) or []:
        if isinstance(d, dict) and d.get("date"):
            dt_s = str(d["date"])[:10]
            if dt_s <= anchor_iso:
                deal_dates.append(dt_s)
    for p_day in data.get("prime_days", []) or []:
        if isinstance(p_day, dict) and p_day.get("date"):
            dt_s = str(p_day["date"])[:10]
            if dt_s <= anchor_iso:
                deal_dates.append(dt_s)
    for s in data.get("sec_kill_records", []) or []:
        if not isinstance(s, dict):
            continue
        st_val = str(s.get("originStatus") or s.get("status") or "").upper()
        if st_val in ("CANCELED", "CANCELLED", "SUPPRESSED"):
            continue
        p_start = str(s.get("promotionStartTime") or "")[:10]
        p_end = str(s.get("promotionEndTime") or "")[:10]
        if p_start and p_end:
            if p_start <= anchor_iso <= p_end:
                deal_dates.append(anchor_iso)
                active_deal_window = f"{p_start} ~ {p_end}"
            elif p_end <= anchor_iso:
                deal_dates.append(p_end)
            # Also expand historical days of the sec_kill record up to anchor_iso for overlap detection
            try:
                cur_d = datetime.strptime(p_start, "%Y-%m-%d").date()
                lim_d = min(ref_dt, datetime.strptime(p_end, "%Y-%m-%d").date())
                while cur_d <= lim_d:
                    deal_dates.append(cur_d.strftime("%Y-%m-%d"))
                    cur_d += timedelta(days=1)
            except Exception:
                pass

    deal_dates_unique = sorted(set(deal_dates))
    data["all_deal_dates"] = deal_dates_unique
    if active_deal_window:
        data["active_deal_window"] = active_deal_window

    if deal_dates_unique:
        data["has_deal_history"] = True
        latest_deal = max(deal_dates_unique)
        data["last_deal_date"] = latest_deal
    elif data.get("last_deal_date") and str(data["last_deal_date"])[:10] > anchor_iso:
        data["last_deal_date"] = anchor_iso

    prof_rows = [r for r in as_rows(load_json(indir / "order_profit.json")) if isinstance(r, dict)]
    if prof_rows:
        row = _select_store_row(prof_rows, (meta or {}).get("sid"))
        promo_disc = float(num(row.get("promotion_discount")))
        data.setdefault("financial_promotions", {})
        data["financial_promotions"]["promotion_discount"] = promo_disc
        data["financial_promotions"]["promotion_fee"] = float(num(row.get("promotion_fee")))
        data["financial_promotions"]["sp_discount"] = float(num(row.get("sp_discount")))
        if promo_disc < -10.0 and not data.get("has_deal_history"):
            data["has_deal_history"] = True

    perf_rows = [r for r in as_rows(load_json(indir / "product_performance.json")) if isinstance(r, dict)]
    if perf_rows:
        row = perf_rows[0]
        data.setdefault("financial_promotions", {})
        data["financial_promotions"]["promotion_amount"] = float(num(row.get("promotion_amount")))
        data["financial_promotions"]["promotion_volume"] = int(num(row.get("promotion_volume")))

    data["anchor_date"] = anchor_iso
    data.setdefault("is_active_deal", False)

    if data.get("last_deal_date"):
        try:
            last_dt = datetime.strptime(str(data["last_deal_date"])[:10], "%Y-%m-%d").date()
            days_diff = max(0, (ref_dt - last_dt).days)
            data["days_since_last_deal"] = days_diff
            data["is_active_deal"] = (days_diff == 0) or bool(active_deal_window)
            data["is_deal_transition"] = (1 <= days_diff <= 10) and not data["is_active_deal"]
            if data["is_active_deal"]:
                data["summary_badge"] = "ACTIVE_DEAL"
                disp_w = active_deal_window or data["last_deal_date"]
                data["summary_desc"] = f"商品当前（{disp_w}）正处于秒杀/大促活动期，严禁因流量激增误砍正常出价"
            elif data["is_deal_transition"]:
                data["summary_badge"] = "DEAL_TRANSITION"
                data["summary_desc"] = f"商品于 {data['last_deal_date']} 结束秒杀/促销（距报表期末 {days_diff} 天），处于促后流量回落过渡窗口"
            else:
                data["summary_badge"] = "RECENT_DEAL_HISTORY"
                data["summary_desc"] = f"近30天含秒杀/促销记录（最近结束于 {data['last_deal_date']}，距期末 {days_diff} 天已回归常态）"
        except Exception:
            pass
    elif data.get("has_deal_history"):
        p_disc = abs(num(data.get("financial_promotions", {}).get("promotion_discount", 0.0)))
        data["summary_badge"] = "COUPON_OR_PROMOTION"
        data["summary_desc"] = f"财务账单显示近期有显著促销折让 (${p_disc:.2f})"
    else:
        data["summary_badge"] = "NONE"
        data["summary_desc"] = "近30天无秒杀/促销活动记录"

    return data


def load_dayparting_analysis(indir: Path) -> tuple[list[dict], list[str]]:
    h_rows = as_rows(load_json(indir / "order_analysis_hourly.json"))
    table_lines = []
    hourly_records = []
    parsed_rows = []
    for r in h_rows:
        k = str(r.get("key"))
        if k == "sub_total" or not k.isdigit():
            continue
        sp_hr = num(r.get("spend"))
        ad_ord_hr = int(num(r.get("ad_orders")))
        ad_sales_hr = num(r.get("ad_sales") or r.get("sales"))
        parsed_rows.append({
            "hr": int(k),
            "sp": sp_hr,
            "ad_ord": ad_ord_hr,
            "st_ord": int(num(r.get("store_orders"))),
            "cvr_v": clean_api_rate(r.get("cvr")),
            "acos_v": clean_api_acos(r.get("acos"), sp_hr, ad_sales_hr if ad_ord_hr > 0 else 0.0),
            "cpc_v": clean_api_rate(r.get("cpc")),
        })

    # Adaptive baseline benchmarks based on the ASIN's actual hourly distribution
    active_hours = [x for x in parsed_rows if x["sp"] > 0 or x["ad_ord"] > 0]
    if active_hours:
        avg_cvr = sum(x["cvr_v"] for x in active_hours) / len(active_hours)
        valid_acos = [x["acos_v"] for x in active_hours if 0 < x["acos_v"] < 900.0]
        avg_acos = (sum(valid_acos) / len(valid_acos)) if valid_acos else 30.0
        avg_ord = sum(x["ad_ord"] for x in active_hours) / len(active_hours)
    else:
        avg_cvr, avg_acos, avg_ord = 2.5, 30.0, 3.0

    for item in parsed_rows:
        hr = item["hr"]
        sp = item["sp"]
        ad_ord = item["ad_ord"]
        st_ord = item["st_ord"]
        cvr_v = item["cvr_v"]
        acos_v = item["acos_v"]
        cpc_v = item["cpc_v"]

        # 1. Peak hour: High orders or superior CVR with controlled ACOS
        is_peak = (
            (ad_ord >= max(5, avg_ord * 1.3) and acos_v <= max(28.0, avg_acos * 0.95))
            or (cvr_v >= max(2.8, avg_cvr * 1.25) and acos_v <= max(25.0, avg_acos * 0.85) and ad_ord >= 3)
            or (ad_ord >= 8 and acos_v <= max(35.0, avg_acos * 1.1))
        )

        # 2. Bleeding hour: Zero orders with spend, or blown out ACOS, or dismal CVR with high spend
        is_bleeding = (
            (sp >= 10.0 and ad_ord == 0)
            or (acos_v >= max(50.0, avg_acos * 1.4) and sp >= 15.0)
            or (cvr_v <= min(1.2, avg_cvr * 0.5) and sp >= 20.0 and acos_v >= 40.0)
        )

        if is_peak and not is_bleeding:
            classification = "PEAK_GOLDEN_HOUR"
            dayparting_adj = "Boost budget/bid +15%~+25%"
        elif is_bleeding and not is_peak:
            classification = "BLEEDING_OFFPEAK_HOUR"
            dayparting_adj = "Throttle bid -20%~-30%"
        else:
            classification = "STEADY_TRANSITION_HOUR"
            dayparting_adj = "Maintain normal baseline"

        hourly_records.append({
            "hour": hr, "spend": sp, "ad_orders": ad_ord, "store_orders": st_ord,
            "cvr": cvr_v, "acos": acos_v, "cpc": cpc_v,
            "classification": classification, "recommendation": dayparting_adj
        })
        acos_disp = fmt_acos_text(acos_v, sp, ad_ord)
        table_lines.append(f"{hr:02d}:00-{hr:02d}:59\t{sp:.2f}\t{ad_ord}\t{st_ord}\t{cvr_v:.1f}%\t{acos_disp}\t{cpc_v:.2f}\t{classification}\t{dayparting_adj}")

    return hourly_records, table_lines


def load_weekly_trends_analysis(indir: Path) -> list[str]:
    """Export weekly_trends.tsv combining rolling 4-week longitudinal buckets (from product_performance_daily.json)
    and day-of-week distribution (from order_analysis_weekly.json)."""
    lines: list[str] = []

    # Part A: Rolling 4-week chronological trend from product_performance_daily.json when available
    daily_raw = [r for r in as_rows(load_json(indir / "product_performance_daily.json")) if isinstance(r, dict)]
    day_items = []
    for r in daily_raw:
        dt_s = ""
        for k in ("rdate", "date", "report_date", "stat_date"):
            v = str(r.get(k) or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}", v):
                dt_s = v[:10]
                break
        if not dt_s:
            continue
        day_sp = _extract_row_ad_costs(r)["total_ad_cost"]
        day_ad_sl = num(r.get("ad_sales_amount") or r.get("ads_sp_sales"))
        day_st_sl = num(r.get("amount") or r.get("net_amount"))
        day_ad_od = int(num(r.get("ad_order_quantity")))
        day_st_od = int(num(r.get("volume") or r.get("order_items")))
        day_clk = int(num(r.get("clicks") or r.get("ad_clicks_sp")))
        day_imp = int(num(r.get("impressions") or r.get("ad_impressions_sp")))
        day_gp = num(r.get("predict_gross_profit") if r.get("predict_gross_profit") is not None else r.get("gross_profit"))
        day_items.append({
            "date": dt_s, "spend": day_sp, "ad_sales": day_ad_sl, "store_sales": day_st_sl,
            "ad_orders": day_ad_od, "store_orders": day_st_od, "clicks": day_clk,
            "impressions": day_imp, "predict_gp": day_gp,
        })
    day_items.sort(key=lambda x: x["date"])
    if len(day_items) >= 14:
        # Take up to last 28 days in 4 weekly buckets (W1 oldest -> W4 latest)
        tail = day_items[-28:]
        n_weeks = max(2, len(tail) // 7)
        for idx_w in range(n_weeks):
            chunk = tail[idx_w * 7:(idx_w + 1) * 7] if idx_w < n_weeks - 1 else tail[idx_w * 7:]
            if not chunk:
                continue
            sp_w = sum(x["spend"] for x in chunk)
            asl_w = sum(x["ad_sales"] for x in chunk)
            ssl_w = sum(x["store_sales"] for x in chunk)
            aod_w = sum(x["ad_orders"] for x in chunk)
            sod_w = sum(x["store_orders"] for x in chunk)
            clk_w = sum(x["clicks"] for x in chunk)
            imp_w = sum(x["impressions"] for x in chunk)
            gp_w = sum(x["predict_gp"] for x in chunk)
            cpc_w = round(sp_w / clk_w, 2) if clk_w > 0 else 0.0
            cvr_w = round(aod_w / clk_w * 100.0, 2) if clk_w > 0 else 0.0
            acos_w = clean_api_acos(0.0, sp_w, asl_w if asl_w > 0 else 0.0)
            acos_s = fmt_acos_text(acos_w, sp_w, asl_w if asl_w > 0 else aod_w)
            tacos_w = round(sp_w / ssl_w * 100.0, 2) if ssl_w > 0 else (999.0 if sp_w > 0 else 0.0)
            tacos_s = fmt_acos_text(tacos_w, sp_w, ssl_w)
            roas_w = calc_roas(sp_w, asl_w)
            label_w = f"Week_{idx_w + 1} ({chunk[0]['date']}~{chunk[-1]['date']})"
            lines.append("\t".join(str(x) for x in [
                "ROLLING_4W", label_w, round(sp_w, 2), round(asl_w, 2), round(ssl_w, 2),
                aod_w, sod_w, clk_w, imp_w, f"{cpc_w:.2f}", f"{cvr_w:.2f}%",
                acos_s, f"{roas_w:.2f}", tacos_s, round(gp_w, 2),
            ]))

    # Part B: Day-of-week distribution from order_analysis_weekly.json
    w_rows = [r for r in as_rows(load_json(indir / "order_analysis_weekly.json")) if isinstance(r, dict)]
    for r in w_rows:
        k = str(r.get("key") or "").strip()
        if not k or k.lower() in ("sub_total", "total", "汇总"):
            continue
        lbl = str(r.get("key_table") or r.get("key_chart") or f"Day_{k}").strip()
        sp_d = num(r.get("spend"))
        asl_d = num(r.get("ad_sales"))
        ssl_d = num(r.get("store_sales"))
        aod_d = int(num(r.get("ad_orders")))
        sod_d = int(num(r.get("store_orders")))
        clk_d = int(num(r.get("clicks")))
        imp_d = int(num(r.get("impressions")))
        cpc_d = clean_api_rate(r.get("cpc"))
        cvr_d = clean_api_rate(r.get("cvr"))
        acos_d = clean_api_acos(r.get("acos"), sp_d, asl_d if aod_d > 0 or asl_d > 0 else 0.0)
        acos_ds = fmt_acos_text(acos_d, sp_d, asl_d if asl_d > 0 else aod_d)
        roas_d = calc_roas(sp_d, asl_d, r.get("roas"))
        tacos_d = round(sp_d / ssl_d * 100.0, 2) if ssl_d > 0 else (999.0 if sp_d > 0 else 0.0)
        tacos_ds = fmt_acos_text(tacos_d, sp_d, ssl_d)
        lines.append("\t".join(str(x) for x in [
            "DAY_OF_WEEK", lbl, round(sp_d, 2), round(asl_d, 2), round(ssl_d, 2),
            aod_d, sod_d, clk_d, imp_d, f"{cpc_d:.2f}", f"{cvr_d:.2f}%",
            acos_ds, f"{roas_d:.2f}", tacos_ds, "--",
        ]))
    return lines


def listing_profile(listing: dict | None, stock: dict | None) -> dict:
    item = listing or {}
    stock_rows = []
    if isinstance(stock, list):
        stock_rows = stock
    elif isinstance(stock, dict):
        data = stock.get("data")
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            stock_rows = inner.get("list") or []
        elif isinstance(data, list):
            stock_rows = data
    s0 = stock_rows[0] if stock_rows else {}
    small_rank = item.get("small_rank")
    if isinstance(small_rank, str):
        try:
            small_rank = json.loads(small_rank)
        except Exception:
            pass
    title = item.get("item_name") or item.get("title") or s0.get("product_name") or ""
    brand = item.get("seller_brand") or item.get("product_brand_text") or s0.get("product_brand_text") or ""
    # Extract feature segments from Title for product functional understanding
    features = []
    chunks = re.split(r"[,|\-–—;()\[\]/]+", title)
    for c in chunks:
        c_clean = c.strip()
        if brand and c_clean.lower().startswith(str(brand).lower()):
            c_clean = c_clean[len(str(brand)):].strip()
        if len(c_clean) >= 3:
            features.append(c_clean)

    return {
        "asin": item.get("asin") or s0.get("asin"),
        "parent_asin": item.get("parent_asin"),
        "msku": item.get("msku") or s0.get("seller_sku"),
        "local_sku": item.get("local_sku") or s0.get("sku"),
        "title": title,
        "brand": brand,
        "features": features,
        "price": num(item.get("listing_price") or item.get("landed_price") or item.get("price")),
        "stars": num(item.get("stars")),
        "reviews": int(num(item.get("reviews_num"))),
        "rank": item.get("rank") or item.get("seller_rank"),
        "small_rank": small_rank,
        "fulfillable": item.get("afn_fulfillable_quantity") or s0.get("afn_fulfillable_quantity"),
        "available_total": s0.get("available_total"),
        "days_of_supply": s0.get("short_term_historical_days_of_supply"),
        "inv_age_91_180": s0.get("inv_age_91_to_180_days"),
        "inv_age_181_270": s0.get("inv_age_181_to_270_days"),
        "inv_age_271_365": s0.get("inv_age_271_to_365_days"),
        "inv_age_365_plus": s0.get("inv_age_365_plus_days"),
        "inv_age_181_plus": int(
            num(s0.get("inv_age_181_to_270_days"))
            + num(s0.get("inv_age_271_to_365_days"))
            + num(s0.get("inv_age_365_plus_days"))
        ),
        "seven_volume": item.get("average_seven_volume"),
        "fourteen_volume": item.get("average_fourteen_volume") or item.get("fourteen_volume"),
        "thirty_volume": item.get("average_thirty_volume") or item.get("thirty_volume"),
        "fourteen_sales": item.get("fourteen_amount"),
        "fourteen_ad_spend": item.get("fourteen_spend"),
        "thirty_sales": item.get("thirty_amount"),
        "thirty_ad_spend": item.get("thirty_spend"),
    }


def placement(row: dict) -> tuple:
    bidding = row.get("bidding") or {}
    tos = num(row.get("placement_top"))
    pp = num(row.get("placement_product_page"))
    ros = num(row.get("placement_rest_of_search"))
    if isinstance(bidding, dict):
        for adj in bidding.get("adjustments") or []:
            pred = adj.get("predicate")
            pct = num(adj.get("percentage"))
            if pred == "placementTop":
                tos = pct
            elif pred == "placementProductPage":
                pp = pct
            elif pred == "placementRestOfSearch":
                ros = pct
    return tos, pp, ros


def campaign_rows(raw: list[dict]) -> list[dict]:
    out = []
    for r in raw:
        if skip_total(r) or not r.get("name"):
            continue
        tos, pp, ros = placement(r)
        bidding = r.get("bidding") or {}
        strat = bidding.get("strategy") if isinstance(bidding, dict) else r.get("bidding_strategy")
        btype = str(r.get("bidding_type") or "")
        if strat == "manual" or "固定" in btype or "fixed" in btype.lower():
            bidding_strategy_desc = "固定竞价 (Fixed Bids)"
        elif strat == "autoForSales" or "提高和降低" in btype or "up" in btype.lower():
            bidding_strategy_desc = "动态竞价 - 提高和降低 (Dynamic Bids - Up & Down)"
        elif strat == "legacyForSales" or "仅降低" in btype or "down" in btype.lower():
            bidding_strategy_desc = "动态竞价 - 仅降低 (Dynamic Bids - Down Only)"
        elif strat == "ruleBased" or "规则" in btype:
            bidding_strategy_desc = "基于规则的竞价 (Rule-based Bidding)"
        else:
            bidding_strategy_desc = btype if btype else (str(strat) if strat else "固定竞价 (Fixed Bids)")

        # Shopper Cohort Bidding (Audience Bid Adjustment %) & Out-Of-Budget (OOB) Telemetry (Item 7)
        cohort_bid_pct = 0.0
        cohort_type = ""
        if isinstance(bidding, dict):
            for sc in (bidding.get("shopperCohortBidding") or []):
                if isinstance(sc, dict):
                    pct_sc = num(sc.get("percentage"))
                    if pct_sc > cohort_bid_pct:
                        cohort_bid_pct = pct_sc
                        cohort_type = str(sc.get("shopperCohortType") or "AUDIENCE_SEGMENT")

        avg_over_budget_time = round(num(r.get("avg_over_budget_time")), 2)
        last_over_budget_at = str(r.get("last_over_budget_at") or "").strip()
        if last_over_budget_at in ("None", "null"):
            last_over_budget_at = ""
        raw_tib = r.get("campaign_budget_suggestion_percent_time_in_budget")
        if raw_tib is not None and str(raw_tib).strip() not in ("", "None", "null"):
            tib_n = num(raw_tib)
            pct_time_in_budget = round(tib_n * (100.0 if 0.0 < tib_n <= 1.0 else 1.0), 1)
        else:
            pct_time_in_budget = 100.0
        suggestion_budget = round(num(r.get("suggestion_budget")), 2)
        creative_asins = [str(x).upper() for x in (r.get("creative_asins") or []) if x]

        # Dayparting & Rule Strategy Metadata from LingXing ERP
        is_apply_time = r.get("is_apply_time") is True or r.get("is_apply_time") == 1
        timing_base_val = num(r.get("timing_base_value")) if r.get("timing_base_value") is not None else None
        applied_templates = r.get("applied_templates") or []
        opt_rule_desc = str(r.get("optimization_rule_desc") or "").strip()
        is_apply_rule = r.get("is_apply_rule") is True or r.get("is_apply_rule") == 1

        is_up_and_down = (strat == "autoForSales" or "提高和降低" in bidding_strategy_desc or "up & down" in bidding_strategy_desc.lower())
        ord_tot = num(r.get("orders"))
        dir_ord = num(r.get("direct_orders"))
        indir_ord = num(r.get("indirect_orders"))
        indir_share = round((indir_ord / ord_tot * 100.0), 1) if ord_tot > 0 else 0.0

        sp_val = num(r.get("spends"))
        sl_val = num(r.get("sales"))
        start_dt_s = str(r.get("start_date") or r.get("startDate") or r.get("created_at") or "").strip()[:10]
        ntb_ord_raw = r.get("ntb_orders") if r.get("ntb_orders") is not None else r.get("new_to_brand_orders")
        ntb_pct_raw = r.get("ntb_orders_pct") if r.get("ntb_orders_pct") is not None else r.get("percent_of_orders_new_to_brand")
        out.append({
            "name": r.get("name"),
            "id": r.get("campaign_id"),
            "state": r.get("state"),
            "start_date": start_dt_s,
            "ntb_orders": ntb_ord_raw,
            "ntb_orders_pct": ntb_pct_raw,
            "targeting": r.get("targeting_type"),
            "sponsored": r.get("sponsored_type"),
            "budget": num(r.get("daily_budget") or r.get("budget")),
            "default_bid": num(r.get("default_bid")),
            "tos": tos, "pp": pp, "ros": ros,
            "bidding_strategy": bidding_strategy_desc,
            "is_up_and_down": is_up_and_down,
            "cohort_bid_pct": cohort_bid_pct,
            "cohort_type": cohort_type,
            "avg_over_budget_time": avg_over_budget_time,
            "last_over_budget_at": last_over_budget_at,
            "pct_time_in_budget": pct_time_in_budget,
            "suggestion_budget": suggestion_budget,
            "off_amazon_settings": str(r.get("off_amazon_settings") or "").strip(),
            "creative_asins": creative_asins,
            "is_shared_multi_asin_campaign": len(creative_asins) > 1,
            "spend": sp_val,
            "sales": sl_val,
            "acos": clean_api_acos(r.get("acos"), sp_val, sl_val),
            "roas": calc_roas(sp_val, sl_val, r.get("roas")),
            "cpc": clean_api_rate(r.get("cpc")),
            "cvr": clean_api_rate(r.get("cvr")),
            "ctr": clean_api_rate(r.get("ctr")),
            "imp": num(r.get("impressions")),
            "clk": num(r.get("clicks")),
            "ord": ord_tot,
            "direct_ord": dir_ord,
            "indirect_ord": indir_ord,
            "indirect_order_share": indir_share,
            "tos_share": num(r.get("top_of_search_impression_share")),
            "portfolio": r.get("portfolio_name") or r.get("portfolio_id"),
            "portfolio_id": str(r.get("portfolio_id") or "").strip(),
            "portfolio_name": str(r.get("portfolio_name") or "").strip(),
            "is_apply_time": is_apply_time,
            "timing_base_value": timing_base_val,
            "applied_templates": applied_templates,
            "opt_rule_desc": opt_rule_desc,
            "is_apply_rule": is_apply_rule,
        })
    return sorted(out, key=lambda x: -x["spend"])


def build_adgroup_bid_lookups(src: Path, asin_campaigns: set[str] | None = None) -> tuple[dict, dict]:
    """Build unified ad-group default bid lookups so keywords/targets with null bid inherit parent group default_bid."""
    raw_ad_groups = [g for g in as_rows(load_json(src / "ad_groups.json")) if not skip_total(g)]
    group_bid_lookup: dict = {}
    camp_group_bid_lookup: dict[str, float] = {}
    for g in raw_ad_groups:
        cn = g.get("campaign_name") or ""
        if asin_campaigns and cn and cn not in asin_campaigns:
            continue
        gn = g.get("name") or g.get("ad_group_name") or ""
        gid = str(g.get("ad_group_id") or "").strip()
        is_g_timing = bool(g.get("is_apply_time")) and g.get("is_apply_time") not in (0, "0", "false", "False")
        g_tbase = num(g.get("timing_base_value"))
        dbid = g_tbase if (is_g_timing and g_tbase > 0) else num(g.get("default_bid") or g.get("bid"))
        g_state = str(g.get("state") or "enabled").lower().strip()
        if dbid > 0:
            if gid:
                group_bid_lookup[gid] = dbid
            if cn and gn:
                group_bid_lookup[(cn, gn)] = dbid
            if cn:
                if cn not in camp_group_bid_lookup or (g_state == "enabled" and dbid > 0):
                    camp_group_bid_lookup[cn] = max(dbid, camp_group_bid_lookup.get(cn, 0.0)) if g_state != "enabled" else dbid
    return group_bid_lookup, camp_group_bid_lookup


def resolve_effective_entity_bid(
    row: dict,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    fallback: float = 0.35,
) -> tuple[float, bool]:
    """Return (effective_base_bid, is_inherited_from_adgroup).
    Item 2 / Iron Law 77: Whenever is_apply_time is True and timing_base_value > 0, ALWAYS anchor to timing_base_value!
    """
    is_timing = bool(row.get("is_apply_time")) and row.get("is_apply_time") not in (0, "0", "false", "False")
    t_base = num(row.get("timing_base_value"))
    if is_timing and t_base > 0:
        return round(t_base, 2), False
    explicit_bid = num(row.get("bid"))
    if explicit_bid > 0:
        return round(explicit_bid, 2), False
    row_def = num(row.get("default_bid"))
    if row_def > 0:
        return round(row_def, 2), True
    c_name = row.get("campaign_name") or row.get("campaign") or ""
    ag_id = str(row.get("ad_group_id") or "").strip()
    ag_name = row.get("ad_group_name") or ""
    if group_bid_lookup:
        if ag_id and ag_id in group_bid_lookup:
            return round(float(group_bid_lookup[ag_id]), 2), True
        if c_name and ag_name and (c_name, ag_name) in group_bid_lookup:
            return round(float(group_bid_lookup[(c_name, ag_name)]), 2), True
    if camp_group_bid_lookup and c_name in camp_group_bid_lookup:
        return round(float(camp_group_bid_lookup[c_name]), 2), True
    return round(fallback, 2), True


def resolve_multi_window_entity_bid(
    row_30d: dict,
    row_7d: dict | None = None,
    row_3d: dict | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    fallback: float = 0.35,
) -> tuple[float, bool, float]:
    """Resolve (effective_base_bid, is_inherited, instant_bid) across 3d/7d/30d slices without EVER letting
    peak-hour instant `bid` overwrite `timing_base_value` when dayparting is active!"""
    r3 = row_3d or {}
    r7 = row_7d or {}
    instant_bid = round(num(r3.get("bid") or r7.get("bid") or row_30d.get("bid")), 2)
    for cand in (r3, r7, row_30d):
        if cand and bool(cand.get("is_apply_time")) and cand.get("is_apply_time") not in (0, "0", "false", "False"):
            tb = num(cand.get("timing_base_value"))
            if tb > 0:
                return round(tb, 2), False, (instant_bid if instant_bid > 0 else round(tb, 2))
    # Not dayparted: prefer freshest explicit bid from 3d -> 7d -> 30d -> adgroup inheritance
    for cand in (r3, r7):
        if cand and num(cand.get("bid")) > 0:
            b = round(num(cand.get("bid")), 2)
            return b, False, b
    eff_b, is_inh = resolve_effective_entity_bid(row_30d, group_bid_lookup, camp_group_bid_lookup, fallback=fallback)
    return eff_b, is_inh, (instant_bid if instant_bid > 0 else eff_b)


def _safe_re_search(pattern: str, text: str, flags: int = re.I):
    """Regex search resilient to malformed user-authored patterns in rules.json."""
    if not pattern:
        return None
    try:
        return re.search(pattern, text, flags)
    except re.error:
        return None


def classify_query(q: str, profile: dict, rules: dict) -> tuple[str, str, bool]:
    """Classify query into intent buckets without category hardcoding.

    Returns (bucket, matched_pattern_or_reason, is_strong_relevance).
    """
    ql = (q or "").lower().strip()
    if not ql:
        return "empty", "", False
    if re.search(r"b0[0-9a-z]{8}", ql):
        return "asin", "asin_target", False

    # 0. Pre-Flight Global Compliance & Hard Category/Mechanism Firewall (Item 8)
    fw_blocked, fw_label, fw_reason = check_global_compliance_firewall(ql, profile, rules)
    if fw_blocked:
        return fw_label, fw_reason, False

    brand = str(profile.get("brand") or "").lower().strip()
    if brand and len(brand) >= 2 and brand in ql:
        return "brand", brand, True

    # 1. Custom IS_NOT patterns (explicit misfits, wrong features, wrong forms)
    for item in rules.get("is_not_patterns", []) or []:
        pat = item if isinstance(item, str) else item.get("pattern", "")
        label = "is_not" if isinstance(item, str) else item.get("label", "is_not")
        # 语义保护：若为耗材类 pattern，但搜索词包含器具/机器名词，属于研磨对象而非耗材错配！
        if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
            if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", ql, re.I):
                continue
        if _safe_re_search(pat, ql):
            return label, pat, False

    # 2. Competitor brands
    for comp in rules.get("competitor_brands", []) or []:
        c = (comp or "").lower().strip()
        if c and len(c) >= 2 and c in ql:
            return "competitor_brand", c, False

    # 3. Custom IS patterns (core product, primary selling points)
    for item in rules.get("is_patterns", []) or []:
        pat = item if isinstance(item, str) else item.get("pattern", "")
        label = "core_feature" if isinstance(item, str) else item.get("label", "core_feature")
        if _safe_re_search(pat, ql):
            return label, pat, True

    # 4. Adjacent patterns (compatible but not primary category food)
    for item in rules.get("adjacent_patterns", []) or []:
        pat = item if isinstance(item, str) else item.get("pattern", "")
        label = "adjacent" if isinstance(item, str) else item.get("label", "adjacent")
        if _safe_re_search(pat, ql):
            return label, pat, False

    # 4.5 Multi-Purpose Cross-Category Hero Engine (Direction 5 & Rule 64)
    # Automatically identify high-converting cross-category multi-purpose selling points
    multi_purpose_patterns = [
        (r"\b(spice|spices|herb|herbs|seed|seeds|flax|flaxseed|chia|grain|grains|nut|nuts|pepper|peppercorn|masala)\b", "multi_purpose_spice_seed"),
        (r"\b(tea|cold brew|iced coffee|single serve|hot water)\b", "multi_purpose_beverage"),
        (r"\b(smoothie|smoothies|shake|protein|juice|baby food)\b", "multi_purpose_blender"),
        (r"\b(pour over|gooseneck|tea|temperature control|drip)\b", "multi_purpose_kettle"),
    ]
    title_or_desc = (str(profile.get("title") or "") + " " + " ".join(profile.get("features", []))).lower()
    for mp_pat, mp_label in multi_purpose_patterns:
        m_mp = re.search(mp_pat, ql, re.I)
        if m_mp and re.search(mp_pat, title_or_desc, re.I):
            return "core_feature", f"hero_multi_purpose:{m_mp.group(0)}", True

    # 5. Smart Title / Token Overlap (Hardened against single generic head-noun overlap like 'weed grinder' / 'acrylic grinder')
    title = str(profile.get("title") or "").lower()
    title_tokens = set(re.findall(r"[\w]+", title, re.UNICODE))
    title_tokens = {t for t in title_tokens if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}

    q_tokens = set(re.findall(r"[\w]+", ql, re.UNICODE))
    q_tokens = {t for t in q_tokens if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}

    common = q_tokens & title_tokens
    if len(common) >= 2:
        return "core_or_title", f"tokens:{','.join(sorted(common)[:3])}", True
    if len(common) == 1 and len(q_tokens) == 1:
        sole_tok = next(iter(common))
        if sole_tok not in GENERIC_APPLIANCE_HEAD_NOUNS:
            return "core_or_title", f"token:{sole_tok}", True
    elif len(common) == 1 and len(q_tokens) == 2:
        sole_tok = next(iter(common))
        # Only allow 1-of-2 overlap if the shared token is a specific domain qualifier (e.g. 'espresso', 'gooseneck'),
        # NEVER when the only shared word is a generic head noun like 'grinder' paired with an alien modifier!
        if sole_tok not in GENERIC_APPLIANCE_HEAD_NOUNS:
            return "core_or_title", f"token:{sole_tok}", True

    # 6. Feature chunk matching
    for feat in profile.get("features", []):
        fl = feat.lower()
        if len(fl) >= 4 and fl in ql:
            return "core_feature", f"feature:{feat}", True

    return "other", "", False


def load_searchrank_map(src: Path) -> dict[str, int]:
    """Load search rank from keyword_analysis.json (Amazon ABA search frequency rank)."""
    kw_analysis_raw = load_json(src / "keyword_analysis.json")
    rows = as_rows(kw_analysis_raw)
    sr_map: dict[str, int] = {}
    for r in rows:
        kw = str(r.get("keyword_text") or r.get("key") or "").lower().strip()
        sr = r.get("searchrank")
        if kw and sr is not None and str(sr).isdigit():
            sr_map[kw] = int(sr)
    return sr_map


def get_searchrank(term: str, sr_map: dict[str, int]) -> tuple[int | None, str]:
    """Map query/keyword to Amazon ABA searchrank and search volume tier (exact match only, never substring!)."""
    t = re.sub(r"\s+", " ", (term or "").lower().strip())
    sr = sr_map.get(t)
    if sr is None and t:
        t_norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()
        if t_norm and t_norm != t:
            sr = sr_map.get(t_norm)

    if sr is None:
        return None, "LONG_TAIL"
    if sr < 5000:
        return sr, "MEGA_HEAD"       # Top 5K 全美超级大词
    if sr < 30000:
        return sr, "CORE_HEAD"       # Top 30K 头部主粮词
    if sr < 150000:
        return sr, "MID_TAIL"        # Top 150K 腰部特征词
    return sr, "LONG_TAIL"           # 长尾细分词


def load_xydc_ranks(src: Path) -> dict[str, dict]:
    """Load keyword organic & sponsored ranks from xydc_ranks.json (西柚洞察)."""
    p = src / "xydc_ranks.json"
    if not p.exists():
        return {}
    data = load_json(p)
    if not data or not isinstance(data, dict):
        return {}
    ranks_map = {}
    for kw, item in data.items():
        if not isinstance(item, dict):
            continue
        kw_clean = str(kw).lower().strip()
        ranks_map[kw_clean] = {
            "search_term": item.get("search_term") or kw_clean,
            "organic_rank": item.get("organic_rank"),
            "organic_page": item.get("organic_page"),
            "sp_rank": item.get("sp_rank"),
            "sp_page": item.get("sp_page"),
            "sb_rank": item.get("sb_rank"),
            "organic_traffic": num(item.get("organic_traffic")),
            "ad_traffic": num(item.get("ad_traffic")),
            "total_traffic": num(item.get("total_traffic")),
            "organic_rate": num(item.get("organic_rate")),
            "ad_rate": num(item.get("ad_rate")),
            "organic_growth": num(item.get("organic_growth")),
            "ad_growth": num(item.get("ad_growth")),
        }
    return ranks_map


def load_xydc_traffic(src: Path) -> dict:
    """Load ASIN/competitor traffic scores, parent 4-channel split, keyword count trends & BSR trends from xydc_traffic.json."""
    p = src / "xydc_traffic.json"
    if not p.exists():
        return {}
    data = load_json(p)
    return data if isinstance(data, dict) else {}


def evaluate_rank_synergy(
    term: str,
    xydc_map: dict[str, dict],
    spend: float = 0.0,
    sales: float = 0.0,
    orders: int = 0,
    clicks: int = 0,
    target_acos: float = 25.0,
    current_bid: float = 0.0,
    scaled_thresholds: dict | None = None,
    is_dedicated_camp: bool = False,
    bid_floor: float = 0.25,
    stage_mode: str = "GROWTH",
    is_fragile: bool = False,
    ord_7d: int = 0,
    cpc: float = 0.0,
    camp_tos: float = 0.0,
) -> dict:
    """Evaluate organic rank vs sponsored rank synergy under v2 §7.6 (single-lever compliant!).

    Categorizes keyword into 5 strategic tiers and determines single-lever bidding actions:
    - Tier 1: 王者双霸屏 vs 防内耗控价 (Organic Rank 1-3) -> bid-only first, TOS next period ONLY in dedicated hero campaign
    - Tier 2: 临界冲刺期 (Flywheel Ascender, Organic Rank 4-15) -> bid-only first, TOS next period ONLY in dedicated hero campaign
    - Tier 3: 腰部攻坚期 (Mid Breakthrough, Organic Rank 16-50) -> <=-20% step cap (no -25%)
    - Tier 4: 自然脱节/远距离 (Organic Disconnect, Organic Rank > 50 or unranked)
    - Tier 5: 暂无排名数据 (Unranked)
    """
    t = (term or "").lower().strip()
    info = xydc_map.get(t)
    if not info:
        t_sub = re.sub(r"[^\w\s]", "", t).strip()
        info = xydc_map.get(t_sub)

    if not info:
        return {
            "organic_rank": None,
            "sp_rank": None,
            "sb_rank": None,
            "organic_traffic": 0.0,
            "ad_traffic": 0.0,
            "total_traffic": 0.0,
            "organic_share_pct": 0.0,
            "ad_share_pct": 0.0,
            "organic_rate": 0.0,
            "ad_rate": 0.0,
            "organic_growth": 0.0,
            "ad_growth": 0.0,
            "cvr_adj_pct": 0.0,
            "synergy_tier": "Tier 5: 暂无排名",
            "synergy_tag": "【未建自然权重】",
            "action_type": "",
            "recommended_value": "",
            "reason": "西柚洞察未收录该词自然/广告排名数据，按常规广告绩效调整",
        }

    org_rank = info.get("organic_rank")
    sp_rank = info.get("sp_rank")
    sb_rank = info.get("sb_rank")
    org_traffic = info.get("organic_traffic", 0.0)
    ad_traffic = info.get("ad_traffic", 0.0)
    tot_traffic = info.get("total_traffic", 0.0)
    org_rate = info.get("organic_rate", 0.0)
    ad_rate = info.get("ad_rate", 0.0)
    org_growth = info.get("organic_growth", 0.0)
    ad_growth = info.get("ad_growth", 0.0)
    denom_traffic = tot_traffic if tot_traffic > 0 else (org_traffic + ad_traffic)
    if denom_traffic > 0:
        org_share_pct = round(min(100.0, max(0.0, (org_traffic / denom_traffic) * 100.0)), 1)
        ad_share_pct = round(min(100.0, max(0.0, 100.0 - org_share_pct if abs((org_traffic + ad_traffic) - denom_traffic) <= 1.0 else (ad_traffic / denom_traffic) * 100.0)), 1)
    else:
        org_share_pct = 0.0
        ad_share_pct = 0.0
    actual_acos = acos(spend, sales)
    acos_disp = fmt_acos_text(actual_acos, spend, sales)

    st = scaled_thresholds or {}
    cvr_base = float(st.get("cvr_base_ratio", 0.08))
    prior_k = float(st.get("cvr_prior_k", 20.0))
    cvr_adj = v2p.calc_smoothed_cvr(orders, clicks, cvr_base, prior_k)
    min_sp_over = float(st.get("min_over_acos_spend_30d", 8.0))
    fail_sp = float(st.get("fail_test_spend", 20.0))
    fail_clk = int(st.get("fail_test_clicks", 25))
    fl = max(0.20, float(bid_floor))
    is_fragile_pillar = bool(is_fragile and (orders >= 6 or ord_7d >= 2))

    def _safe_down_bid(cur_b_v: float, normal_mult: float) -> tuple[float, int, str]:
        step_mult = 0.90 if is_fragile_pillar else normal_mult
        step_fl = _ceil_cents(cur_b_v * step_mult)
        eff_fl = max(fl, step_fl)
        if is_fragile_pillar and cpc > 0:
            asb = _ceil_cents((cpc * 0.85) / (1.0 + max(0.0, camp_tos) / 100.0))
            if asb < cur_b_v:
                eff_fl = max(eff_fl, asb)
        nb = max(eff_fl, _ceil_cents(cur_b_v * step_mult))
        if nb >= cur_b_v and cur_b_v > eff_fl:
            nb = max(eff_fl, round(cur_b_v - 0.02, 2))
        dp = max(0, int(round((cur_b_v - nb) / cur_b_v * 100))) if cur_b_v > 0 else 0
        p_note = "（脆弱品核心支柱词单次限降<=10%）" if is_fragile_pillar else ""
        return nb, dp, p_note

    tos_next_note_down = "下期视总单量再决定是否在该专属活动内微压 TOS" if is_dedicated_camp else "（非单/少词专属活动，严禁按单词改全活动 TOS；若需独立控 TOS 建议第 6 节迁入专属精准仓）"
    tos_next_note_up = "下期效果确认后再在该专属活动内小步加注 TOS（目标区间 60%-75%）" if is_dedicated_camp else "（非专属精准活动，本期仅调基础竞价；如需独立冲 TOS 请在第 6 节规划迁入 *-hero-exact 专属仓）"

    action_type = ""
    recom = ""
    reason = ""

    # Tier 1: Organic Rank 1 ~ 3 (§7.6 single-lever compliant)
    if org_rank is not None and org_rank <= 3:
        if org_rank == 1:
            if orders > 0 and actual_acos <= (target_acos * 1.15):
                tier = "Tier 1: 王者双霸屏"
                tag = "【自然#1·双霸屏稳流】"
                action_type = "Defensive_Dominance_Maintain"
                recom = f"${(current_bid or 0.40):.2f}"
                reason = f"自然排名高居全网第 1 名，且广告 ACOS {acos_disp} 处于健康受控区间（平滑CVR {cvr_adj:.1f}%）。SP 与自然位形成双霸屏守流不动，严禁盲关广告防竞品夺位。"
            elif (spend >= min_sp_over and actual_acos > (target_acos * 1.2)) or ((spend >= fail_sp or clicks >= fail_clk) and orders == 0):
                tier = "Tier 1: 防内耗控价"
                tag = "【自然#1·防内耗控价】"
                cur_b = current_bid or 0.40
                new_bid, dp_pct, fp_note = _safe_down_bid(cur_b, 0.82)
                if new_bid < cur_b:
                    action_type = "Throttle_Cannibalization_Bid"
                    recom = f"${new_bid:.2f}"
                    reason = f"自然排名第 1 但广告 ACOS {acos_disp} 超标（单杠杆原则：本期仅下调基础竞价 {dp_pct}% 至 ${new_bid:.2f}{fp_note}，不同期砍 TOS）；启动 3-5 天总单量监控（自然+广告总单跌>=15%立即回调），{tos_next_note_down}。"
                else:
                    recom = f"已触及保护底价 ${cur_b:.2f}，保持观察"
                    reason = f"自然排名第 1 且 ACOS {acos_disp} 偏高，但当前基础竞价 ${cur_b:.2f} 已触及保护底价/脆弱支柱词护栏，保持稳态观察。"
            else:
                tier = "Tier 1: 头部观察"
                tag = "【自然#1·头部观察】"
                recom = "维持当前竞价与展示，观察增量贡献"
                reason = f"自然排名第 1，当前广告花费较低（${spend:.2f} < 阈值 ${min_sp_over:.2f}），维持低成本观察。"
        else:  # org_rank in (2, 3)
            if actual_acos <= (target_acos * 1.2) or spend < min_sp_over:
                tier = "Tier 1: 前三霸屏"
                tag = f"【自然#{org_rank}·保位霸屏】"
                action_type = "Defensive_Dominance_Maintain"
                recom = f"${(current_bid or 0.40):.2f}"
                reason = f"自然位在第 {org_rank} 位，ACOS {acos_disp} 受控，维持基础竞价守位压制竞品。"
            else:
                tier = "Tier 1: 防内耗控价"
                tag = f"【自然#{org_rank}·微调避耗】"
                cur_b = current_bid or 0.40
                new_bid, dp_pct, fp_note = _safe_down_bid(cur_b, 0.85)
                if new_bid < cur_b:
                    action_type = "Throttle_Cannibalization_Bid"
                    recom = f"${new_bid:.2f}"
                    reason = f"自然位第 {org_rank} 位但广告 ACOS {acos_disp} 偏高（单杠杆原则：本期仅下调基础竞价 {dp_pct}% 至 ${new_bid:.2f}{fp_note}，不叠加砍 TOS）；{tos_next_note_down}。"
                else:
                    recom = f"已触及保护底价 ${cur_b:.2f}，保持观察"
                    reason = f"自然位第 {org_rank} 位且 ACOS {acos_disp} 偏高，但当前基础竞价 ${cur_b:.2f} 已触及保护底价/脆弱支柱词护栏，保持稳态观察。"

    # Tier 2: Organic Rank 4 ~ 15 (Critical Flywheel Ascender — single-lever compliant!)
    elif org_rank is not None and 4 <= org_rank <= 15:
        qualifies_ascend = (
            stage_mode != "STOP_LOSS"
            and (
                (orders >= 1 and cvr_adj >= 5.0 and actual_acos <= target_acos * 1.25)
                or (clicks >= 5 and orders >= 1 and actual_acos <= target_acos * 1.15)
                or (clicks >= 3 and orders >= 1 and actual_acos <= target_acos)
            )
        )
        if qualifies_ascend:
            if sp_rank == 1:
                tier = "Tier 2: 顶展霸位"
                tag = "【广告#1·顶展守位】"
                action_type = "Defensive_Dominance_Maintain"
                new_bid = current_bid or 0.35
                recom = f"${new_bid:.2f}"
                reason = f"自然位第 {org_rank} 位，广告位已居 SP #1 顶展（平滑CVR {cvr_adj:.1f}%），严禁追加提价，保持 ${new_bid:.2f} 稳位助推自然排位。"
            else:
                tier = "Tier 2: 临界冲刺"
                tag = "【临界冲刺·拉升自然位】"
                action_type = "Boost_Ascender_TOS"
                new_bid = _floor_cents((current_bid or 0.35) * 1.15)
                recom = f"${new_bid:.2f}"
                reason = f"自然位第 {org_rank} 位处于临界突破期，转化达标（{orders}单，平滑CVR {cvr_adj:.1f}%，ACOS {acos_disp}）。按单杠杆原则本期先上调基础竞价 +15% 至 ${new_bid:.2f}（不同期加 TOS），{tos_next_note_up}。"
        elif (spend >= fail_sp or clicks >= fail_clk) and orders == 0:
            tier = "Tier 2: 冲刺受阻"
            tag = "【冲刺受阻·控频观察】"
            cur_b = current_bid or 0.40
            new_bid, dp_pct, fp_note = _safe_down_bid(cur_b, 0.80)
            if new_bid < cur_b:
                action_type = "Throttle_TOS_Bid"
                recom = f"${new_bid:.2f}"
                reason = f"自然位第 {org_rank} 位但已达失败测试阈值（花费 ${spend:.2f} / {clicks} 点击 0 单），单步柔性降价 -{dp_pct}% 至 ${new_bid:.2f}{fp_note} 控频避损。"
            else:
                recom = f"已触及保护底价 ${cur_b:.2f}，保持观察"
                reason = f"自然位第 {org_rank} 位达失败测试阈值，当前基础竞价 ${cur_b:.2f} 已触及保护底价。"
        else:
            tier = "Tier 2: 临界培育"
            tag = "【临界培育·蓄力突破】"
            recom = "保持竞价，紧盯转化与自然排名上升趋势"
            reason = f"自然位第 {org_rank} 位（平滑CVR {cvr_adj:.1f}%），保持观察，达标后按单杠杆阶梯冲刺。"

    # Tier 3: Organic Rank 16 ~ 50 (Mid-Page Breakthrough — <=-20% step cap!)
    elif org_rank is not None and 16 <= org_rank <= 50:
        if orders >= 2 and actual_acos <= target_acos:
            tier = "Tier 3: 腰部突围"
            tag = "【腰部突围·单向拉升】"
            recom = "建议精准 Exact 隔离提权投放，向首页冲刺"
            reason = f"自然位在第 {org_rank} 位（第2-3页），转化优异（{orders}单，平滑CVR {cvr_adj:.1f}%，ACOS {acos_disp}），建议迁入精准/专属仓冲首页。"
        elif (spend >= fail_sp or clicks >= fail_clk) and orders == 0:
            tier = "Tier 3: 腰部承压"
            tag = "【腰部承压·防守退让】"
            cur_b = current_bid or 0.35
            new_bid, dp_pct, fp_note = _safe_down_bid(cur_b, 0.80)
            if new_bid < cur_b:
                action_type = "Lower_Bid"
                recom = f"${new_bid:.2f}"
                reason = f"自然位第 {org_rank} 位且达失败测试阈值（花费 ${spend:.2f} / {clicks} 点击 0 单），按步长上限柔性下调基础竞价 -{dp_pct}% 至 ${new_bid:.2f}{fp_note} 退守避损。"
            else:
                recom = f"已触及保护底价 ${cur_b:.2f}，保持观察"
                reason = f"自然位第 {org_rank} 位达失败测试阈值，当前基础竞价 ${cur_b:.2f} 已触及保护底价。"
        else:
            tier = "Tier 3: 腰部常态"
            tag = "【腰部常态·平价测流】"
            recom = "保持中等出价，不给高额 TOS 溢价"
            reason = f"自然位第 {org_rank} 位，处于常态腰部位置，按标准 ROI 与 TCPA 控价。"

    # Tier 4: Organic Rank > 50 or Unranked
    else:
        if sp_rank is not None and sp_rank <= 10:
            tier = "Tier 4: 自然脱节"
            tag = "【自然脱节·警惕虚火】"
            if spend >= min_sp_over and actual_acos > target_acos:
                cur_b = current_bid or 0.40
                new_bid, dp_pct, fp_note = _safe_down_bid(cur_b, 0.85)
                if new_bid < cur_b:
                    action_type = "Throttle_TOS_Bid"
                    recom = f"${new_bid:.2f}"
                else:
                    action_type = ""
                    recom = "已触及保护底价，严禁高溢价打顶展并联动第 9 节补 Listing 埋词"
            else:
                action_type = ""
                recom = "严禁高溢价打顶展，控频并联动第 9 节补 Listing 埋词"
            reason = f"SP 广告位第 {sp_rank} 位，但自然位严重脱节（第 {org_rank or '>50'} 位）。底层相关性未稳，禁买高溢价顶展，单步降价控频并排查 Listing 埋词与承接。"
        else:
            tier = "Tier 4: 长尾边缘"
            tag = "【长尾深水·按效出价】"
            recom = "严格按 ACOS/平滑CVR 控价，达标则投，超标即降"
            reason = f"自然位第 {org_rank or '>50'} 位，长尾边缘流量，按保本公式与 TCPA 精细化调整。"

    return {
        "organic_rank": org_rank,
        "organic_page": info.get("organic_page"),
        "sp_rank": sp_rank,
        "sp_page": info.get("sp_page"),
        "sb_rank": sb_rank,
        "organic_traffic": org_traffic,
        "ad_traffic": ad_traffic,
        "total_traffic": tot_traffic,
        "organic_share_pct": org_share_pct,
        "ad_share_pct": ad_share_pct,
        "organic_rate": org_rate,
        "ad_rate": ad_rate,
        "organic_growth": org_growth,
        "ad_growth": ad_growth,
        "cvr_adj_pct": cvr_adj,
        "synergy_tier": tier,
        "synergy_tag": tag,
        "action_type": action_type,
        "recommended_value": recom,
        "reason": reason,
    }


def extract_asin_target_info(
    r: dict,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> dict | None:
    """Extract competitor ASIN target metadata from target row with ad-group default_bid inheritance and clean ACOS."""
    expr = r.get("expression") or r.get("targeting_text") or ""
    asin_val = None
    if isinstance(expr, str):
        m = re.search(r"b0[0-9a-z]{8}", expr, re.I)
        if m:
            asin_val = m.group(0).upper()
    elif isinstance(expr, list):
        for item in expr:
            if isinstance(item, dict) and item.get("type") == "asinSameAs":
                asin_val = str(item.get("value", "")).upper()
                break
    if not asin_val:
        key = str(r.get("key") or "")
        m = re.search(r"b0[0-9a-z]{8}", key, re.I)
        if m:
            asin_val = m.group(0).upper()
    if not asin_val:
        return None

    title = r.get("asin_title") or ""
    price = num(r.get("asin_price"))
    stars = num(r.get("asin_stars"))
    reviews = int(num(r.get("asin_review_count")))
    cur_bid = num(r.get("bid"))
    def_bid = num(r.get("default_bid"))
    sp_t = num(r.get("spends"))
    sl_t = num(r.get("sales"))
    base_bid, is_inherited = resolve_effective_entity_bid(
        r, group_bid_lookup=group_bid_lookup, camp_group_bid_lookup=camp_group_bid_lookup, fallback=0.40
    )
    return {
        "asin": asin_val,
        "title": title,
        "price": price,
        "stars": stars,
        "reviews": reviews,
        "spend": sp_t,
        "sales": sl_t,
        "orders": int(num(r.get("orders"))),
        "clicks": int(num(r.get("clicks"))),
        "cpc": num(r.get("cpc")),
        "acos": clean_api_acos(r.get("acos"), sp_t, sl_t),
        "campaign_name": r.get("campaign_name") or "",
        "base_bid": base_bid,
        "instant_bid": round(cur_bid, 2),
        "bid": cur_bid,
        "default_bid": def_bid if def_bid > 0 else (base_bid if is_inherited else 0.0),
        "bid_inherited": is_inherited,
    }


def evaluate_competitor_target(comp: dict, our_profile: dict, target_acos: float, is_fragile: bool = False, is_high_refund: bool = False, camp_pp: float = 0.0, scaled_thresholds: dict | None = None, bid_floor_asin: float = 0.36, bid_floor_asin_fragile: float = 0.42) -> tuple[str, str, str]:
    """Compare product competitiveness against competitor ASIN and decide action.
    v2 §7.3: ASIN Product Targeting Dedicated Bid Floor (configurable) & TCPA-scaled bleed threshold.
    Returns (action, recommended_value, reason).
    """
    comp_asin = comp["asin"]
    comp_price = comp["price"]
    comp_stars = comp["stars"]
    comp_reviews = comp["reviews"]

    our_price = num(our_profile.get("price"))
    our_stars = num(our_profile.get("stars"))
    our_reviews = int(num(our_profile.get("reviews")))

    spend = comp["spend"]
    orders = comp["orders"]
    c_acos = comp["acos"]
    c_acos_disp = fmt_acos_text(c_acos, spend, orders)
    cpc = comp["cpc"]
    st = scaled_thresholds or {}
    min_bleed_sp = max(10.0, float(st.get("min_over_acos_spend_30d", 10.0)))

    # 1. Winnable competitor target
    if orders >= 3 and c_acos <= target_acos:
        return (
            "Harvest_ASIN",
            "Keep & Scale Dedicated ASIN Campaign",
            f"Winnable competitor page: orders={orders}, ACOS={c_acos:.1f}% <= {target_acos:.0f}%. Our product converts successfully against {comp_asin}."
        )

    # 2. Deficit / High bleed competitor target
    if spend >= min_bleed_sp and (orders == 0 or c_acos >= target_acos * 1.5):
        has_massive_reviews = (comp_reviews > max(our_reviews * 3, 3000) and comp_stars >= our_stars)
        is_substantially_cheaper = (comp_price > 0 and our_price > 0 and comp_price < our_price * 0.75)

        if has_massive_reviews or is_substantially_cheaper:
            reason_detail = []
            if has_massive_reviews:
                reason_detail.append(f"competitor review moat is overwhelming ({comp_reviews} reviews vs our {our_reviews})")
            if is_substantially_cheaper:
                reason_detail.append(f"competitor price is much cheaper (${comp_price:.2f} vs our ${our_price:.2f})")
            return (
                "Negative_Target",
                "Add Negative Target / Pause Target",
                f"Structurally unwinnable competitor: {'; '.join(reason_detail)}. High spend=${spend:.2f} with {orders} orders. Negate to eliminate waste."
            )

        # Competitor price is similar or higher, and our product has feature parity or advantages
        price_similar_or_higher = (comp_price >= our_price * 0.85) if (comp_price > 0 and our_price > 0) else True
        if price_similar_or_higher:
            target_floor = float(bid_floor_asin_fragile) if (is_fragile or is_high_refund) else float(bid_floor_asin)
            cur_base = num(comp.get("base_bid") or 0.40)

            # If current base bid is already at or below floor, DO NOT lower further (prevent shock)
            if cur_base <= target_floor:
                suggested_bid = cur_base
                return (
                    "Keep_Monitoring",
                    f"${suggested_bid:.2f}",
                    f"我品具备性价比优势（竞品 ${comp_price:.2f} vs 本品 ${our_price:.2f}）。当前基础竞价 ${cur_base:.2f} 已触及商品页安全底价 (${target_floor:.2f})，严禁盲目继续砍出价导致断流；建议保持当前基础竞价，若投产仍超标，优先通过微调活动商品页溢价 (PP) 控费。"
                )

            # Step down relative to CURRENT BASE BID, or reverse-engineer from CPC discounting PP modifier
            bid_from_base = _ceil_cents(cur_base * 0.88)
            bid_from_cpc = round((cpc * 0.85) / (1.0 + (camp_pp / 100.0)), 2) if (cpc > 0 and camp_pp > 0) else bid_from_base
            step_cap_floor = _ceil_cents(cur_base * (0.90 if is_fragile else 0.80))

            suggested_bid = max(target_floor, step_cap_floor, min(bid_from_base, bid_from_cpc))
            if suggested_bid >= cur_base:
                suggested_bid = max(target_floor, step_cap_floor, round(cur_base - 0.02, 2))

            drop_pct = max(1, int(round((cur_base - suggested_bid) / cur_base * 100)))
            eff_old_str = f" (折合商品页有效出价 ${(cur_base * (1.0 + camp_pp / 100.0)):.2f})" if camp_pp > 0 else ""
            eff_new_str = f"，拉平商品页有效扣费至 ${(suggested_bid * (1.0 + camp_pp / 100.0)):.2f}" if camp_pp > 0 else ""

            return (
                "Lower_Bid",
                f"${suggested_bid:.2f}",
                f"我品具备性价比与功能优势（竞品 ${comp_price:.2f} vs 本品 ${our_price:.2f}，评分 {comp_stars} vs {our_stars}）。当前 ACOS {c_acos_disp} 偏高系单次点击成本偏高 (${cpc:.2f})；建议将基础竞价阶梯下调 {drop_pct}%（由 ${cur_base:.2f}{eff_old_str} 降至 ${suggested_bid:.2f}，底价保护 ${target_floor:.2f}）{eff_new_str}，低成本捡漏防守收割。"
            )
        else:
            return (
                "Negative_Target",
                "Add Negative Target",
                f"Competitor conversion advantage; high spend (${spend:.2f}) with zero/poor returns (ACOS={c_acos_disp})."
            )

    return ("Keep_Monitoring", "Keep current bid", "Performance within normal observation range")


def extract_ngrams(text: str) -> list[tuple[str, str]]:
    """Extract deduplicated 1-grams and 2-grams per query after cleaning and stopword removal.

    Returns list of unique (ngram, gram_type) preserving first-seen order so repeated tokens
    in a single query (e.g. 'coffee grinder coffee') are NEVER double-counted!
    """
    words = re.findall(r"[\w]+", (text or "").lower(), re.UNICODE)
    valid_words = [w for w in words if w not in STOP_WORDS and len(w) > 1 and not w.isdigit()]
    out = []
    seen = set()
    for w in valid_words:
        item = (w, "1-gram")
        if item not in seen:
            seen.add(item)
            out.append(item)
    for i in range(len(valid_words) - 1):
        bigram = f"{valid_words[i]} {valid_words[i+1]}"
        item = (bigram, "2-gram")
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


MIN_KNOWN_NEGATIVE_LEN = 3


def is_known_negative(text: str, known_negatives: set[str]) -> bool:
    """Match a query against the already-negated list.

    Substring matching needs a length floor: without it a 1-2 character entry
    (or a stray blank line in known_negatives.txt) suppresses every recommendation.
    """
    t = (text or "").lower().strip()
    if not t or not known_negatives:
        return False
    for kn in known_negatives:
        if kn == t:
            return True
        if len(kn) >= MIN_KNOWN_NEGATIVE_LEN and kn in t:
            return True
    return False


def tsv(path: Path, header: str, lines: list[str]) -> None:
    path.write_text(header + "\n" + "\n".join(lines), encoding="utf-8")


# ==============================================================================
# ACTIVE NEGATIVE AUDIT & RANK PROTECTION MODULE
# ==============================================================================

def _extract_static_negative_text(r: dict, is_target: bool = False) -> tuple[str, str]:
    """Extract (negative_text_or_asin, normalized_match_type) from a LingXing static negative row."""
    mt = str(
        r.get("negative_match_type")
        or r.get("match_type")
        or r.get("negative_type")
        or r.get("expression_type")
        or ("negativeTarget" if is_target else "negativeExact")
    ).strip()
    if is_target:
        for fld in ("negative_text", "target_text", "asin", "target_value", "targeting_text", "expression", "resolved_expression", "name"):
            val = r.get(fld)
            if isinstance(val, str) and val.strip() and val.strip() not in ("None", "null", "--"):
                m_a = re.search(r"(b0[0-9a-z]{8})", val, re.I)
                if m_a:
                    return m_a.group(1).upper(), "asinSameAs"
                return val.strip(), mt or "negativeTarget"
            elif isinstance(val, list):
                for sub in val:
                    if isinstance(sub, dict) and sub.get("value"):
                        sv = str(sub.get("value")).strip()
                        m_a = re.search(r"(b0[0-9a-z]{8})", sv, re.I)
                        if m_a:
                            return m_a.group(1).upper(), "asinSameAs"
                        return sv, str(sub.get("type") or mt or "negativeTarget")
    else:
        for fld in ("negative_text", "keyword_text", "negative_keyword", "targeting_text", "query", "text", "name"):
            val = r.get(fld)
            if isinstance(val, str) and val.strip() and val.strip() not in ("None", "null", "--"):
                return val.strip(), mt or "negativeExact"
    return "", mt


def extract_active_negatives(
    sts: list[dict],
    src: Path | None = None,
    camps: list[dict] | None = None,
    asin_campaigns: set[str] | None = None,
    known_negatives_out: set[str] | None = None,
) -> tuple[dict[tuple[str, str], dict], set[tuple[str, str]], set[str]]:
    """Extract all active negative search term records from search_terms report AND static negative JSON libraries
    (sp_negative_keywords.json, sp_negative_targets.json, hsa_negative_keywords.json, hsa_negative_targets.json).
    Gracefully compatible when new JSON files are absent in legacy bundles.
    """
    active_neg_dict = {}
    active_neg_camp_pairs = set()
    all_neg_queries = set()

    for r in sts:
        if skip_total(r):
            continue
        st_neg = r.get("st_negative")
        kw_neg = r.get("kw_negative")
        neg_val = str(st_neg or kw_neg or "").strip()
        if not neg_val or neg_val in ("--", "0", "null", "None"):
            continue

        c_name = r.get("campaign_name") or ""
        q = (r.get("query") or "").strip()
        if not q or not c_name:
            continue

        ql = q.lower()
        cl = c_name.lower()
        key = (c_name, ql)
        active_neg_camp_pairs.add((cl, ql))
        all_neg_queries.add(ql)

        spend = num(r.get("spends"))
        sales = num(r.get("sales"))
        orders = int(num(r.get("orders")))
        clicks = int(num(r.get("clicks")))

        if key not in active_neg_dict:
            active_neg_dict[key] = {
                "campaign": c_name,
                "query": q,
                "neg_status": neg_val,
                "match_type": r.get("match_type") or r.get("target_match_type") or "",
                "target_text": r.get("keyword_text") or r.get("target_text") or "",
                "spend": spend,
                "sales": sales,
                "orders": orders,
                "clicks": clicks,
            }
        else:
            active_neg_dict[key]["spend"] += spend
            active_neg_dict[key]["sales"] += sales
            active_neg_dict[key]["orders"] += orders
            active_neg_dict[key]["clicks"] += clicks

    # Item 10: Merge static negative keyword & target files fetched by fetch_ads_bundle.py
    if src is not None and Path(src).exists():
        camp_by_id = {
            str(c.get("id")).strip(): str(c.get("name") or "").strip()
            for c in (camps or [])
            if c.get("id") is not None and c.get("name")
        }
        static_sources = (
            ("sp_negative_keywords.json", False, "SP静态否定词"),
            ("sp_negative_targets.json", True, "SP静态否定商品"),
            ("hsa_negative_keywords.json", False, "HSA静态否定词"),
            ("hsa_negative_targets.json", True, "HSA静态否定商品"),
        )
        for fname, is_tg, label_prefix in static_sources:
            fpath = Path(src) / fname
            if not fpath.exists():
                continue
            for r in as_rows(load_json(fpath)):
                if not isinstance(r, dict):
                    continue
                st_state = str(r.get("state") or r.get("status") or "enabled").lower().strip()
                if st_state in ("archived", "deleted", "paused", "0", "removed"):
                    continue
                cid = str(r.get("campaign_id") or "").strip()
                c_name = str(r.get("campaign_name") or camp_by_id.get(cid) or "").strip()
                if asin_campaigns and c_name and c_name not in asin_campaigns and not fname.startswith("hsa_"):
                    continue
                neg_txt, neg_mt = _extract_static_negative_text(r, is_target=is_tg)
                if not neg_txt:
                    continue
                ql = neg_txt.lower().strip()
                if not c_name:
                    c_name = "Portfolio_HSA_Shared" if fname.startswith("hsa_") else "Portfolio_SP_Static"
                cl = c_name.lower()
                active_neg_camp_pairs.add((cl, ql))
                all_neg_queries.add(ql)
                if known_negatives_out is not None and ("phrase" in neg_mt.lower() or c_name.startswith("Portfolio_")):
                    known_negatives_out.add(ql)
                key = (c_name, ql)
                scope_lbl = "活动级" if not r.get("ad_group_id") or str(r.get("ad_group_id")) in ("0", "None", "") else "广告组级"
                status_desc = f"{label_prefix}({scope_lbl}:{neg_mt})"
                if key not in active_neg_dict:
                    active_neg_dict[key] = {
                        "campaign": c_name,
                        "query": neg_txt,
                        "neg_status": status_desc,
                        "match_type": neg_mt,
                        "target_text": str(r.get("ad_group_name") or scope_lbl),
                        "spend": num(r.get("spends") or r.get("spend") or r.get("cost")),
                        "sales": num(r.get("sales")),
                        "orders": int(num(r.get("orders"))),
                        "clicks": int(num(r.get("clicks"))),
                    }

    return active_neg_dict, active_neg_camp_pairs, all_neg_queries


def find_exact_harvest_campaign(camps: list[dict], kws: list[dict]) -> str:
    """Find the best ENABLED campaign for harvesting exact terms (e.g. *-exact-prove or *-精准)."""
    for c in camps:
        if c.get("state") == "enabled":
            name = c.get("name") or c.get("campaign_name") or ""
            if "exact-prove" in name.lower() or "prove" in name.lower():
                return name
    for c in camps:
        if c.get("state") == "enabled":
            name = c.get("name") or c.get("campaign_name") or ""
            if "exact" in name.lower() or "精准" in name:
                return name
    for k in kws:
        if k.get("state") == "enabled":
            name = k.get("campaign_name") or ""
            if "exact-prove" in name.lower() or "prove" in name.lower():
                return name
    enabled_camps = [c for c in camps if c.get("state") == "enabled"]
    if enabled_camps:
        first_c = enabled_camps[0].get("name") or ""
        parts = first_c.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}-SP-EXACT-PROVE"
    return "SP-EXACT-PROVE"


def calc_dynamic_migration_bid(
    orig_item: dict,
    dest_camp_name: str,
    camp_lookup: dict,
    prior_target: dict = None,
) -> tuple[float, float, str]:
    """Calculate dynamic inverted base bid for keyword migration/unpausing."""
    dest_camp_obj = camp_lookup.get(dest_camp_name, {})
    dest_tos = float(dest_camp_obj.get("tos") or dest_camp_obj.get("placement_top") or 0.0)

    orig_orders = orig_item.get("orders", 0)
    orig_clicks = orig_item.get("clicks", 0)
    orig_spend = orig_item.get("spend", 0.0)
    orig_cpc = round(orig_spend / orig_clicks, 2) if orig_clicks else 0.60

    if orig_orders >= 1:
        anchor_cpc = orig_cpc
        cpc_note = f"原活动历史出单 CPC (${anchor_cpc:.2f})"
    elif orig_clicks >= 5:
        anchor_cpc = round(orig_cpc * 0.85, 2)
        cpc_note = f"原活动测试 CPC 折扣价 (${anchor_cpc:.2f})"
    else:
        anchor_cpc = round(orig_cpc, 2) if orig_cpc > 0 else 0.65
        cpc_note = f"基准出价参考 (${anchor_cpc:.2f})"

    recom_base_bid = round(anchor_cpc / (1.0 + dest_tos / 100.0), 2)
    recom_base_bid = max(0.25, min(recom_base_bid, 1.20))
    eff_tos_bid = round(recom_base_bid * (1.0 + dest_tos / 100.0), 2)

    prior_notes = []
    if prior_target:
        prior_bid = float(prior_target.get("bid") or 0)
        prior_cpc = float(prior_target.get("cpc") or 0)
        if prior_bid > 0 and prior_cpc > anchor_cpc:
            prior_notes.append(
                f"此前在精准仓基础竞价设置偏高 (${prior_bid:.2f})，叠加 TOS +{int(dest_tos)}% 导致实际 CPC 飙升至 ${prior_cpc:.2f} 而误暂停"
            )

    calc_explanation = (
        f"依据{cpc_note}与目标仓 TOS +{int(dest_tos)}% 溢价，"
        f"动态倒推建议基础竞价为 ${recom_base_bid:.2f}（折合顶部出价 ${eff_tos_bid:.2f}，精准咬合出单成本）。"
    )
    if prior_notes:
        calc_explanation += f" 历史对账：{'; '.join(prior_notes)}。"

    return recom_base_bid, eff_tos_bid, calc_explanation


def _resolve_search_term_real_bid(
    c_name: str,
    match_type: str,
    target_text: str,
    kw_lookup: dict,
    camp_obj: dict,
    target_bid_lookup: dict | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    ad_group_id: str = "",
    ad_group_name: str = "",
    target_match_type: str = "",
) -> float:
    """Item 6: Penetrate keywords -> targets (auto 4-groups + ASIN/category) -> ad_groups -> campaign default_bid
    so search terms NEVER fall back to a fake $0.60 default bid when real bid data exists!"""
    mt_l = (match_type or "").lower().strip()
    tt_l = (target_text or "").lower().strip()
    tmt_l = (target_match_type or "").lower().strip()

    # 1. Exact keyword lookup (c_name, match_type, target_text)
    if mt_l and tt_l:
        kw_obj = kw_lookup.get(f"{c_name}::{mt_l}::{tt_l}")
        if kw_obj and num(kw_obj.get("bid")) > 0:
            return round(float(kw_obj["bid"]), 2)
    if tt_l:
        for cand_m in ("exact", "phrase", "broad"):
            kw_obj = kw_lookup.get(f"{c_name}::{cand_m}::{tt_l}")
            if kw_obj and num(kw_obj.get("bid")) > 0:
                return round(float(kw_obj["bid"]), 2)

    # 2. Target lookup (auto close-match/loose-match/substitutes/complements + ASIN/category targets)
    if target_bid_lookup:
        cand_keys = []
        if tmt_l:
            cand_keys.append((c_name, tmt_l))
            if ad_group_id:
                cand_keys.append((ad_group_id, tmt_l))
        if mt_l:
            cand_keys.append((c_name, mt_l))
        if tt_l:
            cand_keys.append((c_name, tt_l))
            m_asin = re.search(r"(b0[0-9a-z]{8})", tt_l, re.I)
            if m_asin:
                asin_l = m_asin.group(1).lower()
                cand_keys.append((c_name, asin_l))
                cand_keys.append((c_name, f"asinsameas::{asin_l}"))
            m_type = re.search(r'"type"\s*:\s*"([^"]+)"', target_text or "")
            if m_type:
                et_l = m_type.group(1).lower()
                cand_keys.append((c_name, et_l))
                if ad_group_id:
                    cand_keys.append((ad_group_id, et_l))
        for ck in cand_keys:
            tb = target_bid_lookup.get(ck)
            if tb and num(tb) > 0:
                return round(float(tb), 2)

    # 3. Ad-group default_bid lookup
    if group_bid_lookup:
        ag_id_s = str(ad_group_id or "").strip()
        if ag_id_s and ag_id_s in group_bid_lookup and num(group_bid_lookup[ag_id_s]) > 0:
            return round(float(group_bid_lookup[ag_id_s]), 2)
        if c_name and ad_group_name and (c_name, ad_group_name) in group_bid_lookup:
            return round(float(group_bid_lookup[(c_name, ad_group_name)]), 2)

    # 4. Campaign-level ad-group default_bid or campaign default_bid
    if camp_group_bid_lookup and c_name in camp_group_bid_lookup and num(camp_group_bid_lookup[c_name]) > 0:
        return round(float(camp_group_bid_lookup[c_name]), 2)
    if num(camp_obj.get("default_bid")) > 0:
        return round(float(camp_obj["default_bid"]), 2)
    if target_bid_lookup and (c_name, "__any__") in target_bid_lookup:
        return round(float(target_bid_lookup[(c_name, "__any__")]), 2)

    return 0.45


def infer_search_term_placement(
    query: str,
    spend: float,
    sales: float,
    orders: int,
    clicks: int,
    impressions: int,
    c_name: str,
    match_type: str,
    target_text: str,
    bucket: str,
    is_strong: bool,
    v_tier: str,
    sr_display: str,
    camp_lookup: dict,
    kw_lookup: dict,
    target_acos: float = 25.0,
    bleeding_ng_set: set[str] | None = None,
    target_bid_lookup: dict | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    ad_group_id: str = "",
    ad_group_name: str = "",
    target_match_type: str = "",
    scaled_thresholds: dict | None = None,
) -> dict:
    """Infer search term placement using multi-dimensional behavioral fingerprinting + real target/adgroup bid binding + TCPA-scaled thresholds."""
    camp_obj = camp_lookup.get(c_name, {})

    ctr = round((clicks / impressions * 100.0) if impressions > 0 else 0.0, 2)
    cpc = round((spend / clicks) if clicks > 0 else 0.0, 2)
    base_bid = _resolve_search_term_real_bid(
        c_name=c_name,
        match_type=match_type,
        target_text=target_text,
        kw_lookup=kw_lookup,
        camp_obj=camp_obj,
        target_bid_lookup=target_bid_lookup,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        ad_group_id=ad_group_id,
        ad_group_name=ad_group_name,
        target_match_type=target_match_type,
    )
    tos_pct = float(camp_obj.get("tos", 0))
    pp_pct = float(camp_obj.get("pp", 0))
    ros_pct = float(camp_obj.get("ros", 0))
    cohort_pct = float(camp_obj.get("cohort_bid_pct", 0))
    cohort_mult = 1.0 + (cohort_pct / 100.0) if cohort_pct > 0 else 1.0
    is_up_down = bool(camp_obj.get("is_up_and_down"))
    non_tos_mult = 1.5 if is_up_down else 1.0
    tos_mult = 2.0 if is_up_down else 1.0
    max_non_tos = round(base_bid * (1.0 + max(pp_pct, ros_pct) / 100.0) * non_tos_mult * cohort_mult, 2)
    max_tos_bid = round(base_bid * (1.0 + tos_pct / 100.0) * tos_mult * cohort_mult, 2)

    # Check if campaign has real placement report evidence showing >=75% spend in a single placement
    real_p = camp_obj.get("real_placements_30d") or {}
    tos_spend_share = num((real_p.get("TOS") or {}).get("spend_share"))
    pp_spend_share = num((real_p.get("PP") or {}).get("spend_share"))

    # Inferred placement
    if base_bid > 0 and cpc > (max_non_tos * 1.02):
        inferred_pos = "CONFIRMED_TOS"
        pos_reason = f"实际CPC (${cpc:.2f}) > 非TOS最高竞价上限 (${max_non_tos:.2f})，数学确定为首页顶部扣费"
    elif tos_spend_share >= 80.0 and ctr >= 0.8:
        inferred_pos = "CONFIRMED_TOS"
        pos_reason = f"活动真实广告位报告显示首页顶部花费占比高达 {tos_spend_share:.1f}%，结合 CTR ({ctr:.2f}%) 确认为顶展流量"
    elif ctr >= 1.5:
        inferred_pos = "PROBABLE_TOS"
        pos_reason = f"高CTR ({ctr:.2f}%) 符合首页顶部黄金展示位特征"
    elif (ctr <= 0.40 and impressions >= 500) or (pp_spend_share >= 80.0 and ctr <= 0.65):
        inferred_pos = "CONFIRMED_PP_CAROUSEL"
        pos_reason = f"极低CTR ({ctr:.2f}%) 与高曝光 ({impressions})，典型商品详情页轮播位/底部位"
    elif ctr <= 0.65:
        inferred_pos = "PROBABLE_PP_OR_ROS"
        pos_reason = f"低CTR ({ctr:.2f}%)，偏向商品页或搜索结果底部位"
    else:
        inferred_pos = "MIXED_PLACEMENT"
        pos_reason = f"中等CTR ({ctr:.2f}%) 与中等CPC (${cpc:.2f})，混合展示位"

    query_ngrams = {ng for ng, _ in extract_ngrams(query)} if query else set()
    matched_bleed_ngs = sorted(query_ngrams & bleeding_ng_set) if bleeding_ng_set else []

    # Actionable diagnosis
    action_type = None
    recommended_val = None
    diagnostic_reason = None
    strategy_tag = "NORMAL"

    st = scaled_thresholds or {}
    fail_clicks = int(st.get("fail_test_clicks", 25))
    fail_spend = float(st.get("fail_test_spend", 20.0))
    min_over_acos_sp = float(st.get("min_over_acos_spend_30d", 10.0))
    tcpa_v = float(st.get("tcpa", 8.0))
    periph_neg_clicks = max(18, int(round(fail_clicks * 0.72)))
    periph_neg_spend = max(12.0, round(tcpa_v * 1.5, 2))
    asin_neg_spend = max(6.0, round(tcpa_v * 1.0, 2))

    if orders >= 1:
        current_acos = acos(spend, sales)
        if current_acos <= target_acos and (orders >= 3 or spend >= min_over_acos_sp):
            strategy_tag = "CONVERTING_WINNER"
        elif current_acos > target_acos * 1.15 and spend >= min_over_acos_sp:
            strategy_tag = "CONVERTING_OVER_ACOS"
            action_type = "Throttle_TOS_Bid"
            recommended_val = "动态降竞价控保本：ACOS超标，依平滑转化率阶梯下调基础竞价"
            diagnostic_reason = (
                f"投放出单词投产超标（{orders}单，花费 ${spend:.2f}，ACOS {fmt_acos_text(current_acos, spend, sales)} > 目标 {target_acos:.0f}%），"
                f"依贝叶斯平滑转化率动态下调基础竞价控保本，坚决不暂停不误杀。"
            )
        else:
            strategy_tag = "CONVERTING_MARGINAL"
    elif clicks >= 8 or spend >= max(6.0, round(tcpa_v * 0.8, 2)):
        if is_asin_term(query):
            if clicks >= 8 or spend >= asin_neg_spend:
                strategy_tag = "BLEEDING_ASIN_QUERY"
                action_type = "Negative_Target"
                recommended_val = f"Add Negative Target (asinSameAs={query.upper()})"
                diagnostic_reason = f"竞品详情页 ASIN 流量 ({query.upper()}) 累积 {clicks} 点击 0 订单 (花费 ${spend:.2f})，商品投放否定止血。"
        elif bucket in ("is_not", "competitor_brand") or str(bucket).startswith("is_not"):
            strategy_tag = "HARD_MISFIT"
            action_type = "Negative_Exact"
            recommended_val = "Add Negative Exact"
            diagnostic_reason = (
                f"物理冲突或竞品品牌词 ({bucket})，已累积 {clicks} 点击 0 订单 (花费 ${spend:.2f})，"
                f"无论在任何位置展示均无法形成正向转化，果断精准否定彻底止血。"
            )
        elif matched_bleed_ngs:
            # Rule 56 Veto: Never elevate or harvest a 0-order query containing a confirmed bleeding N-gram root!
            strategy_tag = "BLEEDING_NGRAM_VETO"
            action_type = "Negative_Exact" if (clicks >= 12 or spend >= max(9.0, tcpa_v)) else "Lower_Bid"
            recommended_val = "Add Negative Exact" if action_type == "Negative_Exact" else "Lower Base Bid 20%"
            diagnostic_reason = (
                f"命中高耗零单出血词根 ({', '.join(matched_bleed_ngs)})，累计 {clicks} 点击 0 订单 (花费 ${spend:.2f})，"
                f"触发词根一票否决（严禁误判为受困好词加价提权），执行止血降权/否定。"
            )
        elif (is_strong or bucket in ("core", "feature", "core_feature", "core_or_title") or (v_tier in ("MEGA_HEAD", "CORE_HEAD", "MID_TAIL") and bucket not in ("adjacent", "is_not", "competitor_brand") and not str(bucket).startswith("is_not"))):
            if clicks >= fail_clicks or spend >= fail_spend:
                strategy_tag = "FAILED_EXTENDED_TEST"
                action_type = "Throttle_TOS_Bid"
                recommended_val = "降竞价防守：大样本高耗未转化，降价控费防守"
                diagnostic_reason = (
                    f"核心相关词已达失败测试阈值 ({clicks} 点击 >= {fail_clicks} 或花费 ${spend:.2f} >= ${fail_spend:.2f}) 但仍为 0 订单。"
                    f"数据证实该流量在本品无法有效转化，严禁盲目加价提权！应降基础竞价退守防守位控费避损。"
                )
            elif inferred_pos in ("CONFIRMED_PP_CAROUSEL", "PROBABLE_PP_OR_ROS"):
                strategy_tag = "TRAPPED_CORE_HERO"
                action_type = "Elevate_To_TOS"
                recommended_val = "提权拯救：提高基础竞价 15% 或移入专用精准仓测试顶部"
                diagnostic_reason = (
                    f"核心主粮/功能词（ABA梯队: {v_tier}，排名: {sr_display}），当前 CTR 仅 {ctr:.2f}%、CPC ${cpc:.2f}，"
                    f"数据指纹证实点击耗散在低效商品详情页或底部位 ({inferred_pos})，未获首屏顶部（TOS）充分验证。"
                    f"严禁盲目否定，适度提权测试首屏真实转化力。"
                )
            elif inferred_pos in ("CONFIRMED_TOS", "PROBABLE_TOS"):
                strategy_tag = "FAILED_AT_TOS"
                action_type = "Throttle_TOS_Bid"
                recommended_val = "降竞价防守：基础竞价下调 15-20% 并排查 Listing 首屏转化壁垒"
                diagnostic_reason = (
                    f"核心主粮词在首页顶部黄金位已积累 {clicks} 次高意图点击 (CTR: {ctr:.2f}%, CPC: ${cpc:.2f}) 依然未出单。"
                    f"已享受顶展红利但未转化，严禁继续加价或推高 TOS；应阶梯下调基础竞价退守防守位，并排查主图/价格/评论竞争力。"
                )
            else:
                strategy_tag = "MIXED_CORE_TEST"
                action_type = "Refine_Bid_Placement"
                recommended_val = "控价观察：维持或移入精准仓控价观察"
                diagnostic_reason = (
                    f"核心功能词混杂在宽泛活动中耗损 (${spend:.2f}, {clicks} 点击)，"
                    f"需结合自然排名与出单表现控价观察。"
                )
        else:
            # Adjacent / peripheral
            strategy_tag = "PERIPHERAL_BLEED"
            if clicks >= periph_neg_clicks or spend >= periph_neg_spend or inferred_pos in ("CONFIRMED_TOS", "PROBABLE_TOS"):
                action_type = "Negative_Exact"
                recommended_val = "Add Negative Exact"
                diagnostic_reason = f"边缘弱相关词在 {inferred_pos} 耗损，点击={clicks}，花费=${spend:.2f}（阈值 ${periph_neg_spend:.2f}），0订单，建议精准否定。"
            else:
                action_type = "Lower_Bid"
                recommended_val = "Lower Base Bid 20%"
                diagnostic_reason = f"边缘词转化动力不足，点击={clicks}，花费=${spend:.2f}，降低基础竞价控费观察。"

    return {
        "query": query,
        "campaign": c_name,
        "bucket": bucket,
        "volume_tier": v_tier,
        "searchrank": sr_display,
        "spend": spend,
        "sales": sales,
        "orders": orders,
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "cpc": cpc,
        "base_bid": base_bid,
        "max_non_tos": max_non_tos,
        "max_tos_bid": max_tos_bid,
        "inferred_placement": inferred_pos,
        "placement_evidence": pos_reason,
        "strategy_tag": strategy_tag,
        "action_type": action_type,
        "recommended_value": recommended_val,
        "diagnostic_reason": diagnostic_reason,
    }


def audit_active_negatives(
    active_neg_dict: dict[tuple[str, str], dict],
    profile: dict,
    rules: dict,
    sr_map: dict[str, int],
    kws: list[dict] = None,
    camps: list[dict] = None,
    xydc_map: dict = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Audit existing active negatives cross-referencing portfolio targeting with global compliance firewall."""
    audit_records = []
    unnegative_adjustments = []

    harvest_camp = find_exact_harvest_campaign(camps or [], kws or [])
    camp_lookup = {c.get("name") or c.get("campaign_name"): c for c in (camps or [])}

    # Build portfolio-wide keyword targeting lookup with inherited ad-group default bids
    targeted_map = defaultdict(list)
    if kws:
        for k in kws:
            txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
            if not txt:
                continue
            eff_bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
            targeted_map[txt].append({
                "campaign": k.get("campaign_name") or "",
                "match": k.get("match_type") or "exact",
                "state": (k.get("state") or "").lower(),
                "bid": eff_bid,
                "cpc": num(k.get("cpc")),
                "spend": num(k.get("spends")),
                "sales": num(k.get("sales")),
                "orders": int(num(k.get("orders"))),
            })

    for (c_name, ql), item in sorted(active_neg_dict.items(), key=lambda kv: -kv[1]["spend"]):
        q = item["query"]
        spend = item["spend"]
        sales = item["sales"]
        orders = item["orders"]
        clicks = item["clicks"]
        cpc = round(spend / clicks, 2) if clicks else 0.0
        cvr = round(orders / clicks * 100, 1) if clicks else 0.0

        fw_blocked, fw_label, fw_reason = check_global_compliance_firewall(q, profile, rules)
        bucket, match_reason, is_strong_relevance = classify_query(q, profile, rules)
        if fw_blocked:
            bucket = fw_label
            match_reason = fw_reason
            is_strong_relevance = False

        sr, v_tier = get_searchrank(q, sr_map)
        sr_display = str(sr) if sr is not None else "--"

        is_hard_misfit = fw_blocked or bucket.startswith("is_not") or bucket in ("is_not", "competitor_brand")
        is_own_asin = (bucket == "asin" and (ql == str(profile.get("asin") or "").lower()))

        # 1. Policy-blocked / Physical conflicts & Competitor brands (unconditional firewall!)
        if is_hard_misfit:
            audit_status = "SAFE_HARD_NEGATIVE"
            recom_action = "Keep_Negated"
            recom_val = "Maintain Negative (Permanent Misfit)"
            detail_reason = f"Confirmed compliance/physical/category conflict or excluded competitor ({match_reason}). Hard negative eliminates policy risk, waste, and bad reviews."

        # 2. Own ASIN or Competitor ASIN negative defense
        elif is_own_asin or is_asin_term(q):
            audit_status = "SAFE_ASIN_DEFENSE"
            recom_action = "Keep_Negated"
            recom_val = "Maintain Negative (ASIN Target Isolation)"
            detail_reason = "ASIN negated in Auto/Broad to prevent self-cannibalization or isolate ASIN product targeting."

        # 3. Portfolio targeting cross-reference (Migration Funnel Analysis)
        else:
            targets = targeted_map.get(ql, [])
            enabled_other = [t for t in targets if t["state"] == "enabled" and t["campaign"] != c_name]
            paused_other = [t for t in targets if t["state"] != "enabled" and t["campaign"] != c_name]

            # Case A: Already promoted and actively running in Exact / dedicated campaigns
            if enabled_other:
                audit_status = "PROMOTED_MIGRATED_PROTECTED"
                recom_action = "Keep_Negated_Migrated"
                dest_desc = "; ".join([f"{t['campaign']}[{t['match']}, bid=${t['bid']:.2f}, ord={t['orders']}]" for t in enabled_other])
                tot_exact_ord = sum(t["orders"] for t in enabled_other)
                recom_val = f"Maintain Negative in {c_name} (Active in Exact Funnel: {tot_exact_ord} orders)"
                detail_reason = (
                    f"Automated Migration Funnel Active: Already targeted and active in destination campaign(s): {dest_desc}. "
                    f"Negation in {c_name} prevents internal cannibalization and protects dedicated Exact traffic. DO NOT un-negate in Broad!"
                )

            # Case B: Migrated to Exact, but currently PAUSED (Traffic Blackout vs Intentional Misfit Pause!)
            elif paused_other:
                misfit_in_paused, _, _ = check_global_compliance_firewall(q, profile, rules)

                if misfit_in_paused or is_hard_misfit:
                    audit_status = "INTENTIONAL_PAUSE_CONFLICT"
                    recom_action = "Keep_Paused"
                    recom_val = f"Maintain Paused in {paused_other[0]['campaign']} and Negated in {c_name} (Physical Misfit)"
                    detail_reason = (
                        f"确凿合规/物理属性冲突（{match_reason}）：该词此前因属性矛盾被合规暂停/否定，"
                        f"绝非移词断流误杀！严禁建议 Unpause 重新激活，彻底规避违规、买家差评与高退货。"
                    )
                elif is_strong_relevance or (orders >= 1 and bucket in ("brand", "core_feature", "core_or_title")) or (sr is not None and sr < 30000 and is_strong_relevance):
                    best_dest = sorted(paused_other, key=lambda x: -x["orders"])[0]
                    dest_camp = best_dest["campaign"]
                    dest_match = best_dest["match"]
                    dest_camp_obj = camp_lookup.get(dest_camp, {})
                    dest_camp_state = dest_camp_obj.get("state", "enabled")

                    if dest_camp_state != "enabled":
                        audit_status = "MIGRATED_TO_PAUSED_CAMPAIGN"
                        recom_action = "Keep_Negated_In_Broad"
                        recom_val = f"Maintain Negated in {c_name} (Destination {dest_camp} is Paused)"
                        detail_reason = (
                            f"该词此前移入的活动 {dest_camp} 目前已整组暂停，原广泛仓保持否定防内耗；"
                            f"如需重新测试该词，建议启用目标活动或在其他活跃精准仓投放。"
                        )
                    else:
                        audit_status = "MIGRATED_BUT_PAUSED_IN_EXACT"
                        recom_action = "Unpause_In_Exact"
                        recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                            item, dest_camp, camp_lookup, prior_target=best_dest
                        )
                        recom_val = f"Unpause in {dest_camp} (Exact, 基础竞价 ${recom_bid:.2f}); Maintain Negative in {c_name}"
                        detail_reason = (
                            f"移词断流全账户堵死：该词已移词并在原活动 ({c_name}) 否定，但在精准仓 ({dest_camp}) 处于暂停状态，导致全账户完全断流 "
                            f"({orders}单, SR={sr_display}, {v_tier})。{bid_calc_desc} "
                            f"处理：在 {dest_camp} 重新开启投放（基础竞价 ${recom_bid:.2f}）；原活动 {c_name} 继续保持否定防内耗。"
                        )
                        eval_q = evaluate_rank_synergy(term=q, xydc_map=xydc_map or {})
                        q_org_r = eval_q.get("organic_rank")
                        q_sp_r = eval_q.get("sp_rank")
                        if q_org_r is not None or q_sp_r is not None:
                            rank_tag_q = f"[自然位#{q_org_r or '--'}·广告位#{q_sp_r or '--'} {eval_q.get('synergy_tag', '')}] "
                            rank_cur_q = f"(自然位#{q_org_r or '--'}, 广告位#{q_sp_r or '--'}) "
                        else:
                            rank_tag_q = "[西柚未建位/暂无排名] "
                            rank_cur_q = "(西柚未建位) "

                        unnegative_adjustments.append({
                            "action_type": "Unpause_Keyword",
                            "campaign": dest_camp,
                            "target_entity": q,
                            "match_type": dest_match,
                            "current_value": f"{rank_cur_q}state=paused",
                            "recommended_value": f"state=enabled (基础竞价 ${recom_bid:.2f})",
                            "reason": f"{rank_tag_q}移词断流恢复：在 {c_name} 保持否定防内耗，在 {dest_camp} 开启投放。{bid_calc_desc}",
                        })
                else:
                    audit_status = "SOFT_MONITOR"
                    recom_action = "Keep_Paused"
                    recom_val = f"Maintain Paused in {paused_other[0]['campaign']} and Negated in {c_name}"
                    detail_reason = f"Peripheral or low conversion term paused across campaigns (spend=${spend:.2f}, ord={orders}). Maintain current status."

            # Case C: Not targeted in ANY other campaign (True Orphan or Soft Monitor)
            else:
                misfit_found, _, _ = check_global_compliance_firewall(q, profile, rules)

                if misfit_found:
                    audit_status = "SAFE_HARD_NEGATIVE"
                    recom_action = "Keep_Negated"
                    recom_val = "Maintain Negative (Permanent Misfit)"
                    detail_reason = "Confirmed compliance/physical/category conflict via firewall/rules. Hard negative eliminates structural waste/bad reviews."
                else:
                    generic_conflict_patterns = [
                        r"\bno\s+plastic\b", r"\bplastic\s+free\b", r"\ball\s+glass\b", r"\ball\s+metal\b",
                        r"\b5\s*cup\b", r"\b12\s*cup\b", r"\bpercolator\b", r"\bespresso\b", r"\bk\s*cup\b",
                        r"\bcapsule\b", r"\bpod\b", r"\bmanual\b", r"\bcommercial\b",
                        r"\bvitamix\b", r"\bblendtec\b", r"\bbreville\b", r"\bcuisinart\b",
                    ]
                    has_generic_conflict = any(re.search(pat, q, re.I) for pat in generic_conflict_patterns)

                    already_enabled_here = any(t["state"] == "enabled" and t["match"] == "exact" for t in targets)

                    is_core_asset = (
                        not already_enabled_here
                        and (bucket == "brand" or is_strong_relevance or bucket in ("core_feature", "core_or_title"))
                        and not is_hard_misfit
                        and not has_generic_conflict
                        and (orders >= 2 or (orders >= 1 and is_strong_relevance and bucket not in ("competitor_brand", "adjacent")))
                    )

                    harvest_camp_obj = camp_lookup.get(harvest_camp, {})
                    harvest_camp_state = harvest_camp_obj.get("state", "enabled")

                    if is_core_asset and harvest_camp_state == "enabled":
                        audit_status = "TRUE_ORPHAN_NEGATIVE"
                        recom_action = "Harvest_To_Exact"
                        recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                            item, harvest_camp, camp_lookup, prior_target=None
                        )
                        recom_val = f"Harvest into {harvest_camp} (Exact, 基础竞价 ${recom_bid:.2f}) or Un-negate in Broad"
                        detail_reason = (
                            f"全账户未投放孤儿核心资产：强相关意图/出单/搜索词 ({orders}单, SR={sr_display}, {v_tier})，"
                            f"但在 {c_name} 被否定且全账户未在任何活动投放。{bid_calc_desc} "
                            f"处理：移入精准仓 ({harvest_camp}) 收割（基础竞价 ${recom_bid:.2f}）；原活动 {c_name} 保持否定防内耗。"
                        )
                        eval_q = evaluate_rank_synergy(term=q, xydc_map=xydc_map or {})
                        q_org_r = eval_q.get("organic_rank")
                        q_sp_r = eval_q.get("sp_rank")
                        if q_org_r is not None or q_sp_r is not None:
                            rank_tag_q = f"[自然位#{q_org_r or '--'}·广告位#{q_sp_r or '--'} {eval_q.get('synergy_tag', '')}] "
                            rank_cur_q = f"(自然位#{q_org_r or '--'}, 广告位#{q_sp_r or '--'}) "
                        else:
                            rank_tag_q = "[西柚未建位/暂无排名] "
                            rank_cur_q = "(西柚未建位) "

                        unnegative_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": harvest_camp,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_value": f"{rank_cur_q}Negated in {c_name}, untargeted in portfolio",
                            "recommended_value": f"Add Exact Target (基础竞价 ${recom_bid:.2f})",
                            "reason": f"{rank_tag_q}孤儿核心资产收割：在 {c_name} 保持否定防内耗，移入 {harvest_camp} 独立收割。{bid_calc_desc}",
                        })
                    else:
                        audit_status = "SOFT_MONITOR"
                        recom_action = "Keep_Negated_Offpeak"
                        recom_val = "Maintain Negative; Re-test in Q4 Holiday Season"
                        detail_reason = (
                            f"Adjacent/peripheral query with low off-peak momentum "
                            f"(spend=${spend:.2f}, ord={orders}); untargeted across the portfolio. "
                            f"Keep negated during routine period."
                        )

        audit_records.append({
            "campaign": c_name,
            "query": q,
            "neg_status": item["neg_status"],
            "bucket": bucket,
            "volume_tier": v_tier,
            "searchrank": sr_display,
            "spend": spend,
            "sales": sales,
            "orders": orders,
            "clicks": clicks,
            "cvr": cvr,
            "cpc": cpc,
            "audit_status": audit_status,
            "recommended_action": recom_action,
            "recommended_value": recom_val,
            "reason": detail_reason,
        })

    return audit_records, unnegative_adjustments


def audit_keyword_misfits(
    kws: list[dict],
    rules: dict,
    camps: list[dict] = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    profile: dict | None = None,
) -> list[dict]:
    """Audit enabled keywords in enabled campaigns to detect policy violations & physical misfits."""
    adjustments = []
    camp_lookup = {c.get("name") or c.get("campaign_name"): c for c in (camps or [])}
    is_not_patterns = rules.get("is_not_patterns", [])
    is_patterns = rules.get("is_patterns", [])
    for k in kws:
        if k.get("state") != "enabled":
            continue
        c_name = k.get("campaign_name") or ""
        if camp_lookup and camp_lookup.get(c_name, {}).get("state") != "enabled":
            continue
        text = str(k.get("keyword_text") or k.get("targeting_text") or "").strip()
        bid, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        orders = int(num(k.get("orders")))

        # 0. Policy-blocked terms (built-in + custom rules.json restricted_terms) MUST be paused unconditionally even if they had orders!
        custom_pol_re = v2p.build_restricted_terms_regex(rules)
        m_pol = GLOBAL_POLICY_BLOCKED_RE.search(text.lower()) or (custom_pol_re.search(text.lower()) if custom_pol_re else None)
        if m_pol:
            adjustments.append({
                "action_type": "Pause_Keyword",
                "campaign": c_name,
                "target_entity": text,
                "match_type": k.get("match_type") or "exact",
                "current_bid_num": bid,
                "current_value": f"基础竞价=${bid:.2f}, state=enabled, label=is_not_policy_blocked",
                "recommended_value": "state=paused",
                "reason": f"Level 0 合规高压红线拦截 (policy_blocked:{m_pol.group(0).lower()})：投放词触碰亚马逊/类目违禁敏感词，无论出单多少零豁免强制暂停。",
            })
            continue

        # 1. 核心正向投放词绝对保护：若匹配正向 is_patterns，绝非物理属性错配！
        is_core_match = False
        for is_item in is_patterns:
            is_pat = is_item if isinstance(is_item, str) else is_item.get("pattern", "")
            if _safe_re_search(is_pat, text):
                is_core_match = True
                break
        if is_core_match:
            continue

        # 2. 出单词防误杀最高豁免权（Rule 31）：凡非违禁词且历史累计出单 (orders >= 1)，不直接暂停
        if orders >= 1:
            continue

        fw_hit, fw_lbl, fw_rsn = check_global_compliance_firewall(text, profile, rules)
        if fw_hit:
            adjustments.append({
                "action_type": "Pause_Keyword",
                "campaign": c_name,
                "target_entity": text,
                "match_type": k.get("match_type") or "exact",
                "current_bid_num": bid,
                "current_value": f"基础竞价=${bid:.2f}, state=enabled, label={fw_lbl}",
                "recommended_value": "state=paused",
                "reason": f"物理属性硬冲突 ({fw_lbl}, {fw_rsn})：投放词与本品核心物理属性相悖（0订单已验证），易引流错配买家并推高退款率，建议暂停。",
            })
            continue

        for pat_item in is_not_patterns:
            pat = pat_item if isinstance(pat_item, str) else pat_item.get("pattern", "")
            label = "is_not" if isinstance(pat_item, str) else pat_item.get("label", "is_not")
            if label.startswith("is_not_coffee_beans") or "beans" in label or "consumable" in label:
                if re.search(r"\b(?:grinder|mill|crusher|chopper|machine|maker|electric|blade|appliance|molinillo)\b", text, re.I):
                    continue
            if _safe_re_search(pat, text):
                adjustments.append({
                    "action_type": "Pause_Keyword",
                    "campaign": c_name,
                    "target_entity": text,
                    "match_type": k.get("match_type") or "exact",
                    "current_bid_num": bid,
                    "current_value": f"基础竞价=${bid:.2f}, state=enabled, label={label}",
                    "recommended_value": "state=paused",
                    "reason": f"物理属性硬冲突 ({label}, pattern: {pat})：投放词与本品核心物理属性相悖（0订单已验证），易引流错配买家并推高退款率，建议暂停。",
                })
                break
    return adjustments


# ==============================================================================
# LOCAL DATABASE & PERSISTENT LEDGER MODULE
# ==============================================================================

def resolve_db_dirs(user_db_dir: str | None, src: Path) -> tuple[Path, Path, Path, Path]:
    """Resolve database root, daily_records, ledger, snapshots, expectations, and entity_memory directories."""
    if user_db_dir:
        root = Path(user_db_dir)
    elif Path("db").exists() or Path("db").is_dir():
        root = Path("db")
    elif (src.parent / "db").exists() or (src.parent / "db").is_dir():
        root = src.parent / "db"
    elif (src.parent.parent / "db").exists():
        root = src.parent.parent / "db"
    else:
        root = Path("db")

    daily_records_dir = root / "daily_records"
    ledger_dir = root / "ledger"
    snapshots_dir = root / "snapshots"
    expectations_dir = root / "expectations"
    entity_memory_dir = root / "entity_memory"
    for d in (daily_records_dir, ledger_dir, snapshots_dir, expectations_dir, entity_memory_dir):
        d.mkdir(parents=True, exist_ok=True)
    return root, daily_records_dir, ledger_dir, snapshots_dir


def get_snapshot_key(meta: dict, profile: dict, camps: list[dict] | None = None, shop_alias: str | None = None) -> str:
    """Derive unique snapshot filename key from portfolio or ASIN, normalizing shop alias case (Item 10)."""
    raw_shop = str(shop_alias if shop_alias is not None else (meta.get("shop_alias") or meta.get("shop_name") or profile.get("shop_name") or "")).strip()
    shop_str = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_shop).upper()

    port_id = str(meta.get("portfolio_id") or "").strip()
    if port_id and port_id not in ("None", "0", ""):
        return f"{shop_str}_portfolio_{port_id}" if shop_str else f"portfolio_{port_id}"
    port_name = str(meta.get("portfolio_name") or "").strip()
    if port_name and port_name not in ("None", ""):
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", port_name)
        return f"{shop_str}_portfolio_{clean_name}" if shop_str else f"portfolio_{clean_name}"

    if camps:
        pf_ids = [str(c.get("portfolio_id")) for c in camps if c.get("portfolio_id") and str(c.get("portfolio_id")) not in ("None", "0", "")]
        if pf_ids:
            top_pf = Counter(pf_ids).most_common(1)[0][0]
            return f"{shop_str}_portfolio_{top_pf}" if shop_str else f"portfolio_{top_pf}"

    asin = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper().strip()
    country = str(meta.get("country") or "US").upper().strip()
    return f"{shop_str}_{asin}_{country}" if shop_str else f"{asin}_{country}"


def _find_case_insensitive_db_file(directory: Path, candidates: list[str]) -> Path | None:
    """Resolve an existing file in `directory` matching any candidate name case-insensitively."""
    if not directory.exists():
        return None
    for cand in candidates:
        p = directory / cand
        if p.exists():
            return p
    cand_low_map = {c.lower(): c for c in candidates if c}
    for f in sorted(directory.iterdir()):
        if f.is_file() and f.name.lower() in cand_low_map:
            return f
    return None


def load_snapshot(snapshot_path: Path) -> dict:
    if snapshot_path.exists():
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "updated_at": "",
        "campaigns": {},
        "keywords": {},
        "targets": {},
        "active_cooldowns": [],
    }


CAMPAIGN_FIELDS = [
    ("tos", "campaign_tos", lambda v: f"{v}%"),
    ("pp", "campaign_pp", lambda v: f"{v}%"),
    ("ros", "campaign_ros", lambda v: f"{v}%"),
    ("budget", "campaign_budget", lambda v: f"${num(v):.2f}"),
    ("bidding_strategy", "campaign_strategy", str),
    ("state", "campaign_state", str),
]

CAMPAIGN_LEVEL_ENTITY_TYPES = {
    "campaign_tos",
    "campaign_pp",
    "campaign_ros",
    "campaign_budget",
    "campaign_strategy",
    "campaign_state",
}


def _is_campaign_level_cooldown(item: dict) -> bool:
    """Item 2 / Iron Law 77: Keyword or target bid/state changes ONLY lock that (campaign, match_type, entity)
    itself -- NEVER lock `locked_campaigns`! Only campaign-level parameters lock the campaign."""
    et = str(item.get("entity_type") or "").lower().strip()
    if et in CAMPAIGN_LEVEL_ENTITY_TYPES:
        return True
    if et.startswith(("keyword", "target")):
        return False
    ek = str(item.get("entity_key") or "").strip()
    if "::" in ek:
        return False
    fld = str(item.get("field") or "").lower().strip()
    return fld in ("tos", "pp", "ros", "budget", "bidding_strategy", "strategy", "campaign_state") or (fld == "state" and not ek)


# Bid moves below this are rounding noise, not a deliberate adjustment.
BID_EPSILON = 0.02
BUDGET_EPSILON = 0.01


def _campaign_baseline(camp: dict | None) -> dict:
    """Recent-slice metrics captured at adjustment time, so later runs can attribute the effect."""
    camp = camp or {}
    raw_ac = num(camp.get("acos_7d") or camp.get("acos"))
    return {
        "baseline_cpc": round(num(camp.get("cpc_3d") or camp.get("cpc_7d") or camp.get("cpc")), 2),
        "baseline_daily_imp": round(num(camp.get("daily_imp_3d") or camp.get("daily_imp_30d")), 1),
        "baseline_acos": 0.0 if raw_ac >= 900.0 else round(raw_ac, 1),
    }


def _entity_baseline(ent: dict | None, camp: dict | None = None) -> dict:
    """Capture recent-slice metrics for a specific keyword or target entity."""
    ent = ent or {}
    camp = camp or {}

    e_cpc = ent.get("cpc_3d") or ent.get("cpc_7d") or ent.get("cpc")
    if not e_cpc or num(e_cpc) <= 0:
        e_cpc = camp.get("cpc_3d") or camp.get("cpc_7d") or camp.get("cpc") or 0.0

    e_imp_3d = num(ent.get("imp_3d"))
    e_imp_7d = num(ent.get("imp_7d"))
    e_imp_30d = num(ent.get("imp_30d") or ent.get("impressions"))

    if e_imp_3d > 0:
        e_daily_imp = e_imp_3d / 3.0
    elif e_imp_7d > 0:
        e_daily_imp = e_imp_7d / 7.0
    elif e_imp_30d > 0:
        e_daily_imp = e_imp_30d / 30.0
    else:
        e_daily_imp = num(camp.get("daily_imp_3d") or camp.get("daily_imp_30d"))

    e_acos = num(ent.get("acos_7d") or ent.get("acos") or camp.get("acos") or 0.0)
    if e_acos >= 900.0:
        e_acos = 0.0

    return {
        "baseline_cpc": round(num(e_cpc), 2),
        "baseline_daily_imp": round(num(e_daily_imp), 1),
        "baseline_acos": round(num(e_acos), 1),
    }


def _changed(field: str, old_val, new_val) -> bool:
    if field == "budget":
        return abs(num(new_val) - num(old_val)) >= BUDGET_EPSILON
    if field == "bid":
        return abs(num(new_val) - num(old_val)) >= BID_EPSILON and num(new_val) > 0 and num(old_val) > 0
    return new_val != old_val


def _record_change(
    *,
    now: datetime,
    cooldown_days: int,
    campaign: str,
    entity_type: str,
    entity_name: str,
    entity_key: str | None,
    field: str,
    old_val,
    new_val,
    render,
    msg: str,
    camp_lookup: dict,
    diff_events: list[str],
    new_diff_items: list[dict],
    active_cooldowns: list[dict],
    locked_campaigns: set[str],
    entity_baseline_dict: dict | None = None,
) -> None:
    """Log one control-variable change: diff line, entity/campaign cooldown lock, and ledger seed row."""
    now_iso = now.isoformat()
    diff_events.append(f"Campaign '{campaign}': {msg}")

    cooldown = {
        "entity_type": entity_type,
        "campaign": campaign,
        "field": field,
        "old_value": old_val,
        "new_value": new_val,
        "changed_at": now_iso,
        "cooldown_until": (now + timedelta(days=cooldown_days)).isoformat(),
        "days_elapsed": 0,
        "days_remaining": cooldown_days,
        "detail": msg,
    }
    if entity_key:
        cooldown["entity_key"] = entity_key
    active_cooldowns.append(cooldown)
    if _is_campaign_level_cooldown(cooldown):
        locked_campaigns.add(campaign)

    base_metrics = entity_baseline_dict or _campaign_baseline(camp_lookup.get(campaign))

    new_diff_items.append({
        "id": f"ADJ-{now.strftime('%Y%m%d%H%M%S')}-{len(new_diff_items) + 1:02d}",
        "change_date": now.strftime("%Y-%m-%d"),
        "changed_at": now_iso,
        "campaign": campaign,
        "entity_type": entity_type,
        "entity_name": entity_name,
        "entity_key": entity_key or "",
        "field": field,
        "old_val": render(old_val),
        "new_val": render(new_val),
        **base_metrics,
        "detail": msg,
    })


def _snapshot_campaigns(camps: list[dict]) -> dict[str, dict]:
    out = {}
    for c in camps:
        name = c.get("name")
        if not name:
            continue
        out[name] = {
            "name": name,
            "tos": int(c.get("tos", 0)),
            "pp": int(c.get("pp", 0)),
            "ros": int(c.get("ros", 0)),
            "budget": float(c.get("budget", 0.0)),
            "bidding_strategy": str(c.get("bidding_strategy", "")),
            "state": str(c.get("state", "")),
            "cpc": num(c.get("cpc")),
            "cpc_3d": num(c.get("cpc_3d")),
            "cpc_7d": num(c.get("cpc_7d")),
            "daily_imp_3d": num(c.get("daily_imp_3d")),
            "daily_imp_30d": num(c.get("daily_imp_30d")),
        }
    return out


def _snapshot_keywords(
    kws: list[dict],
    kws_3d: list[dict] | None = None,
    kws_7d: list[dict] | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> dict[str, dict]:
    k3_map = {}
    for k in (kws_3d or []):
        c = k.get("campaign_name") or ""
        m = (k.get("match_type") or "").lower().strip()
        t = (k.get("keyword_text") or "").lower().strip()
        if t:
            k3_map[(c, m, t)] = k

    k7_map = {}
    for k in (kws_7d or []):
        c = k.get("campaign_name") or ""
        m = (k.get("match_type") or "").lower().strip()
        t = (k.get("keyword_text") or "").lower().strip()
        if t:
            k7_map[(c, m, t)] = k

    out = {}
    for k in kws:
        text = k.get("keyword_text") or ""
        if not text:
            continue
        c_name = k.get("campaign_name") or ""
        match = (k.get("match_type") or "").lower().strip()
        clean_t = text.lower().strip()
        k3 = k3_map.get((c_name, match, clean_t), {})
        k7 = k7_map.get((c_name, match, clean_t), {})

        eff_bid, is_inh, instant_bid = resolve_multi_window_entity_bid(
            k, k7, k3, group_bid_lookup, camp_group_bid_lookup, fallback=0.35
        )
        is_timing = any(
            bool(x.get("is_apply_time")) and x.get("is_apply_time") not in (0, "0", "false", "False")
            for x in (k3, k7, k) if x
        )
        t_base_v = next((num(x.get("timing_base_value")) for x in (k3, k7, k) if x and num(x.get("timing_base_value")) > 0), None)

        cpc_v = num(k3.get("cpc") or k7.get("cpc") or k.get("cpc"))
        imp_3d = num(k3.get("impressions") or k3.get("imp"))
        imp_7d = num(k7.get("impressions") or k7.get("imp"))
        imp_30d = num(k.get("impressions") or k.get("imp"))
        ord_3d = int(num(k3.get("orders")))
        ord_7d = int(num(k7.get("orders")))
        ord_30d = int(num(k.get("orders")))

        out[f"{c_name}::{match}::{clean_t}"] = {
            "campaign": c_name,
            "text": text,
            "match": match,
            "bid": eff_bid,
            "instant_bid": instant_bid,
            "is_apply_time": is_timing,
            "timing_base_value": round(t_base_v, 2) if t_base_v else None,
            "bid_inherited": is_inh,
            "state": str(k3.get("state") or k7.get("state") or k.get("state") or ""),
            "cpc": cpc_v,
            "cpc_3d": num(k3.get("cpc")),
            "cpc_7d": num(k7.get("cpc")),
            "imp_3d": imp_3d,
            "imp_7d": imp_7d,
            "imp_30d": imp_30d,
            "ord_3d": ord_3d,
            "ord_7d": ord_7d,
            "ord_30d": ord_30d,
            "acos": clean_api_acos(k.get("acos"), num(k.get("spends") or k.get("spend") or k.get("cost")), num(k.get("sales"))),
            "acos_7d": clean_api_acos(k7.get("acos"), num(k7.get("spends") or k7.get("spend") or k7.get("cost")), num(k7.get("sales"))),
        }
    return out


def _snapshot_targets(
    targets: list[dict],
    targets_3d: list[dict] | None = None,
    targets_7d: list[dict] | None = None,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
) -> dict[str, dict]:
    t3_map = {}
    for t in (targets_3d or []):
        c = t.get("campaign_name") or ""
        tp = (t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or "").lower().strip()
        txt = str(t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or "").lower().strip()
        if txt:
            t3_map[(c, tp, txt)] = t

    t7_map = {}
    for t in (targets_7d or []):
        c = t.get("campaign_name") or ""
        tp = (t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or "").lower().strip()
        txt = str(t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or "").lower().strip()
        if txt:
            t7_map[(c, tp, txt)] = t

    out = {}
    for t in targets:
        if skip_total(t):
            continue
        text = t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or ""
        if not text:
            continue
        c_name = t.get("campaign_name") or ""
        ttype = t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or ""
        clean_txt = str(text).lower().strip()
        t3 = t3_map.get((c_name, ttype.lower().strip(), clean_txt), {})
        t7 = t7_map.get((c_name, ttype.lower().strip(), clean_txt), {})

        eff_bid, is_inh, instant_bid = resolve_multi_window_entity_bid(
            t, t7, t3, group_bid_lookup, camp_group_bid_lookup, fallback=0.40
        )
        is_timing = any(
            bool(x.get("is_apply_time")) and x.get("is_apply_time") not in (0, "0", "false", "False")
            for x in (t3, t7, t) if x
        )
        t_base_v = next((num(x.get("timing_base_value")) for x in (t3, t7, t) if x and num(x.get("timing_base_value")) > 0), None)

        cpc_v = num(t3.get("cpc") or t7.get("cpc") or t.get("cpc"))
        imp_3d = num(t3.get("impressions") or t3.get("imp"))
        imp_7d = num(t7.get("impressions") or t7.get("imp"))
        imp_30d = num(t.get("impressions") or t.get("imp"))
        ord_3d = int(num(t3.get("orders")))
        ord_7d = int(num(t7.get("orders")))
        ord_30d = int(num(t.get("orders")))

        out[f"{c_name}::{ttype}::{clean_txt}"] = {
            "campaign": c_name,
            "text": str(text),
            "type": ttype,
            "bid": eff_bid,
            "instant_bid": instant_bid,
            "is_apply_time": is_timing,
            "timing_base_value": round(t_base_v, 2) if t_base_v else None,
            "bid_inherited": is_inh,
            "state": str(t3.get("state") or t7.get("state") or t.get("state") or ""),
            "cpc": cpc_v,
            "cpc_3d": num(t3.get("cpc")),
            "cpc_7d": num(t7.get("cpc")),
            "imp_3d": imp_3d,
            "imp_7d": imp_7d,
            "imp_30d": imp_30d,
            "ord_3d": ord_3d,
            "ord_7d": ord_7d,
            "ord_30d": ord_30d,
            "acos": clean_api_acos(t.get("acos"), num(t.get("spends") or t.get("spend") or t.get("cost")), num(t.get("sales"))),
            "acos_7d": clean_api_acos(t7.get("acos"), num(t7.get("spends") or t7.get("spend") or t7.get("cost")), num(t7.get("sales"))),
        }
    return out


def _carry_forward_cooldowns(prev_cooldowns: list[dict], now: datetime) -> tuple[list[dict], set[str]]:
    """Keep cooldowns that have not expired yet and refresh their elapsed/remaining counters."""
    active: list[dict] = []
    locked: set[str] = set()
    now_iso = now.isoformat()
    for item in prev_cooldowns:
        until_str = item.get("cooldown_until")
        if not until_str:
            continue
        try:
            until = datetime.fromisoformat(until_str)
        except Exception:
            continue
        if now >= until:
            continue
        try:
            changed_at = datetime.fromisoformat(item.get("changed_at", now_iso))
        except Exception:
            changed_at = now
        item["days_elapsed"] = max(0, (now - changed_at).days)
        item["days_remaining"] = max(0, (until - now).days)
        active.append(item)
        if item.get("campaign") and _is_campaign_level_cooldown(item):
            locked.add(item["campaign"])
    return active, locked


def diff_and_track_cooldown(
    prev_snap: dict,
    curr_camps: list[dict],
    curr_kws: list[dict],
    curr_targets: list[dict],
    curr_kws_3d: list[dict] | None = None,
    curr_kws_7d: list[dict] | None = None,
    curr_targets_3d: list[dict] | None = None,
    curr_targets_7d: list[dict] | None = None,
    cooldown_days: int = 7,
    group_bid_lookup: dict | None = None,
    camp_group_bid_lookup: dict | None = None,
    ref_now: datetime | None = None,
) -> tuple[dict, list[dict], set[str], list[str], list[dict]]:
    """Compare current control variables vs previous snapshot."""
    now = ref_now or datetime.now()

    curr_camp_map = _snapshot_campaigns(curr_camps)
    curr_kw_map = _snapshot_keywords(curr_kws, curr_kws_3d, curr_kws_7d, group_bid_lookup, camp_group_bid_lookup)
    curr_target_map = _snapshot_targets(curr_targets, curr_targets_3d, curr_targets_7d, group_bid_lookup, camp_group_bid_lookup)

    camp_lookup = {c.get("name"): c for c in curr_camps if c.get("name")}

    prev_camps = prev_snap.get("campaigns") or {}
    prev_kws = prev_snap.get("keywords") or {}
    prev_targets = prev_snap.get("targets") or {}

    active_cooldowns, locked_campaigns = _carry_forward_cooldowns(prev_snap.get("active_cooldowns") or [], now)
    diff_events: list[str] = []
    new_diff_items: list[dict] = []

    def log(**kw):
        _record_change(
            now=now, cooldown_days=cooldown_days, camp_lookup=camp_lookup,
            diff_events=diff_events, new_diff_items=new_diff_items,
            active_cooldowns=active_cooldowns, locked_campaigns=locked_campaigns, **kw
        )

    # 1. Campaign placement modifiers, budget, strategy and state
    if prev_camps:
        for name, curr in curr_camp_map.items():
            prev = prev_camps.get(name)
            if prev is None:
                continue
            for field, entity_type, render in CAMPAIGN_FIELDS:
                old_val, new_val = prev.get(field), curr[field]
                if old_val is None or old_val == "" or not _changed(field, old_val, new_val):
                    continue
                label = "State" if field == "state" else field.upper()
                log(
                    campaign=name, entity_type=entity_type, entity_name=name, entity_key=None,
                    field=field, old_val=old_val, new_val=new_val, render=render,
                    msg=f"{label} changed: {render(old_val)} -> {render(new_val)}",
                    entity_baseline_dict=_campaign_baseline(camp_lookup.get(name)),
                )

    # 2. Keyword and product-target bids (anchored to timing_base_value when is_apply_time is True!)
    for prev_map, curr_map, entity_type, label_key in (
        (prev_kws, curr_kw_map, "keyword_bid", "match"),
        (prev_targets, curr_target_map, "target_bid", "type"),
    ):
        if not prev_map:
            continue
        noun = "Keyword" if entity_type == "keyword_bid" else "Target"
        for key, curr in curr_map.items():
            prev = prev_map.get(key)
            if prev is None or not _changed("bid", prev.get("bid", 0.0), curr["bid"]):
                continue
            # Guard first migration run from legacy snapshot where prev had no timing_base_value recorded
            # and prev['bid'] was merely the peak-hour instant_bid while timing_base_value did not actually change!
            if (
                curr.get("is_apply_time")
                and curr.get("timing_base_value")
                and "timing_base_value" not in prev
                and abs(num(prev.get("bid")) - num(curr.get("instant_bid"))) < BID_EPSILON
            ):
                continue
            entity_name = f"{curr['text']} [{curr[label_key]}]"
            ent_base = _entity_baseline(curr, camp_lookup.get(curr["campaign"]))
            log(
                campaign=curr["campaign"], entity_type=entity_type, entity_name=entity_name,
                entity_key=key, field="bid", old_val=prev.get("bid"), new_val=curr["bid"],
                render=lambda v: f"${num(v):.2f}",
                msg=f"{noun} '{curr['text']}' [{curr[label_key]}] bid changed: "
                    f"${num(prev.get('bid')):.2f} -> ${curr['bid']:.2f}",
                entity_baseline_dict=ent_base,
            )

    new_snap = {
        "updated_at": now.isoformat(),
        "campaigns": curr_camp_map,
        "keywords": curr_kw_map,
        "targets": curr_target_map,
        "active_cooldowns": active_cooldowns,
    }
    return new_snap, active_cooldowns, locked_campaigns, diff_events, new_diff_items


def _parse_legacy_entity_name(e_name: str, campaign: str = "") -> tuple[str, str]:
    """Parse legacy ledger entity_name into exact (text_lower, match_lower)."""
    raw = (e_name or "").strip()
    if "::" in raw:
        parts = raw.split("::", 2)
        if len(parts) == 3:
            return parts[2].lower().strip(), parts[1].lower().strip()
    m_quoted = re.match(r"^.*?\[([^\]]+)\]\s*['\"]([^'\"]+)['\"]\s*$", raw)
    if m_quoted:
        return m_quoted.group(2).lower().strip(), m_quoted.group(1).lower().strip()
    cleaned = re.sub(r"^(?:keyword|target)\s*:\s*", "", raw, flags=re.I).strip()
    m = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", cleaned)
    if m:
        return m.group(1).lower().strip(), m.group(2).lower().strip()
    return cleaned.lower().strip(), ""


def load_and_sync_ledger(
    ledger_file: Path,
    prev_snapshot: dict,
    new_diff_items: list[dict],
    curr_camps: list[dict],
    curr_kw_map: dict | None = None,
    curr_target_map: dict | None = None,
    cooldown_days: int = 7,
    xydc_map: dict | None = None,
    deal_info: dict | None = None,
    profile: dict | None = None,
    financial_prof: dict | None = None,
    traffic_prof: dict | None = None,
    target_acos: float = 25.0,
    meta: dict | None = None,
    ref_now: datetime | None = None,
    rules: dict | None = None,
    peak_post_cooldown: bool | None = None,
) -> tuple[list[dict], list[dict]]:
    """Load persistent ledger, merge cross-namespace sibling ledgers ({ASIN}_{country}_ledger.json + portfolio ledgers),
    archive historical dayparting oscillation drift noise, distinguish upward vs downward adjustments, and attribute effects."""
    now = ref_now or datetime.now()
    camp_lookup = {c.get("name"): c for c in curr_camps if c.get("name")}
    ledger = []
    if ledger_file.exists():
        try:
            ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
            if not isinstance(ledger, list):
                ledger = []
        except Exception:
            ledger = []

    rcfg_l = v2p.resolve_rule_config(rules)
    refund_lv1_l = float(rcfg_l.get("refund_lv1", 12.0))
    kw_fl_std_l = float(rcfg_l.get("bid_floor_kw", 0.25))
    kw_fl_frg_l = float(rcfg_l.get("bid_floor_kw_fragile", 0.30))
    as_fl_std_l = float(rcfg_l.get("bid_floor_asin", 0.36))
    as_fl_frg_l = float(rcfg_l.get("bid_floor_asin_fragile", 0.42))
    if peak_post_cooldown is None:
        _, win_end_l = _parse_window_bounds(meta or {})
        pk_ov_l, pk_ph_l, _ = v2p.detect_peak_overlay(
            win_end_l or (deal_info or {}).get("anchor_date") or now.strftime("%Y-%m-%d"),
            deal_info,
            rcfg_l,
        )
        is_peak_post_l = bool(pk_ov_l and pk_ph_l == "POST_COOLDOWN")
    else:
        is_peak_post_l = bool(peak_post_cooldown)

    is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv1_l)
    fulfillable_cnt = num(profile.get("fulfillable")) if profile and profile.get("fulfillable") is not None else None
    dos_cnt = num(profile.get("days_of_supply")) if profile and profile.get("days_of_supply") is not None else None
    is_stockout = bool(profile and ((fulfillable_cnt is not None and fulfillable_cnt == 0) or (dos_cnt is not None and dos_cnt <= 0 and fulfillable_cnt is not None and fulfillable_cnt <= 2)))
    is_low_inventory = bool(profile and not is_stockout and ((dos_cnt is not None and 0 < dos_cnt <= 7) or (fulfillable_cnt is not None and 0 < fulfillable_cnt <= 25)))

    if not ledger and prev_snapshot.get("active_cooldowns"):
        for idx, item in enumerate(prev_snapshot["active_cooldowns"]):
            c_name = item.get("campaign") or ""
            ekey = item.get("entity_key") or ""
            ent_src = (curr_kw_map or {}).get(ekey) or (curr_target_map or {}).get(ekey)
            baseline = _entity_baseline(ent_src, camp_lookup.get(c_name)) if ent_src else _campaign_baseline(camp_lookup.get(c_name))
            ch_at = item.get("changed_at") or now.isoformat()
            ch_date = ch_at[:10]
            old_v = str(item.get("old_value"))
            new_v = str(item.get("new_value"))
            field = str(item.get("field") or "")
            if "bid" in field:
                old_v = f"${num(old_v):.2f}"
                new_v = f"${num(new_v):.2f}"
            elif field in ("tos", "pp", "ros"):
                old_v = f"{old_v}%"
                new_v = f"{new_v}%"

            ledger.append({
                "id": f"ADJ-HIST-{idx+1:02d}",
                "change_date": ch_date,
                "changed_at": ch_at,
                "campaign": c_name,
                "entity_type": item.get("entity_type") or "adjustment",
                "entity_name": ekey or c_name,
                "entity_key": ekey,
                "field": field,
                "old_val": old_v,
                "new_val": new_v,
                "baseline_cpc": baseline["baseline_cpc"],
                "baseline_daily_imp": baseline["baseline_daily_imp"],
                "baseline_acos": baseline["baseline_acos"],
                "detail": item.get("detail") or "",
            })

    def _norm_ledger_entity_slot(x: dict) -> tuple[str, str, str, str]:
        c_n = str(x.get("campaign") or "").strip()
        fld = str(x.get("field") or "").lower().strip()
        ek = str(x.get("entity_key") or "").strip().lower()
        if ek and "::" in ek:
            parts = ek.split("::", 2)
            mt_norm = parts[1].strip() if len(parts) == 3 else ""
            ent_norm = parts[-1].strip()
        else:
            p_t, p_m = _parse_legacy_entity_name(str(x.get("entity_name") or ""), c_n)
            mt_norm = p_m
            ent_norm = p_t or c_n.lower()
        return (c_n, mt_norm, ent_norm, fld)

    def _ledger_dedup_key(x: dict) -> str:
        c_n, mt_n, ent_n, fld_n = _norm_ledger_entity_slot(x)
        return f"{c_n}::{mt_n}::{ent_n}::{fld_n}::{str(x.get('change_date') or '')[:10]}::{x.get('old_val')}->{x.get('new_val')}"

    # Deduplicate existing rows first (cleans historical same-day duplicate spam)
    deduped_seed = []
    seen_keys = set()
    for x in ledger:
        if isinstance(x, dict):
            dk = _ledger_dedup_key(x)
            if dk not in seen_keys:
                seen_keys.add(dk)
                deduped_seed.append(x)
    ledger = deduped_seed

    # Item 10: Merge sibling ledger files across both portfolio_id (case-insensitive) AND {ASIN}_{country}_ledger.json
    if ledger_file.parent.exists():
        sibling_candidates: list[Path] = []
        m_pf = re.search(r"(portfolio_[A-Za-z0-9_-]+)_ledger\.json$", ledger_file.name, re.I)
        if m_pf:
            pf_suf_low = f"{m_pf.group(1).lower()}_ledger.json"
            for f in sorted(ledger_file.parent.glob("*_ledger.json")):
                if f.name.lower().endswith(pf_suf_low):
                    sibling_candidates.append(f)
        asin_u = str((meta or {}).get("asin") or (profile or {}).get("asin") or "").upper().strip()
        ctry_u = str((meta or {}).get("country") or "US").upper().strip()
        if asin_u:
            asin_suf_low = f"{asin_u}_{ctry_u}_ledger.json".lower()
            for f in sorted(ledger_file.parent.glob("*_ledger.json")):
                if f.name.lower() == asin_suf_low or f.name.lower().endswith(f"_{asin_suf_low}"):
                    sibling_candidates.append(f)
        for sib in sibling_candidates:
            if sib.resolve() == ledger_file.resolve():
                continue
            try:
                sib_rows = json.loads(sib.read_text(encoding="utf-8"))
                if isinstance(sib_rows, list):
                    for sr in sib_rows:
                        if isinstance(sr, dict):
                            dk = _ledger_dedup_key(sr)
                            if dk not in seen_keys:
                                ledger.append(sr)
                                seen_keys.add(dk)
            except Exception:
                pass

    for item in new_diff_items:
        dk = _ledger_dedup_key(item)
        if dk not in seen_keys:
            ledger.append(item)
            seen_keys.add(dk)

    ledger.sort(key=lambda x: (str(x.get("change_date") or ""), str(x.get("changed_at") or ""), str(x.get("id") or "")))

    # Item 2: Detect historical dayparting round-trip oscillation noise (e.g. $1.02 -> $1.22 -> $0.82 -> $1.22 or A -> B -> A)
    indices_by_slot: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    latest_idx_by_slot: dict[tuple[str, str, str, str], int] = {}
    for idx_l, row_l in enumerate(ledger):
        sk = _norm_ledger_entity_slot(row_l)
        indices_by_slot[sk].append(idx_l)
        latest_idx_by_slot[sk] = idx_l

    dayparting_drift_indices: set[int] = set()
    for sk, idx_list in indices_by_slot.items():
        if len(idx_list) < 2:
            continue
        c_n, mt_n, ent_n, fld_n = sk
        if fld_n not in ("bid", "tos", "pp", "ros"):
            continue
        # Check if entity/campaign has dayparting active OR exhibits value oscillation (round-trip A->B..->A or >=2 sign reversals)
        kw_match = (curr_kw_map or {}).get(f"{c_n}::{mt_n}::{ent_n}")
        tg_match = (curr_target_map or {}).get(f"{c_n}::{mt_n}::{ent_n}")
        camp_match = camp_lookup.get(c_n)
        has_dayparting_flag = bool(
            (kw_match and kw_match.get("is_apply_time"))
            or (tg_match and tg_match.get("is_apply_time"))
            or (camp_match and camp_match.get("is_apply_time"))
        )
        seq_pairs = [(num(ledger[i].get("old_val")), num(ledger[i].get("new_val")), str(ledger[i].get("change_date") or "")[:10]) for i in idx_list]
        dirs = [1 if nv > ov else (-1 if nv < ov else 0) for ov, nv, _ in seq_pairs if ov > 0 and nv > 0 and abs(nv - ov) >= 0.01]
        sign_flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a * b < 0)
        visited_vals = [round(seq_pairs[0][0], 2)] + [round(nv, 2) for _, nv, _ in seq_pairs]
        has_round_trip = len(visited_vals) > len(set(visited_vals))
        same_day_bounce = any(
            seq_pairs[j][2] == seq_pairs[j + 1][2] and (seq_pairs[j][1] - seq_pairs[j][0]) * (seq_pairs[j + 1][1] - seq_pairs[j + 1][0]) < 0
            for j in range(len(seq_pairs) - 1)
        )
        if same_day_bounce or sign_flips >= 2 or (sign_flips >= 1 and (has_round_trip or has_dayparting_flag)):
            for i_mark in idx_list:
                # Only keep a brand-new today's genuine timing_base_value change if it came from new_diff_items
                if ledger[i_mark] not in new_diff_items:
                    dayparting_drift_indices.add(i_mark)

    # Backfill v2 ledger metadata fields (100% backward-compatible with all legacy *_ledger.json files)
    mem_entities_for_ledger: dict[str, dict] = {}
    if ledger_file and ledger_file.parent.exists():
        mem_dir_cand = ledger_file.parent.parent / "entity_memory"
        if mem_dir_cand.exists():
            snap_base = ledger_file.name.replace("_ledger.json", "")
            mf = _find_case_insensitive_db_file(mem_dir_cand, [f"{snap_base}_memory.json"])
            if mf:
                m_raw = load_json(mf)
                if isinstance(m_raw, dict) and isinstance(m_raw.get("entities"), dict):
                    mem_entities_for_ledger = m_raw["entities"]

    mem_entities_lower_for_ledger: dict[str, dict] = {
        str(k).lower(): v for k, v in mem_entities_for_ledger.items() if isinstance(v, dict)
    }

    for item_m in ledger:
        if not isinstance(item_m, dict):
            continue
        item_m.setdefault("lever_type", "")
        item_m.setdefault("planned_path", "")
        item_m.setdefault("expected_effect", "")
        item_m.setdefault("verify_date", "")
        item_m.setdefault("rollback_rule", "")
        if not item_m["expected_effect"] and mem_entities_lower_for_ledger:
            c_n_m, mt_n_m, ent_n_m, fld_n_m = _norm_ledger_entity_slot(item_m)
            c_low_m = c_n_m.lower()
            cand_keys = [f"{c_low_m}::{mt_n_m}::{ent_n_m}"]
            if fld_n_m in ("tos", "pp", "ros"):
                cand_keys.extend([
                    f"{c_low_m}::placement::campaignplacement",
                    f"{c_low_m}::placement::placementtop",
                    f"{c_low_m}::placement::placementproductpage",
                    f"{c_low_m}::placement::placementrestofsearch",
                ])
            elif fld_n_m == "budget":
                cand_keys.append(f"{c_low_m}::budget::dailybudget")
            elif fld_n_m in ("bidding_strategy", "strategy"):
                cand_keys.extend([
                    f"{c_low_m}::strategy::biddingstrategy",
                    f"{c_low_m}::strategy::offamazonstrategy",
                ])
            cand_mem = {}
            for ck_m in cand_keys:
                hit_m = mem_entities_lower_for_ledger.get(ck_m)
                if hit_m and isinstance(hit_m.get("pending_recommendation"), dict):
                    cand_mem = hit_m
                    break
            pend_m = cand_mem.get("pending_recommendation") if isinstance(cand_mem.get("pending_recommendation"), dict) else {}
            if pend_m:
                item_m["lever_type"] = item_m["lever_type"] or str(pend_m.get("lever_type") or "")
                item_m["planned_path"] = item_m["planned_path"] or str(pend_m.get("planned_path") or "")
                item_m["expected_effect"] = item_m["expected_effect"] or str(pend_m.get("expected_effect") or "")
                item_m["verify_date"] = item_m["verify_date"] or str(pend_m.get("verify_date") or "")
                item_m["rollback_rule"] = item_m["rollback_rule"] or str(pend_m.get("rollback_rule") or "")
        if not item_m["lever_type"]:
            fld_l = str(item_m.get("field") or "").lower().strip()
            item_m["lever_type"] = (
                "PLACEMENT" if fld_l in ("tos", "pp", "ros")
                else ("BUDGET" if fld_l == "budget" else ("STRATEGY_STATE" if fld_l in ("bidding_strategy", "strategy", "state") else "BID"))
            )

    # Dynamic Post-Adjustment Effect Attribution
    active_cooldowns = []
    for idx_l, item in enumerate(ledger):
        if num(item.get("baseline_acos")) >= 900.0:
            item["baseline_acos"] = 0.0
        slot_key = _norm_ledger_entity_slot(item)
        latest_idx = latest_idx_by_slot.get(slot_key, idx_l)
        is_superseded = (idx_l < latest_idx)
        is_drift_archived = (idx_l in dayparting_drift_indices)
        superseded_by_date = str(ledger[latest_idx].get("change_date") or "") if is_superseded else ""

        c_name = item.get("campaign") or ""
        matching_camp = camp_lookup.get(c_name)
        ch_at_str = item.get("changed_at") or now.isoformat()
        try:
            ch_at = datetime.fromisoformat(ch_at_str)
            days_elapsed = max(0, (now - ch_at).days)
        except Exception:
            days_elapsed = 0
        days_remaining = 0 if (is_superseded or is_drift_archived) else max(0, cooldown_days - days_elapsed)
        item["days_elapsed"] = days_elapsed
        item["days_remaining"] = days_remaining

        ent_type = item.get("entity_type") or ""
        is_entity_attr = False
        curr_cpc = 0.0
        curr_imp = 0.0
        curr_acos = 0.0
        ent_state = ""
        ent_ord_3d = None
        ent_ord_7d = None
        ent_imp_30d_daily = None

        if ent_type == "keyword_bid" and curr_kw_map:
            e_key = item.get("entity_key") or ""
            kw_obj = curr_kw_map.get(e_key) if e_key else None
            if not kw_obj:
                p_txt, p_match = _parse_legacy_entity_name(item.get("entity_name") or "", c_name)
                if p_txt and p_match:
                    kw_obj = curr_kw_map.get(f"{c_name}::{p_match}::{p_txt}")
                if not kw_obj and p_txt:
                    for k_k, k_v in curr_kw_map.items():
                        if k_v.get("campaign") == c_name and k_v.get("text", "").lower().strip() == p_txt:
                            if not p_match or k_v.get("match", "").lower().strip() == p_match:
                                kw_obj = k_v
                                item["entity_key"] = k_k
                                break
            if kw_obj:
                curr_cpc = round(num(kw_obj.get("cpc_3d") or kw_obj.get("cpc_7d") or kw_obj.get("cpc")), 2)
                i_3d = num(kw_obj.get("imp_3d"))
                i_7d = num(kw_obj.get("imp_7d"))
                i_30d = num(kw_obj.get("imp_30d"))
                ent_imp_30d_daily = round(i_30d / 30.0, 1)
                ent_state = str(kw_obj.get("state") or "").lower().strip()
                if ent_state == "paused" or (matching_camp and matching_camp.get("state") == "paused"):
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_3d > 0 or days_elapsed >= 3:
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_7d > 0:
                    curr_imp = round(i_7d / 7.0, 1)
                else:
                    curr_imp = ent_imp_30d_daily
                raw_kw_ac = num(kw_obj.get("acos_7d") or kw_obj.get("acos"))
                curr_acos = 0.0 if raw_kw_ac >= 900.0 else round(raw_kw_ac, 1)
                ent_ord_3d = int(num(kw_obj.get("ord_3d")))
                ent_ord_7d = int(num(kw_obj.get("ord_7d")))
                is_entity_attr = True

        elif ent_type == "target_bid" and curr_target_map:
            e_key = item.get("entity_key") or ""
            t_obj = curr_target_map.get(e_key) if e_key else None
            if not t_obj:
                p_txt, p_type = _parse_legacy_entity_name(item.get("entity_name") or "", c_name)
                if p_txt:
                    for t_k, t_v in curr_target_map.items():
                        if t_v.get("campaign") == c_name and t_v.get("text", "").lower().strip() == p_txt:
                            if not p_type or t_v.get("type", "").lower().strip() == p_type:
                                t_obj = t_v
                                item["entity_key"] = t_k
                                break
            if t_obj:
                curr_cpc = round(num(t_obj.get("cpc_3d") or t_obj.get("cpc_7d") or t_obj.get("cpc")), 2)
                i_3d = num(t_obj.get("imp_3d"))
                i_7d = num(t_obj.get("imp_7d"))
                i_30d = num(t_obj.get("imp_30d"))
                ent_imp_30d_daily = round(i_30d / 30.0, 1)
                ent_state = str(t_obj.get("state") or "").lower().strip()
                if ent_state == "paused" or (matching_camp and matching_camp.get("state") == "paused"):
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_3d > 0 or days_elapsed >= 3:
                    curr_imp = round(i_3d / 3.0, 1)
                elif i_7d > 0:
                    curr_imp = round(i_7d / 7.0, 1)
                else:
                    curr_imp = ent_imp_30d_daily
                raw_t_ac = num(t_obj.get("acos_7d") or t_obj.get("acos"))
                curr_acos = 0.0 if raw_t_ac >= 900.0 else round(raw_t_ac, 1)
                ent_ord_3d = int(num(t_obj.get("ord_3d")))
                ent_ord_7d = int(num(t_obj.get("ord_7d")))
                is_entity_attr = True

        if not is_entity_attr and matching_camp:
            curr_cpc = round(num(matching_camp.get("cpc_3d") or matching_camp.get("cpc_7d") or matching_camp.get("cpc")), 2)
            if matching_camp.get("state") == "paused" or days_elapsed >= 3 or num(matching_camp.get("daily_imp_3d")) > 0:
                curr_imp = round(num(matching_camp.get("daily_imp_3d")), 1)
            else:
                curr_imp = round(num(matching_camp.get("daily_imp_7d") or matching_camp.get("daily_imp_30d")), 1)
            raw_c_ac = num(matching_camp.get("acos_7d") or matching_camp.get("acos"))
            curr_acos = 0.0 if raw_c_ac >= 900.0 else round(raw_c_ac, 1)

        if matching_camp or is_entity_attr:
            item["current_cpc"] = curr_cpc
            item["current_daily_imp"] = curr_imp
            item["current_acos"] = curr_acos

            base_cpc = num(item.get("baseline_cpc"))
            base_imp = num(item.get("baseline_daily_imp"))

            if is_entity_attr and ent_imp_30d_daily is not None and base_imp > max(80.0, ent_imp_30d_daily * 4.5):
                camp_30d_imp = num(matching_camp.get("daily_imp_30d")) if matching_camp else 0.0
                if ent_imp_30d_daily > 0 and (camp_30d_imp == 0 or base_imp >= camp_30d_imp * 0.5):
                    base_imp = ent_imp_30d_daily
                    item["baseline_daily_imp"] = base_imp

            delta_cpc = round(((curr_cpc - base_cpc) / base_cpc * 100), 1) if (base_cpc > 0 and curr_cpc > 0) else 0.0
            delta_imp = round(((curr_imp - base_imp) / base_imp * 100), 1) if base_imp > 0 else 0.0
            item["delta_cpc_pct"] = delta_cpc
            item["delta_imp_pct"] = delta_imp

            entity_field = str(item.get("field") or "").lower().strip()
            new_val_str = str(item.get("new_val") or "").lower().strip()
            entity_type_str = str(item.get("entity_type") or "").lower().strip()
            camp_is_paused = bool(matching_camp and matching_camp.get("state") == "paused")
            entity_is_paused = (ent_state == "paused")

            old_num_v = num(item.get("old_val"))
            new_num_v = num(item.get("new_val"))
            is_upward_adj = (old_num_v > 0 and new_num_v > old_num_v)
            is_downward_adj = (old_num_v > 0 and new_num_v < old_num_v)
            item["adjustment_direction"] = "UP" if is_upward_adj else ("DOWN" if is_downward_adj else "STATE")

            is_intentional_pause = (
                (entity_field == "state" and new_val_str == "paused")
                or camp_is_paused
                or entity_is_paused
                or "pause" in entity_type_str
            )
            is_intentional_negative = (
                "negative" in entity_type_str
                or "negative" in entity_field
                or "negative" in str(item.get("action") or "").lower()
            )

            # §3.2 Attribution lag protection: when >=5d elapsed, require 7d orders == 0 (not just 3d) before confirming 0-order shock
            if is_entity_attr and ent_ord_3d is not None:
                no_recent_orders = (ent_ord_3d == 0) and (days_elapsed < 5 or (ent_ord_7d or 0) == 0)
            else:
                no_recent_orders = (
                    not matching_camp
                    or (matching_camp.get("ord_3d", 0) == 0 and (days_elapsed < 5 or matching_camp.get("ord_7d", 0) == 0))
                )
            has_recent_orders_7d = (
                ((ent_ord_3d or 0) > 0 or (ent_ord_7d or 0) > 0)
                if is_entity_attr
                else bool(matching_camp and (matching_camp.get("ord_3d", 0) > 0 or matching_camp.get("ord_7d", 0) > 0))
            )
            is_acos_healthy = (0.0 < curr_acos <= (target_acos or 25.0) * 1.15) and has_recent_orders_7d

            if is_drift_archived:
                verdict = "【历史分时快照漂移·归档】(DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED)"
                item["verdict_code"] = "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED"
                advice = "识别为旧快照分时波峰/波谷瞬时竞价往返跳变噪音，已自动归档并释放冷却锁，现统一锚定 timing_base_value 基准竞价"
            elif is_superseded:
                verdict = "【历史动作已由后续调优覆盖·归档】"
                advice = f"该实体已于 {superseded_by_date or '后续日期'} 执行新一轮参数调整，以最新一期台账裁决为准，本条历史记录归档备查"
            elif is_intentional_pause:
                verdict = "【合规意向暂停·阻断流血】"
                advice = "实体已按计划合规暂停，维持停运状态，严禁触发休克熔断或回踩探底"
            elif is_intentional_negative:
                verdict = "【否定拦截生效·预期归零】"
                advice = "否定词拦截生效，已阻断无效流量，维持否定"
            elif is_upward_adj and is_acos_healthy:
                verdict = "【主动提价扩量·投产达标】"
                advice = f"主动上调参数后有单且近期 ACOS ({curr_acos:.1f}%) 受控，维持当前出价巩固首屏份额"
            elif is_downward_adj and curr_cpc > 0 and delta_cpc <= -15.0 and delta_imp > -70.0:
                verdict = "【调后生效·见效保持】"
                advice = "降价控费见效保持，严禁二次砍价"
            elif delta_imp <= -70.0 and base_imp >= 3.0 and no_recent_orders:
                if is_stockout:
                    verdict = "【Listing断货·系统停推】"
                    advice = "FBA可售库存已归零，商品失去购物车并由亚马逊系统强制停推，严禁误判为出价脱靶或加价回踩，等待补货入仓"
                elif is_low_inventory:
                    verdict = "【极低库存告急·控流防断】"
                    advice = f"可售天数仅剩 {dos_cnt:.0f} 天，系统控流防断货，严禁大幅提价抢量加剧断货风险"
                elif days_elapsed > 14:
                    verdict = "【超期历史断流记录·归档】"
                    advice = f"距今已 {days_elapsed} 天（超出14天短期归因窗口），请以当前实时活动与投放词诊断为准"
                else:
                    entity_kw = str(item.get("entity_name") or "").lower().strip()
                    clean_term, _ = _parse_legacy_entity_name(entity_kw, c_name)
                    r_info = xydc_map.get(clean_term) if xydc_map else None
                    if not r_info and xydc_map:
                        r_info = xydc_map.get(re.sub(r"[^\w\s]", "", clean_term).strip())
                    sp_r = r_info.get("sp_rank") if r_info else None
                    is_top_rank = (sp_r is not None and int(sp_r) <= 3) or (not is_entity_attr and matching_camp and matching_camp.get("assessment") == "TOP_POSITION_MARKET_COOLDOWN")
                    is_deal_trans = bool(deal_info and deal_info.get("is_deal_transition")) or is_peak_post_l or (matching_camp and matching_camp.get("assessment") == "DEAL_TRANSITION_COOLDOWN")

                    if is_top_rank:
                        verdict = "【首屏黄金位·常态冷却】"
                        advice = f"核心词广告位已稳居首屏顶部(SP #{sp_r or 1})，曝光回落非出价脱靶，严禁加价回踩，保持当前出价观察"
                    elif is_deal_trans:
                        verdict = "【大促/秒杀后流量回落·常态冷却】"
                        advice = "近期结束大促/秒杀活动（促后 10~14 天冷却期），曝光回落系常态均值回归，严禁误判断流或回踩，保持当前出价观察"
                    else:
                        verdict = "【调后休克断流】"
                        baseline_cpc = base_cpc or (matching_camp.get("cpc", 0.50) if matching_camp else 0.50)
                        cur_tos = num(matching_camp.get("tos", 0)) if matching_camp else 0
                        is_target_entity = (entity_type_str == "target_bid" or "target" in entity_type_str or (matching_camp and any(x in matching_camp.get("name", "").lower() for x in ["asin", "竞品", "b0", "target", "同类", "关联"])))
                        target_bid_floor = as_fl_frg_l if (is_fragile or is_high_refund) else as_fl_std_l
                        camp_bid_floor = target_bid_floor if is_target_entity else (kw_fl_frg_l if (is_fragile or is_high_refund) else kw_fl_std_l)
                        target_bid = max(camp_bid_floor, round(baseline_cpc / (1.0 + (cur_tos / 100.0)), 2))
                        if entity_field in ["tos", "pp", "ros"]:
                            advice = f"位置溢价调整后休克断流，保持TOS {int(cur_tos)}%，建议广告组基础竞价科学托底补偿至 ${target_bid:.2f}"
                        elif entity_field == "budget":
                            advice = "预算调整后断流休克，建议适度回调日预算至合理出单水平"
                        else:
                            cur_bid = num(re.sub(r"[^\d.]", "", str(item.get("new_val") or "0")))
                            if cur_bid > 5.0 or cur_bid <= 0.0:
                                cur_bid = target_bid
                            probe_bid = max(round(cur_bid * 1.15, 2), target_bid)
                            probe_bid = min(probe_bid, max(2.50, round(baseline_cpc * 1.8, 2)))
                            if is_target_entity:
                                advice = f"商品投放详情页拍卖门槛较高，建议科学倒推回踩至 ${probe_bid:.2f} (底价保护 ${target_bid_floor:.2f}) 激活展示池"
                            else:
                                advice = f"立即科学倒推回踩探底至 ${probe_bid:.2f} 激活展示池"
            elif delta_cpc >= 40.0 and curr_imp > 0 and curr_cpc > 0 and not is_acos_healthy:
                verdict = "【调后失血暴冲】"
                advice = "近期点击成本飙升且投产超标/无单，紧急下调溢价/基础竞价控费"
            elif is_acos_healthy:
                verdict = "【投产达标·稳态运行】"
                advice = f"近期有单且 ACOS ({curr_acos:.1f}%) 处于健康区间，维持稳态监控"
            elif days_elapsed <= 3:
                verdict = f"【动态观察期 Day {days_elapsed}/{cooldown_days}】"
                advice = "数据重敛中，保持观察"
            else:
                verdict = "【稳态监控中】"
                advice = "指标平稳，维持基线"

            item["effect_verdict"] = verdict
            item["action_advice"] = advice
        else:
            if is_drift_archived:
                item["current_cpc"] = "--"
                item["current_daily_imp"] = "--"
                item["current_acos"] = "--"
                item["delta_cpc_pct"] = 0.0
                item["delta_imp_pct"] = 0.0
                item["verdict_code"] = "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED"
                item["effect_verdict"] = "【历史分时快照漂移·归档】(DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED)"
                item["action_advice"] = "识别为旧快照分时波峰/波谷瞬时竞价往返跳变噪音，已自动归档"
            else:
                item["current_cpc"] = "--"
                item["current_daily_imp"] = "--"
                item["current_acos"] = "--"
                item["delta_cpc_pct"] = 0.0
                item["delta_imp_pct"] = 0.0
                item["effect_verdict"] = "【活动已归档/未匹配】"
                item["action_advice"] = "保持监控"

        if days_remaining > 0 and not is_superseded and not is_drift_archived:
            active_cooldowns.append({
                "entity_type": item.get("entity_type"),
                "campaign": item.get("campaign"),
                "entity_key": item.get("entity_key") or item.get("entity_name"),
                "field": item.get("field"),
                "old_value": item.get("old_val"),
                "new_value": item.get("new_val"),
                "changed_at": item.get("changed_at"),
                "days_elapsed": days_elapsed,
                "days_remaining": days_remaining,
                "detail": item.get("detail"),
                "status": item.get("effect_verdict"),
            })

    return ledger, active_cooldowns


def archive_daily_record(
    daily_records_dir: Path,
    snap_key: str,
    today_str: str,
    now_iso: str,
    meta: dict,
    profile: dict,
    financial_prof: dict,
    traffic_prof: dict,
    camps: list[dict],
    dayparting_records: list[dict],
) -> Path:
    record_file = daily_records_dir / f"{snap_key}_{today_str}.json"
    data = {
        "date": today_str,
        "recorded_at": now_iso,
        "snap_key": snap_key,
        "meta": meta,
        "profile": {
            "title": profile.get("title"),
            "price": profile.get("price"),
            "stars": profile.get("stars"),
            "reviews": profile.get("reviews"),
            "small_rank": profile.get("small_rank"),
            "fulfillable": profile.get("fulfillable"),
            "days_of_supply": profile.get("days_of_supply"),
        },
        "financial": financial_prof,
        "traffic": traffic_prof,
        "campaigns_summary": [
            {
                "name": c["name"],
                "state": c["state"],
                "budget": c["budget"],
                "tos": c["tos"],
                "pp": c["pp"],
                "ros": c["ros"],
                "spend_30d": round(c["spend"], 2),
                "sales_30d": round(c["sales"], 2),
                "acos_30d": round(c["acos"], 1),
                "cpc_30d": round(c["cpc"], 2),
                "cpc_7d": round(c["cpc_7d"], 2),
                "cpc_3d": round(c["cpc_3d"], 2),
                "delta_cpc_pct": round(c["delta_cpc_pct"], 1),
                "delta_imp_pct": round(c["delta_imp_pct"], 1),
                "ord_30d": c["ord"],
                "cvr_30d": round(c["cvr"], 1),
                "assessment": c.get("assessment", "HEALTHY"),
            }
            for c in camps if c["state"] == "enabled"
        ],
        "dayparting_peak_hours": [f"{r['hour']:02d}:00" for r in dayparting_records if r.get("classification") == "PEAK_GOLDEN_HOUR"],
        "dayparting_bleeding_hours": [f"{r['hour']:02d}:00" for r in dayparting_records if r.get("classification") == "BLEEDING_OFFPEAK_HOUR"],
    }
    record_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_file


def save_ledger_files(ledger: list[dict], ledger_dir: Path, snap_key: str, dest: Path, export_dir: Path | None, prefix: str, gen_time_str: str) -> None:
    ledger_json = ledger_dir / f"{snap_key}_ledger.json"
    ledger_json.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")

    headers = [
        "change_date", "campaign", "entity_type", "entity_name", "field",
        "old_val", "new_val", "baseline_cpc", "current_cpc", "delta_cpc_pct",
        "baseline_daily_imp", "current_daily_imp", "delta_imp_pct",
        "days_elapsed", "days_remaining", "effect_verdict", "action_advice", "detail",
        "lever_type", "planned_path", "expected_effect", "verify_date", "rollback_rule",
    ]
    lines = []
    for r in sorted(ledger, key=lambda x: str(x.get("changed_at", "")), reverse=True):
        row = [
            str(r.get("change_date", "")),
            str(r.get("campaign", "")),
            str(r.get("entity_type", "")),
            str(r.get("entity_name", "")),
            str(r.get("field", "")),
            str(r.get("old_val", "")),
            str(r.get("new_val", "")),
            f"${num(r.get('baseline_cpc')):.2f}" if num(r.get("baseline_cpc")) > 0 else "--",
            f"${num(r.get('current_cpc')):.2f}" if num(r.get("current_cpc")) > 0 else "--",
            f"{r.get('delta_cpc_pct', 0.0):.1f}%",
            str(r.get("baseline_daily_imp", "")),
            str(r.get("current_daily_imp", "")),
            f"{r.get('delta_imp_pct', 0.0):.1f}%",
            str(r.get("days_elapsed", 0)),
            str(r.get("days_remaining", 0)),
            str(r.get("effect_verdict", "")),
            str(r.get("action_advice", "")),
            str(r.get("detail", "")),
            str(r.get("lever_type") or "--"),
            str(r.get("planned_path") or "--"),
            str(r.get("expected_effect") or "--"),
            str(r.get("verify_date") or "--"),
            str(r.get("rollback_rule") or "--"),
        ]
        lines.append("\t".join(re.sub(r"[\t\r\n]+", " ", c).strip() for c in row))

    tsv_content = "\t".join(headers) + "\n" + "\n".join(lines)
    (ledger_dir / f"{snap_key}_ledger.tsv").write_text(tsv_content, encoding="utf-8")
    (dest / "historical_ledger.tsv").write_text(tsv_content, encoding="utf-8")

    if export_dir and export_dir.exists():
        (export_dir / f"{prefix}-historical-ledger-{gen_time_str}.tsv").write_text(tsv_content, encoding="utf-8")
        (export_dir / f"{prefix}-historical-ledger.tsv").write_text(tsv_content, encoding="utf-8")



def _derive_gen_date_and_dt(gen_time_str: str | None) -> tuple[str, datetime]:
    if gen_time_str and len(str(gen_time_str)) >= 8 and str(gen_time_str)[:8].isdigit():
        gs = str(gen_time_str)
        d_s = f"{gs[:4]}-{gs[4:6]}-{gs[6:8]}"
        try:
            if len(gs) >= 15 and gs[8] == "-" and gs[9:15].isdigit():
                dt_obj = datetime.strptime(gs[:15], "%Y%m%d-%H%M%S")
            else:
                dt_obj = datetime.strptime(d_s, "%Y-%m-%d")
            return d_s, dt_obj
        except Exception:
            pass
    now_dt = datetime.now()
    return now_dt.strftime("%Y-%m-%d"), now_dt


def load_known_negatives(args, src):
    """Collect keywords already negated outside this tool, so we stop re-recommending them."""
    known_negatives = set()
    for kn_candidate in [src / "known_negatives.txt", Path("known_negatives.txt"), src / "known_negatives.json", Path("known_negatives.json")]:
        if kn_candidate.exists():
            try:
                if kn_candidate.suffix == ".json":
                    data = json.loads(kn_candidate.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        known_negatives.update(str(x).strip().lower() for x in data if str(x).strip())
                else:
                    for l in kn_candidate.read_text(encoding="utf-8").splitlines():
                        t = l.strip().lower()
                        if t and not t.startswith("#"):
                            known_negatives.add(t)
            except Exception:
                pass
    if getattr(args, "known_negatives", None):
        for item in str(args.known_negatives).split(","):
            t = item.strip().lower()
            if t:
                known_negatives.add(t)

    return known_negatives


def load_shared_brand_campaigns(src: Path, dest: Path, meta: dict | None = None, profile: dict | None = None) -> list[dict]:
    """Item 10 / Iron Law 80: Load cross-portfolio shared SB/SBV/SD campaigns from shared_brand_campaigns.json,
    write shared_brand_campaigns.tsv with anti-false-pause protection, and optionally export hsa_placement_performance_7d.tsv."""
    shared_path = src / "shared_brand_campaigns.json"
    out_records: list[dict] = []
    if shared_path.exists():
        raw_rows = as_rows(load_json(shared_path))
        tsv_lines: list[str] = []
        for r in raw_rows:
            if not isinstance(r, dict) or skip_total(r):
                continue
            cid = str(r.get("campaign_id") or "").strip()
            cname = str(r.get("campaign_name") or r.get("name") or "").strip()
            if not cname and not cid:
                continue
            ad_fmt = str(r.get("ad_format") or r.get("sponsored_type") or r.get("ads_type") or "SB").upper().strip()
            st = str(r.get("state") or "enabled").lower().strip()
            pf_id = str(r.get("portfolio_id") or "").strip()
            pf_name = str(r.get("portfolio_name") or "").strip()
            budget = round(num(r.get("daily_budget") or r.get("budget")), 2)
            sp30 = round(num(r.get("spend_30d") if r.get("spend_30d") is not None else (r.get("spends") or r.get("spend") or r.get("cost"))), 2)
            sl30 = round(num(r.get("sales_30d") if r.get("sales_30d") is not None else r.get("sales")), 2)
            od30 = int(num(r.get("orders_30d") if r.get("orders_30d") is not None else r.get("orders")))
            ck30 = int(num(r.get("clicks_30d") if r.get("clicks_30d") is not None else r.get("clicks")))
            ac30 = round(clean_api_acos(r.get("acos_30d") if r.get("acos_30d") is not None else r.get("acos"), sp30, sl30), 1)
            ro30 = calc_roas(sp30, sl30, r.get("roas_30d") or r.get("roas"))
            sp7 = round(num(r.get("spend_7d")), 2)
            od7 = int(num(r.get("orders_7d")))
            c_asins = [str(x).upper() for x in (r.get("creative_asins") or []) if x]
            shared_cnt = int(num(r.get("shared_asin_count")) or len(c_asins) or 1)
            is_shared = bool(r.get("is_shared_multi_asin_campaign", shared_cnt > 1))
            verdict = "SHARED_MULTI_ASIN_PROTECTED" if is_shared else "CROSS_PORTFOLIO_BRAND_AD"
            note = (
                f"【跨组合多品共享品牌广告·防误关停保护】创意同时挂载 {shared_cnt} 个ASIN ({','.join(c_asins[:6])})，"
                f"30d总花费 ${sp30:.2f} / 总销额 ${sl30:.2f} (ACOS {fmt_acos_text(ac30, sp30, sl30)}, ROAS {ro30:.2f})；"
                "严禁因单一子品局部归因直接关停或大砍预算，须跨组合联合核算全店品牌光环投产。"
            )
            rec = {
                "campaign_id": cid,
                "campaign_name": cname,
                "ad_format": ad_fmt,
                "state": st,
                "portfolio_id": pf_id,
                "portfolio_name": pf_name,
                "budget": budget,
                "spend_30d": sp30,
                "sales_30d": sl30,
                "acos_30d": ac30,
                "roas_30d": ro30,
                "orders_30d": od30,
                "clicks_30d": ck30,
                "spend_7d": sp7,
                "orders_7d": od7,
                "shared_asin_count": shared_cnt,
                "creative_asins": c_asins,
                "is_shared_multi_asin_campaign": is_shared,
                "protection_verdict": verdict,
                "strategic_note": note,
            }
            out_records.append(rec)
            tsv_lines.append("\t".join(str(x) for x in [
                cid, cname, ad_fmt, st, pf_id, pf_name, budget,
                sp30, sl30, ac30, ro30, od30, ck30, sp7, od7,
                shared_cnt, ",".join(c_asins), verdict, note,
            ]))
        tsv(
            dest / "shared_brand_campaigns.tsv",
            "campaign_id\tcampaign_name\tad_format\tstate\tportfolio_id\tportfolio_name\tbudget\tspend_30d\tsales_30d\tacos_30d\troas_30d\torders_30d\tclicks_30d\tspend_7d\torders_7d\tshared_asin_count\tcreative_asins\tprotection_verdict\tstrategic_note",
            tsv_lines,
        )

    hsa_pl_path = src / "hsa_placement_report_7d.json"
    if hsa_pl_path.exists():
        hsa_rows = [r for r in as_rows(load_json(hsa_pl_path)) if isinstance(r, dict) and (r.get("campaign_id") or r.get("campaign_name"))]
        if hsa_rows:
            hsa_lines = []
            for hr in hsa_rows:
                h_sp = round(num(hr.get("spends") if hr.get("spends") is not None else hr.get("cost")), 2)
                h_sl = round(num(hr.get("sales")), 2)
                h_od = int(num(hr.get("orders")))
                h_ck = int(num(hr.get("clicks")))
                h_im = int(num(hr.get("impressions")))
                h_ac = round(clean_api_acos(hr.get("acos"), h_sp, h_sl), 1)
                h_sh = round(num(hr.get("spend_share_pct")), 1)
                hsa_lines.append("\t".join(str(x) for x in [
                    hr.get("campaign_name") or "", hr.get("ad_format") or "HSA",
                    hr.get("placement_type") or hr.get("placement") or "",
                    h_sp, h_sh, h_sl, h_ac, h_od, h_ck, h_im,
                    round(clean_api_rate(hr.get("cpc")), 2), round(clean_api_rate(hr.get("ctr")), 2), round(clean_api_rate(hr.get("cvr")), 2),
                ]))
            tsv(
                dest / "hsa_placement_performance_7d.tsv",
                "campaign\tad_format\tplacement\tspend_7d\tspend_share_pct\tsales_7d\tacos_7d\torders_7d\tclicks_7d\timpressions_7d\tcpc_7d\tctr_7d\tcvr_7d",
                hsa_lines,
            )
    return out_records


def load_product_context(args, dest, src):
    """Load the listing, financial, traffic, weekly trends, shared brand ads, and dayparting profiles,
    and settle target_acos strictly constrained by true_all_in_break_even_tacos (Item 1 / Iron Law 76)."""
    meta = load_json(src / "meta.json") or {}
    listing = load_json(src / "listing.json")
    stock = load_json(src / "fba_stock.json")
    profile = listing_profile(listing, stock)
    profile["meta"] = meta

    rule_path = Path(args.rules) if args.rules else None
    raw_rules = load_product_rules(rule_path, src)
    rules = sanitize_product_rules(raw_rules, profile)

    financial_prof = load_financial_profile(src, meta=meta)
    traffic_prof = load_traffic_profile(src, db_dir=args.db_dir, financial_prof=financial_prof, rules=rules)
    deal_info = load_deal_info(src, meta=meta)
    dayparting_records, dayparting_table_lines = load_dayparting_analysis(src)
    weekly_trend_lines = load_weekly_trends_analysis(src)
    shared_brand_records = load_shared_brand_campaigns(src, dest, meta=meta, profile=profile)

    if shared_brand_records:
        profile["shared_brand_campaigns"] = shared_brand_records
        if financial_prof is not None:
            financial_prof["shared_brand_campaigns_count"] = len(shared_brand_records)
            financial_prof["shared_brand_campaigns_spend_30d"] = round(sum(x["spend_30d"] for x in shared_brand_records), 2)

    if traffic_prof:
        profile["traffic_profile"] = traffic_prof
        dest.joinpath("traffic_profile.json").write_text(
            json.dumps(traffic_prof, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if deal_info:
        profile["deal_info"] = deal_info
        dest.joinpath("deal_info.json").write_text(
            json.dumps(deal_info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if dayparting_table_lines:
        tsv(
            dest / "dayparting_analysis.tsv",
            "hour_window\tspend\tad_orders\tstore_orders\tcvr\tacos\tcpc\tclassification\trecommendation",
            dayparting_table_lines,
        )
    if weekly_trend_lines:
        tsv(
            dest / "weekly_trends.tsv",
            "period_type\tperiod_label\tspend\tad_sales\tstore_sales\tad_orders\tstore_orders\tclicks\timpressions\tcpc\tcvr\tacos\troas\ttacos\tpredict_gross_profit",
            weekly_trend_lines,
        )

    gen_time_str = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")

    # Initial stage & target settlement (re-settled after xydc/macro shrinkage check in main()!)
    _, win_end = _parse_window_bounds(meta)
    stage_ctx = v2p.determine_stage_and_targets(
        raw_mode_arg=getattr(args, "mode", None),
        peak_flag=bool(getattr(args, "peak", False)),
        no_peak_flag=bool(getattr(args, "no_peak", False)),
        rules=rules,
        profile=profile,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        deal_info=deal_info,
        xydc_map=None,
        shrinkage_signal=False,
        explicit_target_acos=args.target_acos,
        anchor_date_str=win_end or (deal_info or {}).get("anchor_date"),
    )
    target_acos = float(stage_ctx["target_acos"])
    profile["stage_context"] = stage_ctx
    profile["stage_mode"] = stage_ctx["stage_mode"]
    profile["peak_overlay"] = stage_ctx["peak_overlay"]
    profile["peak_phase"] = stage_ctx["peak_phase"]
    profile["zero_ad_loss_circuit_breaker"] = bool(stage_ctx["zero_ad_loss_circuit_breaker"])
    profile["ad_unsustainable_zone"] = bool(stage_ctx["ad_unsustainable_zone"])
    profile["critical_refund_alert"] = bool(stage_ctx["critical_refund_lv1"])
    profile["target_acos"] = target_acos
    profile["target_tacos"] = stage_ctx["target_tacos"]
    profile["tcpa"] = stage_ctx["tcpa"]
    if financial_prof:
        financial_prof["effective_target_acos"] = target_acos
        financial_prof["target_tacos"] = stage_ctx["target_tacos"]
        financial_prof["target_cpa"] = stage_ctx["tcpa"]
        financial_prof["stage_mode"] = stage_ctx["stage_mode"]
        financial_prof["ad_unsustainable_zone"] = stage_ctx["ad_unsustainable_zone"]
        financial_prof["zero_ad_loss_circuit_breaker"] = stage_ctx["zero_ad_loss_circuit_breaker"]
        profile["financial_profile"] = financial_prof
        dest.joinpath("financial_profile.json").write_text(
            json.dumps(financial_prof, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    profile["rules_loaded"] = {
        "is_patterns_count": len(rules.get("is_patterns", [])),
        "is_not_patterns_count": len(rules.get("is_not_patterns", [])),
        "quarantined_self_title_patterns_count": len(rules.get("quarantined_self_title_patterns", [])),
        "competitor_brands_count": len(rules.get("competitor_brands", [])),
        "target_acos": target_acos,
        "target_tacos": stage_ctx["target_tacos"],
        "stage_mode": stage_ctx["stage_mode"],
        "peak_overlay": stage_ctx["peak_overlay"],
        "zero_ad_loss_circuit_breaker": bool(profile.get("zero_ad_loss_circuit_breaker")),
        "ad_unsustainable_zone": bool(profile.get("ad_unsustainable_zone")),
    }

    dest.joinpath("product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return dayparting_records, dayparting_table_lines, financial_prof, gen_time_str, meta, profile, rules, target_acos, traffic_prof, deal_info


def _normalize_placement_slot(raw_p: str) -> str:
    s = (raw_p or "").upper().strip()
    if "TOP" in s or "顶部" in s:
        return "TOS"
    if "DETAIL" in s or "PRODUCT" in s or "商品" in s:
        return "PP"
    if "OTHER" in s or "REST" in s or "其余" in s:
        return "ROS"
    if "OFF" in s or "站外" in s:
        return "OFF"
    return s or "UNKNOWN"


def load_real_placement_report(src: Path, camps: list[dict], dest: Path, target_acos: float = 25.0, scaled_thresholds: dict | None = None) -> list[dict]:
    """Load real SP placement report (30d & 7d), normalize spend_share_pct strictly within each campaign (sum == 100%),
    and perform differentiated OFF AMAZON (站外) audit with TCPA-scaled spend thresholds (§7.4)."""
    by_id = {str(c.get("id")): c for c in camps if c.get("id") is not None and str(c.get("id")) != ""}
    by_name = {str(c.get("name")): c for c in camps if c.get("name")}

    def _parse_file(fname: str) -> dict[str, dict[str, dict]]:
        rows_raw = as_rows(load_json(src / fname))
        grouped: dict[str, dict[str, dict]] = defaultdict(dict)
        for r in rows_raw:
            if not isinstance(r, dict):
                continue
            cid = str(r.get("campaign_id") or "").strip()
            cname = str(r.get("campaign_name") or r.get("name") or "").strip()
            if (not cid or cid == "None") and not cname:
                continue
            camp = by_id.get(cid) or by_name.get(cname)
            if not camp:
                continue
            slot = _normalize_placement_slot(str(r.get("placement") or r.get("placement_type") or ""))
            sp = num(r.get("spends") or r.get("spend") or r.get("cost"))
            sl = num(r.get("sales"))
            od = int(num(r.get("orders")))
            dir_od = int(num(r.get("direct_orders")))
            indir_od = int(num(r.get("indirect_orders")))
            ck = int(num(r.get("clicks")))
            im = int(num(r.get("impressions")))
            grouped[camp["name"]][slot] = {
                "slot": slot,
                "raw_placement": str(r.get("placement") or slot),
                "modifier_pct": num(r.get("percentage") or r.get("bid_adjustment")),
                "strategy": str(r.get("strategy") or ""),
                "spend": round(sp, 2),
                "sales": round(sl, 2),
                "orders": od,
                "direct_orders": dir_od,
                "indirect_orders": indir_od,
                "clicks": ck,
                "impressions": im,
                "cpc": round((sp / ck) if ck > 0 else clean_api_rate(r.get("cpc")), 2),
                "cvr": round((od / ck * 100.0) if ck > 0 else clean_api_rate(r.get("cvr")), 2),
                "ctr": round((ck / im * 100.0) if im > 0 else clean_api_rate(r.get("ctr")), 2),
                "acos": round(clean_api_acos(r.get("acos"), sp, sl), 1),
                "spend_share": 0.0,
            }
        # Strictly normalize spend_share within each single campaign so sum(spend_share) == 100.0%
        for cname_k, slots in grouped.items():
            tot_sp = sum(v["spend"] for v in slots.values())
            if tot_sp > 0:
                max_slot_k = max(slots.keys(), key=lambda k: slots[k]["spend"])
                running_sum = 0.0
                for sk, v in slots.items():
                    if sk != max_slot_k:
                        sh = round(v["spend"] / tot_sp * 100.0, 1)
                        v["spend_share"] = sh
                        running_sum += sh
                slots[max_slot_k]["spend_share"] = round(max(0.0, 100.0 - running_sum), 1)
            else:
                for v in slots.values():
                    v["spend_share"] = 0.0
        return grouped

    p30 = _parse_file("placement_report.json")
    p7 = _parse_file("placement_report_7d.json")

    tsv_lines = []
    flat_records = []
    for c in camps:
        cn = c["name"]
        c["real_placements_30d"] = p30.get(cn, {})
        c["real_placements_7d"] = p7.get(cn, {})
        c["off_amazon_status"] = "NONE"

        off_bleed_sp_thresh = float((scaled_thresholds or {}).get("off_amazon_bleed_spend", 15.0))
        off30 = c["real_placements_30d"].get("OFF")
        if off30 and off30.get("spend", 0) > 0:
            off_sp = off30["spend"]
            off_sh = off30["spend_share"]
            off_od = off30["orders"]
            off_ac = off30["acos"]
            if ((off_sh >= 20.0 and off_sp >= 5.0) or off_sp >= off_bleed_sp_thresh) and (off_od == 0 or off_ac > target_acos * 1.25):
                c["off_amazon_status"] = "OFF_AMAZON_BLEED"
            elif off_od >= 2 and 0.0 < off_ac <= target_acos * 1.15:
                c["off_amazon_status"] = "OFF_AMAZON_PROTECTED"
            else:
                c["off_amazon_status"] = "OFF_AMAZON_MONITOR"

        for slot in ("TOS", "PP", "ROS", "OFF"):
            s30 = c["real_placements_30d"].get(slot)
            s7 = c["real_placements_7d"].get(slot, {})
            if not s30 and not s7:
                continue
            base = s30 or {"slot": slot, "spend": 0.0, "sales": 0.0, "orders": 0, "direct_orders": 0, "indirect_orders": 0, "clicks": 0, "impressions": 0, "cpc": 0.0, "cvr": 0.0, "acos": 0.0, "spend_share": 0.0}
            if slot == "OFF":
                slot_audit = c["off_amazon_status"] if base["spend"] > 0 else "NONE"
            elif base["spend"] >= off_bleed_sp_thresh and (base["orders"] == 0 or base["acos"] > target_acos * 1.50):
                slot_audit = f"{slot}_BLEED"
            elif base["orders"] >= 2 and 0.0 < base["acos"] <= target_acos * 1.15:
                slot_audit = f"{slot}_HEALTHY"
            else:
                slot_audit = "NORMAL"

            rec = {
                "campaign": cn,
                "state": c.get("state", ""),
                "bidding_strategy": c.get("bidding_strategy", ""),
                "slot": slot,
                "configured_pct": int(c.get(slot.lower(), 0)) if slot in ("TOS", "PP", "ROS") else 0,
                **base,
                "spend_7d": s7.get("spend", 0.0),
                "orders_7d": s7.get("orders", 0),
                "acos_7d": s7.get("acos", 0.0),
                "cpc_7d": s7.get("cpc", 0.0),
                "slot_audit_verdict": slot_audit,
            }
            flat_records.append(rec)
            tsv_lines.append("\t".join(str(x) for x in [
                cn, c.get("state", ""), slot, rec["configured_pct"], c.get("bidding_strategy", ""),
                rec["spend"], rec["spend_share"], rec["sales"], rec["acos"],
                rec["orders"], rec["direct_orders"], rec["indirect_orders"],
                rec["clicks"], rec["cpc"], rec["cvr"],
                rec["spend_7d"], rec["orders_7d"], rec["acos_7d"], rec["cpc_7d"],
                slot_audit,
            ]))

    if tsv_lines:
        tsv(
            dest / "real_placement_performance.tsv",
            "campaign\tstate\tslot\tmodifier_pct\tbidding_strategy\tspend_30d\tspend_share_pct\tsales_30d\tacos_30d\torders_30d\tdirect_ord\tindirect_ord\tclicks_30d\tcpc_30d\tcvr_30d\tspend_7d\torders_7d\tacos_7d\tcpc_7d\tslot_audit_verdict",
            tsv_lines,
        )
    return flat_records


def _extract_daily_date_str(row: dict) -> str | None:
    for k in ("rdate", "date", "report_date", "purchase_date", "stat_date", "day_date", "date_str", "time", "snapshot_date"):
        val = str(row.get(k) or "").strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", val)
        if m:
            return m.group(1)
    return None


def _extract_small_bsr_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    if isinstance(val, list) and val:
        ranks = [int(num(x.get("rank"))) for x in val if isinstance(x, dict) and num(x.get("rank")) > 0]
        return min(ranks) if ranks else None
    if isinstance(val, dict) and num(val.get("rank")) > 0:
        return int(num(val.get("rank")))
    m = re.search(r"#?\s*(\d{1,6})", str(val).replace(",", ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def _extract_clean_operation_events(op_rows: list) -> dict:
    """Item 9a: Parse operate_logs.json inspecting ONLY real operational event descriptions/remarks,
    completely excluding metadata keys such as `product_info`, `small_image_url`, `image_url`, or passive BSR/Rating logs."""
    op_events = {
        "total_logs": 0,
        "price_changes": 0,
        "copy_or_image_changes": 0,
        "ad_or_promo_changes": 0,
        "highlights": [],
    }
    event_items: list[tuple[str, str]] = []
    for entry in (op_rows or [])[:100]:
        if not isinstance(entry, dict):
            continue
        src_type = str(entry.get("source") or "").strip()
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
        if src_type == "analytics_log_list_v2" or "auto_log_data" in payload:
            ald = payload.get("auto_log_data")
            if isinstance(ald, dict):
                for _dt, day_obj in ald.items():
                    if isinstance(day_obj, dict):
                        for ld in (day_obj.get("log_data") or []):
                            if isinstance(ld, dict):
                                event_items.append((str(ld.get("type_name") or ""), str(ld.get("remark") or "")))
            if isinstance(payload.get("log_data"), list):
                for ld in payload.get("log_data"):
                    if isinstance(ld, dict):
                        event_items.append((str(ld.get("type_name") or ""), str(ld.get("remark") or "")))
        else:
            # sales_operate_log_list or flat operational log row: inspect ONLY explicit event text values, NEVER image_url metadata
            t_name = str(payload.get("operate_type_name") or payload.get("type_name") or payload.get("module") or "")
            desc_parts = []
            for k_ev in ("operate_desc", "content", "remark", "change_detail", "log_desc", "detail"):
                v_ev = payload.get(k_ev)
                if isinstance(v_ev, str) and v_ev.strip():
                    desc_parts.append(v_ev.strip())
            if desc_parts or t_name:
                event_items.append((t_name, " ".join(desc_parts)))

    op_events["total_logs"] = len(event_items)
    for t_name, remark in event_items:
        tn_l = t_name.lower().strip()
        rm_l = remark.lower().strip()
        combined = f"{tn_l} {rm_l}".strip()
        if not combined:
            continue
        # Ignore passive metric fluctuations (e.g. Rating/Review/BSR changes)
        if tn_l == "表现" or any(p in rm_l for p in ("[评分变动]", "[rating变动]", "[bsr变动]", "[排名变动]")):
            continue
        if any(w in combined for w in ("[价格]", "[售价]", "优惠券", "coupon", "sale price", "standard price", "修改价格", "调价")) or tn_l in ("价格", "促销价"):
            op_events["price_changes"] += 1
            if len(op_events["highlights"]) < 5:
                op_events["highlights"].append(remark[:80])
        if (
            any(w in combined for w in ("[标题]", "[五点]", "[主图]", "[图片]", "[描述]", "[a+]", "修改标题", "修改五点", "修改主图", "修改图片", "修改描述", "bullet_point", "item_name edit", "main_image edit"))
            or tn_l in ("文案", "图片", "listing编辑", "商品信息")
        ):
            op_events["copy_or_image_changes"] += 1
            if len(op_events["highlights"]) < 5:
                op_events["highlights"].append(remark[:80])
        if tn_l == "广告" or any(w in combined for w in ("[广告", "[投放]", "[否定投放]", "竞价", "预算", "秒杀", "deal")):
            op_events["ad_or_promo_changes"] += 1
    return op_events


def evaluate_macro_scorecard(
    *,
    src: Path,
    dest: Path,
    export_dir: str | None,
    db_root: Path,
    snap_key: str,
    legacy_key: str,
    gen_time_str: str,
    meta: dict,
    profile: dict,
    financial_prof: dict,
    traffic_prof: dict,
    camps: list[dict],
    deal_info: dict,
    target_acos: float,
    stage_ctx: dict | None = None,
    battlecard: list[dict] | None = None,
    change_budget_stats: dict | None = None,
    write_expectations: bool = True,
) -> dict:
    """Evaluate Macro North-Star KPI outcomes with 5-fold calibration (Item 9):
    1) Zero false-positive operate_logs parsing (excludes small_image_url / product_info metadata);
    2) Non-self-overwriting expectations.json anchor on same-day or unadvanced window runs;
    3) Growth protection + predict_gross_profit / nature_cvr / ad_cvr PROFIT_OPTIMIZATION_ON_TRACK detection;
    4) Daily negative nature_order_items recovery via max(0, vol - ad_direct_order_quantity);
    5) Cross-shop shared Buy Box ASIN (is_cross_shop_asin) automatic omni_tacos linkage.
    """
    expectations_dir = db_root / "expectations"
    expectations_dir.mkdir(parents=True, exist_ok=True)
    curr_gen_date, curr_gen_dt = _derive_gen_date_and_dt(gen_time_str)
    curr_window = str((meta or {}).get("window") or "").strip()

    exp_file = expectations_dir / f"{snap_key}_expectations.json"
    existing_exp_path = _find_case_insensitive_db_file(
        expectations_dir,
        [f"{snap_key}_expectations.json", f"{legacy_key}_expectations.json"],
    )
    prev_exp = load_json(existing_exp_path) if existing_exp_path else None

    def _daily_ratio_to_pct(val) -> float:
        if val is None or str(val).strip() in ("", "None", "--"):
            return 0.0
        s = str(val).strip()
        v = num(val)
        return round(v, 2) if s.endswith("%") else round(v * 100.0, 2)

    # 1. Parse daily ASIN trajectory from product_performance_daily.json
    daily_raw = as_rows(load_json(src / "product_performance_daily.json"))
    by_day: dict[str, dict] = {}
    for r in daily_raw:
        if not isinstance(r, dict):
            continue
        d_str = _extract_daily_date_str(r)
        if not d_str:
            continue
        ad_sp = _extract_row_ad_costs(r)["total_ad_cost"]
        vol = int(num(r.get("volume") or r.get("order_items_quantity") or r.get("order_items")))
        nat_vol_raw = int(num(r.get("nature_order_items")))
        ad_dir_q = int(num(r.get("ad_direct_order_quantity")))
        # Item 9d: Recover true organic units when cross-variant halo orders push nature_order_items < 0
        if nat_vol_raw < 0:
            org_units = max(0, vol - ad_dir_q) if ad_dir_q > 0 else 0
        else:
            org_units = nat_vol_raw
        day_sales = num(r.get("amount") or r.get("sales_amount") or r.get("net_amount"))
        pred_gp = num(r.get("predict_gross_profit") if r.get("predict_gross_profit") is not None else r.get("gross_profit"))
        nat_cvr_d = max(0.0, _daily_ratio_to_pct(r.get("nature_cvr")))
        ad_cvr_d = max(0.0, _daily_ratio_to_pct(r.get("ad_cvr")))
        by_day[d_str] = {
            "date": d_str,
            "sales": day_sales,
            "units": vol,
            "organic_units": org_units,
            "ad_units": max(0, vol - org_units),
            "sessions": int(num(r.get("sessions_total") or r.get("sessions"))),
            "ad_spend": ad_sp,
            "predict_gross_profit": pred_gp,
            "nature_cvr_pct": nat_cvr_d,
            "ad_cvr_pct": ad_cvr_d,
            "buy_box_pct": num(r.get("buy_box_percentage")),
            "avg_price": num(r.get("avg_landed_price") or r.get("landed_price") or r.get("price")) or (round(day_sales / vol, 2) if vol > 0 else 0.0),
            "small_bsr": _extract_small_bsr_int(r.get("small_cate_rank") or r.get("small_rank")),
            "returns": int(num(r.get("return_count"))),
        }

    sorted_days = [by_day[d] for d in sorted(by_day.keys())]

    def _agg_slice(days_slice: list[dict], fallback_days: int = 7) -> dict:
        n = len(days_slice)
        if n == 0:
            return {
                "days": 0, "daily_sales": 0.0, "daily_units": 0.0, "daily_organic_units": 0.0,
                "daily_ad_units": 0.0, "organic_unit_share_pct": 0.0, "daily_sessions": 0.0,
                "daily_ad_spend": 0.0, "daily_predict_gross_profit": 0.0, "avg_nature_cvr_pct": 0.0,
                "avg_ad_cvr_pct": 0.0, "real_tacos_pct": 0.0, "buy_box_pct": 0.0, "avg_price": 0.0, "small_bsr": None,
            }
        tot_s = sum(x["sales"] for x in days_slice)
        tot_u = sum(x["units"] for x in days_slice)
        tot_org = sum(x["organic_units"] for x in days_slice)
        tot_adu = sum(x["ad_units"] for x in days_slice)
        tot_gp = sum(x.get("predict_gross_profit", 0.0) for x in days_slice)
        sess_vals = [x["sessions"] for x in days_slice if x["sessions"] > 0]
        nc_vals = [x["nature_cvr_pct"] for x in days_slice if x.get("nature_cvr_pct", 0) > 0]
        ac_vals = [x["ad_cvr_pct"] for x in days_slice if x.get("ad_cvr_pct", 0) > 0]
        tot_sp = sum(x["ad_spend"] for x in days_slice)
        bb_vals = [x["buy_box_pct"] for x in days_slice if x["buy_box_pct"] > 0]
        pr_vals = [x["avg_price"] for x in days_slice if x["avg_price"] > 0]
        bsr_vals = [x["small_bsr"] for x in days_slice if x["small_bsr"]]
        bb_raw = (sum(bb_vals) / len(bb_vals)) if bb_vals else 0.0
        bb_pct = round(bb_raw * (100.0 if 0 < bb_raw <= 1.0 else 1.0), 1)
        return {
            "days": n,
            "start_date": days_slice[0]["date"],
            "end_date": days_slice[-1]["date"],
            "daily_sales": round(tot_s / n, 2),
            "daily_units": round(tot_u / n, 2),
            "daily_organic_units": round(tot_org / n, 2),
            "daily_ad_units": round(tot_adu / n, 2),
            "organic_unit_share_pct": round((tot_org / tot_u * 100.0), 1) if tot_u > 0 else 0.0,
            "daily_sessions": round(sum(sess_vals) / len(sess_vals), 1) if sess_vals else 0.0,
            "daily_ad_spend": round(tot_sp / n, 2),
            "daily_predict_gross_profit": round(tot_gp / n, 2),
            "avg_nature_cvr_pct": round(sum(nc_vals) / len(nc_vals), 2) if nc_vals else 0.0,
            "avg_ad_cvr_pct": round(sum(ac_vals) / len(ac_vals), 2) if ac_vals else 0.0,
            "real_tacos_pct": round((tot_sp / tot_s * 100.0), 2) if tot_s > 0 else (999.0 if tot_sp > 0 else 0.0),
            "buy_box_pct": bb_pct,
            "avg_price": round((sum(pr_vals) / len(pr_vals)), 2) if pr_vals else 0.0,
            "small_bsr": int(round(sum(bsr_vals) / len(bsr_vals))) if bsr_vals else None,
        }

    has_daily_series = len(sorted_days) >= 10
    all_deal_dates = set((deal_info or {}).get("all_deal_dates") or [])
    prior_deal_dates_hit: list[str] = []
    if has_daily_series:
        fetched_day_str = str((meta or {}).get("fetched_at") or "")[:10]
        eval_days = sorted_days
        if len(eval_days) >= 15 and (
            (fetched_day_str and eval_days[-1]["date"] == fetched_day_str)
            or (eval_days[-1]["sales"] == 0 and eval_days[-1]["ad_spend"] == 0)
        ):
            eval_days = eval_days[:-1]
        recent_slice = eval_days[-7:]
        prior_slice = eval_days[-14:-7] if len(eval_days) >= 14 else eval_days[:-7]
        recent_deal_hits = [d["date"] for d in recent_slice if d["date"] in all_deal_dates]
        prior_deal_hits = [d["date"] for d in prior_slice if d["date"] in all_deal_dates]
        if len(prior_deal_hits) > len(recent_deal_hits):
            prior_deal_dates_hit = prior_deal_hits
        recent_7d = _agg_slice(recent_slice)
        prior_7d = _agg_slice(prior_slice)
        baseline_30d = _agg_slice(eval_days)
        data_source = "product_performance_daily_usd"
    else:
        fin_sales_30d = num((financial_prof or {}).get("total_sales"))
        fin_vol_30d = num((financial_prof or {}).get("volume"))
        fin_ad_30d = num((financial_prof or {}).get("total_ad_cost"))
        fin_gp_30d = num((financial_prof or {}).get("adjusted_gross_profit") if (financial_prof or {}).get("adjusted_gross_profit") is not None else (financial_prof or {}).get("gross_profit"))
        trf_sess_30d = num((traffic_prof or {}).get("sessions_total"))
        trf_nat_30d = num((traffic_prof or {}).get("nature_order_items"))
        trf_ncvr = num((traffic_prof or {}).get("nature_cvr"))
        trf_acvr = num((traffic_prof or {}).get("ad_cvr"))
        camp_sp_30d = sum(c.get("spend", 0.0) for c in camps)
        camp_sl_30d = sum(c.get("sales", 0.0) for c in camps)
        camp_od_30d = sum(c.get("ord", 0.0) for c in camps)
        camp_sp_7d = sum(c.get("spend_7d", 0.0) for c in camps)
        camp_sl_7d = sum(c.get("sales_7d", 0.0) for c in camps)
        camp_od_7d = sum(c.get("ord_7d", 0.0) for c in camps)

        avg_p = num((financial_prof or {}).get("avg_price") or profile.get("price") or 25.0)
        base_d_sales = round((fin_sales_30d or num(profile.get("thirty_sales")) or camp_sl_30d) / 30.0, 2)
        base_d_units = round((fin_vol_30d or camp_od_30d) / 30.0, 2)
        base_d_org = round(trf_nat_30d / 30.0, 2) if trf_nat_30d > 0 else round(max(0.0, base_d_units - (camp_od_30d / 30.0)), 2)
        base_d_sp = round((fin_ad_30d or camp_sp_30d) / 30.0, 2)
        base_d_gp = round(fin_gp_30d / 30.0, 2)

        lst_7d_avg_u = num(profile.get("seven_volume"))
        lst_14d_raw = num(profile.get("fourteen_volume"))
        lst_14d_sales = num(profile.get("fourteen_sales"))
        lst_14d_sp = num(profile.get("fourteen_ad_spend"))

        if lst_7d_avg_u > 0 and lst_14d_raw > 0:
            lst_14d_avg_u = (lst_14d_raw / 14.0) if (lst_14d_sales > 0 and avg_p > 0 and abs(lst_14d_raw * avg_p - lst_14d_sales) < abs(lst_14d_raw * 14.0 * avg_p - lst_14d_sales)) else lst_14d_raw
            rec_d_units = round(lst_7d_avg_u, 2)
            pri_d_units = round(max(0.01, 2.0 * lst_14d_avg_u - lst_7d_avg_u), 2)
            eff_unit_p = (lst_14d_sales / (lst_14d_avg_u * 14.0)) if (lst_14d_sales > 0 and lst_14d_avg_u > 0) else avg_p
            rec_d_sales = round(rec_d_units * eff_unit_p, 2)
            pri_d_sales = round(pri_d_units * eff_unit_p, 2)
            rec_d_ad_u = round(min(rec_d_units, camp_od_7d / 7.0), 2)
            rec_d_org = round(max(0.0, rec_d_units - rec_d_ad_u), 2)
            pri_ad_u_est = round(min(pri_d_units, max(0.0, (camp_od_30d - camp_od_7d) / 23.0)), 2)
            pri_d_org = round(max(0.0, pri_d_units - pri_ad_u_est), 2)
            rec_d_sp = round(camp_sp_7d / 7.0, 2) if camp_sp_7d > 0 else base_d_sp
            pri_d_sp = round(max(0.0, (lst_14d_sp - camp_sp_7d) / 7.0), 2) if lst_14d_sp > camp_sp_7d else base_d_sp
            data_source = "listing_7d_vs_14d_authoritative_fallback"
        else:
            adv_share = (num((traffic_prof or {}).get("adv_sales_share")) / 100.0) if traffic_prof else 0.0
            est_7d_total_sales = (camp_sl_7d / adv_share) if (adv_share >= 0.20 and camp_sl_7d > 0) else (base_d_sales * 7.0 if camp_sl_30d == 0 else (base_d_sales * 7.0 * (camp_sl_7d / max(1.0, camp_sl_30d / 30.0 * 7.0))))
            rec_d_sales = round(est_7d_total_sales / 7.0, 2) if camp_sl_7d > 0 else base_d_sales
            rec_d_units = round(rec_d_sales / avg_p, 2) if avg_p > 0 else round(camp_od_7d / 7.0, 2)
            rec_d_ad_u = round(camp_od_7d / 7.0, 2)
            rec_d_org = round(max(0.0, rec_d_units - rec_d_ad_u), 2)
            rec_d_sp = round(camp_sp_7d / 7.0, 2)
            pri_d_sales, pri_d_units, pri_d_org, pri_ad_u_est, pri_d_sp = base_d_sales, base_d_units, base_d_org, round(camp_od_30d / 30.0, 2), base_d_sp
            data_source = "aggregate_30d_and_campaign_7d_fallback"

        baseline_30d = {
            "days": 30, "daily_sales": base_d_sales, "daily_units": base_d_units,
            "daily_organic_units": base_d_org, "daily_ad_units": round(camp_od_30d / 30.0, 2),
            "organic_unit_share_pct": round((base_d_org / base_d_units * 100.0), 1) if base_d_units > 0 else 0.0,
            "daily_sessions": round(trf_sess_30d / 30.0, 1), "daily_ad_spend": base_d_sp,
            "daily_predict_gross_profit": base_d_gp, "avg_nature_cvr_pct": trf_ncvr, "avg_ad_cvr_pct": trf_acvr,
            "real_tacos_pct": round(num((traffic_prof or {}).get("tacos")), 2),
            "buy_box_pct": round(num((traffic_prof or {}).get("buy_box_percentage")), 1),
            "avg_price": round(avg_p, 2), "small_bsr": _extract_small_bsr_int(profile.get("small_rank")),
        }
        prior_7d = {
            "days": 7, "daily_sales": pri_d_sales, "daily_units": pri_d_units,
            "daily_organic_units": pri_d_org, "daily_ad_units": pri_ad_u_est,
            "organic_unit_share_pct": round((pri_d_org / pri_d_units * 100.0), 1) if pri_d_units > 0 else 0.0,
            "daily_sessions": baseline_30d["daily_sessions"], "daily_ad_spend": pri_d_sp,
            "daily_predict_gross_profit": base_d_gp, "avg_nature_cvr_pct": trf_ncvr, "avg_ad_cvr_pct": trf_acvr,
            "real_tacos_pct": round((pri_d_sp / pri_d_sales * 100.0), 2) if pri_d_sales > 0 else baseline_30d["real_tacos_pct"],
            "buy_box_pct": baseline_30d["buy_box_pct"], "avg_price": round(avg_p, 2),
            "small_bsr": baseline_30d["small_bsr"],
        }
        recent_7d = {
            "days": 7, "daily_sales": rec_d_sales, "daily_units": rec_d_units,
            "daily_organic_units": rec_d_org, "daily_ad_units": rec_d_ad_u,
            "organic_unit_share_pct": round((rec_d_org / rec_d_units * 100.0), 1) if rec_d_units > 0 else 0.0,
            "daily_sessions": baseline_30d["daily_sessions"], "daily_ad_spend": rec_d_sp,
            "daily_predict_gross_profit": base_d_gp, "avg_nature_cvr_pct": trf_ncvr, "avg_ad_cvr_pct": trf_acvr,
            "real_tacos_pct": round((rec_d_sp / rec_d_sales * 100.0), 2) if rec_d_sales > 0 else 0.0,
            "buy_box_pct": baseline_30d["buy_box_pct"], "avg_price": round(avg_p, 2),
            "small_bsr": baseline_30d["small_bsr"],
        }

    # Item 9e: Cross-shop shared Buy Box ASIN automatic omni_tacos linkage (e.g. cross-shop shared SBV campaign)
    is_cross_shop_asin = bool(financial_prof and financial_prof.get("is_cross_shop_asin"))
    omni_summary = (financial_prof or {}).get("omni_shop_summary") or {}
    omni_tacos_pct = num(omni_summary.get("omni_tacos")) if is_cross_shop_asin else 0.0
    single_shop_recent_tacos = recent_7d["real_tacos_pct"]
    if is_cross_shop_asin and omni_tacos_pct > 0 and omni_summary.get("omni_total_sales", 0) > (financial_prof or {}).get("total_sales", 0) * 1.20:
        effective_eval_tacos = omni_tacos_pct
        recent_7d["single_shop_tacos_pct"] = single_shop_recent_tacos
        recent_7d["omni_tacos_pct"] = omni_tacos_pct
    else:
        effective_eval_tacos = single_shop_recent_tacos

    def _pct_delta(curr_v: float, base_v: float) -> float:
        if abs(base_v) <= 1e-6:
            return 0.0
        return round((curr_v - base_v) / abs(base_v) * 100.0, 1)

    gp_delta_usd = round(recent_7d.get("daily_predict_gross_profit", 0.0) - prior_7d.get("daily_predict_gross_profit", 0.0), 2)
    gp_delta_pct = _pct_delta(recent_7d.get("daily_predict_gross_profit", 0.0), prior_7d.get("daily_predict_gross_profit", 0.0))

    deltas_7d_vs_prior7d = {
        "sales_delta_pct": _pct_delta(recent_7d["daily_sales"], prior_7d["daily_sales"]),
        "units_delta_pct": _pct_delta(recent_7d["daily_units"], prior_7d["daily_units"]),
        "organic_units_delta_pct": _pct_delta(recent_7d["daily_organic_units"], prior_7d["daily_organic_units"]),
        "sessions_delta_pct": _pct_delta(recent_7d["daily_sessions"], prior_7d["daily_sessions"]),
        "ad_spend_delta_pct": _pct_delta(recent_7d["daily_ad_spend"], prior_7d["daily_ad_spend"]),
        "predict_gross_profit_delta_usd": gp_delta_usd,
        "predict_gross_profit_delta_pct": gp_delta_pct,
        "nature_cvr_delta_pp": round(recent_7d.get("avg_nature_cvr_pct", 0.0) - prior_7d.get("avg_nature_cvr_pct", 0.0), 2),
        "ad_cvr_delta_pp": round(recent_7d.get("avg_ad_cvr_pct", 0.0) - prior_7d.get("avg_ad_cvr_pct", 0.0), 2),
        "tacos_delta_pp": round(recent_7d["real_tacos_pct"] - prior_7d["real_tacos_pct"], 2),
        "price_delta_pct": _pct_delta(recent_7d["avg_price"], prior_7d["avg_price"]),
    }

    # 2. Parse operational logs cleanly (0% false positives on small_image_url / product_info!)
    op_logs_raw = load_json(src / "operate_logs.json")
    op_rows = op_logs_raw if isinstance(op_logs_raw, list) else as_rows(op_logs_raw)
    op_events = _extract_clean_operation_events(op_rows)

    # 3. Isolate external confounders before attributing outcome to ad changes
    confounders = []
    fulfillable_cnt = num(profile.get("fulfillable")) if profile.get("fulfillable") is not None else None
    dos_cnt = num(profile.get("days_of_supply")) if profile.get("days_of_supply") is not None else None
    if (fulfillable_cnt is not None and fulfillable_cnt == 0) or (dos_cnt is not None and 0 < dos_cnt <= 7):
        confounders.append({"code": "STOCKOUT_OR_CRITICAL_INVENTORY", "detail": f"FBA库存告急/断货 (可售={fulfillable_cnt}, 可售天数={dos_cnt})"})
    if recent_7d["buy_box_pct"] > 0 and recent_7d["buy_box_pct"] < 85.0:
        confounders.append({"code": "BUY_BOX_LOSS", "detail": f"近7天购物车赢得率仅 {recent_7d['buy_box_pct']:.1f}% (<85%)"})
    if is_cross_shop_asin and omni_summary.get("omni_total_sales", 0) > (financial_prof or {}).get("total_sales", 0) * 1.25:
        confounders.append({
            "code": "CROSS_SHOP_SHARED_BUYBOX",
            "detail": f"跨店铺共卖同ASIN：全店铺总销额 ${omni_summary.get('omni_total_sales', 0):.2f} (全域omni_tacos={omni_tacos_pct:.2f}%) vs 当前店铺 ${(financial_prof or {}).get('total_sales', 0):.2f}（已自动联动全域TACOS对账防误判）",
        })
    if deal_info and deal_info.get("is_active_deal"):
        confounders.append({"code": "ACTIVE_DEAL", "detail": deal_info.get("summary_desc", "当前正处秒杀期")})
    elif deal_info and deal_info.get("is_deal_transition"):
        confounders.append({"code": "DEAL_POST_COOLDOWN", "detail": deal_info.get("summary_desc", "秒杀后回落过渡期")})
    elif prior_deal_dates_hit:
        confounders.append({
            "code": "DEAL_POST_COOLDOWN",
            "detail": f"前7天对比基期包含秒杀/促销峰值日 ({', '.join(prior_deal_dates_hit)})，近7天日均回落属促后正常均值回归（非广告断流）",
        })
    if stage_ctx and stage_ctx.get("peak_overlay") and stage_ctx.get("peak_phase") == "POST_COOLDOWN":
        confounders.append({
            "code": "CONF_PEAK_POST_COOLDOWN",
            "detail": stage_ctx.get("peak_reason", "大促后 10~14 天回落冷却期"),
        })
    xydc_price_delta = num((deal_info or {}).get("price_summary", {}).get("price_delta_7d_pct"))
    eff_price_delta = deltas_7d_vs_prior7d["price_delta_pct"] or xydc_price_delta
    if eff_price_delta >= 8.0:
        confounders.append({"code": "PRICE_HIKE_CONFOUNDER", "detail": f"近7天成交均价上调 +{eff_price_delta:.1f}%，抑制转化率与自然流速"})
    if op_events["copy_or_image_changes"] > 0:
        confounders.append({"code": "LISTING_CONTENT_EDIT", "detail": f"运营日志检测到近期 {op_events['copy_or_image_changes']} 次真实文案/图片变更"})

    hard_volume_confounders = {"STOCKOUT_OR_CRITICAL_INVENTORY", "BUY_BOX_LOSS", "CROSS_SHOP_SHARED_BUYBOX", "DEAL_POST_COOLDOWN", "CONF_PEAK_POST_COOLDOWN"}
    has_hard_confounder = any(c["code"] in hard_volume_confounders for c in confounders)

    # 4. Reconcile against North-Star Targets & Non-Self-Overwriting Prior Expectation Anchor (Item 9b + v2 §1.2)
    true_be_tacos = num((financial_prof or {}).get("true_all_in_break_even_tacos"))
    if is_cross_shop_asin and num(omni_summary.get("omni_true_all_in_break_even_tacos")) > 0:
        be_anchor = num(omni_summary.get("omni_true_all_in_break_even_tacos"))
    elif true_be_tacos > 0:
        be_anchor = true_be_tacos
    else:
        be_anchor = max(8.0, num((financial_prof or {}).get("marginal_break_even_acos") or target_acos))
    if stage_ctx and stage_ctx.get("target_tacos") is not None:
        target_tacos = round(float(stage_ctx["target_tacos"]), 1)
    else:
        target_tacos = round(max(6.0, min(be_anchor * 0.75, max(8.0, target_acos * 0.75))), 1)

    is_same_window_or_day = bool(
        isinstance(prev_exp, dict)
        and (
            (curr_window and str(prev_exp.get("window_end") or "").strip() == curr_window)
            or str(prev_exp.get("generated_at") or "")[:10] == curr_gen_date
        )
    )
    reconcile_exp = (prev_exp.get("prior_anchor") if (is_same_window_or_day and isinstance(prev_exp, dict)) else prev_exp)

    missed_prior_sales_floor = False
    prior_reconciliation = None
    if isinstance(reconcile_exp, dict) and num(reconcile_exp.get("circuit_breaker_daily_sales_floor")) > 0:
        exp_floor = num(reconcile_exp.get("circuit_breaker_daily_sales_floor"))
        exp_tacos_max = num(reconcile_exp.get("expected_tacos_max"))
        missed_prior_sales_floor = (
            recent_7d["daily_sales"] > 0
            and recent_7d["daily_sales"] < exp_floor
            and deltas_7d_vs_prior7d["sales_delta_pct"] <= -10.0
        )
        prior_reconciliation = {
            "saved_at": reconcile_exp.get("generated_at"),
            "saved_window": reconcile_exp.get("window_end"),
            "expected_daily_sales_min": num(reconcile_exp.get("expected_daily_sales_min")),
            "circuit_breaker_daily_sales_floor": exp_floor,
            "actual_recent_7d_daily_sales": recent_7d["daily_sales"],
            "expected_tacos_max": exp_tacos_max,
            "actual_recent_7d_tacos": effective_eval_tacos,
            "sales_goal_met": recent_7d["daily_sales"] >= num(reconcile_exp.get("expected_daily_sales_min")) * 0.95,
            "tacos_goal_met": (exp_tacos_max <= 0) or (effective_eval_tacos <= exp_tacos_max * 1.05),
        }

    # Item 9c & v2 §7.1: Growing portfolios NEVER trigger shrinkage lock; cutting spend with >=15% unit drop triggers guard!
    is_growing_or_steady = (deltas_7d_vs_prior7d["sales_delta_pct"] >= 0.0 and deltas_7d_vs_prior7d["units_delta_pct"] >= -5.0)
    sales_drop = (not is_growing_or_steady) and (
        deltas_7d_vs_prior7d["sales_delta_pct"] <= -20.0
        or deltas_7d_vs_prior7d["units_delta_pct"] <= -20.0
        or (deltas_7d_vs_prior7d["ad_spend_delta_pct"] <= -10.0 and deltas_7d_vs_prior7d["units_delta_pct"] <= -15.0)
    )
    org_drop = (
        not is_growing_or_steady
        and prior_7d["daily_organic_units"] >= 1.0
        and deltas_7d_vs_prior7d["organic_units_delta_pct"] <= -25.0
        and deltas_7d_vs_prior7d["sales_delta_pct"] <= -12.0
        and deltas_7d_vs_prior7d["units_delta_pct"] <= -12.0
    )

    # Detect proactive profit optimization: toxic ad spend trimmed, sales dipped mildly, yet daily gross profit clearly improved!
    is_profit_opt_on_track = (
        has_daily_series
        and -25.0 <= deltas_7d_vs_prior7d["sales_delta_pct"] < 0.0
        and (deltas_7d_vs_prior7d["ad_spend_delta_pct"] <= -8.0 or deltas_7d_vs_prior7d["tacos_delta_pp"] <= -1.5)
        and (gp_delta_usd >= 3.0 or (prior_7d["daily_predict_gross_profit"] != 0 and gp_delta_pct >= 10.0))
    )

    velocity_shrinkage_guard = False
    if is_profit_opt_on_track:
        verdict = "PROFIT_OPTIMIZATION_ON_TRACK"
        verdict_cn = "【主动去肥增肌见效·日均毛利显著改善】"
        guidance = (
            f"主动削减低效毒流后（日均广告费 {deltas_7d_vs_prior7d['ad_spend_delta_pct']:+.1f}%），近7天日均销额虽微调 ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)，"
            f"但日均预估毛利由 ${prior_7d['daily_predict_gross_profit']:.2f}/d 提升至 ${recent_7d['daily_predict_gross_profit']:.2f}/d ({gp_delta_usd:+.2f}/d, {gp_delta_pct:+.1f}%)，"
            f"自然CVR {recent_7d['avg_nature_cvr_pct']:.2f}%；利润优化正轨推进，无需触发防缩量锁死。"
        )
    elif (sales_drop or org_drop or missed_prior_sales_floor) and not has_hard_confounder and prior_7d["daily_sales"] >= 30.0:
        verdict = "VELOCITY_SHRINKAGE_ALERT"
        velocity_shrinkage_guard = True
        verdict_cn = "【大盘萎缩红色预警·启动防缩量刹车】"
        guidance = (
            f"近7天日均销额 (${recent_7d['daily_sales']:.1f}/d vs 前7天 ${prior_7d['daily_sales']:.1f}/d, {deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%) "
            f"或自然单流速 ({recent_7d['daily_organic_units']:.2f}/d, {deltas_7d_vs_prior7d['organic_units_delta_pct']:+.1f}%) 显著下滑且无断货/秒杀退潮干扰！"
            "本期自动激活【防缩量刹车锁】：严禁对任何出单词降价或大砍活动 TOS，仅清理纯0单硬出血流量，并优先回提核心高转化词展示。"
        )
    elif has_hard_confounder and (sales_drop or org_drop):
        verdict = "CONFOUNDER_DISTORTED"
        verdict_cn = "【外部非广告因子干扰·稳态防误判】"
        c_desc = "；".join(c["detail"] for c in confounders)
        guidance = f"大盘波动主要由外部因子驱动（{c_desc}），严禁将非广告销量回落误判为出价问题而激进乱调。"
    elif not is_growing_or_steady and deltas_7d_vs_prior7d["sales_delta_pct"] <= -10.0 and deltas_7d_vs_prior7d["tacos_delta_pp"] <= 0:
        verdict = "EFFICIENCY_MET_VOLUME_DRIFT"
        verdict_cn = "【投产达标但销量轻度漂移·控幅稳量】"
        guidance = (
            f"有效 TACOS 控制在 {effective_eval_tacos:.1f}%，但近7天日均销额轻度回落 ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)；"
            "本期严控降价范围，重点向高转化精准词与黄金时段倾斜预算以拉升总单量。"
        )
    elif not is_growing_or_steady and deltas_7d_vs_prior7d["sales_delta_pct"] <= -10.0 and deltas_7d_vs_prior7d["tacos_delta_pp"] > 0:
        verdict = "VOLUME_AND_EFFICIENCY_DRIFT"
        velocity_shrinkage_guard = True
        verdict_cn = "【销额回落且投产承压·启动防缩量稳盘】"
        guidance = (
            f"近7天日均销额回落 ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%) 且真实 TACOS 上浮 ({deltas_7d_vs_prior7d['tacos_delta_pp']:+.2f}pp)；"
            "已激活【防缩量刹车锁】：严禁对有转化出单词一刀切降价，仅精准否定纯0单流血词并稳住核心出单词展示位。"
        )
    elif effective_eval_tacos > max(target_tacos * 1.30, be_anchor * 0.85) and recent_7d["daily_ad_spend"] >= 10.0:
        verdict = "BLEED_UNCONTAINED"
        verdict_cn = "【TACOS超标失血·精准止血控费】"
        guidance = (
            f"近7天有效 TACOS 达 {effective_eval_tacos:.1f}%（目标 <={target_tacos:.1f}%，真实保本线 {be_anchor:.1f}%）；"
            "需果断切断零单出血词根与低效广告位溢价，将花费收敛至盈利核心词。"
        )
    elif prev_exp is None:
        verdict = "BASELINE_INITIALIZED"
        verdict_cn = "【首期北极星基线建档】"
        tacos_lbl = f"全域TACOS {effective_eval_tacos:.1f}%" if (is_cross_shop_asin and omni_tacos_pct > 0) else f"真实TACOS {effective_eval_tacos:.1f}%"
        guidance = (
            f"已建立大盘北极星基线（近7天日均 ${recent_7d['daily_sales']:.1f}/d，自然单 {recent_7d['daily_organic_units']:.2f}/d，{tacos_lbl}），"
            "下期将自动对账实际结果是否符合本期设定预期。"
        )
    else:
        verdict = "MET_EXPECTATION"
        verdict_cn = "【大盘符合预期·良性飞轮演进】"
        tacos_lbl = f"全域TACOS {effective_eval_tacos:.1f}%" if (is_cross_shop_asin and omni_tacos_pct > 0) else f"真实TACOS {effective_eval_tacos:.1f}%"
        guidance = (
            f"近7天日均销额 ${recent_7d['daily_sales']:.1f}/d ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)，"
            f"自然单占比 {recent_7d['organic_unit_share_pct']:.1f}%，{tacos_lbl} 处于健康轨道。"
        )

    # 5. Build & persist next-period quantitative expectations + v2 §1.2 Stage Target Card WITHOUT self-overwriting!
    anchor_sales = max(recent_7d["daily_sales"], prior_7d["daily_sales"] * 0.90) if velocity_shrinkage_guard else recent_7d["daily_sales"]
    cur_daily_gp = recent_7d.get("daily_predict_gross_profit", 0.0)
    target_daily_gp = round(max(cur_daily_gp * 1.05, cur_daily_gp + 3.0), 2) if cur_daily_gp >= 0 else round(cur_daily_gp * 0.70, 2)
    core_kw_targets = [
        {
            "keyword": b["keyword"],
            "role": b["role"],
            "curr_or": b.get("curr_org_rank"),
            "target_or": b.get("target_or"),
            "verify_window_days": b.get("verify_window_days", 14),
        }
        for b in (battlecard or [])
    ]
    next_expectations = {
        "generated_at": curr_gen_dt.isoformat(),
        "snap_key": snap_key,
        "window_end": curr_window,
        "stage_mode": (stage_ctx or {}).get("stage_mode", "GROWTH"),
        "stage_reason": (stage_ctx or {}).get("stage_reason", ""),
        "peak_overlay": bool((stage_ctx or {}).get("peak_overlay", False)),
        "peak_phase": (stage_ctx or {}).get("peak_phase", "NONE"),
        "target_acos": round(target_acos, 1),
        "target_tacos": round(target_tacos, 1),
        "tcpa": (stage_ctx or {}).get("tcpa", round(max(1.0, (recent_7d.get("avg_price") or 25.0) * target_acos / 100.0), 2)),
        "expected_daily_sales_min": round(anchor_sales * 0.95, 2),
        "expected_daily_sales_target": round(anchor_sales * 1.08, 2),
        "circuit_breaker_daily_sales_floor": round(anchor_sales * 0.82, 2),
        "expected_daily_contribution_profit_target": target_daily_gp,
        "expected_daily_organic_units_min": round(max(0.0, recent_7d["daily_organic_units"] * 0.95), 2),
        "expected_tacos_max": round(max(target_tacos, min(effective_eval_tacos * 0.92, be_anchor * 0.85)) if effective_eval_tacos > target_tacos else target_tacos, 1),
        "expected_ad_acos_max": round(target_acos, 1),
        "change_budget_cap_usd": (change_budget_stats or {}).get("change_budget_cap_usd"),
        "change_budget_used_usd": (change_budget_stats or {}).get("change_budget_used_usd"),
        "core_keyword_targets": core_kw_targets,
    }
    if write_expectations:
        if not is_same_window_or_day:
            if isinstance(prev_exp, dict):
                preserved = {k: v for k, v in prev_exp.items() if k != "prior_anchor"}
                next_expectations["prior_anchor"] = preserved
            exp_file.write_text(json.dumps(next_expectations, ensure_ascii=False, indent=2), encoding="utf-8")
        elif isinstance(prev_exp, dict):
            # Preserve existing expectation file untouched on same-day / unadvanced-window reruns!
            if prev_exp.get("prior_anchor"):
                next_expectations["prior_anchor"] = prev_exp["prior_anchor"]
            if not exp_file.exists():
                exp_file.write_text(json.dumps(prev_exp, ensure_ascii=False, indent=2), encoding="utf-8")

    scorecard = {
        "verdict": verdict,
        "verdict_cn": verdict_cn,
        "velocity_shrinkage_guard": velocity_shrinkage_guard,
        "guidance": guidance,
        "data_source": data_source,
        "stage_mode": (stage_ctx or {}).get("stage_mode", "GROWTH"),
        "stage_reason": (stage_ctx or {}).get("stage_reason", ""),
        "peak_overlay": bool((stage_ctx or {}).get("peak_overlay", False)),
        "peak_phase": (stage_ctx or {}).get("peak_phase", "NONE"),
        "target_acos": target_acos,
        "target_tacos": target_tacos,
        "tcpa": next_expectations["tcpa"],
        "break_even_acos": round(be_anchor, 1),
        "true_all_in_break_even_tacos": true_be_tacos,
        "is_cross_shop_asin": is_cross_shop_asin,
        "effective_eval_tacos_pct": effective_eval_tacos,
        "omni_tacos_pct": omni_tacos_pct if is_cross_shop_asin else None,
        "recent_7d": recent_7d,
        "prior_7d": prior_7d,
        "baseline_30d": baseline_30d,
        "deltas_7d_vs_prior7d": deltas_7d_vs_prior7d,
        "confounders": confounders,
        "operation_logs_summary": op_events,
        "prior_expectation_reconciliation": prior_reconciliation,
        "next_7d_expectations": next_expectations,
        "change_budget_stats": change_budget_stats,
    }

    dest.joinpath("macro_scorecard.json").write_text(
        json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sc_tsv_lines = [
        f"stage_target_card\t{scorecard['stage_mode']}" + (f"+PEAK({scorecard['peak_phase']})" if scorecard["peak_overlay"] else "") + f"\t目标ACOS={target_acos:.1f}% | 目标TACOS={target_tacos:.1f}% | TCPA=${next_expectations['tcpa']:.2f} | {scorecard['stage_reason']}",
        f"verdict\t{verdict}\t{verdict_cn}",
        f"velocity_shrinkage_guard\t{velocity_shrinkage_guard}\t{guidance}",
        f"daily_sales_7d_vs_prior7d\t${recent_7d['daily_sales']:.2f}\t${prior_7d['daily_sales']:.2f} ({deltas_7d_vs_prior7d['sales_delta_pct']:+.1f}%)",
        f"daily_units_7d_vs_prior7d\t{recent_7d['daily_units']:.2f}\t{prior_7d['daily_units']:.2f} ({deltas_7d_vs_prior7d['units_delta_pct']:+.1f}%)",
        f"daily_organic_units_7d_vs_prior7d\t{recent_7d['daily_organic_units']:.2f} ({recent_7d['organic_unit_share_pct']:.1f}%)\t{prior_7d['daily_organic_units']:.2f} ({deltas_7d_vs_prior7d['organic_units_delta_pct']:+.1f}%)",
        f"daily_predict_gross_profit_7d\t${recent_7d.get('daily_predict_gross_profit', 0.0):.2f}/d\t${prior_7d.get('daily_predict_gross_profit', 0.0):.2f}/d ({gp_delta_usd:+.2f}/d, {gp_delta_pct:+.1f}%)",
        f"cvr_7d_nature_vs_ad\tNatureCVR={recent_7d.get('avg_nature_cvr_pct', 0.0):.2f}% ({deltas_7d_vs_prior7d['nature_cvr_delta_pp']:+.2f}pp)\tAdCVR={recent_7d.get('avg_ad_cvr_pct', 0.0):.2f}% ({deltas_7d_vs_prior7d['ad_cvr_delta_pp']:+.2f}pp)",
        f"daily_sessions_7d_vs_prior7d\t{recent_7d['daily_sessions']:.1f}\t{prior_7d['daily_sessions']:.1f} ({deltas_7d_vs_prior7d['sessions_delta_pct']:+.1f}%)",
        f"real_tacos_7d_vs_prior7d\t{recent_7d['real_tacos_pct']:.2f}%\t{prior_7d['real_tacos_pct']:.2f}% ({deltas_7d_vs_prior7d['tacos_delta_pp']:+.2f}pp)",
        f"buy_box_and_price_7d\tBuyBox={recent_7d['buy_box_pct']:.1f}%\tAvgPrice=${recent_7d['avg_price']:.2f} ({deltas_7d_vs_prior7d['price_delta_pct']:+.1f}%)",
        f"small_bsr_7d\t{recent_7d['small_bsr'] or '--'}\tprior={prior_7d['small_bsr'] or '--'}",
        f"confounders\t{len(confounders)}\t{' ; '.join(c['code'] + ':' + c['detail'] for c in confounders) if confounders else 'NONE'}",
        f"next_7d_targets\tsales>=${next_expectations['expected_daily_sales_min']:.2f}/d (floor ${next_expectations['circuit_breaker_daily_sales_floor']:.2f}/d)\ttacos<={next_expectations['expected_tacos_max']:.1f}% | acos<={next_expectations['expected_ad_acos_max']:.1f}%",
    ]
    if is_cross_shop_asin and omni_tacos_pct > 0:
        sc_tsv_lines.insert(9, f"omni_tacos_reconciliation\tomni_tacos={omni_tacos_pct:.2f}%\tsingle_shop_tacos={single_shop_recent_tacos:.2f}% (跨店共享购物车统一以全域omni_tacos对账)")
    tsv(dest / "macro_scorecard.tsv", "metric\tcurrent_value\tcomparison_or_target", sc_tsv_lines)

    if export_dir:
        exp_p = Path(export_dir)
        exp_p.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        exp_p.joinpath(f"{asin_str}-{country_str}-macro-scorecard-{gen_time_str}.json").write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        exp_p.joinpath(f"{asin_str}-{country_str}-macro-scorecard.json").write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return scorecard


def load_report_frames(meta, profile, src):
    """Load every ads report, scope it to this ASIN's campaigns, filter summary rows on all tables, and attach 7d/3d deltas."""
    product_ads = [r for r in as_rows(load_json(src / "product_ads.json")) if not skip_total(r)]
    asin = str((meta.get("asin") or profile.get("asin") or "")).upper()
    asin_campaigns = set()
    camp_asins_by_name: dict[str, set[str]] = defaultdict(set)
    for row in product_ads:
        row_asin = str(row.get("asin") or row.get("advertised_asin") or "").upper()
        cn_pa = row.get("campaign_name")
        if row_asin and cn_pa:
            camp_asins_by_name[cn_pa].add(row_asin)
        if asin and row_asin == asin and cn_pa:
            asin_campaigns.add(cn_pa)

    raw_campaigns = as_rows(load_json(src / "campaign_report.json"))
    for c_raw in raw_campaigns:
        if skip_total(c_raw):
            continue
        c_asins = [str(x).upper() for x in (c_raw.get("creative_asins") or []) if x]
        c_name = c_raw.get("name") or c_raw.get("campaign_name")
        if c_name and c_asins:
            camp_asins_by_name[c_name].update(c_asins)
        if asin and asin in c_asins and c_name:
            asin_campaigns.add(c_name)
        if meta.get("portfolio_id") and c_name:
            asin_campaigns.add(c_name)

    camps = campaign_rows(raw_campaigns)
    if asin_campaigns:
        camps = [c for c in camps if c["name"] in asin_campaigns]

    shared_brand_names = {
        str(x.get("campaign_name") or "").strip()
        for x in (profile.get("shared_brand_campaigns") or [])
        if x.get("campaign_name")
    }

    camps_14d_raw = load_json(src / "campaign_report_14d.json")
    camps_14d = campaign_rows(as_rows(camps_14d_raw)) if camps_14d_raw else []
    map_14d = {c["name"]: c for c in camps_14d}

    camps_7d_raw = load_json(src / "campaign_report_7d.json")
    camps_7d = campaign_rows(as_rows(camps_7d_raw)) if camps_7d_raw else []
    map_7d = {c["name"]: c for c in camps_7d}

    camps_3d_raw = load_json(src / "campaign_report_3d.json")
    camps_3d = campaign_rows(as_rows(camps_3d_raw)) if camps_3d_raw else []
    map_3d = {c["name"]: c for c in camps_3d}

    for c in camps:
        c14 = map_14d.get(c["name"], {})
        c7 = map_7d.get(c["name"], {})
        c3 = map_3d.get(c["name"], {})
        known_asins = camp_asins_by_name.get(c["name"], set())
        if len(known_asins) > 1 or c["name"] in shared_brand_names:
            c["is_shared_multi_asin_campaign"] = True

        c["spend_14d"] = c14.get("spend", 0.0)
        c["sales_14d"] = c14.get("sales", 0.0)
        c["cpc_14d"] = c14.get("cpc", 0.0)
        c["acos_14d"] = c14.get("acos", 0.0)
        c["roas_14d"] = c14.get("roas", 0.0)
        c["cvr_14d"] = c14.get("cvr", 0.0)
        c["imp_14d"] = c14.get("imp", 0)
        c["clk_14d"] = c14.get("clk", 0)
        c["ord_14d"] = c14.get("ord", 0)

        c["spend_7d"] = c7.get("spend", 0.0)
        c["sales_7d"] = c7.get("sales", 0.0)
        c["cpc_7d"] = c7.get("cpc", 0.0)
        c["acos_7d"] = c7.get("acos", 0.0)
        c["roas_7d"] = c7.get("roas", 0.0)
        c["cvr_7d"] = c7.get("cvr", 0.0)
        c["imp_7d"] = c7.get("imp", 0)
        c["clk_7d"] = c7.get("clk", 0)
        c["ord_7d"] = c7.get("ord", 0)

        c["spend_3d"] = c3.get("spend", 0.0)
        c["sales_3d"] = c3.get("sales", 0.0)
        c["cpc_3d"] = c3.get("cpc", 0.0)
        c["acos_3d"] = c3.get("acos", 0.0)
        c["roas_3d"] = c3.get("roas", 0.0)
        c["cvr_3d"] = c3.get("cvr", 0.0)
        c["imp_3d"] = c3.get("imp", 0)
        c["clk_3d"] = c3.get("clk", 0)
        c["ord_3d"] = c3.get("ord", 0)

        daily_imp_30d = c["imp"] / 30.0 if c["imp"] > 0 else 0.0
        daily_imp_7d = c["imp_7d"] / 7.0 if c["imp_7d"] > 0 else 0.0
        daily_imp_3d = c["imp_3d"] / 3.0 if c["imp_3d"] > 0 else 0.0

        c["daily_imp_30d"] = round(daily_imp_30d, 1)
        c["daily_imp_7d"] = round(daily_imp_7d, 1)
        c["daily_imp_3d"] = round(daily_imp_3d, 1)

        if c["cpc"] > 0 and c["cpc_3d"] > 0:
            c["delta_cpc_pct"] = round((c["cpc_3d"] - c["cpc"]) / c["cpc"] * 100, 1)
        elif c["cpc"] > 0 and c["cpc_7d"] > 0:
            c["delta_cpc_pct"] = round((c["cpc_7d"] - c["cpc"]) / c["cpc"] * 100, 1)
        else:
            c["delta_cpc_pct"] = 0.0

        if daily_imp_30d >= 15:
            c["delta_imp_pct"] = round((daily_imp_3d - daily_imp_30d) / daily_imp_30d * 100, 1)
        else:
            c["delta_imp_pct"] = 0.0

    kws = [r for r in as_rows(load_json(src / "keywords.json")) if not skip_total(r)]
    if asin_campaigns:
        kws = [r for r in kws if r.get("campaign_name") in asin_campaigns]

    kws_7d_raw = load_json(src / "keywords_7d.json")
    kws_7d = [r for r in as_rows(kws_7d_raw) if not skip_total(r)] if kws_7d_raw else []
    if asin_campaigns:
        kws_7d = [r for r in kws_7d if r.get("campaign_name") in asin_campaigns]

    kws_3d_raw = load_json(src / "keywords_3d.json")
    kws_3d = [r for r in as_rows(kws_3d_raw) if not skip_total(r)] if kws_3d_raw else []
    if asin_campaigns:
        kws_3d = [r for r in kws_3d if r.get("campaign_name") in asin_campaigns]

    targets = [r for r in as_rows(load_json(src / "targets.json")) if not skip_total(r)]
    if asin_campaigns:
        targets = [r for r in targets if r.get("campaign_name") in asin_campaigns]

    targets_7d_raw = load_json(src / "targets_7d.json")
    targets_7d = [r for r in as_rows(targets_7d_raw) if not skip_total(r)] if targets_7d_raw else []
    if asin_campaigns:
        targets_7d = [r for r in targets_7d if r.get("campaign_name") in asin_campaigns]

    targets_3d_raw = load_json(src / "targets_3d.json")
    targets_3d = [r for r in as_rows(targets_3d_raw) if not skip_total(r)] if targets_3d_raw else []
    if asin_campaigns:
        targets_3d = [r for r in targets_3d if r.get("campaign_name") in asin_campaigns]

    return asin_campaigns, camps, kws, kws_3d, kws_7d, targets, targets_3d, targets_7d


def load_optional_14d_frames(src: Path, asin_campaigns: set[str] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Load optional 14d slices (keywords_14d, targets_14d, search_terms_14d); gracefully returns empty lists for legacy bundles."""
    out = []
    for fname in ("keywords_14d.json", "targets_14d.json", "search_terms_14d.json"):
        raw = load_json(src / fname)
        rows = [r for r in as_rows(raw) if not skip_total(r)] if raw else []
        if asin_campaigns and rows:
            rows = [r for r in rows if r.get("campaign_name") in asin_campaigns]
        out.append(rows)
    return out[0], out[1], out[2]


def run_state_tracking(
    args, camps, dayparting_records, financial_prof, kws, meta, profile, src, targets, traffic_prof,
    kws_3d=None, kws_7d=None, targets_3d=None, targets_7d=None, xydc_map=None, deal_info=None,
    group_bid_lookup=None, camp_group_bid_lookup=None, target_acos: float = 25.0, gen_time_str: str | None = None,
    rules: dict | None = None,
):
    """Diff control variables against the last snapshot, sync the ledger, and return state context."""
    db_root, daily_records_dir, ledger_dir, db_snapshots_dir = resolve_db_dirs(args.db_dir, src)
    snapshots_dir = Path(args.snapshots_dir) if args.snapshots_dir else db_snapshots_dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    gen_date_str, ref_now = _derive_gen_date_and_dt(gen_time_str)

    shop_alias = getattr(args, "shop", None) or meta.get("shop_alias") or profile.get("shop_name")
    snap_key = get_snapshot_key(meta, profile, camps, shop_alias=shop_alias)
    legacy_key = get_snapshot_key(meta, profile, camps, shop_alias="")

    snap_file = snapshots_dir / f"{snap_key}_snapshot.json"
    existing_snap = _find_case_insensitive_db_file(
        snapshots_dir,
        [f"{snap_key}_snapshot.json", f"{legacy_key}_snapshot.json"],
    )
    if not existing_snap and (src.parent / "snapshots").exists():
        existing_snap = _find_case_insensitive_db_file(
            src.parent / "snapshots",
            [f"{snap_key}_snapshot.json", f"{legacy_key}_snapshot.json"],
        )

    rcfg_st = v2p.resolve_rule_config(rules)
    eff_cooldown_days = int(args.cooldown_days) if getattr(args, "cooldown_days", None) is not None else int(rcfg_st.get("cooldown_days", 7))

    prev_snapshot = load_snapshot(existing_snap or snap_file)
    curr_snapshot, raw_cooldowns, locked_campaigns, diff_events, new_diff_items = diff_and_track_cooldown(
        prev_snapshot, camps, kws, targets,
        curr_kws_3d=kws_3d, curr_kws_7d=kws_7d,
        curr_targets_3d=targets_3d, curr_targets_7d=targets_7d,
        cooldown_days=eff_cooldown_days,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        ref_now=ref_now,
    )

    ledger_file = ledger_dir / f"{snap_key}_ledger.json"
    existing_ledger = _find_case_insensitive_db_file(
        ledger_dir,
        [f"{snap_key}_ledger.json", f"{legacy_key}_ledger.json"],
    )

    curr_kw_map = curr_snapshot.get("keywords")
    curr_target_map = curr_snapshot.get("targets")
    _, win_end_st = _parse_window_bounds(meta or {})
    _, explicit_pk_st = v2p.parse_mode_arg(
        getattr(args, "mode", None),
        peak_flag=bool(getattr(args, "peak", False)),
        no_peak_flag=bool(getattr(args, "no_peak", False)),
    )
    pk_ov_st, pk_ph_st, _ = v2p.detect_peak_overlay(
        win_end_st or (deal_info or {}).get("anchor_date"),
        deal_info,
        rcfg_st,
        explicit_peak=explicit_pk_st,
    )
    ledger, active_cooldowns = load_and_sync_ledger(
        existing_ledger or ledger_file, prev_snapshot, new_diff_items, camps,
        curr_kw_map=curr_kw_map, curr_target_map=curr_target_map,
        cooldown_days=eff_cooldown_days,
        xydc_map=xydc_map, deal_info=deal_info,
        profile=profile, financial_prof=financial_prof, traffic_prof=traffic_prof,
        target_acos=target_acos, meta=meta, ref_now=ref_now,
        rules=rules,
        peak_post_cooldown=bool(pk_ov_st and pk_ph_st == "POST_COOLDOWN"),
    )
    curr_snapshot["active_cooldowns"] = active_cooldowns
    snap_file.write_text(json.dumps(curr_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    # Item 2 / Iron Law 77: ONLY campaign-level cooldowns lock `locked_campaigns`!
    # Keyword / Target bid changes only lock that (campaign, match_type, entity) triple!
    locked_campaigns = {
        cd["campaign"] for cd in active_cooldowns
        if cd.get("campaign")
        and _is_campaign_level_cooldown(cd)
        and "合规意向暂停" not in str(cd.get("status", ""))
        and "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED" not in str(cd.get("status", ""))
    }

    return active_cooldowns, daily_records_dir, db_root, diff_events, gen_date_str, ledger, ledger_dir, legacy_key, locked_campaigns, snap_key


def write_campaigns_tsv(camps, dest):
    """campaigns.tsv -- 30d macro, 7d trend, 3d latest slice, deltas, dayparting tactic, OOB, and cohort bid %."""
    camp_lines = []
    for c in camps:
        timing_str = "已生效" if c.get("is_apply_time") else "未应用"
        if c.get("is_apply_time") and c.get("timing_base_value") is not None:
            timing_str += f"(基准值:${c['timing_base_value']:.2f})"
        oob_hrs = round(num(c.get("avg_over_budget_time")), 2)
        last_oob = c.get("last_over_budget_at") or "--"
        tib_pct = round(num(c.get("pct_time_in_budget", 100.0)), 1)
        sug_bgt = round(num(c.get("suggestion_budget")), 2)
        cohort_pct = int(round(num(c.get("cohort_bid_pct"))))
        camp_lines.append(
            "\t".join(str(x) for x in [
                c["state"], c["targeting"], c["sponsored"], int(c["budget"]),
                int(c["tos"]), int(c["pp"]), int(c["ros"]),
                c.get("bidding_strategy", "固定竞价 (Fixed Bids)"),
                timing_str,
                round(c["spend"], 2), round(c["sales"], 2), round(c["acos"], 1),
                round(c["cpc"], 2), round(c["cpc_7d"], 2), round(c["cpc_3d"], 2),
                round(c["delta_cpc_pct"], 1), round(c["delta_imp_pct"], 1),
                round(c["cvr"], 1), round(c["ctr"], 2),
                int(c["clk"]), int(c["ord"]), int(c["direct_ord"]), int(c["indirect_ord"]),
                round(c["spend_7d"], 2), round(c["sales_7d"], 2), round(c["acos_7d"], 1), int(c["ord_7d"]), int(c["clk_7d"]), int(c["imp_7d"]),
                round(c["spend_3d"], 2), round(c["sales_3d"], 2), round(c["acos_3d"], 1), int(c["ord_3d"]), int(c["clk_3d"]), int(c["imp_3d"]),
                round(c["tos_share"], 2), c["name"],
                round(c.get("roas", 0.0), 2), round(c.get("roas_7d", 0.0), 2), round(c.get("roas_3d", 0.0), 2),
                oob_hrs, last_oob, tib_pct, sug_bgt, cohort_pct,
                round(c.get("spend_14d", 0.0), 2), round(c.get("sales_14d", 0.0), 2),
                round(c.get("acos_14d", 0.0), 1), int(c.get("ord_14d", 0)), round(c.get("roas_14d", 0.0), 2),
            ])
        )
    tsv(
        dest / "campaigns.tsv",
        "state\ttype\tsponsored\tbudget\tTOS\tPP\tROS\tbidding_strategy\ttiming_tactic\tspend_30d\tsales_30d\tacos_30d\tcpc_30d\tcpc_7d\tcpc_3d\tdelta_cpc_pct\tdelta_imp_pct\tcvr_30d\tctr_30d\tclk_30d\tord_30d\tdir\tindir\tspend_7d\tsales_7d\tacos_7d\tord_7d\tclk_7d\timp_7d\tspend_3d\tsales_3d\tacos_3d\tord_3d\tclk_3d\timp_3d\tshare\tname\troas_30d\troas_7d\troas_3d\toob_avg_hours\tlast_oob_at\ttime_in_budget_pct\tsuggested_budget\tcohort_bid_pct\tspend_14d\tsales_14d\tacos_14d\tord_14d\troas_14d",
        camp_lines,
    )


def assess_and_write_placements(active_cooldowns, args, camps, dest, locked_campaigns, target_acos, traffic_prof=None, kws=None, xydc_map=None, deal_info=None, financial_prof=None, profile=None, stage_ctx=None, scaled_thresholds=None):
    """Grade each enabled campaign (v2 TCPA-scaled circuit breakers, 7d attribution lag protection, PEAK post-cooldown, OFF AMAZON, OOB, cohort) into placement_audit.tsv."""
    placement_lines = []
    st = scaled_thresholds or {}
    rcfg = (stage_ctx or {}).get("config") or v2p.DEFAULT_RULE_CONFIG
    cb_7d_sp = float(st.get("cb_bleed_spend_7d", 15.0))
    cb_3d_sp = float(st.get("cb_bleed_spend_3d", 15.0))
    cb_7d_over_sp = float(st.get("cb_bleed_over_acos_spend_7d", 25.0))
    act_7d_sp = float(st.get("active_bleed_spend_7d", 3.0))
    min_act_sp = float(st.get("min_action_spend", 5.0))
    tcpa_p = float(st.get("tcpa", 8.0))
    real_tos_bleed_sp = max(18.0, round(tcpa_p * 2.0, 2))
    real_pp_bleed_sp = max(15.0, round(tcpa_p * 1.5, 2))
    dead_camp_sp = max(60.0, round(tcpa_p * 5.0, 2))
    eval_target_acos = float((stage_ctx or {}).get("effective_eval_acos") or target_acos)
    is_peak_post = bool(stage_ctx and stage_ctx.get("peak_overlay") and stage_ctx.get("peak_phase") == "POST_COOLDOWN")
    kw_fl_std = float(rcfg.get("bid_floor_kw", 0.25))
    kw_fl_frg = float(rcfg.get("bid_floor_kw_fragile", 0.30))
    as_fl_std = float(rcfg.get("bid_floor_asin", 0.36))
    as_fl_frg = float(rcfg.get("bid_floor_asin_fragile", 0.42))
    refund_lv1 = float(rcfg.get("refund_lv1", 12.0))
    eff_cd_days = int(args.cooldown_days) if getattr(args, "cooldown_days", None) is not None else int(rcfg.get("cooldown_days", 7))
    for c in camps:
        if c["state"] != "enabled":
            continue
        assessment = "HEALTHY"
        recom = "Keep monitoring"

        effective_cpc = c["cpc_3d"] if c["cpc_3d"] > 0 else (c["cpc_7d"] if c["cpc_7d"] > 0 else c["cpc"])
        real_p30 = c.get("real_placements_30d") or {}
        real_tos = real_p30.get("TOS") or {}
        real_pp = real_p30.get("PP") or {}
        real_ros = real_p30.get("ROS") or {}
        real_off = real_p30.get("OFF") or {}
        off_status = c.get("off_amazon_status", "NONE")

        camp_kws = [k for k in (kws or []) if (k.get("campaign_name") == c["name"] or str(k.get("campaign_id")) == str(c.get("id")))]
        best_sp_rank = None
        best_kw = None
        if xydc_map and camp_kws:
            for kw_obj in camp_kws:
                kw_str = str(kw_obj.get("keyword_text") or "").strip().lower()
                r_info = xydc_map.get(kw_str) or xydc_map.get(re.sub(r"[^\w\s]", "", kw_str).strip())
                if r_info and r_info.get("sp_rank") is not None:
                    sp_r = int(r_info["sp_rank"])
                    if best_sp_rank is None or sp_r < best_sp_rank:
                        best_sp_rank = sp_r
                        best_kw = kw_str

        is_top_ad_rank = (best_sp_rank is not None and best_sp_rank <= 3) or (best_sp_rank is not None and best_sp_rank <= 5 and c.get("tos_share", 0) >= 50.0)
        is_deal_trans = bool(deal_info and deal_info.get("is_deal_transition")) or is_peak_post

        is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
        is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv1)
        fulfillable_cnt = num(profile.get("fulfillable")) if profile and profile.get("fulfillable") is not None else None
        dos_cnt = num(profile.get("days_of_supply")) if profile and profile.get("days_of_supply") is not None else None
        is_stockout = bool(profile and ((fulfillable_cnt is not None and fulfillable_cnt == 0) or (dos_cnt is not None and dos_cnt <= 0 and fulfillable_cnt is not None and fulfillable_cnt <= 2)))
        is_low_inventory = bool(profile and not is_stockout and ((dos_cnt is not None and 0 < dos_cnt <= 7) or (fulfillable_cnt is not None and 0 < fulfillable_cnt <= 25)))
        has_min_spend = (c["spend_3d"] >= min_act_sp or c["spend_7d"] >= max(8.0, min_act_sp) or c["spend"] >= max(8.0, min_act_sp))
        cd_camp_match = [x for x in active_cooldowns if x.get("campaign") == c["name"] and _is_campaign_level_cooldown(x)]
        is_day0_just_changed = bool(cd_camp_match and all(int(x.get("days_elapsed", 99)) == 0 for x in cd_camp_match))

        # 1. Stockout / Asphyxiation check
        if is_stockout:
            assessment = "STOCKOUT_SUSPENDED"
            recom = (
                "[Listing断货·系统停推] FBA可售库存已归零（fulfillable=0）；"
                "商品失去购物车并被亚马逊系统强制停止广告展示，严禁盲目加价回踩探底；建议准备补货到仓恢复计划（先恢复核心精准词，再恢复泛流）"
            )
        elif is_low_inventory and c["delta_imp_pct"] <= -70.0:
            assessment = "LOW_INVENTORY_THROTTLE"
            recom = (
                f"[极低库存告急·控流防断] 可售天数仅剩 {dos_cnt:.0f} 天（库存 {fulfillable_cnt:.0f} 件）；"
                "曝光下降系库存告急控流，严禁提价抢量加剧断货，可同步评估小幅提价控速"
            )
        elif c["delta_imp_pct"] <= -70.0 and c["daily_imp_30d"] >= 30:
            if is_top_ad_rank:
                assessment = "TOP_POSITION_MARKET_COOLDOWN"
                c["best_sp_rank"] = best_sp_rank
                recom = (
                    f"[首屏黄金位·大盘常态冷却] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 降至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；"
                    f"西柚洞察核验核心词广告位仍高居首屏顶部（SP #{best_sp_rank}" + (f" 词: {best_kw}" if best_kw else "") + "），"
                    "严禁盲目加价或回踩探底，保持当前稳态运行"
                )
            elif is_deal_trans:
                assessment = "DEAL_TRANSITION_COOLDOWN"
                last_d = (deal_info or {}).get("last_deal_date") or "大促窗口"
                recom = (
                    f"[大促/秒杀后常态冷却·流量异动归因消除] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 降至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；"
                    f"商品于 {last_d} 结束促销活动，回落系促后 10~14 天正常均值回归，严禁误判为断流休克"
                )
            else:
                assessment = "CIRCUIT_BREAKER_SHOCK"
                baseline_cpc = c.get("cpc", 0.50)
                cur_tos = c.get("tos", 0)
                is_target_camp = any(x in c["name"].lower() for x in ["asin", "竞品", "b0", "target", "同类", "关联"]) or c.get("targeting") == "TARGETING"
                camp_floor = as_fl_frg if (is_target_camp and (is_fragile or is_high_refund)) else (as_fl_std if is_target_camp else (kw_fl_frg if (is_fragile or is_high_refund) else kw_fl_std))
                suggested_bid = max(camp_floor, round(baseline_cpc / (1.0 + (cur_tos / 100.0)), 2))
                recom = f"[调后休克断流·立即回踩探底] 近3天日均曝光从 {c['daily_imp_30d']:.0f} 暴跌至 {c['daily_imp_3d']:.0f} ({c['delta_imp_pct']:+.1f}%)；建议回提基础竞价至 ${suggested_bid:.2f} (底价保护 ${camp_floor:.2f}) 探底拉回展示池"

        # 2. Dominant OFF AMAZON Bleed
        elif off_status == "OFF_AMAZON_BLEED" and real_off and real_off.get("spend_share", 0) >= 50.0:
            assessment = "OFF_AMAZON_BLEED"
            off_ac_s = fmt_acos_text(real_off.get("acos", 999.0), real_off.get("spend", 0), real_off.get("orders", 0))
            recom = (
                f"[真实广告位核验·OFF AMAZON站外严重失血] 站外展位花费 ${real_off['spend']:.2f}（占本活动总花费 {real_off['spend_share']:.1f}%），"
                f"出单 {real_off['orders']} 单，ACOS {off_ac_s} > 阈值 {eval_target_acos * 1.25:.1f}%；"
                "建议立即将站外设置调为最低 (Minimize Off-Amazon Spend / Down Only) 并阶梯下调基础竞价阻断站外泄洪"
            )

        # 2b. Ongoing Bleed — v2 §3.2 & §3.3 TCPA-scaled 7d primary + extreme 3d only!
        elif (
            (c["spend_7d"] >= cb_7d_sp and c["ord_7d"] == 0)
            or (c["spend_3d"] >= cb_3d_sp and c["ord_3d"] == 0 and c["ord_7d"] == 0)
            or (num(c.get("pct_time_in_budget", 100.0)) <= 50.0 and c["ord_7d"] == 0 and c["spend_7d"] >= act_7d_sp)
        ):
            assessment = "CIRCUIT_BREAKER_BLEED"
            recom = (
                f"[调后持续失血·TCPA熔断纠偏] 近7天花费 ${c['spend_7d']:.1f} 0单 (熔断线 ${cb_7d_sp:.1f}) / "
                f"近3天花费 ${c['spend_3d']:.1f} 0单 (极端线 ${cb_3d_sp:.1f})；打破观察期，按步长削减高耗溢价或重度档降基础竞价 -20%"
            )
        elif not is_day0_just_changed and c["ord_7d"] > 0 and c["spend_7d"] >= cb_7d_over_sp and c["acos_7d"] >= (eval_target_acos * 2.0):
            assessment = "CIRCUIT_BREAKER_BLEED"
            recom = (
                f"[调后持续失血·7d ACOS爆表] 近7天花费 ${c['spend_7d']:.1f} (>=${cb_7d_over_sp:.1f}) 且 ACOS 达 "
                f"{fmt_acos_text(c['acos_7d'], c['spend_7d'], c['ord_7d'])} (>= 2x目标 {eval_target_acos:.0f}%)；打破观察期紧急防守"
            )

        # 3. Observation Cooldown (campaign-level only!)
        elif c["name"] in locked_campaigns:
            cd_match = cd_camp_match
            detail = cd_match[0].get("detail", "Recent parameter adjustment") if cd_match else "Recent parameter adjustment"
            elapsed = cd_match[0].get("days_elapsed", 0) if cd_match else 0
            if c["delta_cpc_pct"] <= -15.0 and c["delta_imp_pct"] > -60.0:
                assessment = "ADJUSTMENT_EFFECTIVE"
                recom = f"[调后生效·见效保持] 近3天 CPC 压至 ${c['cpc_3d']:.2f} ({c['delta_cpc_pct']:+.1f}%)，曝光稳定；严禁二次砍价"
            else:
                assessment = "IN_OBSERVATION"
                recom = f"[动态观察期 Day {elapsed}/{eff_cd_days}·正常公差] {detail}；近3天 CPC ${effective_cpc:.2f}，保持监控"

        # 4. Real Placement (incl. OFF AMAZON!) & Bidding Strategy Diagnostics
        elif off_status == "OFF_AMAZON_BLEED":
            assessment = "OFF_AMAZON_BLEED"
            off_ac_s = fmt_acos_text(real_off.get("acos", 999.0), real_off.get("spend", 0), real_off.get("orders", 0))
            recom = (
                f"[真实广告位核验·OFF AMAZON站外严重失血] 站外展位花费 ${real_off['spend']:.2f}（占本活动总花费 {real_off['spend_share']:.1f}%），"
                f"出单 {real_off['orders']} 单，ACOS {off_ac_s} > 阈值 {eval_target_acos * 1.25:.1f}%；"
                "建议立即将站外设置调为最低 (Minimize Off-Amazon Spend / Down Only) 并阶梯下调基础竞价阻断站外泄洪"
            )
        elif c.get("is_up_and_down") and c["tos"] >= 60 and has_min_spend and (c["acos"] >= eval_target_acos * 1.25 or effective_cpc >= 1.15):
            assessment = "UP_AND_DOWN_2X_AMPLIFICATION"
            recom = (
                f"[动态提高和降低·2倍叠乘预警] 当前竞价策略为“提高和降低”且 TOS={int(c['tos'])}%，"
                f"首屏实际最高出价被放大至基础竞价的 {2.0 * (1.0 + c['tos'] / 100.0):.2f} 倍（近期CPC ${effective_cpc:.2f}, ACOS {fmt_acos_text(c['acos'], c['spend'], c['ord'])}）；"
                "建议切换为“动态竞价 - 仅降低 (Down Only)”或将 TOS 压至 <=30% 消除天价点击"
            )
        elif real_tos and real_tos.get("spend", 0) >= real_tos_bleed_sp and (real_tos.get("orders", 0) == 0 or real_tos.get("acos", 0) >= eval_target_acos * 1.65) and c["tos"] >= 40:
            assessment = "REAL_TOS_BLEED"
            recom = (
                f"[真实广告位核验·TOS顶展失血] 真实位置报告显示 TOS 花费 ${real_tos['spend']:.2f} (占比 {real_tos.get('spend_share', 0):.0f}%)，"
                f"出单 {real_tos.get('orders', 0)} 单，ACOS {fmt_acos_text(real_tos.get('acos', 0), real_tos['spend'], real_tos.get('orders', 0))} 严重超标；建议精准下调 TOS 溢价控费"
            )
        elif real_pp and real_pp.get("spend", 0) >= real_pp_bleed_sp and (real_pp.get("orders", 0) == 0 or real_pp.get("acos", 0) >= eval_target_acos * 1.60) and c["pp"] >= 20:
            assessment = "INEFFICIENT_PP"
            recom = (
                f"[真实广告位核验·商品页PP失血] 真实位置报告显示 PP 花费 ${real_pp['spend']:.2f}，"
                f"出单 {real_pp.get('orders', 0)} 单，ACOS {fmt_acos_text(real_pp.get('acos', 0), real_pp['spend'], real_pp.get('orders', 0))}；建议将 PP 溢价下调至 0%~10%"
            )
        elif c["spend"] >= dead_camp_sp and c["ord"] == 0 and c["tos"] >= 50 and not c.get("is_shared_multi_asin_campaign"):
            assessment = "DEAD_CAMPAIGN_CANDIDATE"
            recom = f"30天花费 ${c['spend']:.1f} (>=阈值 ${dead_camp_sp:.0f}) 仍为 0 单，近7天 CPC ${effective_cpc:.2f}；建议执行归档或重建精准仓"
        elif c["tos"] >= 150 and has_min_spend and (not real_tos or real_tos.get("acos", 999.0) > eval_target_acos):
            assessment = "HIGH_TOS_RISK"
            recom = f"TOS {int(c['tos'])}% 虚高，近期 CPC ${effective_cpc:.2f}；建议阶梯下调 TOS 至 50-80%"
        elif c["pp"] >= 100 and (
            (c["spend_7d"] >= min_act_sp and c["acos_7d"] >= eval_target_acos * 1.4)
            or (c["spend_7d"] < min_act_sp and c["spend"] >= min_act_sp and c["acos"] >= eval_target_acos * 1.4)
        ):
            eval_sp = c["spend_7d"] if c["spend_7d"] >= min_act_sp else c["spend"]
            eval_od = c["ord_7d"] if c["spend_7d"] >= min_act_sp else c["ord"]
            current_eval_acos = c["acos_7d"] if c["spend_7d"] >= min_act_sp else c["acos"]
            assessment = "INEFFICIENT_PP"
            recom = f"商品页溢价 {int(c['pp'])}% 过高，近期 ACOS {fmt_acos_text(current_eval_acos, eval_sp, eval_od)} 严重超标；下调 PP 至 <=20%"
        elif effective_cpc >= 1.20 and has_min_spend and c["acos"] >= eval_target_acos:
            assessment = "HIGH_CPC_THROTTLE"
            recom = f"近期实际 CPC ${effective_cpc:.2f} 偏高 (30天均值 ${c['cpc']:.2f})；按近期成本下调基础竞价 15-20% 控费"
        elif c["spend_7d"] >= cb_7d_sp and c["ord_7d"] == 0:
            assessment = "ZERO_CONVERSION_BLEED"
            recom = f"近7天花费 ${c['spend_7d']:.1f} (>= TCPA缩放阈值 ${cb_7d_sp:.1f}) 为 0 单；排查最新搜索词相关性，下调基础竞价或暂停低效投放"
        elif c["tos"] <= 30 and c["tos_share"] < 15 and ((real_tos and real_tos.get("orders", 0) >= 3 and real_tos.get("acos", 999) <= eval_target_acos * 0.9) or (c["cvr_7d"] >= 15 if c["cvr_7d"] > 0 else c["cvr"] >= 15)):
            assessment = "TOS_UNDERBID"
            recom = f"顶展转化优异但顶部份额仅 {c['tos_share']:.0f}%；建议提高 TOS 至 50-70% 放大首屏单量"

        # v2 §7.5: Dayparting benign exemption uses 7d conversion (ord_7d > 0 & acos_7d <= target * 1.35 & delta_cpc <= +25%)
        if bool(c.get("is_apply_time")):
            if (
                assessment in ["CIRCUIT_BREAKER_BLEED", "HIGH_CPC_THROTTLE"]
                and c.get("ord_7d", 0) > 0
                and c.get("acos_7d", 999.0) <= eval_target_acos * 1.35
                and c.get("delta_cpc_pct", 0) <= 25.0
            ):
                assessment = "DAYPARTING_ACTIVE_STABLE"
                timing_val_str = f"${c['timing_base_value']:.2f}" if c.get("timing_base_value") is not None else f"${c['cpc']:.2f}"
                recom = (
                    f"[领星分时调价生效·良性溢价稳态] 已开启分时调价（基准值: {timing_val_str}），"
                    f"近7天有单 ({int(c['ord_7d'])}单) 且 7d ACOS {c['acos_7d']:.1f}% 受控，保持分时调度稳态观察"
                )
            elif assessment == "CIRCUIT_BREAKER_SHOCK":
                recom += "（注意：该活动已启用领星分时策略，请同步核查分时基准值与低谷时段折扣梯度）"

        # OFF AMAZON protection badge when healthy
        if off_status == "OFF_AMAZON_PROTECTED" and real_off:
            if assessment == "HEALTHY":
                assessment = "OFF_AMAZON_PROTECTED"
            recom += f" [OFF AMAZON站外达标保护: 花费${real_off['spend']:.2f}({real_off['spend_share']:.1f}%)/{real_off['orders']}单/ACOS {real_off['acos']:.1f}% <= {eval_target_acos * 1.15:.1f}%，显式保留不误杀]"

        # Intraday Out-Of-Budget (OOB) & Shopper Cohort Bidding badges (Item 7c)
        tib_pct_c = num(c.get("pct_time_in_budget", 100.0))
        oob_hrs_c = num(c.get("avg_over_budget_time", 0.0))
        sug_bgt_c = num(c.get("suggestion_budget", 0.0))
        if tib_pct_c < 90.0 or oob_hrs_c >= 1.0:
            if c.get("ord", 0) >= 2 and 0.0 < c.get("acos", 999.0) <= eval_target_acos * 1.15:
                recom += f" [日内超预算断流预警(OOB): 在预算内时长 {tib_pct_c:.0f}%/日均断流 {oob_hrs_c:.1f}h，高投产受抑建议扩预算(参考${sug_bgt_c:.0f})]"
            else:
                recom += f" [日内超预算提示(OOB): 在预算内时长 {tib_pct_c:.0f}%/日均超预算 {oob_hrs_c:.1f}h，先降CPC止血切勿盲目加预算]"
        if num(c.get("cohort_bid_pct")) > 0:
            recom += f" [人群溢价激活: ShopperCohort +{int(num(c['cohort_bid_pct']))}%]"
        if c.get("is_shared_multi_asin_campaign"):
            recom += " [跨品共享活动保护：多ASIN共用，禁止单品视角误关停]"

        # Append real placement (TOS/PP/ROS/OFF) & indirect halo summary note
        if real_p30:
            p_notes = []
            for sl_k, sl_lbl in (("TOS", "TOS"), ("PP", "PP"), ("ROS", "ROS"), ("OFF", "OFF AMAZON")):
                if sl_k in real_p30 and real_p30[sl_k]["spend"] > 0:
                    rp_i = real_p30[sl_k]
                    ac_lbl = fmt_acos_text(rp_i["acos"], rp_i["spend"], rp_i["orders"])
                    p_notes.append(f"{sl_lbl}:${rp_i['spend']:.0f}({rp_i['spend_share']:.0f}%)/{rp_i['orders']}单/ACOS {ac_lbl}")
            if p_notes:
                recom += f" [真实展位30d: {' | '.join(p_notes)}]"
        if c.get("indirect_order_share", 0) >= 35.0 and c.get("ord", 0) >= 3:
            recom += f" [光环关联单占比 {c['indirect_order_share']:.0f}%：带动变体出单显著]"

        c["assessment"] = assessment
        c["recom"] = recom

        timing_str = "已生效" if c.get("is_apply_time") else "未应用"
        if c.get("is_apply_time") and c.get("timing_base_value") is not None:
            timing_str += f"(基准值:${c['timing_base_value']:.2f})"

        placement_lines.append("\t".join(str(x) for x in [
            c["name"], c["targeting"], timing_str, round(c["spend"], 2), round(c["cpc"], 2), round(c["acos"], 1),
            round(c["cpc_7d"], 2), round(c["acos_7d"], 1), round(c["cpc_3d"], 2),
            round(c["delta_cpc_pct"], 1), round(c["delta_imp_pct"], 1),
            int(c["tos"]), int(c["pp"]), int(c["ros"]),
            round(c["tos_share"], 2), assessment, recom
        ]))
    tsv(
        dest / "placement_audit.tsv",
        "campaign\ttargeting\ttiming_tactic\tspend_30d\tcpc_30d\tacos_30d\tcpc_7d\tacos_7d\tcpc_3d\tdelta_cpc_pct\tdelta_imp_pct\tTOS\tPP\tROS\ttos_share\tassessment\trecommendation",
        placement_lines,
    )


def write_keywords_tsv(
    dest, kws, sr_map, xydc_map=None, kws_7d=None, kws_3d=None,
    target_acos: float = 25.0, group_bid_lookup=None, camp_group_bid_lookup=None,
    scaled_thresholds=None, stage_ctx=None,
):
    """keywords.tsv -- targeted keywords with ABA searchrank, volume tier, rank synergy, 7d/3d slices, direct/indirect ACOS & Amazon suggested bids."""
    kw_lines = []
    xydc_map = xydc_map or {}
    stage_mode = (stage_ctx or {}).get("stage_mode", "GROWTH")
    kw_7d_lookup = {(k.get("campaign_name"), (k.get("match_type") or "").lower(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_7d or [])}
    kw_3d_lookup = {(k.get("campaign_name"), (k.get("match_type") or "").lower(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_3d or [])}

    for r in sorted(kws, key=lambda x: -num(x.get("spends"))):
        spend = num(r.get("spends"))
        if spend < 1 and num(r.get("orders")) < 1:
            continue
        kw_text = r.get("keyword_text") or r.get("targeting_text") or ""
        sr, v_tier = get_searchrank(kw_text, sr_map)
        sr_display = str(sr) if sr is not None else "--"

        c_name_k = r.get("campaign_name")
        m_type_k = (r.get("match_type") or "").lower()
        txt_k = (kw_text or "").strip().lower()
        k7 = kw_7d_lookup.get((c_name_k, m_type_k, txt_k), {})
        k3 = kw_3d_lookup.get((c_name_k, m_type_k, txt_k), {})

        eff_bid, is_inh, _ = resolve_multi_window_entity_bid(r, k7, k3, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        def_bid_disp = r.get("default_bid") if num(r.get("default_bid")) > 0 else (eff_bid if is_inh else "--")

        eval_res = evaluate_rank_synergy(
            term=kw_text,
            xydc_map=xydc_map,
            spend=spend,
            sales=num(r.get("sales")),
            orders=int(num(r.get("orders"))),
            clicks=int(num(r.get("clicks"))),
            target_acos=target_acos or 25.0,
            current_bid=eff_bid,
            scaled_thresholds=scaled_thresholds,
            stage_mode=stage_mode,
        )
        org_rank_str = str(eval_res["organic_rank"]) if eval_res["organic_rank"] is not None else "--"
        sp_rank_str = str(eval_res["sp_rank"]) if eval_res["sp_rank"] is not None else "--"

        is_timing = bool(r.get("is_apply_time")) and r.get("is_apply_time") not in (0, "0", "false", "False")
        t_base = f"{num(r.get('timing_base_value')):.2f}" if r.get("timing_base_value") is not None and num(r.get("timing_base_value")) > 0 else "--"
        app_t = r.get("applied_templates") or []
        t_name = app_t[0].get("name", "") if app_t else "--"
        timing_status = "分时生效" if is_timing else "未应用"

        sp_7d = round(num(k7.get("spends") or k7.get("spend") or k7.get("cost")), 2) if k7 else 0.0
        ord_7d = int(num(k7.get("orders"))) if k7 else 0
        sp_3d = round(num(k3.get("spends") or k3.get("spend") or k3.get("cost")), 2) if k3 else 0.0
        ord_3d = int(num(k3.get("orders"))) if k3 else 0
        sales_30d = num(r.get("sales"))
        acos_clean = clean_api_acos(r.get("acos"), spend, sales_30d)
        roas_30d = calc_roas(spend, sales_30d, r.get("roas"))
        dir_ord_k = int(num(r.get("direct_orders")))
        indir_ord_k = int(num(r.get("indirect_orders")))
        tos_is_k = round(num(r.get("top_of_search_impression_share")), 2)

        # Item 7c: Activate direct_acos, indirect_acos, and Amazon suggested bid (recommends)
        dir_sales_k = num(r.get("direct_sales"))
        indir_sales_k = num(r.get("indirect_sales"))
        dir_acos_k = round(clean_api_acos(r.get("direct_acos"), spend, dir_sales_k if (dir_ord_k > 0 or dir_sales_k > 0) else 0.0), 1)
        indir_acos_k = round(clean_api_acos(r.get("indirect_acos"), spend, indir_sales_k if (indir_ord_k > 0 or indir_sales_k > 0) else 0.0), 1)
        rec_obj = r.get("recommends") if isinstance(r.get("recommends"), dict) else {}
        amz_sug = f"{num(rec_obj.get('suggested')):.2f}" if num(rec_obj.get("suggested")) > 0 else "--"
        amz_rng = (
            f"{num(rec_obj.get('rangeStart')):.2f}-{num(rec_obj.get('rangeEnd')):.2f}"
            if num(rec_obj.get("rangeEnd")) > 0
            else "--"
        )

        kw_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("match_type"), kw_text,
            sr_display, v_tier,
            eff_bid, def_bid_disp, r.get("state"),
            timing_status, t_base, t_name,
            round(spend, 2), round(sales_30d, 2), round(acos_clean, 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))),
            round(clean_api_rate(r.get("cvr")), 1), round(clean_api_rate(r.get("cpc")), 2), round(clean_api_rate(r.get("ctr")), 2),
            sp_7d, ord_7d, sp_3d, ord_3d,
            org_rank_str, sp_rank_str, eval_res["synergy_tier"], eval_res["synergy_tag"],
            dir_ord_k, indir_ord_k, tos_is_k, roas_30d,
            dir_acos_k, indir_acos_k, amz_sug, amz_rng,
        ]))
    tsv(
        dest / "keywords.tsv",
        "campaign\tmatch\ttext\tsearchrank\tvolume_tier\tbid\tdefbid\tstate\ttiming_status\ttiming_base_value\ttiming_template\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tctr\tspend_7d\tord_7d\tspend_3d\tord_3d\torganic_rank\tsp_rank\tsynergy_tier\tsynergy_tag\tdirect_ord\tindirect_ord\ttos_share\troas\tdirect_acos\tindirect_acos\tamz_suggested_bid\tamz_bid_range",
        kw_lines,
    )


def _compute_protected_ngram_tokens(profile: dict, rules: dict) -> set[str]:
    """Tokens from product title, brand, positive IS patterns, and morphological stems that must never be treated as bleeding roots."""
    blobs = [str(profile.get("title") or "").lower(), str(profile.get("brand") or "").lower()]
    for item in (rules or {}).get("is_patterns", []):
        pat = item if isinstance(item, str) else item.get("pattern", "")
        if pat:
            blobs.append(str(pat).lower())
    tokens = set()
    for b in blobs:
        for w in re.findall(r"[a-z0-9]+", b):
            if len(w) > 2 and not w.isdigit() and w not in STOP_WORDS:
                tokens.add(w)
                if w.endswith("er") and len(w) > 4:
                    tokens.add(w[:-2])
                elif w.endswith("ers") and len(w) > 5:
                    tokens.add(w[:-3])
                elif w.endswith("s") and len(w) > 3:
                    tokens.add(w[:-1])
    # Core appliance & functional nouns that must NEVER be killed as 1-gram roots
    core_functional_nouns = {
        "grind", "grinder", "grinders", "mill", "mills", "machine", "machines",
        "coffee", "bean", "beans", "electric", "blade", "blades", "spice", "spices",
        "herb", "herbs", "seed", "seeds", "bowl", "bowls", "motor", "capacity",
        "stainless", "steel", "dry", "wet", "nut", "nuts", "grain", "grains"
    }
    tokens |= core_functional_nouns
    return tokens


def analyze_search_terms(
    asin_campaigns,
    camps,
    known_negatives,
    kws,
    kws_3d,
    kws_7d,
    profile,
    rules,
    sr_map,
    src,
    target_acos,
    xydc_map=None,
    financial_prof=None,
    traffic_prof=None,
    targets=None,
    targets_7d=None,
    targets_3d=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    macro_scorecard=None,
    entity_memory=None,
    stage_ctx=None,
    scaled_thresholds=None,
):
    """The core pass: aggregate queries by (campaign, query), pre-compute bleeding N-gram veto,
    classify queries & ASINs cleanly, enforce 2-stage migration soft handoff, and build raw adjustments.
    """
    sts = [r for r in as_rows(load_json(src / "search_terms.json")) if not skip_total(r) and (r.get("query") or r.get("campaign_name"))]
    if asin_campaigns:
        sts = [r for r in sts if r.get("campaign_name") in asin_campaigns]

    sts_7d_raw = load_json(src / "search_terms_7d.json")
    sts_7d = [r for r in as_rows(sts_7d_raw) if not skip_total(r) and (r.get("query") or r.get("campaign_name"))] if sts_7d_raw else []
    if asin_campaigns and sts_7d:
        sts_7d = [r for r in sts_7d if r.get("campaign_name") in asin_campaigns]

    st_7d_map = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "impressions": 0})
    for r7 in sts_7d:
        q7 = (r7.get("query") or "").strip().lower()
        c7 = r7.get("campaign_name") or ""
        if q7 and c7:
            slot7 = st_7d_map[(c7, q7)]
            slot7["spend"] += num(r7.get("spends") or r7.get("spend") or r7.get("cost"))
            slot7["sales"] += num(r7.get("sales"))
            slot7["orders"] += int(num(r7.get("orders")))
            slot7["clicks"] += int(num(r7.get("clicks")))
            slot7["impressions"] += int(num(r7.get("impressions")))

    if group_bid_lookup is None or camp_group_bid_lookup is None:
        group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(src, asin_campaigns)

    active_neg_dict, active_neg_camp_pairs, all_neg_queries = extract_active_negatives(
        sts, src=src, camps=camps, asin_campaigns=asin_campaigns, known_negatives_out=known_negatives,
    )
    audit_records, unnegative_adjustments = audit_active_negatives(
        active_neg_dict, profile, rules, sr_map, kws=kws, camps=camps, xydc_map=xydc_map,
        group_bid_lookup=group_bid_lookup, camp_group_bid_lookup=camp_group_bid_lookup,
    )
    kw_misfit_adjustments = audit_keyword_misfits(
        kws, rules, camps=camps, group_bid_lookup=group_bid_lookup, camp_group_bid_lookup=camp_group_bid_lookup,
        profile=profile,
    )

    bucket_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0})
    ngram_stats = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0.0, "clk": 0.0, "queries": set(), "gram_type": "1-gram"})
    cannibal_map = defaultdict(list)

    st_lines = []
    waste = []
    harvest = []
    bulk_adjustments = []

    shrinkage_guard = bool(macro_scorecard and macro_scorecard.get("velocity_shrinkage_guard"))
    zero_ad_cb = bool(
        (financial_prof or {}).get("zero_ad_loss_circuit_breaker")
        or (profile or {}).get("zero_ad_loss_circuit_breaker")
        or (stage_ctx or {}).get("zero_ad_loss_circuit_breaker")
    )
    stage_perms = (stage_ctx or {}).get("permissions") or {}
    freeze_expansion = bool(zero_ad_cb or stage_perms.get("freeze_expansion"))
    block_non_p0_cuts = bool(stage_perms.get("block_non_p0_neg_and_cuts"))
    eval_target_acos = float((stage_ctx or {}).get("effective_eval_acos") or target_acos)
    rcfg = (stage_ctx or {}).get("config") or v2p.DEFAULT_RULE_CONFIG
    st_cfg = scaled_thresholds or v2p.compute_scaled_thresholds(
        tcpa=max(1.0, num((financial_prof or {}).get("avg_price") or profile.get("price") or 25.0) * max(target_acos, 8.0) / 100.0),
        cvr_base_ratio=v2p.compute_portfolio_base_cvr(camps, traffic_prof),
        cfg=rcfg,
    )
    act_bleed_7d = float(st_cfg["active_bleed_spend_7d"])
    act_sp_3d = float(st_cfg["activity_spend_3d"])
    cb_bleed_3d = float(st_cfg["cb_bleed_spend_3d"])
    cb_bleed_7d = float(st_cfg["cb_bleed_spend_7d"])
    severe_bleed_sp = float(st_cfg["severe_bleed_spend"])
    min_over_sp_30d = float(st_cfg["min_over_acos_spend_30d"])
    min_act_sp = float(st_cfg["min_action_spend"])
    cvr_base_ratio = float(st_cfg["cvr_base_ratio"])
    cvr_prior_k = float(st_cfg["cvr_prior_k"])
    kw_fl_std = float(rcfg.get("bid_floor_kw", 0.25))
    kw_fl_frg = float(rcfg.get("bid_floor_kw_fragile", 0.30))
    refund_lv1 = float(rcfg.get("refund_lv1", 12.0))

    # Build 7d and 3d keyword maps for fresh slice analysis
    kw_7d_map = {}
    for k in kws_7d:
        cn = k.get("campaign_name") or ""
        mt = (k.get("match_type") or "").lower().strip()
        txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if txt:
            kw_7d_map[(cn, mt, txt)] = k

    kw_3d_map = {}
    for k in kws_3d:
        cn = k.get("campaign_name") or ""
        mt = (k.get("match_type") or "").lower().strip()
        txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if txt:
            kw_3d_map[(cn, mt, txt)] = k

    camp_lookup = {c["name"]: c for c in camps if c.get("name")}

    # Build lookups for placement fingerprinting, fresh slice verification, and exact-keyword presence
    kw_lookup = {}
    enabled_exact_by_text = defaultdict(list)
    paused_exact_by_camp_text = {}
    for k in kws:
        c_name_k = k.get("campaign_name") or ""
        text_k = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        match_k = (k.get("match_type") or "").lower().strip()
        if not text_k:
            continue
        key_k = f"{c_name_k}::{match_k}::{text_k}"
        k7 = kw_7d_map.get((c_name_k, match_k, text_k), {})
        k3 = kw_3d_map.get((c_name_k, match_k, text_k), {})

        # Strictly anchor to timing_base_value when is_apply_time is True (never overwritten by peak-hour k3/k7 bid!)
        current_bid, is_k_inh, instant_k_bid = resolve_multi_window_entity_bid(
            k, k7, k3, group_bid_lookup, camp_group_bid_lookup, fallback=0.30
        )
        current_state = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()

        imp_30d = int(num(k.get("impressions")))
        clk_30d = int(num(k.get("clicks")))
        spend_30d = num(k.get("cost") or k.get("spend") or k.get("spends"))
        sales_30d = num(k.get("sales"))
        ord_30d = int(num(k.get("orders")))
        cvr_30d = round((ord_30d / clk_30d * 100.0) if clk_30d > 0 else num(k.get("cvr")), 2)
        acos_30d = round((spend_30d / sales_30d * 100.0) if sales_30d > 0 else (999.0 if spend_30d > 0 else 0.0), 1)
        cpc_30d = round((spend_30d / clk_30d) if clk_30d > 0 else num(k.get("cpc")), 2)

        imp_7d = int(num(k7.get("impressions"))) if k7 else 0
        clk_7d = int(num(k7.get("clicks"))) if k7 else 0
        spend_7d = num(k7.get("cost") or k7.get("spend") or k7.get("spends")) if k7 else 0.0
        sales_7d = num(k7.get("sales")) if k7 else 0.0
        ord_7d = int(num(k7.get("orders"))) if k7 else 0
        cvr_7d = round((ord_7d / clk_7d * 100.0) if clk_7d > 0 else 0.0, 2)
        acos_7d = round((spend_7d / sales_7d * 100.0) if sales_7d > 0 else (999.0 if spend_7d > 0 else 0.0), 1)
        cpc_7d = round((spend_7d / clk_7d) if clk_7d > 0 else 0.0, 2)

        imp_3d = int(num(k3.get("impressions"))) if k3 else 0
        clk_3d = int(num(k3.get("clicks"))) if k3 else 0
        spend_3d = num(k3.get("cost") or k3.get("spend") or k3.get("spends")) if k3 else 0.0
        sales_3d = num(k3.get("sales")) if k3 else 0.0
        ord_3d = int(num(k3.get("orders"))) if k3 else 0
        cvr_3d = round((ord_3d / clk_3d * 100.0) if clk_3d > 0 else 0.0, 2)
        acos_3d = round((spend_3d / sales_3d * 100.0) if sales_3d > 0 else (999.0 if spend_3d > 0 else 0.0), 1)
        cpc_3d = round((spend_3d / clk_3d) if clk_3d > 0 else 0.0, 2)

        has_zero_recent_traffic = (
            (imp_3d <= 25 and spend_3d == 0.0)
            or (imp_7d == 0 and spend_7d == 0.0)
            or (imp_3d == 0 and spend_3d == 0.0)
        )

        # §3.2 & §3.3 Attribution-lag-safe active bleeding: require 7d 0-order spend >= max($3, 0.5*TCPA) + recent 3d activity
        # OR extreme 3d circuit breaker spend >= max($15, 3*TCPA) & 0 orders. Never kill solely on low 3d spend!
        has_active_bleeding = (
            (spend_7d >= act_bleed_7d and ord_7d == 0 and (spend_3d >= act_sp_3d or imp_3d > 25))
            or (spend_3d >= cb_bleed_3d and ord_3d == 0 and ord_7d == 0)
        )

        is_timing = bool(k.get("is_apply_time"))
        t_base = num(k.get("timing_base_value")) if k.get("timing_base_value") is not None else None
        app_t = k.get("applied_templates") or []
        t_name = app_t[0].get("name", "") if app_t else ""
        t_eff = app_t[0].get("effective_time", {}).get("from", "") if app_t else ""

        entry_k = {
            "campaign": c_name_k,
            "match": match_k,
            "text": text_k,
            "bid": current_bid,
            "instant_bid": instant_k_bid,
            "is_inherited_bid": is_k_inh,
            "cpc": num(k.get("cpc")),
            "state": current_state,
            "imp_30d": imp_30d, "clk_30d": clk_30d, "spend_30d": spend_30d, "sales_30d": sales_30d, "ord_30d": ord_30d, "cvr_30d": cvr_30d, "acos_30d": acos_30d, "cpc_30d": cpc_30d,
            "imp_7d": imp_7d, "clk_7d": clk_7d, "spend_7d": spend_7d, "sales_7d": sales_7d, "ord_7d": ord_7d, "cvr_7d": cvr_7d, "acos_7d": acos_7d, "cpc_7d": cpc_7d,
            "imp_3d": imp_3d, "clk_3d": clk_3d, "spend_3d": spend_3d, "sales_3d": sales_3d, "ord_3d": ord_3d, "cvr_3d": cvr_3d, "acos_3d": acos_3d, "cpc_3d": cpc_3d,
            "orders": ord_30d, "cvr": cvr_30d, "acos": acos_30d, "spends": spend_30d, "sales": sales_30d, "clicks": clk_30d,
            "has_zero_recent_traffic": has_zero_recent_traffic,
            "has_active_bleeding": has_active_bleeding,
            "is_apply_time": is_timing,
            "timing_base_value": t_base,
            "timing_template": t_name,
            "timing_effective_from": t_eff,
        }
        kw_lookup[key_k] = entry_k
        if match_k == "exact":
            camp_state_k = (camp_lookup.get(c_name_k) or {}).get("state", "enabled")
            if current_state == "enabled" and camp_state_k == "enabled":
                enabled_exact_by_text[text_k].append(entry_k)
            elif current_state == "paused" and camp_state_k == "enabled":
                paused_exact_by_camp_text[(c_name_k, text_k)] = entry_k

    # Track explicit ASIN targets AND build target_bid_lookup for Auto/Product Targeting real bid penetration
    explicit_asin_targets_by_camp = set()
    enabled_asin_target_any = set()
    enabled_asin_campaigns = []
    target_bid_lookup: dict[tuple[str, str], float] = {}
    t7_by_key = {
        (t7.get("campaign_name") or "", str(t7.get("targeting_text") or t7.get("expression") or t7.get("exp_type") or "").strip().lower()): t7
        for t7 in (targets_7d or [])
    }
    t3_by_key = {
        (t3.get("campaign_name") or "", str(t3.get("targeting_text") or t3.get("expression") or t3.get("exp_type") or "").strip().lower()): t3
        for t3 in (targets_3d or [])
    }
    for c in camps:
        if c.get("state") == "enabled":
            cn_low = (c.get("name") or "").lower()
            if any(w in cn_low for w in ("asin", "竞品", "target", "同类", "关联")) or str(c.get("targeting", "")).lower() in ("targets", "targeting"):
                enabled_asin_campaigns.append(c["name"])
    for t_row in (targets or []):
        if skip_total(t_row):
            continue
        cn_t_raw = str(t_row.get("campaign_name") or "").strip()
        ag_id_t = str(t_row.get("ad_group_id") or "").strip()
        raw_t_key = str(t_row.get("targeting_text") or t_row.get("expression") or t_row.get("exp_type") or "").strip().lower()
        t7_r = t7_by_key.get((cn_t_raw, raw_t_key), {})
        t3_r = t3_by_key.get((cn_t_raw, raw_t_key), {})
        eff_t_bid, _, _ = resolve_multi_window_entity_bid(
            t_row, t7_r, t3_r, group_bid_lookup, camp_group_bid_lookup, fallback=0.0
        )
        if eff_t_bid > 0 and cn_t_raw:
            for tk_cand in (
                t_row.get("exp_type"),
                t_row.get("exp_type_text"),
                t_row.get("exp_type_zh"),
                t_row.get("match_type"),
                t_row.get("targeting_text"),
                t_row.get("expression"),
            ):
                tk_low = str(tk_cand or "").strip().lower()
                if tk_low:
                    target_bid_lookup[(cn_t_raw, tk_low)] = eff_t_bid
                    if ag_id_t:
                        target_bid_lookup[(ag_id_t, tk_low)] = eff_t_bid
            if (cn_t_raw, "__any__") not in target_bid_lookup or num(t_row.get("spends")) > 0:
                target_bid_lookup[(cn_t_raw, "__any__")] = eff_t_bid
            if ag_id_t and (ag_id_t, "__any__") not in target_bid_lookup:
                target_bid_lookup[(ag_id_t, "__any__")] = eff_t_bid

        c_info_t = extract_asin_target_info(t_row, group_bid_lookup, camp_group_bid_lookup)
        if c_info_t and c_info_t.get("asin"):
            asin_low = c_info_t["asin"].lower()
            cn_t = c_info_t.get("campaign_name") or cn_t_raw
            explicit_asin_targets_by_camp.add((cn_t, asin_low))
            if eff_t_bid > 0 and cn_t:
                target_bid_lookup[(cn_t, asin_low)] = eff_t_bid
                target_bid_lookup[(cn_t, f"asinsameas::{asin_low}")] = eff_t_bid
            t_st = str(t_row.get("state") or "enabled").lower().strip()
            if t_st == "enabled" and (camp_lookup.get(cn_t) or {}).get("state") == "enabled":
                enabled_asin_target_any.add(asin_low)
                if cn_t not in enabled_asin_campaigns:
                    enabled_asin_campaigns.append(cn_t)

    # Step 1: Aggregate search_terms rows by (campaign_name, query_lower) while also retaining per-match-type
    # slices (by_match) so multi-match campaigns (Exact + Broad/Phrase in same campaign) never misattribute bleed!
    agg_sts_map = {}
    for r in sts:
        q_raw = (r.get("query") or "").strip()
        c_name = r.get("campaign_name") or ""
        if not q_raw or not c_name:
            continue
        ql = q_raw.lower()
        sp_r = num(r.get("spends"))
        sl_r = num(r.get("sales"))
        od_r = num(r.get("orders"))
        ck_r = num(r.get("clicks"))
        im_r = int(num(r.get("impressions")))
        mt_r = r.get("match_type") or r.get("target_match_type") or ""
        tmt_r = r.get("target_match_type") or ""
        tt_r = r.get("keyword_text") or r.get("target_text") or ""
        ag_id_r = str(r.get("ad_group_id") or "").strip()
        ag_nm_r = str(r.get("ad_group_name") or "").strip()
        mt_norm = str(mt_r).strip().lower()
        akey = (c_name, ql)
        if akey not in agg_sts_map:
            agg_sts_map[akey] = {
                "query": q_raw,
                "ql": ql,
                "campaign_name": c_name,
                "spends": sp_r,
                "sales": sl_r,
                "orders": od_r,
                "clicks": ck_r,
                "impressions": im_r,
                "match_type": mt_r,
                "target_match_type": tmt_r,
                "target_text": tt_r,
                "ad_group_id": ag_id_r,
                "ad_group_name": ag_nm_r,
                "_top_row_spend": sp_r,
                "by_match": {},
            }
        else:
            slot = agg_sts_map[akey]
            slot["spends"] += sp_r
            slot["sales"] += sl_r
            slot["orders"] += od_r
            slot["clicks"] += ck_r
            slot["impressions"] += im_r
            if sp_r > slot["_top_row_spend"]:
                slot["_top_row_spend"] = sp_r
                slot["match_type"] = mt_r
                slot["target_match_type"] = tmt_r
                slot["target_text"] = tt_r
                slot["ad_group_id"] = ag_id_r
                slot["ad_group_name"] = ag_nm_r

        bm = agg_sts_map[akey]["by_match"].setdefault(
            mt_norm,
            {
                "spends": 0.0,
                "sales": 0.0,
                "orders": 0.0,
                "clicks": 0.0,
                "impressions": 0,
                "target_text": tt_r,
                "target_match_type": tmt_r,
                "ad_group_id": ag_id_r,
                "ad_group_name": ag_nm_r,
            },
        )
        bm["spends"] += sp_r
        bm["sales"] += sl_r
        bm["orders"] += od_r
        bm["clicks"] += ck_r
        bm["impressions"] += im_r

    agg_sts = sorted(agg_sts_map.values(), key=lambda x: -x["spends"])

    # Step 2: Pre-compute bucket_stats, ngram_stats, cannibal_map, and bleeding_ng_set BEFORE placement diagnosis
    # so Rule 56 bleeding N-gram veto actively blocks boosting/harvesting 0-order or extreme-loss root queries!
    for r in agg_sts:
        q = r["query"]
        ql = r["ql"]
        spend = r["spends"]
        sales = r["sales"]
        ordn = r["orders"]
        clk = r["clicks"]
        c_name = r["campaign_name"]

        bucket, match_reason, is_strong_relevance = classify_query(q, profile, rules)
        r["_bucket"] = bucket
        r["_is_strong"] = is_strong_relevance

        bucket_stats[bucket]["spend"] += spend
        bucket_stats[bucket]["sales"] += sales
        bucket_stats[bucket]["ord"] += ordn
        bucket_stats[bucket]["clk"] += clk

        if not is_asin_term(q):
            for ng, gtype in extract_ngrams(q):
                ngram_stats[ng]["spend"] += spend
                ngram_stats[ng]["sales"] += sales
                ngram_stats[ng]["ord"] += ordn
                ngram_stats[ng]["clk"] += clk
                ngram_stats[ng]["queries"].add(ql)
                ngram_stats[ng]["gram_type"] = gtype

        cannibal_map[ql].append({
            "campaign": c_name,
            "match": r["match_type"],
            "target": r["target_text"],
            "spend": spend,
            "sales": sales,
            "orders": ordn,
            "clicks": clk,
            "cpc": round(spend / clk, 2) if clk > 0 else 0.0,
        })

    protected_ng_tokens = _compute_protected_ngram_tokens(profile, rules)
    eff_be_ng = num((financial_prof or {}).get("true_all_in_break_even_tacos")) or num((financial_prof or {}).get("break_even_acos")) or target_acos or 25.0
    if eff_be_ng <= 0:
        eff_be_ng = target_acos or 15.0
    bleeding_ng_set: set[str] = set()
    ng_min_sp = float(st_cfg.get("ngram_bleed_spend", 10.0))
    ng_min_clk = int(st_cfg.get("ngram_bleed_clicks", 8))
    for ng, stat in ngram_stats.items():
        is_zero_ord_bleed = (stat["spend"] >= ng_min_sp and stat["ord"] == 0 and stat["clk"] >= ng_min_clk and len(stat["queries"]) >= 2)
        is_extreme_loss_bleed = (
            stat["ord"] >= 1
            and len(stat["queries"]) >= 3
            and stat["spend"] >= max(25.0, float(st_cfg.get("fail_test_spend", 20.0)) * 1.25)
            and acos(stat["spend"], stat["sales"]) >= max(150.0, eff_be_ng * 3.0)
        )
        if is_zero_ord_bleed or is_extreme_loss_bleed:
            _, _, ng_strong = classify_query(ng, profile, rules)
            ng_tokens = {t for t in re.findall(r"[a-z0-9]+", ng.lower()) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}
            if ng_strong or not ng_tokens:
                continue
            if stat["gram_type"] == "1-gram" and (ng_tokens & protected_ng_tokens):
                continue
            if stat["gram_type"] != "1-gram" and ng_tokens.issubset(protected_ng_tokens):
                continue
            bleeding_ng_set.add(ng)

    harvest_camp = find_exact_harvest_campaign(camps, kws)
    harvest_asin_camp = enabled_asin_campaigns[0] if enabled_asin_campaigns else ""
    placement_diagnoses = []

    # Step 3: Evaluate each aggregated (campaign, query) with full placement, rank, 7d freshness, and 2-stage migration awareness
    for r in agg_sts:
        q = r["query"]
        ql = r["ql"]
        spend = r["spends"]
        sales = r["sales"]
        ordn = r["orders"]
        clk = r["clicks"]
        imp_cnt = r["impressions"]
        c_name = r["campaign_name"]
        match_type = r["match_type"]
        target_text = r["target_text"]
        bucket = r["_bucket"]
        is_strong_relevance = r["_is_strong"]
        fw_blocked, _, _ = check_global_compliance_firewall(q, profile, rules)

        sr, v_tier = get_searchrank(q, sr_map)
        sr_display = str(sr) if sr is not None else "--"
        st7 = st_7d_map.get((c_name, ql), {})
        st7_spend = st7.get("spend", 0.0)
        st7_sales = st7.get("sales", 0.0)
        st7_ord = st7.get("orders", 0)
        st7_acos = acos(st7_spend, st7_sales) if st7_sales > 0 else (999.0 if st7_spend > 0 else 0.0)

        eval_st = evaluate_rank_synergy(
            term=q,
            xydc_map=xydc_map or {},
            spend=spend,
            sales=sales,
            orders=int(ordn),
            clicks=int(clk),
            target_acos=eval_target_acos or 25.0,
            scaled_thresholds=st_cfg,
            stage_mode=(stage_ctx or {}).get("stage_mode", "GROWTH"),
        )
        st_org_rank = eval_st.get("organic_rank")
        st_sp_rank = eval_st.get("sp_rank")
        st_org_rank_str = str(st_org_rank) if st_org_rank is not None else "--"
        st_sp_rank_str = str(st_sp_rank) if st_sp_rank is not None else "--"
        if st_org_rank is not None or st_sp_rank is not None:
            rank_tag = f"[自然位#{st_org_rank_str}·广告位#{st_sp_rank_str} {eval_st.get('synergy_tag', '')}] "
            rank_cur_desc = f"(自然位#{st_org_rank_str}, 广告位#{st_sp_rank_str}) "
        else:
            rank_tag = "[西柚未建位/暂无排名] "
            rank_cur_desc = "(西柚未建位) "

        row_cpc = round(spend / clk, 2) if clk > 0 else 0.0
        row_cvr = round(ordn / clk * 100.0, 1) if clk > 0 else 0.0
        row_acos = round(acos(spend, sales), 1)
        row_acos_txt = fmt_acos_text(row_acos, spend, sales)
        if spend >= 3 or ordn >= 1:
            st_lines.append("\t".join(str(x) for x in [
                bucket, v_tier, sr_display, c_name, q, match_type, target_text,
                round(spend, 2), round(sales, 2), row_acos,
                int(ordn), int(clk), row_cvr, row_cpc,
                st_org_rank_str, st_sp_rank_str, eval_st["synergy_tier"],
            ]))

        diag = infer_search_term_placement(
            query=q,
            spend=spend,
            sales=sales,
            orders=int(ordn),
            clicks=int(clk),
            impressions=imp_cnt,
            c_name=c_name,
            match_type=match_type,
            target_text=target_text,
            bucket=bucket,
            is_strong=is_strong_relevance,
            v_tier=v_tier,
            sr_display=sr_display,
            camp_lookup=camp_lookup,
            kw_lookup=kw_lookup,
            target_acos=eval_target_acos,
            bleeding_ng_set=bleeding_ng_set,
            target_bid_lookup=target_bid_lookup,
            group_bid_lookup=group_bid_lookup,
            camp_group_bid_lookup=camp_group_bid_lookup,
            ad_group_id=r.get("ad_group_id", ""),
            ad_group_name=r.get("ad_group_name", ""),
            target_match_type=r.get("target_match_type", ""),
            scaled_thresholds=st_cfg,
        )
        if spend >= 3 or ordn >= 1 or clk >= 5:
            placement_diagnoses.append(diag)

        camp_obj = camp_lookup.get(c_name, {})
        if camp_obj.get("state", "enabled") != "enabled":
            continue

        is_already_negated = (
            (c_name.lower(), ql) in active_neg_camp_pairs
            or is_known_negative(q, known_negatives)
        )

        # ---- Branch A: ASIN Customer Queries (Strictly Product-Targeting Domain, NEVER Keyword Domain) ----
        if is_asin_term(q):
            asin_upper = q.upper()
            # Skip if already an explicit asinSameAs target in this campaign (handled by write_targets_tsv with full competitor profile)
            if (c_name, ql) in explicit_asin_targets_by_camp:
                continue
            if not is_already_negated and not block_non_p0_cuts and ordn == 0 and (clk >= 8 or spend >= max(6.0, float(st_cfg["tcpa"]))):
                waste.append(f"{asin_upper}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag=BLEEDING_ASIN_QUERY\taction=Negative_Target")
                bulk_adjustments.append({
                    "action_type": "Negative_Target",
                    "campaign": c_name,
                    "target_entity": asin_upper,
                    "match_type": "asinSameAs",
                    "current_value": f"竞品详情页搜索流量 (触发: {target_text} [{match_type}]), 30d花费=${spend:.2f}, {int(clk)}点击0单",
                    "recommended_value": f"Add Negative Target (asinSameAs={asin_upper})",
                    "reason": f"[竞品ASIN商品投放否定] 自动/类目活动跑出低效竞品详情页 {asin_upper}，累计 {int(clk)} 点击 0 订单（花费 ${spend:.2f}），执行商品投放否定（Negative Target）止血。",
                })
            elif not freeze_expansion and ordn >= 2 and acos(spend, sales) <= eval_target_acos and spend >= 6.0 and ql not in enabled_asin_target_any:
                dest_asin_camp = harvest_asin_camp or c_name
                if (camp_lookup.get(dest_asin_camp) or {}).get("state", "enabled") == "enabled":
                    dest_pp = num((camp_lookup.get(dest_asin_camp) or {}).get("pp", 0))
                    is_frg_asin = (traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE") or (financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv1)
                    asin_fl = float(rcfg.get("bid_floor_asin_fragile", 0.42)) if is_frg_asin else float(rcfg.get("bid_floor_asin", 0.36))
                    asin_bid = max(asin_fl, min(1.10, round((row_cpc or 0.55) / (1.0 + dest_pp / 100.0), 2)))
                    harvest.append(f"{asin_upper}\t{c_name}\tspend={spend:.2f}\tacos={row_acos:.1f}\tord={int(ordn)}\tbucket=asin\ttier=ASIN_TARGET")
                    bulk_adjustments.append({
                        "action_type": "Harvest_ASIN",
                        "campaign": dest_asin_camp,
                        "target_entity": asin_upper,
                        "match_type": "asinSameAs",
                        "current_value": f"自动/泛流跑出高转化竞品页 {asin_upper} in {c_name}: {int(ordn)}单, ACOS={row_acos_txt}, CPC=${row_cpc:.2f}",
                        "recommended_value": f"${asin_bid:.2f}",
                        "reason": f"[高转化竞品ASIN定向收割] 在 {c_name} 跑出 {int(ordn)} 单 (ACOS {row_acos_txt})，作为商品投放 (asinSameAs) 移入 {dest_asin_camp} 独立出价 ${asin_bid:.2f} 精准拦截。",
                    })
            continue

        # ---- Branch B: 2-Stage Soft-Handoff Migration Closure Check ----
        # If q is ALREADY an enabled Exact keyword in ANOTHER enabled campaign, NEVER re-harvest it;
        # instead check if Stage-2 Negative_Exact isolation in c_name (Broad/Auto/Phrase) is now due!
        other_exact_entries = [e for e in enabled_exact_by_text.get(ql, []) if e["campaign"] != c_name]
        c_is_exact_camp = ("exact" in c_name.lower() or "精准" in c_name)
        matching_kw_matches = [m for m in ("exact", "phrase", "broad") if f"{c_name}::{m}::{ql}" in kw_lookup]
        is_targeted_kw = bool(matching_kw_matches)

        if other_exact_entries and not c_is_exact_camp and not is_targeted_kw and not is_already_negated:
            best_ex = max(other_exact_entries, key=lambda e: (e["ord_30d"], e["imp_7d"], e["imp_30d"]))
            exact_is_delivering = (best_ex["imp_7d"] >= 10 or best_ex["imp_30d"] >= 30 or best_ex["ord_30d"] >= 1)
            broad_bleeding_or_cannibalizing = (
                (ordn == 0 and (spend >= 5.0 or clk >= 6))
                or (ordn >= 1 and acos(spend, sales) > target_acos * 1.25)
                or (spend >= 8.0 and best_ex["ord_30d"] >= 1 and row_cpc >= max(0.01, best_ex["cpc_30d"]))
            )
            if exact_is_delivering and broad_bleeding_or_cannibalizing:
                bulk_adjustments.append({
                    "action_type": "Negative_Exact",
                    "campaign": c_name,
                    "target_entity": q,
                    "match_type": "negativeExact",
                    "current_value": f"买家搜索词 {rank_cur_desc}在 {c_name} 花费=${spend:.2f}({int(ordn)}单, CPC=${row_cpc:.2f}) | 已在精准仓 {best_ex['campaign']} 承接(7d曝光={best_ex['imp_7d']}, 30d={best_ex['ord_30d']}单)",
                    "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                    "reason": f"{rank_tag}[移词二阶段闭环隔离] 该词已在精准仓 ({best_ex['campaign']}) 稳定承接流量（7天曝光 {best_ex['imp_7d']}，30天 {best_ex['ord_30d']} 单）；原宽泛/自动活动 ({c_name}) 继续分流内耗（花费 ${spend:.2f}，{int(ordn)} 单），执行二阶段精准否定完成流量收口。",
                })
                continue

        # ---- Branch C: Placement-Based Dynamic Adjustment (Multi-Match-Type Decoupled!) ----
        if is_targeted_kw and not is_already_negated:
            # Evaluate EACH enabled match type in c_name on its OWN keyword metrics & slice (never hardcoding "exact"!)
            for actual_match in matching_kw_matches:
                kw_info = kw_lookup[f"{c_name}::{actual_match}::{ql}"]
                actual_base_bid = kw_info["bid"]
                kw_state = kw_info["state"]

                if kw_state != "enabled":
                    continue

                bm_slice = (r.get("by_match") or {}).get(actual_match)
                if len(matching_kw_matches) > 1:
                    m_sp = bm_slice["spends"] if bm_slice else float(kw_info.get("spend_30d", 0.0))
                    m_sl = bm_slice["sales"] if bm_slice else float(kw_info.get("sales_30d", 0.0))
                    m_od = int(bm_slice["orders"]) if bm_slice else int(kw_info.get("ord_30d", 0))
                    m_ck = int(bm_slice["clicks"]) if bm_slice else int(kw_info.get("clk_30d", 0))
                    m_im = int(bm_slice["impressions"]) if bm_slice else int(kw_info.get("imp_30d", 0))
                    diag_m = infer_search_term_placement(
                        query=q,
                        spend=m_sp,
                        sales=m_sl,
                        orders=m_od,
                        clicks=m_ck,
                        impressions=m_im,
                        c_name=c_name,
                        match_type=actual_match,
                        target_text=q,
                        bucket=bucket,
                        is_strong=is_strong_relevance,
                        v_tier=v_tier,
                        sr_display=sr_display,
                        camp_lookup=camp_lookup,
                        kw_lookup=kw_lookup,
                        target_acos=eval_target_acos,
                        bleeding_ng_set=bleeding_ng_set,
                        target_bid_lookup=target_bid_lookup,
                        group_bid_lookup=group_bid_lookup,
                        camp_group_bid_lookup=camp_group_bid_lookup,
                        scaled_thresholds=st_cfg,
                    )
                else:
                    diag_m = diag

                action_code = diag_m.get("action_type")
                if not action_code:
                    continue

                if action_code in ("Throttle_TOS_Bid", "Lower_Bid"):
                    if block_non_p0_cuts:
                        continue
                    kw_ord = int(kw_info.get("ord_30d", 0))
                    kw_ord_7d = int(kw_info.get("ord_7d", 0))
                    kw_acos_7d = float(kw_info.get("acos_7d", 0.0))
                    kw_ord_3d = int(kw_info.get("ord_3d", 0))
                    kw_sp_3d = float(kw_info.get("spend_3d", 0.0))
                    kw_imp_3d = int(kw_info.get("imp_3d", 0))
                    kw_acos_3d = float(kw_info.get("acos_3d", 0.0))

                    # Stop-bleed check: if already 0 traffic/spend in recent 3d, do NOT cut again on stale 30d average!
                    if kw_info["has_zero_recent_traffic"] or kw_sp_3d == 0.0 or kw_imp_3d <= 25:
                        continue

                    # Velocity-Shrinkage Guard: NEVER cut bids on converting keywords when macro sales velocity collapsed!
                    if shrinkage_guard and (kw_ord >= 1 or kw_ord_7d >= 1 or kw_ord_3d >= 1):
                        continue

                    # 7d Freshness Guard (attribution-lag safe: do NOT invalidate solely because ord_3d==0 unless extreme 3d CB!)
                    if kw_ord_7d >= 2 and 0 < kw_acos_7d <= eval_target_acos * 1.05 and kw_sp_3d < cb_bleed_3d:
                        continue

                    is_fragile_org = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
                    is_high_ref = bool(financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv1)
                    bid_floor = kw_fl_frg if (is_fragile_org or is_high_ref) else kw_fl_std
                    if actual_base_bid <= bid_floor:
                        continue

                    kw_cvr = float(kw_info.get("cvr_30d", 0.0))
                    kw_acos = float(kw_info.get("acos_30d", 0.0))
                    kw_sp_30d = float(kw_info.get("spend_30d", 0.0))
                    kw_sl_30d = float(kw_info.get("sales_30d", 0.0))
                    kw_clk_30d = int(kw_info.get("clk_30d", 0))
                    kw_sp_7d = float(kw_info.get("spend_7d", 0.0))
                    kw_cpc = float(kw_info.get("cpc_30d", 0.0)) or float(kw_info.get("cpc", 0.0)) or float(diag_m.get("cpc", 0.0))
                    avg_p = num(financial_prof.get("avg_price", 25.0)) if financial_prof else 25.0
                    tos_pct = num(camp_obj.get("tos", 0))

                    if kw_ord >= 2 and kw_acos <= eval_target_acos and kw_sp_3d < cb_bleed_3d:
                        continue

                    # Require TCPA-scaled meaningful spend floor before cutting bid on over-ACOS!
                    if kw_sp_30d < min_act_sp and kw_sp_7d < min_act_sp:
                        continue

                    is_over_acos = (
                        (kw_ord >= 1 and kw_acos > eval_target_acos and kw_sp_7d >= act_bleed_7d and kw_sp_30d >= min_over_sp_30d)
                        or (kw_sp_7d >= cb_bleed_7d and kw_ord_7d > 0 and kw_acos_7d > eval_target_acos * 1.2)
                    )
                    if not kw_info["has_active_bleeding"] and not is_over_acos:
                        continue

                    timing_desc = ""
                    t_base_val = num(kw_info.get("timing_base_value")) if kw_info.get("is_apply_time") else 0.0
                    if kw_info.get("is_apply_time"):
                        t_name_disp = kw_info.get("timing_template") or "未命名"
                        t_base_str = f"${t_base_val:.2f}" if t_base_val > 0 else "未设"
                        timing_desc = f" [领星分时策略运行中: 模板'{t_name_disp}', 当前分时基准值:{t_base_str}]"

                    if kw_ord >= 1 and avg_p > 0:
                        # §3.3 & §7.3 Bayesian smoothed CVR_adj prevents low-sample 1-order/3-click explosions!
                        cvr_adj = v2p.calc_smoothed_cvr(kw_ord, max(1, kw_clk_30d), cvr_base_ratio, cvr_prior_k)
                        target_cpc = (eval_target_acos / 100.0) * avg_p * (cvr_adj / 100.0)
                        target_base_bid = max(bid_floor, round(target_cpc / (1.0 + (tos_pct / 100.0)), 2))
                        is_fragile_pillar = bool(is_fragile_org and (kw_ord >= 6 or kw_ord_7d >= 2))
                        is_pillar_kw = is_fragile_pillar or ((kw_ord >= 6 or kw_ord_7d >= 2) and kw_acos <= max(eval_target_acos * 1.45, 36.0))
                        if is_fragile_pillar:
                            max_drop = _ceil_cents(actual_base_bid * 0.90)
                            min_drop = _ceil_cents(actual_base_bid * 0.92)
                        else:
                            max_drop = _ceil_cents(actual_base_bid * (0.85 if is_pillar_kw else 0.80))
                            min_drop = _ceil_cents(actual_base_bid * 0.90)
                        auction_safe_base = _ceil_cents((kw_cpc * 0.85) / (1.0 + (tos_pct / 100.0))) if (kw_cpc > 0 and is_pillar_kw) else bid_floor
                        if is_fragile_pillar:
                            eff_floor = max(bid_floor, auction_safe_base) if auction_safe_base < actual_base_bid else bid_floor
                        else:
                            eff_floor = max(bid_floor, min(min_drop, auction_safe_base))
                        new_bid = max(eff_floor, min(min_drop, max(max_drop, target_base_bid)))
                        if new_bid >= actual_base_bid:
                            new_bid = max(eff_floor, max_drop, round(actual_base_bid - 0.02, 2))
                        if t_base_val > 0 and new_bid >= t_base_val:
                            new_bid = max(eff_floor, max_drop, round(t_base_val - 0.02, 2))
                        if new_bid >= actual_base_bid or (t_base_val > 0 and new_bid >= t_base_val):
                            continue
                        drop_pct = int(round((actual_base_bid - new_bid) / actual_base_bid * 100))

                        kw_acos_txt = fmt_acos_text(kw_acos, kw_sp_30d, kw_sl_30d)
                        slice_str = f"，7d:${kw_sp_7d:.1f}/{kw_ord_7d}单，3d:${kw_sp_3d:.1f}/{kw_ord_3d}单"
                        pillar_note = "（脆弱品核心出单支柱词单次限降<=10%防断流）" if is_fragile_pillar else ""
                        reason_msg = f"{rank_tag}投放词 [{actual_match}] 近期投产超标（30d:${kw_sp_30d:.2f}/{kw_ord}单/ACOS {kw_acos_txt}{slice_str}），依目标ACOS({eval_target_acos:.1f}%)与平滑CVR({cvr_adj:.1f}%, 原始{kw_cvr:.1f}%)倒推保本基础竞价(${target_base_bid:.2f})，单步微调 -{drop_pct}% 至 ${new_bid:.2f} 防断流控费{pillar_note}。"
                        current_val_str = f"投放词 [{actual_match}] {rank_cur_desc}(当前基础竞价 ${actual_base_bid:.2f}){timing_desc}, 30d=${kw_sp_30d:.2f}({kw_ord}单, ACOS={kw_acos_txt}), 7d=${kw_sp_7d:.2f}({kw_ord_7d}单), 3d=${kw_sp_3d:.2f}({kw_ord_3d}单), CPC=${kw_cpc:.2f} [{diag_m['inferred_placement']}]"
                    else:
                        is_severe = (kw_sp_7d >= severe_bleed_sp or kw_sp_3d >= cb_bleed_3d or kw_sp_30d >= severe_bleed_sp)
                        if shrinkage_guard:
                            drop_pct = 0.90
                            drop_label = "10%(防缩量限幅)"
                        else:
                            drop_pct = 0.80 if is_severe else 0.85
                            drop_label = "20%(重度流血)" if is_severe else "15%(中度流血)"
                        new_bid = max(bid_floor, _ceil_cents(actual_base_bid * drop_pct))
                        if t_base_val > 0 and new_bid >= t_base_val:
                            new_bid = max(bid_floor, round(t_base_val - 0.02, 2))
                        if new_bid >= actual_base_bid or (t_base_val > 0 and new_bid >= t_base_val):
                            continue
                        clk_disp = kw_clk_30d if kw_clk_30d > 0 else int(clk)
                        reason_msg = f"{rank_tag}投放关键词 [{actual_match}] 7d活跃流血（30d:${kw_sp_30d:.2f}/{clk_disp}点击0单，7d:${kw_sp_7d:.2f}/0单，重度线${severe_bleed_sp:.1f}），阶梯微降基础竞价 -{drop_label} 至 ${new_bid:.2f} 防守避损。"
                        current_val_str = f"投放词 [{actual_match}] {rank_cur_desc}(当前基础竞价 ${actual_base_bid:.2f}){timing_desc}, 30d=${kw_sp_30d:.2f}({clk_disp}点击0单), 7d=${kw_sp_7d:.2f}(0单), 3d=${kw_sp_3d:.2f}(0单), CPC=${kw_cpc:.2f} [{diag_m['inferred_placement']}]"

                    timing_rec = ""
                    if kw_info.get("is_apply_time"):
                        t_name_disp = kw_info.get("timing_template") or "未命名"
                        timing_rec = f" 【领星分时联动提示】：已绑定分时模板'{t_name_disp}'，请同步将领星分时基准值(timing_base_value)更新为 ${new_bid:.2f}。"

                    waste.append(f"{q}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag_m['ctr']}%\tcpc=${diag_m['cpc']:.2f}\tpos={diag_m['inferred_placement']}\ttag={diag_m['strategy_tag']}\taction={action_code}")
                    bulk_adjustments.append({
                        "action_type": "Throttle_TOS_Bid",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": actual_match,
                        "current_bid_num": actual_base_bid,
                        "current_value": current_val_str,
                        "recommended_value": f"${new_bid:.2f}",
                        "reason": f"{reason_msg}{timing_rec}",
                    })
                elif action_code == "Elevate_To_TOS":
                    if freeze_expansion or fw_blocked:
                        continue
                    # Attribution-lag safe: check 7d bleed or extreme 3d bleed CB before vetoing Elevate!
                    if (kw_info["spend_7d"] >= cb_bleed_7d and kw_info["ord_7d"] == 0) or (kw_info["spend_3d"] >= cb_bleed_3d and kw_info["ord_3d"] == 0):
                        continue
                    new_bid = _floor_cents(actual_base_bid * 1.15)
                    timing_desc = ""
                    timing_rec = ""
                    if kw_info.get("is_apply_time"):
                        t_name_disp = kw_info.get("timing_template") or "未命名"
                        t_base_val_e = kw_info.get("timing_base_value")
                        t_base_str = f"${t_base_val_e:.2f}" if t_base_val_e is not None else "未设"
                        timing_desc = f" [领星分时策略运行中: 模板'{t_name_disp}', 当前分时基准值:{t_base_str}]"
                        timing_rec = f" 【领星分时联动提示】：已绑定分时模板'{t_name_disp}'，建议同步更新领星分时基准值(timing_base_value)为 ${new_bid:.2f}。"

                    bulk_adjustments.append({
                        "action_type": "Elevate_To_TOS",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": actual_match,
                        "current_bid_num": actual_base_bid,
                        "current_value": f"投放词 [{actual_match}] {rank_cur_desc}(当前基础竞价 ${actual_base_bid:.2f}){timing_desc}, 30d=${kw_info['spend_30d']:.2f}({kw_info['clk_30d']}点击), CTR={diag_m['ctr']:.2f}% [{diag_m['inferred_placement']}]",
                        "recommended_value": f"${new_bid:.2f}",
                        "reason": f"{rank_tag}核心投放词 [{actual_match}] 在详情页/底部位受抑，基础竞价提至 ${new_bid:.2f} 专打首屏黄金位测试真实转化力。{timing_rec}",
                    })
                elif action_code == "Negative_Exact":
                    # Only emit Negative_Exact if broad/phrase matched an unwanted query AND no enabled exact keyword exists in c_name
                    is_p0_c = fw_blocked or bucket in ("is_not", "competitor_brand") or str(bucket).startswith("is_not")
                    if block_non_p0_cuts and not is_p0_c:
                        continue
                    has_enabled_exact_same = (
                        f"{c_name}::exact::{ql}" in kw_lookup
                        and kw_lookup[f"{c_name}::exact::{ql}"]["state"] == "enabled"
                    )
                    if actual_match in ("broad", "phrase") and not has_enabled_exact_same:
                        bulk_adjustments.append({
                            "action_type": "Negative_Exact",
                            "campaign": c_name,
                            "target_entity": q,
                            "match_type": "negativeExact",
                            "current_bid_num": actual_base_bid,
                            "current_value": f"投放词 [{actual_match}] 触发 {rank_cur_desc}30d=${kw_info['spend_30d']:.2f}, 点击={kw_info['clk_30d']}, 0订单",
                            "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                            "reason": f"{rank_tag}{diag_m['diagnostic_reason']}",
                        })
                        break
        elif diag.get("action_type") and not is_already_negated:
            action_code = diag["action_type"]
            # q is NOT a targeted keyword in c_name; it is a customer query triggered by broad/auto/phrase
            already_in_exact_anywhere = bool(enabled_exact_by_text.get(ql))
            harvest_camp_obj = camp_lookup.get(harvest_camp, {})
            can_harvest_to_exact = (
                not fw_blocked
                and harvest_camp_obj.get("state", "enabled") == "enabled"
                and c_name != harvest_camp
                and not c_is_exact_camp
                and not already_in_exact_anywhere
                and (harvest_camp, ql) not in paused_exact_by_camp_text
            )

            if action_code in ("Throttle_TOS_Bid", "Lower_Bid", "Negative_Exact"):
                # Rule 31 & Rule 24 Hard Guard:
                # NEVER negate a converting search term (orders >= 1)!
                if ordn >= 1:
                    pass
                elif st7_ord >= 1 and st7_acos <= target_acos:
                    pass
                else:
                    is_core_hero = (
                        not fw_blocked
                        and (bucket in ("brand", "core", "core_feature", "core_or_title") or is_strong_relevance)
                        and diag.get("strategy_tag") != "BLEEDING_NGRAM_VETO"
                    )
                    has_proven_organic_rank = (st_org_rank is not None and 1 <= st_org_rank <= 25)
                    if not freeze_expansion and is_core_hero and action_code == "Throttle_TOS_Bid" and has_proven_organic_rank and can_harvest_to_exact:
                        target_cpc = diag.get("cpc", 0.0) or (spend / max(1, clk))
                        dest_tos = num(harvest_camp_obj.get("tos", 0))
                        defensive_bid = max(0.25, round((target_cpc * 0.75) / (1.0 + dest_tos / 100.0), 2))
                        bulk_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": harvest_camp,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_bid_num": defensive_bid,
                            "current_value": f"核心有排名搜索词 {rank_cur_desc}in {c_name} (触发: {target_text}), 30d=${spend:.2f}, {int(clk)}点击{int(ordn)}单 [{diag['inferred_placement']}]",
                            "recommended_value": f"${defensive_bid:.2f}",
                            "reason": f"{rank_tag}自然位前25核心主粮词在宽泛仓高CPC耗损；移入专用精准仓 ({harvest_camp}) 以防守基础竞价 ${defensive_bid:.2f} 独立控费（软交接第1阶段：原仓观察3-5天后再隔离）。",
                        })
                        continue
                    # During PEAK_DAY, only P0 hard-misfit/policy negatives are allowed (§7.9)
                    is_p0_hard_misfit = fw_blocked or bucket in ("is_not", "competitor_brand") or str(bucket).startswith("is_not")
                    if block_non_p0_cuts and not is_p0_hard_misfit:
                        continue
                    # If unranked 0-order core query below fail-test threshold, hold off from hard negating core phrase prematurely
                    if is_core_hero and not has_proven_organic_rank and clk < int(st_cfg["fail_test_clicks"]) and spend < float(st_cfg["fail_test_spend"]):
                        continue

                    waste.append(f"{q}\t{c_name}\tspend={spend:.2f}\tclk={int(clk)}\tctr={diag['ctr']}%\tcpc=${diag['cpc']:.2f}\tpos={diag['inferred_placement']}\ttag={diag['strategy_tag']}\taction=Negative_Exact")
                    bulk_adjustments.append({
                        "action_type": "Negative_Exact",
                        "campaign": c_name,
                        "target_entity": q,
                        "match_type": "negativeExact",
                        "current_value": f"买家搜索词 {rank_cur_desc}(触发: {target_text} [{match_type}]), 30d=${spend:.2f}, {int(clk)}点击{int(ordn)}单 [{diag['inferred_placement']}]",
                        "recommended_value": f"在活动 {c_name} 添加精准否定 (Negative Exact)",
                        "reason": f"{rank_tag}非独立投放词由宽泛/自动匹配触发，累计 {int(clk)} 点击 {int(ordn)} 订单（花费 ${spend:.2f}），在活动 {c_name} 中精准否定阻断无效出血。",
                    })
            elif action_code in ("Elevate_To_TOS", "Refine_Bid_Placement"):
                if not freeze_expansion and not fw_blocked and not (bucket in ("is_not", "competitor_brand", "adjacent") or str(bucket).startswith("is_not")):
                    qualifies_trapped_hero = (
                        (ordn >= 1 and acos(spend, sales) <= eval_target_acos * 1.15)
                        or (not freeze_expansion and st_org_rank is not None and 1 <= st_org_rank <= 20 and is_strong_relevance and action_code == "Elevate_To_TOS")
                    )
                    if qualifies_trapped_hero and can_harvest_to_exact:
                        target_cpc = diag.get("cpc", 0.0) or (spend / max(1, clk))
                        recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                            {"cpc": target_cpc, "spend": spend, "orders": ordn, "clicks": clk},
                            harvest_camp, camp_lookup, prior_target=None
                        )
                        bulk_adjustments.append({
                            "action_type": "Harvest_To_Exact",
                            "campaign": harvest_camp,
                            "target_entity": q,
                            "match_type": "exact",
                            "current_value": f"受困优质词 in {c_name} {rank_cur_desc}(30d=${spend:.2f}, {int(ordn)}单, CPC=${target_cpc:.2f})",
                            "recommended_value": f"${recom_bid:.2f}",
                            "reason": f"{rank_tag}高潜核心词在宽泛仓低效位受抑；移入专用精准仓 ({harvest_camp}) 独立控价测试首屏（软交接第1阶段）。{bid_calc_desc}",
                        })

        # ---- Branch D: Proven Converting Winner Harvest (respects freeze_expansion, Level 2 MULTI_PURPOSE_PROVEN_HERO CVR_adj >= 5% & spend >= TCPA) ----
        q_cvr_adj = v2p.calc_smoothed_cvr(ordn, max(1, clk), cvr_base_ratio, cvr_prior_k)
        hero_sp_min = float(st_cfg.get("hero_proven_spend", 10.0))
        is_winning_harvest_candidate = (
            not freeze_expansion
            and not fw_blocked
            and (
                (ordn >= 3 and acos(spend, sales) <= eval_target_acos * 1.10 and spend >= 8.0)
                or (ordn >= 2 and acos(spend, sales) <= eval_target_acos * 0.90 and is_strong_relevance and spend >= 6.0)
                or (ordn >= 1 and q_cvr_adj >= 5.0 and spend >= hero_sp_min and acos(spend, sales) <= eval_target_acos and is_strong_relevance)
            )
            and bucket not in ("is_not", "competitor_brand", "adjacent")
            and not str(bucket).startswith("is_not")
        )
        if is_winning_harvest_candidate:
            harvest_camp_obj = camp_lookup.get(harvest_camp, {})
            already_in_exact_anywhere = bool(enabled_exact_by_text.get(ql))
            if (
                harvest_camp_obj.get("state", "enabled") == "enabled"
                and c_name != harvest_camp
                and not c_is_exact_camp
                and not already_in_exact_anywhere
            ):
                harvest.append(f"{q}\t{c_name}\tspend={spend:.2f}\tacos={row_acos:.1f}\tord={int(ordn)}\tbucket={bucket}\ttier={v_tier}")
                target_cpc = round(spend / max(1, clk), 2)
                paused_prior = paused_exact_by_camp_text.get((harvest_camp, ql))
                recom_bid, eff_tos, bid_calc_desc = calc_dynamic_migration_bid(
                    {"cpc": target_cpc, "spend": spend, "orders": ordn, "clicks": clk},
                    harvest_camp, camp_lookup, prior_target=paused_prior
                )
                if paused_prior:
                    bulk_adjustments.append({
                        "action_type": "Unpause_Keyword",
                        "campaign": harvest_camp,
                        "target_entity": q,
                        "match_type": "exact",
                        "current_value": f"{rank_cur_desc}精准仓已存在但处于暂停；原仓 {c_name} 30d={int(ordn)}单, ACOS={row_acos_txt}",
                        "recommended_value": f"state=enabled (bid=${recom_bid:.2f})",
                        "reason": f"{rank_tag}高转化赢家词重启收割：该词在 {c_name} 出单 {int(ordn)} 单 (ACOS {row_acos_txt})，无需重复新建，直接在精准仓 ({harvest_camp}) 恢复启用并校准基础竞价。{bid_calc_desc}",
                    })
                else:
                    bulk_adjustments.append({
                        "action_type": "Harvest_To_Exact",
                        "campaign": harvest_camp,
                        "target_entity": q,
                        "match_type": "exact",
                        "current_value": f"{rank_cur_desc}30d表现: {int(ordn)}单, ACOS={row_acos_txt}, 7d={st7_ord}单 in {c_name}",
                        "recommended_value": f"${recom_bid:.2f}",
                        "reason": f"{rank_tag}高转化赢家词收割至精准仓（软交接第1阶段：先在 {harvest_camp} 建词起量，原仓保留观察3-5天后再执行二阶段精准否定防断流）。{bid_calc_desc}",
                    })

    return active_neg_dict, all_neg_queries, audit_records, bucket_stats, bulk_adjustments, camp_lookup, cannibal_map, harvest, kw_misfit_adjustments, ngram_stats, placement_diagnoses, st_lines, unnegative_adjustments, waste


def write_search_term_outputs(args, dest, gen_time_str, harvest, meta, placement_diagnoses, profile, st_lines, waste):
    """search_terms.tsv, waste.txt, harvest.txt and the placement diagnosis tables."""
    tsv(
        dest / "search_terms.tsv",
        "bucket\tvolume_tier\tsearchrank\tcampaign\tquery\tmatch\ttarget\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\torganic_rank\tsp_rank\tsynergy_tier",
        st_lines,
    )
    dest.joinpath("waste.txt").write_text("\n".join(waste), encoding="utf-8")
    dest.joinpath("harvest.txt").write_text("\n".join(harvest), encoding="utf-8")

    diag_lines = []
    for d in placement_diagnoses:
        diag_lines.append("\t".join(str(x) for x in [
            d["query"], d["campaign"], d["bucket"], d["volume_tier"], d["searchrank"],
            round(d["spend"], 2), round(d["sales"], 2), d["orders"], d["clicks"], d["impressions"],
            d["ctr"], d["cpc"], d["base_bid"], d["inferred_placement"], d["placement_evidence"],
            d["strategy_tag"], d["action_type"] or "--", d["recommended_value"] or "--", d["diagnostic_reason"] or "--"
        ]))
    tsv(
        dest / "search_term_placement_diagnosis.tsv",
        "query\tcampaign\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\timpressions\tctr\tcpc\tbase_bid\tinferred_placement\tplacement_evidence\tstrategy_tag\taction_type\trecommended_value\tdiagnostic_reason",
        diag_lines,
    )
    tsv(
        dest / f"search_term_placement_diagnosis_{gen_time_str}.tsv",
        "query\tcampaign\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\timpressions\tctr\tcpc\tbase_bid\tinferred_placement\tplacement_evidence\tstrategy_tag\taction_type\trecommended_value\tdiagnostic_reason",
        diag_lines,
    )
    if args.export_dir:
        exp_dir = Path(args.export_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        tsv(
            exp_dir / f"{asin_str}-{country_str}-placement-diagnosis-{gen_time_str}.tsv",
            "query\tcampaign\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\timpressions\tctr\tcpc\tbase_bid\tinferred_placement\tplacement_evidence\tstrategy_tag\taction_type\trecommended_value\tdiagnostic_reason",
            diag_lines,
        )


def _sanitize_tsv_cell(val) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(val if val is not None else "")).strip()


def write_rank_synergy_tsv(
    args,
    dest,
    gen_time_str,
    kws,
    meta,
    profile,
    target_acos,
    xydc_map,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    camps=None,
    rules=None,
    sr_map=None,
    entity_memory=None,
    gen_date_str=None,
    scaled_thresholds=None,
    stage_ctx=None,
):
    """Output rank_synergy.tsv & xydc_blindspot_opportunities.tsv -- comprehensive mapping of organic rank vs ad rank (西柚洞察)."""
    if not xydc_map:
        return [], []
    stage_mode = (stage_ctx or {}).get("stage_mode", "GROWTH")

    camp_state_lu = {
        str(c.get("name") or c.get("campaign_name") or "").strip(): str(c.get("state") or "enabled").lower().strip()
        for c in (camps or [])
    }
    kw_perf = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "ord": 0, "clk": 0, "camps": set(), "enabled_camps": set(), "bids": []})
    conv_spend_sum = 0.0
    conv_click_sum = 0
    for k in (kws or []):
        t = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if not t:
            continue
        sp_k = num(k.get("spends"))
        sl_k = num(k.get("sales"))
        od_k = int(num(k.get("orders")))
        ck_k = int(num(k.get("clicks")))
        kw_perf[t]["spend"] += sp_k
        kw_perf[t]["sales"] += sl_k
        kw_perf[t]["ord"] += od_k
        kw_perf[t]["clk"] += ck_k
        if od_k > 0 and ck_k > 0:
            conv_spend_sum += sp_k
            conv_click_sum += ck_k
        cn_k = str(k.get("campaign_name") or "").strip()
        if cn_k:
            kw_perf[t]["camps"].add(cn_k)
            k_state = str(k.get("state") or "enabled").lower().strip()
            c_state = camp_state_lu.get(cn_k, "enabled")
            if k_state == "enabled" and c_state == "enabled":
                kw_perf[t]["enabled_camps"].add(cn_k)
        eff_b, _ = resolve_effective_entity_bid(k, group_bid_lookup, camp_group_bid_lookup, fallback=0.0)
        if eff_b > 0:
            kw_perf[t]["bids"].append(eff_b)

    anchor_portfolio_cpc = (conv_spend_sum / conv_click_sum) if conv_click_sum > 0 else 0.55
    anchor_portfolio_cpc = max(0.35, min(1.10, anchor_portfolio_cpc))
    harvest_camp = find_exact_harvest_campaign(camps or [], kws or [])
    dest_camp_obj = next((c for c in (camps or []) if (c.get("name") or c.get("campaign_name")) == harvest_camp), {})
    dest_tos = float(num(dest_camp_obj.get("tos") or dest_camp_obj.get("placement_top") or 40.0))

    today_ref = gen_date_str or datetime.now().strftime("%Y-%m-%d")
    eff_rules = rules if isinstance(rules, dict) else {}
    eff_prof = profile if isinstance(profile, dict) else {}
    own_brand_low = str(eff_prof.get("brand") or "").strip().lower()
    own_title_low = str(eff_prof.get("title") or "").strip().lower()

    _COMP_BRAND_PATS = (
        r"\bhamilton\s*beach\b", r"\bmr\.?\s*coffee\b", r"\bkitchen\s*aid\b", r"\bkrups\b",
        r"\bcuisinart\b", r"\bbreville\b", r"\bbaratza\b", r"\bbodum\b", r"\bcapresso\b",
        r"\boxo\b", r"\bde'?longhi\b", r"\bninja\b", r"\bkeurig\b", r"\bnespresso\b",
        r"\bblack\s*(?:and|\+|&)?\s*decker\b", r"\boster\b", r"\bkingrinder\b", r"\btimemore\b",
        r"\b1zpresso\b", r"\bfellow\b", r"\bcomandante\b", r"\bhario\b", r"\bporlex\b",
        r"\bjavapresse\b", r"\bmueller\b", r"\bsecura\b", r"\bsecrua\b", r"\bwancle\b",
        r"\bmamba\b", r"\bvevor\b", r"\bnutribullet\b", r"\bmagic\s*bullet\b",
        r"\bvitamix\b", r"\bblendtec\b", r"\bbraun\b", r"\bphilips\b",
        r"\bpanasonic\b", r"\btoastmaster\b", r"\bproctor\s*silex\b", r"\bgevi\b",
        r"\bhibrew\b", r"\bcasabrews\b", r"\bk[246]\b",
    )
    _TITLE_GATED_CLAIM_PATS = (
        r"\bburr\b", r"\bconical\b", r"\bmanual\b", r"\bhand\s+crank\b",
        r"\bcordless\b", r"\brechargeable\b", r"\bbattery\b", r"\busb\b", r"\bportable\b",
        r"\bno\s+plastic\b", r"\bplastic[\s-]+free\b", r"\bzero\s+plastic\b", r"\ball\s+glass\b",
        r"\bceramic\b", r"\btitanium\b", r"\bespresso\b", r"\bturkish\b",
        r"\bk[\s-]*cup\b", r"\bpod\b", r"\bcapsule\b", r"\bcommercial\b", r"\bindustrial\b",
        r"\breplacement\b", r"\bparts?\b",
    )
    _POLICY_BLOCKED_PAT = re.compile(r"\b(?:weed|marijuana|cannabis|tobacco|kratom)\b", re.I)

    def _is_clean_blindspot_term(term_low: str) -> bool:
        if re.fullmatch(r"b0[0-9a-z]{8}", term_low):
            return False
        fw_hit, _, _ = check_global_compliance_firewall(term_low, eff_prof, eff_rules)
        if fw_hit or _POLICY_BLOCKED_PAT.search(term_low):
            return False
        for bp in _COMP_BRAND_PATS:
            if re.search(bp, term_low, re.I) and not (
                (own_brand_low and re.search(bp, own_brand_low, re.I))
                or (own_title_low and re.search(bp, own_title_low, re.I))
            ):
                return False
        if own_title_low:
            for cp in _TITLE_GATED_CLAIM_PATS:
                if re.search(cp, term_low, re.I) and not re.search(cp, own_title_low, re.I):
                    return False
        return True

    synergy_rows = []
    blindspot_candidates = []
    sorted_kws = sorted(xydc_map.keys(), key=lambda k: -xydc_map[k].get("total_traffic", 0.0))

    for kw in sorted_kws:
        perf = kw_perf.get(kw, {"spend": 0.0, "sales": 0.0, "ord": 0, "clk": 0, "camps": set(), "enabled_camps": set(), "bids": []})
        sp = perf["spend"]
        sa = perf["sales"]
        od = perf["ord"]
        ck = perf["clk"]
        cur_bid = min(perf["bids"]) if perf["bids"] else 0.0

        eval_res = evaluate_rank_synergy(
            term=kw,
            xydc_map=xydc_map,
            spend=sp,
            sales=sa,
            orders=od,
            clicks=ck,
            target_acos=target_acos or 25.0,
            current_bid=cur_bid,
            scaled_thresholds=scaled_thresholds,
            stage_mode=stage_mode,
        )

        rk = _compute_rank_deltas(kw, xydc_map, sr_map or {}, entity_memory or {}, today_ref)
        org_rank = eval_res["organic_rank"]
        org_traffic = eval_res["organic_traffic"]
        q_bucket, _, q_strong = classify_query(kw, eff_prof, eff_rules)

        is_tier5_blindspot = (
            len(perf["enabled_camps"]) == 0
            and org_rank is not None
            and (1 <= org_rank <= 45 or (org_rank <= 60 and org_traffic >= 120.0))
            and q_bucket not in ("is_not", "competitor_brand", "asin", "adjacent")
            and not str(q_bucket).startswith("is_not")
            and (q_strong or q_bucket in ("brand", "core_feature", "core_or_title"))
            and _is_clean_blindspot_term(kw.lower())
        )

        syn_tier = eval_res["synergy_tier"]
        syn_tag = eval_res["synergy_tag"]
        syn_recom = eval_res["recommended_value"]
        syn_reason = eval_res["reason"]

        def _fmt_growth_pct(val: float) -> str:
            if val != val or val == float("inf") or val == float("-inf"):
                return "NEW"
            return f"{val * 100.0:+.1f}%"

        if is_tier5_blindspot:
            syn_tier = "Tier 5: 盲区潜力词"
            syn_tag = "【自然盲区·高潜未投】"
            blind_bid = max(0.30, min(0.95, round((anchor_portfolio_cpc * 0.90) / (1.0 + dest_tos / 100.0), 2)))
            eff_tos_b = round(blind_bid * (1.0 + dest_tos / 100.0), 2)
            syn_recom = f"建议拓词至 {harvest_camp} [Exact]，起步基础竞价 ${blind_bid:.2f}（折合顶展出价 ${eff_tos_b:.2f}）"
            syn_reason = (
                f"西柚洞察自然位已达 #{org_rank}（自然流量得分 {org_traffic:.1f}，自然占比 {eval_res['organic_share_pct']:.1f}%），"
                f"属本品强相关核心词（{q_bucket}），但当前全组合无在跑广告投放词覆盖；建议纳入精准仓低风险拓词放大飞轮。"
            )
            blindspot_candidates.append({
                "kw": kw,
                "org_rank": org_rank,
                "sp_rank": eval_res["sp_rank"],
                "aba_rank": rk["aba_rank"],
                "org_traffic": round(org_traffic, 1),
                "ad_traffic": round(eval_res["ad_traffic"], 1),
                "org_share": f"{eval_res['organic_share_pct']:.1f}%",
                "org_growth": _fmt_growth_pct(eval_res["organic_growth"]),
                "bucket": q_bucket,
                "dest_campaign": harvest_camp,
                "suggested_bid": f"${blind_bid:.2f}",
                "eff_tos_bid": f"${eff_tos_b:.2f}",
                "reason": syn_reason,
            })

        org_rank_str = str(org_rank) if org_rank is not None else "--"
        sp_rank_str = str(eval_res["sp_rank"]) if eval_res["sp_rank"] is not None else "--"
        ad_acos_str = fmt_acos_text(acos(sp, sa), sp, sa)
        ad_cvr_str = f"{(od / ck * 100):.1f}%" if ck > 0 else "--"
        ad_cpc_str = f"${(sp / ck):.2f}" if ck > 0 else "--"
        active_camps_list = sorted(perf["enabled_camps"]) if perf["enabled_camps"] else sorted(perf["camps"])
        campaigns_str = "; ".join(active_camps_list[:2]) if active_camps_list else "--"
        if not perf["enabled_camps"] and perf["camps"]:
            campaigns_str = f"[已暂停] {campaigns_str}"

        aba_str = str(rk["aba_rank"]) if rk["aba_rank"] is not None else "--"
        prev_or_str = str(rk["prev_organic_rank"]) if rk["prev_organic_rank"] is not None else "--"
        dor_str = f"{rk['delta_or']:+d}" if rk["delta_or"] is not None else "--"
        prev_sp_str = str(rk["prev_sp_rank"]) if rk["prev_sp_rank"] is not None else "--"
        dsp_str = f"{rk['delta_sp']:+d}" if rk["delta_sp"] is not None else "--"

        synergy_rows.append("\t".join(_sanitize_tsv_cell(x) for x in [
            kw,
            org_rank_str,
            sp_rank_str,
            round(eval_res["organic_traffic"], 1),
            round(eval_res["ad_traffic"], 1),
            round(eval_res["total_traffic"], 1),
            f"{eval_res['organic_share_pct']:.1f}%",
            f"{eval_res['ad_share_pct']:.1f}%",
            campaigns_str,
            round(sp, 2),
            round(sa, 2),
            ad_acos_str,
            od,
            ck,
            ad_cvr_str,
            ad_cpc_str,
            syn_tier,
            syn_tag,
            syn_recom,
            syn_reason,
            aba_str,
            prev_or_str,
            dor_str,
            prev_sp_str,
            dsp_str,
            f"{eval_res['organic_rate'] * 100.0:.1f}%",
            f"{eval_res['ad_rate'] * 100.0:.1f}%",
            _fmt_growth_pct(eval_res["organic_growth"]),
            _fmt_growth_pct(eval_res["ad_growth"]),
        ]))

    headers = (
        "query\torganic_rank\tsp_rank\torganic_traffic\tad_traffic\ttotal_traffic\torganic_share\tad_share\t"
        "active_campaigns\tad_spend\tad_sales\tad_acos\tad_orders\tad_clicks\tad_cvr\tad_cpc\t"
        "synergy_tier\tsynergy_tag\trecommended_value\treason\t"
        "aba_rank\tprev_org_rank\tdelta_org_rank\tprev_sp_rank\tdelta_sp_rank\t"
        "org_acq_rate\tad_acq_rate\torg_growth_pct\tad_growth_pct"
    )
    tsv(dest / "rank_synergy.tsv", headers, synergy_rows)
    tsv(dest / f"rank_synergy_{gen_time_str}.tsv", headers, synergy_rows)

    blindspot_candidates.sort(key=lambda x: (-x["org_traffic"], x["org_rank"]))
    blind_headers = (
        "query\torganic_rank\tsp_rank\taba_rank\torganic_traffic\tad_traffic\torganic_share\t"
        "org_growth_pct\tintent_bucket\trecommended_campaign\tsuggested_base_bid\teffective_tos_bid\treason"
    )
    blind_rows = [
        "\t".join(_sanitize_tsv_cell(c[k]) for k in (
            "kw", "org_rank", "sp_rank", "aba_rank", "org_traffic", "ad_traffic",
            "org_share", "org_growth", "bucket", "dest_campaign", "suggested_bid", "eff_tos_bid", "reason"
        ))
        for c in [
            {
                **cand,
                "sp_rank": cand["sp_rank"] if cand["sp_rank"] is not None else "--",
                "aba_rank": cand["aba_rank"] if cand["aba_rank"] is not None else "--",
            }
            for cand in blindspot_candidates
        ]
    ]
    tsv(dest / "xydc_blindspot_opportunities.tsv", blind_headers, blind_rows)

    if getattr(args, "export_dir", None):
        exp_dir = Path(args.export_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or eff_prof.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        tsv(exp_dir / f"{asin_str}-{country_str}-rank-synergy-{gen_time_str}.tsv", headers, synergy_rows)
        tsv(exp_dir / f"{asin_str}-{country_str}-rank-synergy.tsv", headers, synergy_rows)
        if blind_rows:
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-blindspot-opportunities-{gen_time_str}.tsv", blind_headers, blind_rows)
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-blindspot-opportunities.tsv", blind_headers, blind_rows)

    return synergy_rows, blind_rows


def write_xydc_traffic_and_competitor_tsvs(
    args,
    dest,
    gen_time_str,
    meta,
    profile,
    target_acos,
    targets,
    xydc_traffic,
    camp_lookup=None,
    financial_prof=None,
    traffic_prof=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    src=None,
    asin_campaigns=None,
    scaled_thresholds=None,
    stage_ctx=None,
):
    """Export xydc_traffic_overview.json and xydc_competitor_benchmark.tsv combining LingXing ASIN targets & Auto ASIN clicks with xydc-mcp 7d traffic."""
    asin_str = str((meta or {}).get("asin") or (profile or {}).get("asin") or "UNKNOWN").upper()
    country_str = str((meta or {}).get("country") or "US").upper()
    own_store_asins = {asin_str}
    if src is not None:
        for pa in as_rows(load_json(Path(src) / "product_ads.json")):
            pa_a = str(pa.get("asin") or pa.get("advertised_asin") or "").strip().upper()
            if pa_a:
                own_store_asins.add(pa_a)

    if xydc_traffic and isinstance(xydc_traffic, dict):
        dest.joinpath("xydc_traffic_overview.json").write_text(
            json.dumps(xydc_traffic, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if getattr(args, "export_dir", None):
            exp_dir = Path(args.export_dir)
            exp_dir.mkdir(parents=True, exist_ok=True)
            exp_dir.joinpath(f"{asin_str}-{country_str}-xydc-traffic.json").write_text(
                json.dumps(xydc_traffic, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    comp_traffic_map = (xydc_traffic or {}).get("competitor_traffic_7d") if isinstance(xydc_traffic, dict) else {}
    if not isinstance(comp_traffic_map, dict):
        comp_traffic_map = {}
    self_trf = (xydc_traffic or {}).get("self_traffic_7d") if isinstance(xydc_traffic, dict) else {}
    self_org_score = num((self_trf or {}).get("organic_traffic_score"))

    # Aggregate LingXing competitor ASIN targets
    by_comp: dict[str, dict] = {}
    for r in (targets or []):
        c_info = extract_asin_target_info(r, group_bid_lookup, camp_group_bid_lookup)
        if not c_info:
            continue
        ca = c_info["asin"].upper()
        if ca == asin_str:
            continue
        entry = by_comp.setdefault(ca, {
            "asin": ca,
            "title": c_info.get("title") or "",
            "price": c_info.get("price") or 0.0,
            "stars": c_info.get("stars") or 0.0,
            "reviews": c_info.get("reviews") or 0,
            "spend": 0.0,
            "sales": 0.0,
            "orders": 0,
            "clicks": 0,
            "base_bid": c_info.get("base_bid") or 0.40,
            "campaigns": set(),
            "max_pp": 0.0,
        })
        if not entry["title"] and c_info.get("title"):
            entry["title"] = c_info["title"]
        if entry["price"] <= 0 and num(c_info.get("price")) > 0:
            entry["price"] = num(c_info["price"])
        if entry["stars"] <= 0 and num(c_info.get("stars")) > 0:
            entry["stars"] = num(c_info["stars"])
        if entry["reviews"] <= 0 and int(num(c_info.get("reviews"))) > 0:
            entry["reviews"] = int(num(c_info["reviews"]))
        entry["spend"] += num(c_info.get("spend"))
        entry["sales"] += num(c_info.get("sales"))
        entry["orders"] += int(num(c_info.get("orders")))
        entry["clicks"] += int(num(c_info.get("clicks")))
        if c_info.get("campaign_name"):
            cn = c_info["campaign_name"]
            entry["campaigns"].add(cn)
            if camp_lookup and cn in camp_lookup:
                entry["max_pp"] = max(entry["max_pp"], num(camp_lookup[cn].get("pp")))

    target_covered_pairs = {(ca_k, cn_k) for ca_k, ent_k in by_comp.items() for cn_k in ent_k["campaigns"]}
    if src is not None:
        for sr in as_rows(load_json(Path(src) / "search_terms.json")):
            if skip_total(sr):
                continue
            cn_s = str(sr.get("campaign_name") or "").strip()
            if asin_campaigns and cn_s not in asin_campaigns:
                continue
            q_up = str(sr.get("query") or "").strip().upper()
            if not re.fullmatch(r"B0[0-9A-Z]{8}", q_up) or q_up in own_store_asins:
                continue
            if (q_up, cn_s) in target_covered_pairs:
                continue
            entry = by_comp.setdefault(q_up, {
                "asin": q_up, "title": "", "price": 0.0, "stars": 0.0, "reviews": 0,
                "spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0,
                "base_bid": (camp_group_bid_lookup or {}).get(cn_s, 0.40),
                "campaigns": set(), "max_pp": 0.0,
            })
            entry["spend"] += num(sr.get("spends"))
            entry["sales"] += num(sr.get("sales"))
            entry["orders"] += int(num(sr.get("orders")))
            entry["clicks"] += int(num(sr.get("clicks")))
            if cn_s:
                entry["campaigns"].add(cn_s)
                if camp_lookup and cn_s in camp_lookup:
                    entry["max_pp"] = max(entry["max_pp"], num(camp_lookup[cn_s].get("pp")))

    for ca_x, x_ent in comp_traffic_map.items():
        ca_up = str(ca_x).upper()
        if ca_up not in own_store_asins and ca_up not in by_comp:
            by_comp[ca_up] = {
                "asin": ca_up, "title": "", "price": 0.0, "stars": 0.0, "reviews": 0,
                "spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0,
                "base_bid": 0.40, "campaigns": set(), "max_pp": 0.0,
            }

    if not by_comp:
        return []

    rcfg_comp = (stage_ctx or {}).get("config") or v2p.DEFAULT_RULE_CONFIG
    eval_ta_comp = float((stage_ctx or {}).get("effective_eval_acos") or target_acos or 25.0)
    is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= float(rcfg_comp.get("refund_lv1", 12.0)))
    our_price = num((profile or {}).get("price"))
    our_reviews = int(num((profile or {}).get("reviews")))

    comp_rows = []
    for ca, ent in sorted(by_comp.items(), key=lambda kv: (-kv[1]["spend"], -num((comp_traffic_map.get(kv[0]) or {}).get("total_traffic_score")))):
        if ent["spend"] < 0.5 and ent["orders"] < 1 and ca not in comp_traffic_map:
            continue
        c_acos = round(acos(ent["spend"], ent["sales"]), 1) if ent["sales"] > 0 else (999.0 if ent["spend"] > 0 else 0.0)
        c_cpc = round(ent["spend"] / ent["clicks"], 2) if ent["clicks"] > 0 else 0.0
        eval_input = {
            "asin": ca,
            "price": ent["price"],
            "stars": ent["stars"],
            "reviews": ent["reviews"],
            "spend": ent["spend"],
            "orders": ent["orders"],
            "acos": c_acos,
            "cpc": c_cpc,
            "base_bid": ent["base_bid"],
        }
        act_code, rec_v, rsn_v = evaluate_competitor_target(
            eval_input, profile or {}, eval_ta_comp,
            is_fragile=is_fragile, is_high_refund=is_high_refund, camp_pp=ent["max_pp"],
            scaled_thresholds=scaled_thresholds,
            bid_floor_asin=float(rcfg_comp.get("bid_floor_asin", 0.36)),
            bid_floor_asin_fragile=float(rcfg_comp.get("bid_floor_asin_fragile", 0.42)),
        )
        xt = comp_traffic_map.get(ca) or {}
        c_org_sc = num(xt.get("organic_traffic_score"))
        c_ad_sc = num(xt.get("ad_traffic_score"))
        c_org_sh = f"{num(xt.get('organic_share_pct')):.1f}%" if xt else "--"
        c_org_kw = str(int(num(xt.get("organic_keyword_count")))) if xt else "--"
        c_org_gr = f"{num(xt.get('organic_growth_pct')):+.1f}%" if xt else "--"

        if xt and self_org_score > 0 and c_org_sc > 0:
            ratio = c_org_sc / self_org_score
            if ratio >= 2.5:
                moat_desc = f"流量霸主碾压 ({ratio:.1f}x本品自然流)"
            elif ratio >= 1.2:
                moat_desc = f"竞品自然流领先 ({ratio:.1f}x本品)"
            elif ratio >= 0.7:
                moat_desc = f"自然流势均力敌 ({ratio:.1f}x本品)"
            else:
                moat_desc = f"本品自然流压制 ({ratio:.2f}x本品)"
        elif xt:
            moat_desc = f"竞品自然得分 {c_org_sc:.1f}"
        else:
            moat_desc = "仅领星商品投放对标"

        comp_rows.append("\t".join(_sanitize_tsv_cell(x) for x in [
            ca,
            (ent["title"] or "--")[:70],
            f"${ent['price']:.2f}" if ent["price"] > 0 else "--",
            f"${our_price:.2f}" if our_price > 0 else "--",
            f"{ent['stars']:.1f}" if ent["stars"] > 0 else "--",
            ent["reviews"] if ent["reviews"] > 0 else "--",
            our_reviews,
            round(c_org_sc, 1) if xt else "--",
            round(c_ad_sc, 1) if xt else "--",
            c_org_sh,
            c_org_kw,
            c_org_gr,
            moat_desc,
            round(ent["spend"], 2),
            ent["orders"],
            fmt_acos_text(c_acos, ent["spend"], ent["sales"]),
            "; ".join(sorted(ent["campaigns"])[:2]) if ent["campaigns"] else "--",
            f"{act_code} -> {rec_v} ({rsn_v[:90]})",
        ]))

    comp_headers = (
        "competitor_asin\ttitle\tcomp_price\tour_price\tcomp_stars\tcomp_reviews\tour_reviews\t"
        "xydc_org_score_7d\txydc_ad_score_7d\txydc_org_share_pct\txydc_org_kw_count\txydc_org_growth_pct\t"
        "traffic_moat_vs_self\tad_spend_30d\tad_orders_30d\tad_acos_30d\tactive_campaigns\tstrategic_verdict"
    )
    if comp_rows:
        tsv(dest / "xydc_competitor_benchmark.tsv", comp_headers, comp_rows)
        if getattr(args, "export_dir", None):
            exp_dir = Path(args.export_dir)
            exp_dir.mkdir(parents=True, exist_ok=True)
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-competitor-benchmark-{gen_time_str}.tsv", comp_headers, comp_rows)
            tsv(exp_dir / f"{asin_str}-{country_str}-xydc-competitor-benchmark.tsv", comp_headers, comp_rows)
    return comp_rows


def write_buckets_tsv(bucket_stats, dest):
    """buckets.tsv -- spend and conversion rolled up by intent bucket."""
    b_lines = ["bucket\tspend\tsales\tacos\tord\tclk\tcvr"]
    for b, v in sorted(bucket_stats.items(), key=lambda kv: -kv[1]["spend"]):
        b_lines.append("\t".join(str(x) for x in [
            b, round(v["spend"], 1), round(v["sales"], 1), round(acos(v["spend"], v["sales"]), 1),
            int(v["ord"]), int(v["clk"]),
            round((v["ord"] / v["clk"] * 100) if v["clk"] else 0, 1),
        ]))
    dest.joinpath("buckets.tsv").write_text("\n".join(b_lines), encoding="utf-8")


def write_ngram_outputs(all_neg_queries, bulk_adjustments, dest, known_negatives, ngram_stats, profile, rules, financial_prof=None, target_acos=None, scaled_thresholds=None):
    """ngrams.tsv and bleeding_ngrams.txt, appending TCPA-scaled negative-phrase adjustments."""
    protected_ng_tokens = _compute_protected_ngram_tokens(profile, rules)
    eff_be = num((financial_prof or {}).get("true_all_in_break_even_tacos")) or num((financial_prof or {}).get("break_even_acos")) or target_acos or 25.0
    if eff_be <= 0:
        eff_be = target_acos or 15.0
    st = scaled_thresholds or {}
    ng_min_sp = float(st.get("ngram_bleed_spend", 10.0))
    ng_min_clk = int(st.get("ngram_bleed_clicks", 8))
    ng_ext_sp = max(25.0, float(st.get("fail_test_spend", 20.0)) * 1.25)
    ng_lines = []
    bleeding_ng = []
    negated_1grams = set()
    for ng, stat in sorted(ngram_stats.items(), key=lambda kv: (0 if kv[1]["gram_type"] == "1-gram" else 1, -kv[1]["spend"])):
        sp = stat["spend"]
        sl = stat["sales"]
        od = stat["ord"]
        ck = stat["clk"]
        if sp < 2 and od < 1:
            continue
        cvr_val = round((od / ck * 100) if ck else 0.0, 1)
        cpc_val = round((sp / ck) if ck else 0.0, 2)
        acos_val = round(acos(sp, sl), 1)
        ng_lines.append((sp, "\t".join(str(x) for x in [
            ng, stat["gram_type"], round(sp, 2), round(sl, 2), acos_val,
            int(od), int(ck), cvr_val, cpc_val, len(stat["queries"])
        ])))

        is_zero_ord_bleed = (sp >= ng_min_sp and od == 0 and ck >= ng_min_clk and len(stat["queries"]) >= 2)
        is_extreme_loss_bleed = (od >= 1 and len(stat["queries"]) >= 3 and sp >= ng_ext_sp and acos_val >= max(150.0, eff_be * 3.0))
        if is_zero_ord_bleed or is_extreme_loss_bleed:
            _, _, ng_strong = classify_query(ng, profile, rules)
            ng_tokens = {t for t in re.findall(r"[a-z0-9]+", ng.lower()) if len(t) > 2 and not t.isdigit() and t not in STOP_WORDS}
            is_protected = (
                ng_strong
                or not ng_tokens
                or (stat["gram_type"] == "1-gram" and bool(ng_tokens & protected_ng_tokens))
                or (stat["gram_type"] != "1-gram" and ng_tokens.issubset(protected_ng_tokens))
            )
            if is_protected:
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tord={int(od)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tPROTECTED(core_title_or_positive_token)")
                continue

            if stat["gram_type"] != "1-gram" and (ng_tokens & negated_1grams):
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tord={int(od)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tCOVERED_BY_1GRAM")
                continue

            all_queries_negated = all(q_item.lower() in all_neg_queries for q_item in stat["queries"])
            is_known_neg = is_known_negative(ng, known_negatives)
            if ng.lower() in all_neg_queries or all_queries_negated or is_known_neg:
                bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tord={int(od)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}\tALREADY_NEGATED")
                continue

            if stat["gram_type"] == "1-gram":
                negated_1grams |= ng_tokens
            bleeding_ng.append(f"{ng}\tspend=${sp:.2f}\tclk={int(ck)}\tord={int(od)}\tqueries_count={len(stat['queries'])}\ttype={stat['gram_type']}")
            if od == 0:
                cur_ng_desc = f"[词根词组否定·非单独立词] 累计花费=${sp:.2f}, {int(ck)}点击0订单 (覆盖{len(stat['queries'])}个搜索词)"
                rsn_ng_desc = f"[高耗零单词根词组否定] 词根 '{ng}' 跨 {len(stat['queries'])} 个搜索词累计花费 ${sp:.2f} 且 0 转化，在广泛/自动仓添加词组否定一举止血。"
            else:
                cur_ng_desc = f"[高亏非核心词根否定] 累计花费=${sp:.2f}, {int(od)}单, ACOS={acos_val:.1f}% (覆盖{len(stat['queries'])}个搜索词)"
                rsn_ng_desc = f"[极端亏损非核心词根否定] 词根 '{ng}' 跨 {len(stat['queries'])} 个搜索词累计花费 ${sp:.2f} 仅 {int(od)} 单（ACOS {acos_val:.1f}% >> 全口径保本上限），在广泛/自动仓添加词组否定止血。"
            bulk_adjustments.append({
                "action_type": "Negative_Phrase",
                "campaign": "ALL_NON_EXACT",
                "target_entity": ng,
                "match_type": "negativePhrase",
                "current_value": cur_ng_desc,
                "recommended_value": "Add Negative Phrase to Broad/Auto Campaigns",
                "reason": rsn_ng_desc,
            })

    ng_lines_sorted = [line for _, line in sorted(ng_lines, key=lambda x: -x[0])]
    tsv(
        dest / "ngrams.tsv",
        "ngram\ttype\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tquery_count",
        ng_lines_sorted,
    )
    dest.joinpath("bleeding_ngrams.txt").write_text("\n".join(bleeding_ng), encoding="utf-8")

    return bleeding_ng


def write_cannibalization_tsv(cannibal_map, dest):
    """cannibalization.tsv -- queries competing across two or more DISTINCT campaigns."""
    cannibal_lines = []
    for q_text, occurrences in sorted(cannibal_map.items(), key=lambda kv: -sum(x["spend"] for x in kv[1])):
        distinct_camps = {x["campaign"] for x in occurrences if x.get("campaign")}
        if len(distinct_camps) < 2:
            continue
        total_sp = sum(x["spend"] for x in occurrences)
        total_od = sum(x["orders"] for x in occurrences)
        if total_sp < 2 and total_od == 0:
            continue

        risk = "NORMAL_OVERLAP"
        has_exact = any("exact" in str(x["match"]).lower() or "精准" in x["campaign"] or "exact" in x["campaign"].lower() for x in occurrences)
        has_broad_or_auto = any(
            "broad" in str(x["match"]).lower() or "phrase" in str(x["match"]).lower()
            or "auto" in x["campaign"].lower() or "自动" in x["campaign"]
            or "广泛" in x["campaign"] or "词组" in x["campaign"]
            for x in occurrences
        )
        if has_exact and has_broad_or_auto:
            risk = "HIGH_INTERNAL_COMPETITION"

        detail_strs = [
            f"{x['campaign']}({x['match']}|sp=${x['spend']:.1f}|ord={int(x['orders'])}|cpc=${x['cpc']:.2f})"
            for x in occurrences
        ]
        cannibal_lines.append("\t".join([
            q_text, str(len(distinct_camps)), f"{total_sp:.2f}", str(int(total_od)),
            risk, " ; ".join(detail_strs)
        ]))

    tsv(
        dest / "cannibalization.tsv",
        "query\tcampaign_count\ttotal_spend\ttotal_orders\trisk_level\tdetails",
        cannibal_lines,
    )

    return cannibal_lines


def write_groups_tsv(asin_campaigns, dest, src):
    """groups.tsv -- ad group level performance."""
    groups = [r for r in as_rows(load_json(src / "ad_groups.json")) if not skip_total(r)]
    if asin_campaigns:
        groups = [r for r in groups if r.get("campaign_name") in asin_campaigns]
    g_lines = []
    for r in sorted(groups, key=lambda x: -num(x.get("spends"))):
        if not r.get("campaign_name") and not r.get("name"):
            continue
        sp_g = num(r.get("spends"))
        sl_g = num(r.get("sales"))
        g_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("name") or r.get("ad_group_name"), r.get("state"),
            r.get("default_bid") or r.get("bid"),
            round(sp_g, 2), round(sl_g, 2), round(clean_api_acos(r.get("acos"), sp_g, sl_g), 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(clean_api_rate(r.get("cvr")), 1),
            round(clean_api_rate(r.get("cpc")), 2),
        ]))
    tsv(
        dest / "groups.tsv",
        "campaign\tadgroup\tstate\tdefbid\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        g_lines,
    )


def write_targets_tsv(
    bulk_adjustments,
    camp_lookup,
    dest,
    profile,
    target_acos,
    targets,
    financial_prof=None,
    traffic_prof=None,
    targets_7d=None,
    targets_3d=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    macro_scorecard=None,
    locked_campaigns=None,
    stage_ctx=None,
    scaled_thresholds=None,
):
    """targets.tsv -- product targets with competitor profile, inherited bids, TCPA scaling, and 7d attribution-lag guard."""
    rcfg = (stage_ctx or {}).get("config") or v2p.DEFAULT_RULE_CONFIG
    refund_lv1 = float(rcfg.get("refund_lv1", 12.0))
    as_fl_std = float(rcfg.get("bid_floor_asin", 0.36))
    as_fl_frg = float(rcfg.get("bid_floor_asin_fragile", 0.42))
    is_fragile = bool(traffic_prof and traffic_prof.get("organic_readiness") == "ORGANIC_FRAGILE")
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv1)
    shrinkage_guard = bool(macro_scorecard and macro_scorecard.get("velocity_shrinkage_guard"))
    stage_perms = (stage_ctx or {}).get("permissions") or {}
    zero_ad_cb = bool(
        (financial_prof or {}).get("zero_ad_loss_circuit_breaker")
        or (profile or {}).get("zero_ad_loss_circuit_breaker")
        or (stage_ctx or {}).get("zero_ad_loss_circuit_breaker")
        or stage_perms.get("freeze_expansion")
    )
    block_non_p0_cuts = bool(stage_perms.get("block_non_p0_neg_and_cuts"))
    eval_target_acos = float((stage_ctx or {}).get("effective_eval_acos") or target_acos)
    st_t = scaled_thresholds or v2p.compute_scaled_thresholds(
        tcpa=max(1.0, num((financial_prof or {}).get("avg_price") or profile.get("price") or 25.0) * max(target_acos, 8.0) / 100.0),
        cvr_base_ratio=v2p.compute_portfolio_base_cvr(list((camp_lookup or {}).values()), traffic_prof),
        cfg=rcfg,
    )
    cb_3d_t = float(st_t["cb_bleed_spend_3d"])
    cb_7d_t = float(st_t["cb_bleed_spend_7d"])
    locked_set = set(locked_campaigns or [])

    t7_map = {}
    for t7 in (targets_7d or []):
        if skip_total(t7):
            continue
        ci7 = extract_asin_target_info(t7, group_bid_lookup, camp_group_bid_lookup)
        raw_t7_txt = str(t7.get("targeting_text") or t7.get("expression") or t7.get("keyword_text") or "").strip()
        cn7 = t7.get("campaign_name") or ""
        if ci7 and ci7.get("asin"):
            t7_map[(cn7, ci7["asin"])] = t7
        if raw_t7_txt:
            t7_map[(cn7, raw_t7_txt.lower())] = t7
    t3_map = {}
    for t3 in (targets_3d or []):
        if skip_total(t3):
            continue
        ci3 = extract_asin_target_info(t3, group_bid_lookup, camp_group_bid_lookup)
        raw_t3_txt = str(t3.get("targeting_text") or t3.get("expression") or t3.get("keyword_text") or "").strip()
        cn3 = t3.get("campaign_name") or ""
        if ci3 and ci3.get("asin"):
            t3_map[(cn3, ci3["asin"])] = t3
        if raw_t3_txt:
            t3_map[(cn3, raw_t3_txt.lower())] = t3

    t_lines = []
    for r in sorted(targets, key=lambda x: -num(x.get("spends"))):
        if skip_total(r):
            continue
        spend = num(r.get("spends"))
        if spend < 0.5 and num(r.get("orders")) < 1:
            continue
        c_info = extract_asin_target_info(r, group_bid_lookup, camp_group_bid_lookup)
        comp_str = "--"
        comp_eval = "--"

        is_timing = bool(r.get("is_apply_time"))
        t_base_num = num(r.get("timing_base_value")) if r.get("timing_base_value") is not None else 0.0
        t_base = f"{t_base_num:.2f}" if r.get("timing_base_value") is not None else "--"
        app_t = r.get("applied_templates") or []
        t_name = app_t[0].get("name", "") if app_t else "--"
        timing_status = "分时生效" if is_timing else "未应用"

        c_name_r = r.get("campaign_name") or ""
        raw_txt_r = str(r.get("targeting_text") or r.get("expression") or r.get("keyword_text") or "").strip()
        t7_row = t7_map.get((c_name_r, c_info["asin"]), {}) if c_info else t7_map.get((c_name_r, raw_txt_r.lower()), {})
        t3_row = t3_map.get((c_name_r, c_info["asin"]), {}) if c_info else t3_map.get((c_name_r, raw_txt_r.lower()), {})
        eff_t_bid, is_t_inh, _ = resolve_multi_window_entity_bid(
            r, t7_row, t3_row, group_bid_lookup, camp_group_bid_lookup, fallback=0.40
        )
        if c_info:
            c_info["base_bid"] = eff_t_bid

        sp_7d = round(num(t7_row.get("spends") or t7_row.get("spend") or t7_row.get("cost")), 2) if t7_row else 0.0
        sl_7d = num(t7_row.get("sales")) if t7_row else 0.0
        ord_7d = int(num(t7_row.get("orders"))) if t7_row else 0
        ac_7d = round(clean_api_acos(t7_row.get("acos"), sp_7d, sl_7d), 1) if t7_row else 0.0
        imp_3d = int(num(t3_row.get("impressions"))) if t3_row else 0
        sp_3d = round(num(t3_row.get("spends") or t3_row.get("spend") or t3_row.get("cost")), 2) if t3_row else 0.0
        sl_3d = num(t3_row.get("sales")) if t3_row else 0.0
        ord_3d = int(num(t3_row.get("orders"))) if t3_row else 0
        ac_3d = round(clean_api_acos(t3_row.get("acos"), sp_3d, sl_3d), 1) if t3_row else 0.0

        if c_info:
            comp_str = f"ASIN={c_info['asin']}|P=${c_info['price']:.2f}|R={c_info['stars']}|REV={c_info['reviews']}"
            c_name_t = c_info.get("campaign_name", "")
            camp_t = camp_lookup.get(c_name_t, {})
            camp_pp = num(camp_t.get("pp", 0))

            action_code, rec_val, reason_text = evaluate_competitor_target(
                c_info, profile, eval_target_acos,
                is_fragile=is_fragile, is_high_refund=is_high_refund,
                camp_pp=camp_pp,
                scaled_thresholds=st_t,
                bid_floor_asin=as_fl_std,
                bid_floor_asin_fragile=as_fl_frg,
            )
            comp_eval = action_code
            t_state = str(t3_row.get("state") or t7_row.get("state") or r.get("state") or "enabled").lower().strip()
            camp_state_t = camp_t.get("state", "enabled")

            if camp_state_t == "enabled" and t_state == "enabled":
                cur_base_num = eff_t_bid
                timing_note = f" [领星分时策略运行中: 模板'{t_name}', 当前分时基准值:${t_base}]" if is_timing else ""

                if action_code == "Lower_Bid" and not block_non_p0_cuts:
                    zero_recent = (t7_row and sp_7d <= 0.3 and imp_3d == 0 and sp_3d == 0.0)
                    healthy_7d = (ord_7d >= 2 and acos(sp_7d, sl_7d) <= eval_target_acos)
                    m_rec_t = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", str(rec_val))
                    rec_num_t = float(m_rec_t.group(1)) if m_rec_t else 0.0
                    if is_timing and t_base_num > 0 and rec_num_t >= t_base_num:
                        t_floor = as_fl_frg if (is_fragile or is_high_refund) else as_fl_std
                        rec_num_t = max(t_floor, round(t_base_num - 0.02, 2))
                        rec_val = f"${rec_num_t:.2f}"
                    valid_cut_t = (rec_num_t > 0 and rec_num_t < cur_base_num and (not (is_timing and t_base_num > 0) or rec_num_t < t_base_num))
                    if valid_cut_t and not zero_recent and not healthy_7d and not (shrinkage_guard and c_info["orders"] >= 1):
                        timing_rec = f" 【领星分时联动提示】：绑定分时模板'{t_name}'，建议同步更新分时基准值至 {rec_val}。" if is_timing else ""
                        bulk_adjustments.append({
                            "action_type": "Lower_Bid",
                            "campaign": c_info["campaign_name"],
                            "target_entity": c_info["asin"],
                            "match_type": "asinSameAs",
                            "current_bid_num": cur_base_num,
                            "current_value": f"[竞品ASIN投放]{timing_note} 当前基础竞价=${cur_base_num:.2f}, 30d=${c_info['spend']:.2f}({c_info['orders']}单), 7d=${sp_7d:.2f}({ord_7d}单), CPC=${c_info['cpc']:.2f}, 竞品=${c_info['price']:.2f} vs 本品=${profile.get('price')}",
                            "recommended_value": rec_val,
                            "reason": f"[竞品ASIN投放] {reason_text}{timing_rec}",
                        })
                elif action_code == "Negative_Target" and not block_non_p0_cuts:
                    if not (ord_7d >= 1 and acos(sp_7d, sl_7d) <= eval_target_acos):
                        bulk_adjustments.append({
                            "action_type": "Negative_Target",
                            "campaign": c_info["campaign_name"],
                            "target_entity": c_info["asin"],
                            "match_type": "asinSameAs",
                            "current_bid_num": cur_base_num,
                            "current_value": f"[竞品ASIN投放]{timing_note} 当前基础竞价=${cur_base_num:.2f}, 30d=${c_info['spend']:.2f}({c_info['orders']}单), 竞品=${c_info['price']:.2f}({c_info['reviews']}评) vs 本品=${profile.get('price')}",
                            "recommended_value": f"Add Negative Target (asinSameAs={c_info['asin']})",
                            "reason": f"[竞品ASIN投放止血] {reason_text}",
                        })
                elif not zero_ad_cb and action_code == "Harvest_ASIN" and c_info["acos"] <= eval_target_acos * 0.80 and c_info["campaign_name"] not in locked_set:
                    scale_bid = round(min(1.20, cur_base_num * 1.12), 2)
                    if scale_bid > cur_base_num and not ((sp_7d >= cb_7d_t and ord_7d == 0) or (sp_3d >= cb_3d_t and ord_3d == 0)):
                        timing_rec = f" 【领星分时联动提示】：绑定分时模板'{t_name}'，建议同步更新分时基准值至 ${scale_bid:.2f}。" if is_timing else ""
                        bulk_adjustments.append({
                            "action_type": "Elevate_To_TOS",
                            "campaign": c_info["campaign_name"],
                            "target_entity": c_info["asin"],
                            "match_type": "asinSameAs",
                            "current_bid_num": cur_base_num,
                            "current_value": f"[赢家竞品ASIN放量]{timing_note} 当前基础竞价=${cur_base_num:.2f}, 30d=${c_info['spend']:.2f}({c_info['orders']}单, ACOS={c_info['acos']:.1f}%)",
                            "recommended_value": f"${scale_bid:.2f}",
                            "reason": f"[赢家竞品页扩量] 在竞品 {c_info['asin']} 详情页出单 {c_info['orders']} 单且 ACOS 仅 {c_info['acos']:.1f}%（远优于目标 {target_acos:.0f}%），小幅上调基础竞价 +12% 至 ${scale_bid:.2f} 抢占更高商品页首屏份额。{timing_rec}",
                        })

        def_disp = r.get("default_bid") if num(r.get("default_bid")) > 0 else (eff_t_bid if is_t_inh else "--")
        sales_r = num(r.get("sales"))
        acos_r = clean_api_acos(r.get("acos"), spend, sales_r)
        t_lines.append("\t".join(str(x) for x in [
            r.get("campaign_name"), r.get("exp_type_zh") or r.get("exp_type_text") or r.get("match_type"),
            r.get("targeting_text") or r.get("expression") or r.get("keyword_text"),
            comp_str, comp_eval,
            eff_t_bid, def_disp, r.get("state"),
            timing_status, t_base, t_name,
            round(spend, 2), round(sales_r, 2), round(acos_r, 1),
            int(num(r.get("orders"))), int(num(r.get("clicks"))), round(clean_api_rate(r.get("cvr")), 1),
            round(clean_api_rate(r.get("cpc")), 2),
            sp_7d, ord_7d, ac_7d, sp_3d, ord_3d, ac_3d,
        ]))
    tsv(
        dest / "targets.tsv",
        "campaign\ttype\ttext\tcompetitor_info\tevaluation\tbid\tdefbid\tstate\ttiming_status\ttiming_base_value\ttiming_template\tspend\tsales\tacos\tord\tclk\tcvr\tcpc\tspend_7d\tord_7d\tacos_7d\tspend_3d\tord_3d\tacos_3d",
        t_lines,
    )


def write_negative_tsvs(active_neg_dict, audit_records, dest):
    """active_negatives.tsv, active_negatives_audit.tsv and unnegative_recommendations.tsv."""
    # 10. Output active_negatives.tsv, active_negatives_audit.tsv, unnegative_recommendations.tsv
    active_neg_lines = []
    for (c_name, ql), item in sorted(active_neg_dict.items(), key=lambda kv: -kv[1]["spend"]):
        sp = item["spend"]
        sl = item["sales"]
        od = item["orders"]
        ck = item["clicks"]
        cvr_v = round(od / ck * 100, 1) if ck else 0.0
        cpc_v = round(sp / ck, 2) if ck else 0.0
        acos_v = round(sp / sl * 100, 1) if sl > 0 else (999.0 if sp > 0 else 0.0)
        active_neg_lines.append("\t".join(str(x) for x in [
            item["campaign"], item["query"], item["neg_status"],
            round(sp, 2), round(sl, 2), acos_v, od, ck, cvr_v, cpc_v
        ]))
    tsv(
        dest / "active_negatives.tsv",
        "campaign\tquery\tnegative_status\tspend\tsales\tacos\tord\tclk\tcvr\tcpc",
        active_neg_lines,
    )

    audit_lines = []
    for rec in audit_records:
        audit_lines.append("\t".join(str(x) for x in [
            rec["campaign"], rec["query"], rec["neg_status"], rec["bucket"],
            rec["volume_tier"], rec["searchrank"],
            round(rec["spend"], 2), round(rec["sales"], 2), str(rec["orders"]), str(rec["clicks"]),
            str(rec["cvr"]), str(rec["cpc"]), rec["audit_status"], rec["recommended_action"],
            rec["recommended_value"], rec["reason"]
        ]))
    tsv(
        dest / "active_negatives_audit.tsv",
        "campaign\tquery\tnegative_status\tbucket\tvolume_tier\tsearchrank\tspend\tsales\torders\tclicks\tcvr\tcpc\taudit_status\trecommended_action\trecommended_value\treason",
        audit_lines,
    )

    unneg_lines = []
    for rec in audit_records:
        if rec["audit_status"] in ("MIGRATED_BUT_PAUSED_IN_EXACT", "TRUE_ORPHAN_NEGATIVE"):
            unneg_lines.append("\t".join(str(x) for x in [
                rec["campaign"], rec["query"], rec["volume_tier"], rec["searchrank"],
                round(rec["spend"], 2), str(rec["orders"]), rec["audit_status"],
                rec["recommended_action"], rec["recommended_value"], rec["reason"]
            ]))
    tsv(
        dest / "unnegative_recommendations.tsv",
        "campaign\tquery\tvolume_tier\tsearchrank\tspend\torders\taudit_status\trecommended_action\trecommended_value\treason",
        unneg_lines,
    )


def load_entity_memory(db_root: Path, snap_key: str, legacy_key: str = "") -> dict:
    """Load persistent entity & keyword-rank memory across optimization runs (case-insensitive shop alias compatible)."""
    mem_dir = db_root / "entity_memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    candidates = [f"{snap_key}_memory.json"]
    if legacy_key:
        candidates.append(f"{legacy_key}_memory.json")
    found_mem = _find_case_insensitive_db_file(mem_dir, candidates)
    data = load_json(found_mem) if found_mem else None
    if not isinstance(data, dict):
        return {"entities": {}, "terms": {}}
    data.setdefault("entities", {})
    data.setdefault("terms", {})
    return data


def _compute_rank_deltas(term: str, xydc_map: dict | None, sr_map: dict | None, entity_memory: dict | None, today_str: str) -> dict:
    """Compute organic/SP rank & longitudinal rank deltas (positive delta = climbed closer to #1)."""
    tl = (term or "").lower().strip()
    t_norm = re.sub(r"[^\w\s]", "", tl).strip()
    x_info = (xydc_map or {}).get(tl) or (xydc_map or {}).get(t_norm) or {}
    curr_or = x_info.get("organic_rank")
    curr_sp = x_info.get("sp_rank")
    aba_sr, v_tier = get_searchrank(tl, sr_map or {})

    t_mem = ((entity_memory or {}).get("terms") or {}).get(tl) or {}
    curr_mem_or = t_mem.get("curr_org_rank") if t_mem.get("curr_org_rank") is not None else t_mem.get("organic_rank")
    curr_mem_sp = t_mem.get("curr_sp_rank") if t_mem.get("curr_sp_rank") is not None else t_mem.get("sp_rank")
    prev_mem_or = t_mem.get("prev_org_rank") if t_mem.get("prev_org_rank") is not None else t_mem.get("prev_organic_rank")
    prev_mem_sp = t_mem.get("prev_sp_rank")
    if t_mem.get("last_date") == today_str:
        prev_or = prev_mem_or
        prev_sp = prev_mem_sp
    else:
        prev_or = curr_mem_or
        prev_sp = curr_mem_sp

    delta_or = (int(prev_or) - int(curr_or)) if (prev_or is not None and curr_or is not None) else None
    delta_sp = (int(prev_sp) - int(curr_sp)) if (prev_sp is not None and curr_sp is not None) else None
    return {
        "organic_rank": curr_or,
        "curr_org_rank": curr_or,
        "prev_organic_rank": prev_or,
        "prev_org_rank": prev_or,
        "delta_or": delta_or,
        "sp_rank": curr_sp,
        "curr_sp_rank": curr_sp,
        "prev_sp_rank": prev_sp,
        "delta_sp": delta_sp,
        "aba_rank": aba_sr,
        "volume_tier": v_tier,
    }


def _parse_ledger_entity_triple(l_item: dict) -> tuple[str, str, str]:
    """Extract (campaign, match_type_lower, entity_lower) from a ledger entry."""
    lc = str(l_item.get("campaign") or "").strip()
    ek = str(l_item.get("entity_key") or "")
    parts = ek.split("::")
    mt_low = ""
    ent_txt = ""
    if len(parts) >= 4:
        mt_low = parts[2].lower().strip()
        ent_txt = parts[-1].lower().strip()
    elif len(parts) == 3:
        ent_txt = parts[-1].lower().strip()
    en = str(l_item.get("entity_name") or "").strip()
    if not mt_low:
        m_mt = re.search(r"\[(exact|phrase|broad|asinsameas|target|bidding|placement|budget|strategy)\]", en, re.I)
        if m_mt:
            mt_low = m_mt.group(1).lower().strip()
    if not ent_txt:
        m_q = re.search(r"'([^']+)'", en)
        if m_q:
            ent_txt = m_q.group(1).lower().strip()
        elif "[" in en:
            ent_txt = en.split("[")[0].lower().strip()
        else:
            ent_txt = en.lower().strip()
    return lc, mt_low, ent_txt


def _recommended_pp_ros(c: dict, default_pp: int, default_ros: int, target_acos: float) -> tuple[int, int, str]:
    """Protect profitable PP / ROS placements verified by real placement report instead of blindly zeroing them out."""
    real_p30 = c.get("real_placements_30d") or {}
    real_pp = real_p30.get("PP") or {}
    real_ros = real_p30.get("ROS") or {}
    rec_pp = default_pp
    rec_ros = default_ros
    notes = []

    if real_pp.get("orders", 0) >= 2 and 0 < real_pp.get("acos", 999.0) <= target_acos * 1.15:
        rec_pp = min(int(c.get("pp", 0)), max(default_pp, 25)) if int(c.get("pp", 0)) > 0 else default_pp
        if rec_pp > default_pp:
            notes.append(f"真实展位PP出单{real_pp['orders']}单(ACOS {real_pp['acos']:.1f}%)保留PP={rec_pp}%")
    if real_ros.get("orders", 0) >= 2 and 0 < real_ros.get("acos", 999.0) <= target_acos * 1.15:
        rec_ros = min(int(c.get("ros", 0)), max(default_ros, 25)) if int(c.get("ros", 0)) > 0 else default_ros
        if rec_ros > default_ros:
            notes.append(f"真实展位ROS出单{real_ros['orders']}单(ACOS {real_ros['acos']:.1f}%)保留ROS={rec_ros}%")

    note_str = f"（{'；'.join(notes)}）" if notes else ""
    return rec_pp, rec_ros, note_str


ACTION_PRIORITY_RANK = {
    "Stockout_Conserve_Hold": 100,
    "Low_Inventory_Conserve_Hold": 99,
    "Pause_Keyword": 98,
    "Remove_Negative": 97,
    "Probe_Bid_Recovery": 94,
    "Top_Rank_Cooldown_Hold": 93,
    "Rank_Slide_Protection_Hold": 92,
    "Compensated_Base_Bid_Floor": 91,
    "Anti_Double_Damping_Hold": 90,
    "Update_Bidding_Strategy": 88,
    "Placement_TOS_PP_ROS": 87,
    "Emergency_Placement_Throttle": 86,
    "Placement_TOS": 85,
    "Placement_PP": 84,
    "High_Refund_Throttle_Budget": 83,
    "Archive_Fragmented_Campaign": 82,
    "Lower_Budget": 80,
    "Increase_Budget": 79,
    "Update_Budget": 79,
    "Negative_Target": 78,
    "Negative_Exact": 77,
    "Negative_Phrase": 76,
    "Unpause_Keyword": 75,
    "Anti_Drift_Base_Bid_Throttle": 68,
    "Throttle_TOS_Bid": 66,
    "Lower_Bid": 65,
    "Harvest_To_Exact": 64,
    "Harvest_ASIN": 63,
    "Converge_Harvest_To_Exact": 62,
    "Elevate_To_TOS": 61,
    "Throttle_Cannibalization_Bid": 54,
    "Boost_Ascender_TOS": 53,
    "Dayparting_Budget_Pacing": 45,
    "Dayparting_Boost": 44,
    "Dayparting_Throttle": 43,
}

UPWARD_BID_ACTIONS = {"Elevate_To_TOS", "Boost_Ascender_TOS", "Probe_Bid_Recovery", "Compensated_Base_Bid_Floor"}
DOWNWARD_BID_ACTIONS = {"Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid", "Anti_Drift_Base_Bid_Throttle"}

_INHERITED_PREFIX_RE = re.compile(r"\[待执行继承·连续\d+天\]\s*")
_EXCEPTION_TAG_PREFIX_RE = re.compile(r"\[例外\s*E[1-5][^\]]*\]\s*")
_RECLAMP_NOTE_RE = re.compile(r"（[^（）]*重校准:[^（）]*）")
_LEGACY_DUAL_BRACKET_RE = re.compile(
    r"\[基础竞价[^\]\n]{0,48}?[，,]\s*(?:同步|同时|并)?\s*(?:加注|提高|上调|推高|拉高|降低|压低|下调|削减)(?:该活动)?\s*TOS[^\]\n]*\]"
)
_LEGACY_DUAL_INLINE_RE = re.compile(
    r"[，,;；]\s*(?:并|且|同时|同步)?\s*(?:加注|提高|上调|推高|拉高|降低|压低|下调|削减)(?:该活动)?\s*TOS\s*(?:溢价)?(?:至\s*[\d%~～\-]+)?"
)
_LEGACY_DUAL_SIMUL_RE = re.compile(
    r"(?:同时|同步)\s*(?:加注|提高|上调|推高|拉高|降低|压低|下调|削减)(?:该活动)?\s*TOS\s*(?:溢价)?(?:至\s*[\d%~～\-]+)?"
)
_LEGACY_DUAL_EN_RE = re.compile(
    r"(?:[,;]\s*|\s+)(?:and\s+)?(?:simultaneously\s+)?(?:raise|boost|increase|push|lower|cut|reduce|slash)\s+(?:campaign\s+)?TOS(?:\s+(?:modifier|bid|premium|placement))?(?:\s+to\s+[\d%~-]+)?",
    re.I,
)


def sanitize_inherited_reason(reason: str, action_type: str, effective_rec_val: str = "", strip_reclamp_note: bool = False) -> str:
    """Normalize legacy entity_memory reason strings so inherited rows comply with §5.1 Single-Lever
    and §5.5 campaign-level TOS rules (stripping stale dual-lever TOS clauses, duplicate prefixes, and exception tags)."""
    cleaned = _INHERITED_PREFIX_RE.sub("", str(reason or ""))
    cleaned = _EXCEPTION_TAG_PREFIX_RE.sub("", cleaned)
    if strip_reclamp_note:
        cleaned = _RECLAMP_NOTE_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    act = str(action_type or "").strip()
    is_routine_bid = (
        act in ("Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid",
                "Elevate_To_TOS", "Boost_Ascender_TOS", "Raise_Bid", "Increase_Bid", "Update_Bid")
    )
    if is_routine_bid and cleaned:
        rec_tag = f"至 {effective_rec_val}" if effective_rec_val else ""
        bracket_repl = f"[单杠杆·本期仅调基础竞价{rec_tag}，不同期改 TOS]"
        cleaned = _LEGACY_DUAL_BRACKET_RE.sub(bracket_repl, cleaned)
        cleaned = _LEGACY_DUAL_INLINE_RE.sub("（单杠杆：本期仅调基础竞价，不同期改 TOS）", cleaned)
        cleaned = _LEGACY_DUAL_SIMUL_RE.sub("（单杠杆：本期仅调基础竞价，不同期改 TOS）", cleaned)
        cleaned = _LEGACY_DUAL_EN_RE.sub(" (single-lever: base bid only this cycle, do not change TOS simultaneously)", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def reclamp_inherited_bid_recommendation(
    *,
    action_type: str,
    recommended_value: str,
    anchor_bid: float,
    timing_base: float,
    match_type: str,
    entity_text: str,
    is_fragile: bool,
    is_high_refund: bool,
    od30: int,
    od7: int,
    sp_rank: int | None,
    kw_fl_std: float = 0.25,
    kw_fl_frg: float = 0.30,
    as_fl_std: float = 0.36,
    as_fl_frg: float = 0.42,
    block_shock_recovery: bool = False,
    has_paired_placement: bool = False,
    recent_cpc: float = 0.0,
    camp_tos: float = 0.0,
) -> tuple[str | None, str, str]:
    """Re-clamp legacy inherited bid recommendations in 11.7b against v2 §5.2 step caps,
    v2 bid floors, and SP #1 guard.

    Returns: (reclamped_rec_val_or_None, superseded_reason, reclamp_note)
    """
    m_usd = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", str(recommended_value or ""))
    rec_num = float(m_usd.group(1)) if m_usd else 0.0
    eff_anchor = timing_base if timing_base > 0 else anchor_bid

    use_frg_floor = bool(is_fragile or is_high_refund)
    is_asin_ent = (str(match_type or "").lower().strip() == "asinsameas" or is_asin_term(entity_text))
    bid_floor = (as_fl_frg if is_asin_ent else kw_fl_frg) if use_frg_floor else (as_fl_std if is_asin_ent else kw_fl_std)

    if action_type in DOWNWARD_BID_ACTIONS:
        if rec_num <= 0 or (eff_anchor > 0 and eff_anchor <= rec_num) or (timing_base > 0 and rec_num >= timing_base):
            return None, f"当前基准竞价(${eff_anchor:.2f})已不高于原目标值({recommended_value})，无需再降", ""
        if eff_anchor > 0:
            if eff_anchor <= bid_floor:
                return None, f"当前基础竞价(${eff_anchor:.2f})已触及底价保护线(${bid_floor:.2f})，无需再降", ""
            is_fragile_pillar = bool(is_fragile and (od30 >= 6 or od7 >= 2))
            step_floor = _ceil_cents(eff_anchor * (0.90 if is_fragile_pillar else 0.80))
            min_allowed = max(bid_floor, step_floor)
            if is_fragile_pillar and recent_cpc > 0:
                auction_safe_base = _ceil_cents((recent_cpc * 0.85) / (1.0 + max(0.0, camp_tos) / 100.0))
                if auction_safe_base < eff_anchor:
                    min_allowed = max(min_allowed, auction_safe_base)
            clamped = max(min_allowed, rec_num)
            if clamped >= eff_anchor or (timing_base > 0 and clamped >= timing_base):
                return None, f"当前基准竞价(${eff_anchor:.2f})受 v2 步长/底价(${min_allowed:.2f})保护约束已无可降空间", ""
            if abs(clamped - rec_num) >= 0.005:
                tag = "脆弱品支柱词-10%上限/底价" if is_fragile_pillar else "v2 -20%步长上限/底价"
                return f"${clamped:.2f}", "", f"（{tag}重校准: {recommended_value}→${clamped:.2f}）"
        return None, "", ""

    if action_type in UPWARD_BID_ACTIONS or action_type in ("Raise_Bid", "Increase_Bid"):
        if action_type == "Probe_Bid_Recovery" and block_shock_recovery:
            return None, "当前处于大促后冷静期(POST_COOLDOWN)，屏蔽历史休克回踩建议", ""
        if action_type == "Compensated_Base_Bid_Floor" and not has_paired_placement:
            return None, "所属活动本期未下调展位溢价，撤销孤立历史 E3 托底补偿建议", ""
        if sp_rank == 1 and action_type != "Compensated_Base_Bid_Floor":
            return None, "广告位已居 SP #1 首屏顶展（Defensive_Dominance_Maintain），严禁追加提价，撤销历史提价建议", ""
        if action_type == "Probe_Bid_Recovery" and sp_rank is not None and sp_rank <= 3:
            return None, f"广告位已居首屏黄金位(SP #{sp_rank})，拦截历史休克回踩建议", ""
        if rec_num <= 0 or (eff_anchor > 0 and eff_anchor >= rec_num):
            return None, f"当前基础竞价(${eff_anchor:.2f})已达或高于原建议值({recommended_value})", ""
        if eff_anchor > 0 and action_type not in ("Probe_Bid_Recovery", "Compensated_Base_Bid_Floor"):
            up_cap_mult = 1.15 if is_fragile else 1.20
            max_allowed = _floor_cents(eff_anchor * up_cap_mult)
            clamped_u = min(rec_num, max_allowed)
            if clamped_u <= eff_anchor:
                return None, f"当前基础竞价(${eff_anchor:.2f})在 v2 上调步长上限内已达标，无需再提", ""
            if abs(clamped_u - rec_num) >= 0.005:
                tag_u = "脆弱品+15%步长上限" if is_fragile else "v2 +20%步长上限"
                return f"${clamped_u:.2f}", "", f"（{tag_u}重校准: {recommended_value}→${clamped_u:.2f}）"
        return None, "", ""

    return None, "", ""


def resolve_entity_action_conflicts(
    adjustments: list[dict],
    profile: dict | None = None,
    rules: dict | None = None,
    financial_prof: dict | None = None,
    stage_ctx: dict | None = None,
) -> list[dict]:
    """Deterministic single-winner conflict resolver keyed by (campaign_low, match_type_low, entity_low),
    preserving Negative_Exact + Broad Lower_Bid coexistence while guaranteeing 0 intra-TSV entity conflicts,
    ironclad compliance firewall veto, and STOP_LOSS/estimated-COGS expansion veto.
    """
    zero_ad_cb = bool(
        (financial_prof or {}).get("zero_ad_loss_circuit_breaker")
        or (profile or {}).get("zero_ad_loss_circuit_breaker")
        or (stage_ctx or {}).get("zero_ad_loss_circuit_breaker")
        or ((stage_ctx or {}).get("permissions") or {}).get("freeze_expansion")
    )
    paused_misfit_terms = {
        str(x.get("target_entity") or "").lower().strip()
        for x in adjustments
        if x.get("action_type") == "Pause_Keyword" and x.get("target_entity")
    }
    neg_phrase_roots = {
        str(x.get("target_entity") or "").lower().strip()
        for x in adjustments
        if x.get("action_type") == "Negative_Phrase" and x.get("target_entity")
    }
    archived_camps = {
        str(x.get("campaign") or "").strip()
        for x in adjustments
        if x.get("action_type") == "Archive_Fragmented_Campaign" and x.get("campaign")
    }
    full_placement_camps = {
        str(x.get("campaign") or "").strip()
        for x in adjustments
        if x.get("action_type") == "Placement_TOS_PP_ROS" and x.get("campaign")
    }

    pre_filtered = []
    for item in adjustments:
        act = item.get("action_type", "")
        camp = str(item.get("campaign") or "").strip()
        mt_low = str(item.get("match_type") or "").lower().strip()
        ent_low = str(item.get("target_entity") or "").lower().strip()
        if not act or not camp or not ent_low:
            continue

        # 0a. Ironclad Global Pre-Flight Compliance & Hard Domain Firewall Veto
        if act in POSITIVE_RECOMMENDATION_ACTIONS or re.search(r"(?:Unpause|Harvest|Boost|Elevate|Probe_Bid_Recovery|Recover)", act, re.I):
            fw_hit, _, _ = check_global_compliance_firewall(ent_low, profile, rules)
            if fw_hit:
                continue

        # 0b. STOP_LOSS / Zero-Ad-Loss / Estimated-COGS / Low-Stock Conservative Veto (block ALL expansion/harvest/unpause)
        if zero_ad_cb and act in (
            "Elevate_To_TOS",
            "Boost_Ascender_TOS",
            "Dayparting_Boost",
            "Increase_Budget",
            "Harvest_To_Exact",
            "Harvest_ASIN",
            "Converge_Harvest_To_Exact",
            "Unpause_Keyword",
            "Unpause_Target",
        ):
            continue

        # 1. Cross-campaign misfit veto
        if act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact", "Elevate_To_TOS", "Boost_Ascender_TOS", "Unpause_Keyword"):
            if ent_low in paused_misfit_terms:
                continue
            ent_words = set(re.findall(r"[a-z0-9]+", ent_low))
            if any((nr in ent_words if " " not in nr else nr in ent_low) for nr in neg_phrase_roots):
                continue

        # 2. Archived campaign veto
        if camp in archived_camps and act != "Archive_Fragmented_Campaign":
            continue

        # 3. Campaign placement unification
        if camp in full_placement_camps and act in ("Placement_TOS", "Placement_PP", "Emergency_Placement_Throttle"):
            continue

        pre_filtered.append(item)

    winners_by_key: dict[tuple[str, str, str], dict] = {}
    order_keys: list[tuple[str, str, str]] = []
    for item in pre_filtered:
        camp = str(item.get("campaign") or "").strip()
        mt_low = str(item.get("match_type") or "").lower().strip()
        ent_low = str(item.get("target_entity") or "").lower().strip()
        key = (camp.lower(), mt_low, ent_low)
        prio = ACTION_PRIORITY_RANK.get(item.get("action_type", ""), 50)
        if key not in winners_by_key:
            winners_by_key[key] = dict(item, _prio=prio)
            order_keys.append(key)
        else:
            existing = winners_by_key[key]
            if prio > existing.get("_prio", 50):
                prev_act = existing.get("action_type", "")
                new_winner = dict(item, _prio=prio)
                if prev_act and prev_act != new_winner.get("action_type"):
                    new_winner["reason"] = f"{new_winner.get('reason', '')} [冲突消解: 优先于同实体次级建议 {prev_act}]".strip()
                winners_by_key[key] = new_winner

    resolved = []
    for k in order_keys:
        w = dict(winners_by_key[k])
        w.pop("_prio", None)
        resolved.append(w)
    return resolved


def sync_and_write_entity_full_context(
    *,
    db_root: Path,
    dest: Path,
    snap_key: str,
    gen_date_str: str,
    entity_memory: dict,
    kws: list[dict],
    kws_7d: list[dict],
    kws_3d: list[dict],
    targets: list[dict],
    targets_7d: list[dict],
    targets_3d: list[dict],
    xydc_map: dict | None,
    sr_map: dict | None,
    group_bid_lookup: dict | None,
    camp_group_bid_lookup: dict | None,
    ledger: list[dict],
    deduped_adjustments: list[dict],
    inheritance_records: list[dict] | None = None,
    deferred_queue: list[dict] | None = None,
) -> None:
    """Persist longitudinal rank + three-state recommendation memory (executed vs pending_recommendation)
    to db/entity_memory/ and output entity_full_context.tsv.
    """
    mem_dir = db_root / "entity_memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_file = mem_dir / f"{snap_key}_memory.json"

    old_entities_mem = dict(entity_memory.get("entities") or {})
    old_terms_mem = dict(entity_memory.get("terms") or {})
    entities_mem = dict(old_entities_mem)
    terms_mem = dict(old_terms_mem)

    # Map today's emitted adjustments strictly by (campaign, match_type_low, target_entity_low)
    today_adj_map: dict[tuple[str, str, str], dict] = {}
    for a in (deduped_adjustments or []):
        c_a = str(a.get("campaign") or "").strip()
        m_a = str(a.get("match_type") or "").lower().strip()
        e_a = str(a.get("target_entity") or "").lower().strip()
        if c_a and e_a:
            today_adj_map[(c_a, m_a, e_a)] = a

    # Map inheritance outcomes by (campaign, match_type_low, target_entity_low)
    inheritance_by_key: dict[tuple[str, str, str], dict] = {}
    for irec in (inheritance_records or []):
        ic = str(irec.get("campaign") or "").strip()
        im = str(irec.get("match_type") or "").lower().strip()
        ie = str(irec.get("target_entity") or "").lower().strip()
        if ic and ie:
            inheritance_by_key[(ic, im, ie)] = irec

    # Map latest non-drift ledger event by (campaign, match_type_low, entity_lower)
    ledger_by_ent: dict[tuple[str, str, str], dict] = {}
    for l_item in (ledger or []):
        if "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED" in str(l_item.get("effect_verdict") or ""):
            continue
        lc, mt_l, ent_txt = _parse_ledger_entity_triple(l_item)
        if lc and ent_txt:
            ledger_by_ent[(lc, mt_l, ent_txt)] = l_item
            if not mt_l:
                ledger_by_ent[(lc, "*", ent_txt)] = l_item
            m_asin = re.search(r"(b0[0-9a-z]{8})", ent_txt)
            if m_asin:
                ledger_by_ent[(lc, mt_l or "asinsameas", m_asin.group(1))] = l_item

    def _apply_entity_mem_state(ent_record: dict, cn: str, mt_low: str, tl: str, today_adj: dict | None, l_ev: dict | None, rk: dict | None = None) -> None:
        inh = inheritance_by_key.get((cn, mt_low, tl))
        if l_ev:
            l_dt = str(l_ev.get("change_date") or l_ev.get("changed_at") or l_ev.get("adjusted_at") or "")[:10]
            l_old = l_ev.get("old_val") if l_ev.get("old_val") is not None else l_ev.get("old_value")
            l_new = l_ev.get("new_val") if l_ev.get("new_val") is not None else l_ev.get("new_value")
            ent_record["executed"] = {
                "executed_date": l_dt or gen_date_str,
                "field": str(l_ev.get("field") or ""),
                "old_value": l_old,
                "new_value": l_new,
                "action_type": (inh or {}).get("prior_action") or ent_record.get("last_executed_action_type") or str(l_ev.get("field") or "backend_diff"),
                "effect_verdict": str(l_ev.get("effect_verdict") or ""),
            }
            ent_record["last_executed_date"] = l_dt or gen_date_str
            ent_record["last_executed_action_type"] = ent_record["executed"]["action_type"]
            ent_record["last_executed_value"] = str(l_new)
        elif inh and inh.get("inheritance_status") == "EXECUTED_IN_COOLDOWN":
            ent_record["executed"] = {
                "executed_date": gen_date_str,
                "field": "verified_state",
                "old_value": "",
                "new_value": inh.get("current_backend_value", ""),
                "action_type": inh.get("prior_action", ""),
                "effect_verdict": "EXECUTED_IN_COOLDOWN",
            }
            ent_record["last_executed_date"] = gen_date_str
            ent_record["last_executed_action_type"] = inh.get("prior_action", "")
            ent_record["last_executed_value"] = inh.get("current_backend_value", "")

        if today_adj:
            today_act_type = today_adj["action_type"]
            today_rec_val = today_adj["recommended_value"]
            prev_pend = ent_record.get("pending_recommendation") if isinstance(ent_record.get("pending_recommendation"), dict) else None
            # Status-hold notices (HOLD lever) are NOT executable backend mutations: never store them as
            # `pending_recommendation` or they become immortal unexecutable zombies across cycles!
            if v2p.classify_action_lever(today_act_type) == "HOLD":
                if prev_pend and v2p.classify_action_lever(str(prev_pend.get("action_type") or "")) == "HOLD":
                    ent_record["pending_recommendation"] = None
                elif inh and inh.get("inheritance_status") in ("EXECUTED_IN_COOLDOWN", "SUPERSEDED_BY_DATA"):
                    ent_record["pending_recommendation"] = None
                hist = list(ent_record.get("history") or [])
                if not hist or hist[-1].get("date") != gen_date_str or hist[-1].get("action_type") != today_act_type:
                    h_entry = {
                        "date": gen_date_str,
                        "action_type": today_act_type,
                        "recommended_value": today_rec_val,
                        "status": "HOLD_NOTICE",
                    }
                    if rk:
                        h_entry["organic_rank"] = rk.get("organic_rank")
                        h_entry["sp_rank"] = rk.get("sp_rank")
                    hist.append(h_entry)
                ent_record["history"] = hist[-10:]
                ent_record["last_action_date"] = gen_date_str
                ent_record["last_action_type"] = today_act_type
                ent_record["last_recommended_value"] = today_rec_val
            else:
                first_dt = gen_date_str
                if inh and inh.get("inheritance_status") == "INHERITED_PENDING" and inh.get("first_recommended_date"):
                    first_dt = str(inh["first_recommended_date"])[:10]
                elif prev_pend and prev_pend.get("action_type") == today_act_type and prev_pend.get("first_recommended_date"):
                    first_dt = str(prev_pend["first_recommended_date"])[:10]
                days_pend = int(inh["days_pending"]) if (inh and inh.get("inheritance_status") == "INHERITED_PENDING") else 0
                ent_record["pending_recommendation"] = {
                    "action_type": today_act_type,
                    "recommended_value": today_rec_val,
                    "current_value": today_adj.get("current_value", ""),
                    "current_bid_num": today_adj.get("current_bid_num"),
                    "first_recommended_date": first_dt,
                    "last_recommended_date": gen_date_str,
                    "days_pending": days_pend,
                    "reason": today_adj.get("reason", ""),
                    "lever_type": today_adj.get("lever_type", ""),
                    "planned_path": today_adj.get("planned_path", ""),
                    "expected_effect": today_adj.get("expected_effect", ""),
                    "verify_date": today_adj.get("verify_date", ""),
                    "rollback_rule": today_adj.get("rollback_rule", ""),
                }
                hist = list(ent_record.get("history") or [])
                if not hist or hist[-1].get("date") != gen_date_str or hist[-1].get("action_type") != today_act_type:
                    h_entry = {
                        "date": gen_date_str,
                        "action_type": today_act_type,
                        "recommended_value": today_rec_val,
                        "status": "INHERITED_PENDING" if days_pend > 0 else "RECOMMENDED_PENDING",
                    }
                    if rk:
                        h_entry["organic_rank"] = rk.get("organic_rank")
                        h_entry["sp_rank"] = rk.get("sp_rank")
                    hist.append(h_entry)
                ent_record["history"] = hist[-10:]
                ent_record["last_action_date"] = gen_date_str
                ent_record["last_action_type"] = today_act_type
                ent_record["last_recommended_value"] = today_rec_val
        elif inh and inh.get("inheritance_status") in ("EXECUTED_IN_COOLDOWN", "SUPERSEDED_BY_DATA"):
            ent_record["pending_recommendation"] = None
            ent_record["last_inheritance_resolution"] = {
                "resolved_date": gen_date_str,
                "status": inh["inheritance_status"],
                "prior_action": inh.get("prior_action", ""),
                "prior_recommended_value": inh.get("prior_recommended_value", ""),
                "evidence": inh.get("five_dim_evidence", ""),
            }

    kw_7d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_7d or [])}
    kw_3d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_3d or [])}
    tg_7d_lu = {(t.get("campaign_name") or "", str(t.get("targeting_text") or t.get("expression") or "").strip().lower()): t for t in (targets_7d or [])}
    tg_3d_lu = {(t.get("campaign_name") or "", str(t.get("targeting_text") or t.get("expression") or "").strip().lower()): t for t in (targets_3d or [])}

    ctx_rows = []
    tracked_term_keys: set[str] = set()
    # 1. Process all keywords
    for k in sorted(kws or [], key=lambda r: -num(r.get("spends"))):
        cn = k.get("campaign_name") or ""
        mt = (k.get("match_type") or "").lower().strip()
        txt = (k.get("keyword_text") or k.get("targeting_text") or "").strip()
        tl = txt.lower()
        if not cn or not tl:
            continue
        tracked_term_keys.add(tl)
        sp30 = num(k.get("spends"))
        od30 = int(num(k.get("orders")))
        ac30 = round(clean_api_acos(k.get("acos"), sp30, num(k.get("sales"))), 1)
        k7 = kw_7d_lu.get((cn, mt, tl), {})
        k3 = kw_3d_lu.get((cn, mt, tl), {})
        sp7 = round(num(k7.get("spends") or k7.get("spend") or k7.get("cost")), 2) if k7 else 0.0
        od7 = int(num(k7.get("orders"))) if k7 else 0
        ac7 = round(acos(sp7, num(k7.get("sales"))), 1) if k7 and num(k7.get("sales")) > 0 else (999.0 if sp7 > 0 else 0.0)
        sp3 = round(num(k3.get("spends") or k3.get("spend") or k3.get("cost")), 2) if k3 else 0.0
        od3 = int(num(k3.get("orders"))) if k3 else 0

        eff_bid, _, _ = resolve_multi_window_entity_bid(k, k7, k3, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        st = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()
        timing_st = "分时生效" if k.get("is_apply_time") else "未应用"

        rk = _compute_rank_deltas(tl, xydc_map, sr_map, entity_memory, gen_date_str)
        ekey = f"{cn}::{mt}::{tl}"
        prev_ent = old_entities_mem.get(ekey) or {}
        l_ev = ledger_by_ent.get((cn, mt, tl)) or ledger_by_ent.get((cn, "*", tl))
        prior_act_str = "--"
        if isinstance(prev_ent.get("executed"), dict) and prev_ent["executed"].get("executed_date"):
            ex_d = prev_ent["executed"]
            prior_act_str = f"已执行({ex_d.get('executed_date')}):{ex_d.get('old_value')}->{ex_d.get('new_value')}"
        elif isinstance(prev_ent.get("pending_recommendation"), dict) and prev_ent["pending_recommendation"].get("action_type"):
            pd_d = prev_ent["pending_recommendation"]
            prior_act_str = f"待执行({pd_d.get('first_recommended_date')}):{pd_d.get('action_type')}->{pd_d.get('recommended_value')}"
        elif prev_ent.get("last_action_type"):
            prior_act_str = f"{prev_ent.get('last_action_date', '')}:{prev_ent['last_action_type']}->{prev_ent.get('last_recommended_value', '')}"
        elif l_ev:
            l_dt = str(l_ev.get("change_date") or l_ev.get("changed_at") or l_ev.get("adjusted_at") or "")[:10]
            l_old = l_ev.get("old_val") if l_ev.get("old_val") is not None else l_ev.get("old_value")
            l_new = l_ev.get("new_val") if l_ev.get("new_val") is not None else l_ev.get("new_value")
            prior_act_str = f"{l_dt}:{l_old}->{l_new}({l_ev.get('effect_verdict', '')})"

        today_adj = today_adj_map.get((cn, mt, tl))
        today_act_type = today_adj["action_type"] if today_adj else "--"
        today_rec_val = today_adj["recommended_value"] if today_adj else "--"

        ent_record = dict(prev_ent)
        ent_record.update({
            "campaign": cn,
            "match_type": mt,
            "entity": txt,
            "current_bid": eff_bid,
            "state": st,
            "organic_rank": rk["organic_rank"],
            "sp_rank": rk["sp_rank"],
            "updated_at": gen_date_str,
        })
        _apply_entity_mem_state(ent_record, cn, mt, tl, today_adj, l_ev, rk=rk)
        entities_mem[ekey] = ent_record

        if sp30 >= 1.0 or od30 >= 1 or today_adj or sp7 > 0:
            dor_s = f"{rk['delta_or']:+d}" if rk["delta_or"] is not None else "--"
            dsp_s = f"{rk['delta_sp']:+d}" if rk["delta_sp"] is not None else "--"
            ctx_rows.append("\t".join(str(x) for x in [
                cn, "keyword", mt, txt, st, f"${eff_bid:.2f}", timing_st,
                rk["aba_rank"] if rk["aba_rank"] is not None else "--",
                rk["prev_organic_rank"] if rk["prev_organic_rank"] is not None else "--",
                rk["organic_rank"] if rk["organic_rank"] is not None else "--",
                dor_s,
                rk["prev_sp_rank"] if rk["prev_sp_rank"] is not None else "--",
                rk["sp_rank"] if rk["sp_rank"] is not None else "--",
                dsp_s,
                round(sp30, 2), od30, ac30, sp7, od7, ac7, sp3, od3,
                prior_act_str, today_act_type, today_rec_val,
            ]))

    # 1b. Process all targets (Auto / Competitor ASIN / Category targets)
    for t in sorted(targets or [], key=lambda r: -num(r.get("spends"))):
        if skip_total(t):
            continue
        cn = t.get("campaign_name") or ""
        raw_txt = str(t.get("targeting_text") or t.get("expression") or "").strip()
        raw_tl = raw_txt.lower()
        if not cn or not raw_tl:
            continue
        ci_info = extract_asin_target_info(t, group_bid_lookup, camp_group_bid_lookup)
        txt = ci_info["asin"] if ci_info else raw_txt
        tl = txt.lower()
        mt = "asinsameas" if ci_info else (t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or "target").lower().strip()
        mt_disp = "asinSameAs" if ci_info else mt
        sp30 = num(t.get("spends"))
        od30 = int(num(t.get("orders")))
        ac30 = round(clean_api_acos(t.get("acos"), sp30, num(t.get("sales"))), 1)
        t7 = tg_7d_lu.get((cn, raw_tl), {})
        t3 = tg_3d_lu.get((cn, raw_tl), {})
        sp7 = round(num(t7.get("spends") or t7.get("spend") or t7.get("cost")), 2) if t7 else 0.0
        od7 = int(num(t7.get("orders"))) if t7 else 0
        ac7 = round(acos(sp7, num(t7.get("sales"))), 1) if t7 and num(t7.get("sales")) > 0 else (999.0 if sp7 > 0 else 0.0)
        sp3 = round(num(t3.get("spends") or t3.get("spend") or t3.get("cost")), 2) if t3 else 0.0
        od3 = int(num(t3.get("orders"))) if t3 else 0

        eff_bid, _, _ = resolve_multi_window_entity_bid(t, t7, t3, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
        st = str(t3.get("state") or t7.get("state") or t.get("state") or "enabled").lower().strip()
        timing_st = "分时生效" if t.get("is_apply_time") else "未应用"

        ekey = f"{cn}::{mt}::{tl}"
        prev_ent = old_entities_mem.get(ekey) or old_entities_mem.get(f"{cn}::{mt_disp}::{tl}") or {}
        l_ev = ledger_by_ent.get((cn, mt, tl)) or ledger_by_ent.get((cn, "*", tl)) or ledger_by_ent.get((cn, "*", raw_tl))
        prior_act_str = "--"
        if isinstance(prev_ent.get("executed"), dict) and prev_ent["executed"].get("executed_date"):
            ex_d = prev_ent["executed"]
            prior_act_str = f"已执行({ex_d.get('executed_date')}):{ex_d.get('old_value')}->{ex_d.get('new_value')}"
        elif isinstance(prev_ent.get("pending_recommendation"), dict) and prev_ent["pending_recommendation"].get("action_type"):
            pd_d = prev_ent["pending_recommendation"]
            prior_act_str = f"待执行({pd_d.get('first_recommended_date')}):{pd_d.get('action_type')}->{pd_d.get('recommended_value')}"
        elif prev_ent.get("last_action_type"):
            prior_act_str = f"{prev_ent.get('last_action_date', '')}:{prev_ent['last_action_type']}->{prev_ent.get('last_recommended_value', '')}"
        elif l_ev:
            l_dt = str(l_ev.get("change_date") or l_ev.get("changed_at") or l_ev.get("adjusted_at") or "")[:10]
            l_old = l_ev.get("old_val") if l_ev.get("old_val") is not None else l_ev.get("old_value")
            l_new = l_ev.get("new_val") if l_ev.get("new_val") is not None else l_ev.get("new_value")
            prior_act_str = f"{l_dt}:{l_old}->{l_new}({l_ev.get('effect_verdict', '')})"

        today_adj = today_adj_map.get((cn, mt, tl)) or today_adj_map.get((cn, mt, raw_tl))
        today_act_type = today_adj["action_type"] if today_adj else "--"
        today_rec_val = today_adj["recommended_value"] if today_adj else "--"

        ent_record = dict(prev_ent)
        ent_record.update({
            "campaign": cn,
            "match_type": mt_disp,
            "entity": txt,
            "current_bid": eff_bid,
            "state": st,
            "updated_at": gen_date_str,
        })
        _apply_entity_mem_state(ent_record, cn, mt, tl, today_adj, l_ev)
        entities_mem[ekey] = ent_record

        if sp30 >= 1.0 or od30 >= 1 or today_adj or sp7 > 0:
            ctx_rows.append("\t".join(str(x) for x in [
                cn, "target", mt_disp, txt, st, f"${eff_bid:.2f}", timing_st,
                "--", "--", "--", "--", "--", "--", "--",
                round(sp30, 2), od30, ac30, sp7, od7, ac7, sp3, od3,
                prior_act_str, today_act_type, today_rec_val,
            ]))

    # 2. Persist 100% of emitted adjustments (including search-term Negative_Exact, Harvest_To_Exact, Placement, Strategy, etc.)
    for a in (deduped_adjustments or []):
        cn = str(a.get("campaign") or "").strip()
        mt = str(a.get("match_type") or "").lower().strip()
        txt = str(a.get("target_entity") or "").strip()
        tl = txt.lower()
        if not cn or not tl or mt == "schedule":
            continue
        ekey = f"{cn}::{mt}::{tl}"
        if ekey not in entities_mem or entities_mem[ekey].get("updated_at") != gen_date_str:
            rk = _compute_rank_deltas(tl, xydc_map, sr_map, entity_memory, gen_date_str)
            ent_record = dict(old_entities_mem.get(ekey) or {})
            ent_record.update({
                "campaign": cn,
                "match_type": a.get("match_type", mt),
                "entity": txt,
                "organic_rank": rk["organic_rank"],
                "sp_rank": rk["sp_rank"],
                "updated_at": gen_date_str,
            })
            if a.get("current_bid_num") is not None:
                ent_record["current_bid"] = num(a.get("current_bid_num"))
            l_ev = ledger_by_ent.get((cn, mt, tl)) or ledger_by_ent.get((cn, "*", tl))
            _apply_entity_mem_state(ent_record, cn, mt, tl, a, l_ev, rk=rk)
            entities_mem[ekey] = ent_record
        tracked_term_keys.add(tl)

    # 2b. Persist deferred change-budget/single-lever queue items as pending_recommendation so next cycle inherits them!
    for d_item in (deferred_queue or []):
        if v2p.classify_action_lever(str(d_item.get("action_type") or "")) == "HOLD":
            continue
        cn = str(d_item.get("campaign") or "").strip()
        mt = str(d_item.get("match_type") or "").lower().strip()
        txt = str(d_item.get("target_entity") or "").strip()
        tl = txt.lower()
        if not cn or not tl or mt == "schedule":
            continue
        ekey = f"{cn}::{mt}::{tl}"
        if (cn, mt, tl) in today_adj_map:
            continue
        ent_record = dict(entities_mem.get(ekey) or old_entities_mem.get(ekey) or {})
        ent_record.update({
            "campaign": cn,
            "match_type": d_item.get("match_type", mt),
            "entity": txt,
            "updated_at": gen_date_str,
        })
        prev_pend_d = ent_record.get("pending_recommendation") if isinstance(ent_record.get("pending_recommendation"), dict) else None
        first_dt_d = str(prev_pend_d["first_recommended_date"])[:10] if (prev_pend_d and prev_pend_d.get("first_recommended_date")) else gen_date_str
        ent_record["pending_recommendation"] = {
            "action_type": d_item.get("action_type", ""),
            "recommended_value": d_item.get("recommended_value", ""),
            "current_value": d_item.get("current_value", ""),
            "current_bid_num": d_item.get("current_bid_num"),
            "first_recommended_date": first_dt_d,
            "last_recommended_date": gen_date_str,
            "days_pending": 0,
            "reason": d_item.get("reason", ""),
            "defer_reason": d_item.get("defer_reason", "DEFERRED_CHANGE_BUDGET"),
            "lever_type": d_item.get("lever_type", ""),
            "planned_path": d_item.get("planned_path", ""),
            "expected_effect": d_item.get("expected_effect", ""),
            "verify_date": d_item.get("verify_date", ""),
            "rollback_rule": d_item.get("rollback_rule", ""),
        }
        entities_mem[ekey] = ent_record

    # 2c. Also clear resolved prior pending recommendations for entities not in kws/targets (e.g. search terms / campaign actions)
    for irec in (inheritance_records or []):
        if irec.get("inheritance_status") in ("EXECUTED_IN_COOLDOWN", "SUPERSEDED_BY_DATA"):
            ic = str(irec.get("campaign") or "").strip()
            im = str(irec.get("match_type") or "").lower().strip()
            ie = str(irec.get("target_entity") or "").lower().strip()
            ekey = f"{ic}::{im}::{ie}"
            if ekey in entities_mem and (ic, im, ie) not in today_adj_map:
                entities_mem[ekey]["pending_recommendation"] = None
                entities_mem[ekey]["last_inheritance_resolution"] = {
                    "resolved_date": gen_date_str,
                    "status": irec["inheritance_status"],
                    "prior_action": irec.get("prior_action", ""),
                    "prior_recommended_value": irec.get("prior_recommended_value", ""),
                    "evidence": irec.get("five_dim_evidence", ""),
                }

    # 3. Item 5 Fix: Rolling rank update across ALL xydc_map & tracked terms (NO `if tl not in terms_mem:` freeze!)
    all_rank_terms = set(tracked_term_keys) | {str(kw_x).lower().strip() for kw_x in (xydc_map or {}).keys() if str(kw_x).strip()}
    for tl in all_rank_terms:
        rk = _compute_rank_deltas(tl, xydc_map, sr_map, entity_memory, gen_date_str)
        if rk["organic_rank"] is not None or rk["sp_rank"] is not None or tl in old_terms_mem:
            terms_mem[tl] = {
                "curr_org_rank": rk["organic_rank"],
                "curr_sp_rank": rk["sp_rank"],
                "prev_org_rank": rk["prev_organic_rank"],
                "prev_sp_rank": rk["prev_sp_rank"],
                "organic_rank": rk["organic_rank"],
                "sp_rank": rk["sp_rank"],
                "prev_organic_rank": rk["prev_organic_rank"],
                "aba_rank": rk["aba_rank"],
                "last_date": gen_date_str,
            }

    mem_payload = {"updated_at": datetime.now().isoformat(), "entities": entities_mem, "terms": terms_mem}
    mem_file.write_text(json.dumps(mem_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    tsv(
        dest / "entity_full_context.tsv",
        "campaign\tentity_type\tmatch_type\tentity\tstate\tcurrent_bid\ttiming_status\taba_rank\tprev_org_rank\tcurr_org_rank\tdelta_org_rank\tprev_sp_rank\tcurr_sp_rank\tdelta_sp_rank\tspend_30d\tord_30d\tacos_30d\tspend_7d\tord_7d\tacos_7d\tspend_3d\tord_3d\tprior_action\ttoday_action\ttoday_recommended_value",
        ctx_rows,
    )


def assemble_and_write_adjustments(
    args,
    bulk_adjustments,
    camps,
    dayparting_records,
    dayparting_table_lines,
    dest,
    financial_prof,
    gen_time_str,
    kw_misfit_adjustments,
    locked_campaigns,
    meta,
    profile,
    target_acos,
    traffic_prof,
    unnegative_adjustments,
    kws=None,
    kws_7d=None,
    kws_3d=None,
    xydc_map=None,
    sr_map=None,
    deal_info=None,
    targets=None,
    targets_7d=None,
    targets_3d=None,
    group_bid_lookup=None,
    camp_group_bid_lookup=None,
    macro_scorecard=None,
    entity_memory=None,
    ledger=None,
    rules=None,
    active_neg_dict=None,
    known_negatives=None,
    stage_ctx=None,
    scaled_thresholds=None,
    battlecard=None,
):
    """Merge every adjustment source, apply real-placement, OFF AMAZON & velocity-shrinkage guards,
    enforce Three-State Recommendation Inheritance (EXECUTED_IN_COOLDOWN / INHERITED_PENDING / SUPERSEDED_BY_DATA),
    resolve single-winner per (campaign, match_type, entity), enforce v2 Single-Lever Discipline & Global Change Budget,
    and write bulk_adjustments.tsv, action_plan_details.tsv, next_cycle_queue.tsv, budget_reallocation.tsv & recommendation_inheritance.tsv.
    """
    all_final_adjustments = []
    today_str, today_dt = _derive_gen_date_and_dt(gen_time_str)
    shrinkage_guard = bool(macro_scorecard and macro_scorecard.get("velocity_shrinkage_guard"))
    _, win_end_adj = _parse_window_bounds(meta or {})
    stage_ctx_eff = stage_ctx or v2p.determine_stage_and_targets(
        raw_mode_arg=getattr(args, "mode", None),
        peak_flag=bool(getattr(args, "peak", False)),
        no_peak_flag=bool(getattr(args, "no_peak", False)),
        rules=rules or {},
        profile=profile or {},
        financial_prof=financial_prof or {},
        traffic_prof=traffic_prof or {},
        deal_info=deal_info,
        xydc_map=xydc_map,
        shrinkage_signal=shrinkage_guard,
        explicit_target_acos=getattr(args, "target_acos", None),
        anchor_date_str=win_end_adj or (deal_info or {}).get("anchor_date"),
    )
    zero_ad_cb = bool(
        (financial_prof or {}).get("zero_ad_loss_circuit_breaker")
        or (profile or {}).get("zero_ad_loss_circuit_breaker")
        or stage_ctx_eff.get("zero_ad_loss_circuit_breaker")
    )
    stage_perms = stage_ctx_eff.get("permissions") or {}
    stage_mode = str(stage_ctx_eff.get("stage_mode") or "GROWTH")
    peak_overlay = bool(stage_ctx_eff.get("peak_overlay", False))
    freeze_expansion = bool(zero_ad_cb or stage_perms.get("freeze_expansion"))
    block_non_p0_cuts = bool(stage_perms.get("block_non_p0_neg_and_cuts"))
    eval_target_acos = float(stage_ctx_eff.get("effective_eval_acos") or target_acos)
    rcfg = stage_ctx_eff.get("config") or v2p.DEFAULT_RULE_CONFIG
    st_cfg = scaled_thresholds or v2p.compute_scaled_thresholds(
        tcpa=max(1.0, num((financial_prof or {}).get("avg_price") or profile.get("price") or 25.0) * max(target_acos, 8.0) / 100.0),
        cvr_base_ratio=v2p.compute_portfolio_base_cvr(camps, traffic_prof),
        cfg=rcfg,
    )
    act_bleed_7d = float(st_cfg["active_bleed_spend_7d"])
    act_sp_3d = float(st_cfg["activity_spend_3d"])
    cb_bleed_3d = float(st_cfg["cb_bleed_spend_3d"])
    cb_bleed_7d = float(st_cfg["cb_bleed_spend_7d"])
    severe_bleed_sp = float(st_cfg["severe_bleed_spend"])
    min_over_sp_30d = float(st_cfg["min_over_acos_spend_30d"])
    min_act_sp = float(st_cfg["min_action_spend"])
    fail_test_sp = float(st_cfg["fail_test_spend"])
    off_bleed_sp = float(st_cfg.get("off_amazon_bleed_spend", 15.0))
    cvr_base_ratio = float(st_cfg["cvr_base_ratio"])
    cvr_prior_k = float(st_cfg["cvr_prior_k"])
    kw_fl_std = float(rcfg.get("bid_floor_kw", 0.25))
    kw_fl_frg = float(rcfg.get("bid_floor_kw_fragile", 0.30))
    as_fl_std = float(rcfg.get("bid_floor_asin", 0.36))
    as_fl_frg = float(rcfg.get("bid_floor_asin_fragile", 0.42))
    refund_lv1 = float(rcfg.get("refund_lv1", 12.0))
    eff_cd_days = int(args.cooldown_days) if getattr(args, "cooldown_days", None) is not None else int(rcfg.get("cooldown_days", 7))
    refund_lv2 = float(rcfg.get("refund_lv2", 15.0))
    battle_terms = {str(b.get("keyword") or "").strip().lower() for b in (battlecard or []) if b.get("keyword")}
    true_be_tacos = num((financial_prof or {}).get("true_all_in_break_even_tacos"))
    attribution_lag = bool((traffic_prof or {}).get("attribution_lag"))

    def _step_placement_triple(cur_tos: float, cur_pp: float, cur_ros: float, goal_tos: int, goal_pp: int, goal_ros: int) -> tuple[int, int, int, str]:
        g_tos = min(int(cur_tos), goal_tos) if freeze_expansion else goal_tos
        g_pp = min(int(cur_pp), goal_pp) if freeze_expansion else goal_pp
        g_ros = min(int(cur_ros), goal_ros) if freeze_expansion else goal_ros
        s_tos, p_tos = v2p.calc_placement_step(cur_tos, g_tos)
        s_pp, p_pp = v2p.calc_placement_step(cur_pp, g_pp)
        s_ros, p_ros = v2p.calc_placement_step(cur_ros, g_ros)
        paths = []
        if p_tos:
            paths.append(f"TOS: {p_tos}")
        if p_pp:
            paths.append(f"PP: {p_pp}")
        if p_ros:
            paths.append(f"ROS: {p_ros}")
        return s_tos, s_pp, s_ros, " ; ".join(paths)

    if group_bid_lookup is None or camp_group_bid_lookup is None:
        group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(Path(args.indir))
    camp_base_bid_map = dict(camp_group_bid_lookup or {})

    organic_readiness = traffic_prof.get("organic_readiness", "ORGANIC_BALANCED") if traffic_prof else "ORGANIC_BALANCED"
    is_fragile = (organic_readiness == "ORGANIC_FRAGILE")
    is_high_refund_lv1 = bool(financial_prof and num(financial_prof.get("refund_rate")) >= refund_lv1)
    use_elevated_floor = bool(is_fragile or is_high_refund_lv1)

    # Build fresh 30d/7d/3d keyword & target lookups strictly keyed by (campaign, match_type_low, entity_low)
    kw_7d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_7d or [])}
    kw_3d_lu = {(k.get("campaign_name") or "", (k.get("match_type") or "").lower().strip(), (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()): k for k in (kws_3d or [])}
    kw_30d_by_camp_mt_ent: dict[tuple[str, str, str], dict] = {}
    kw_30d_by_camp_ent_fallback: dict[tuple[str, str], dict] = {}
    for k in (kws or []):
        cn_k = k.get("campaign_name") or ""
        mt_k = (k.get("match_type") or "").lower().strip()
        tl_k = (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if cn_k and tl_k:
            kw_30d_by_camp_mt_ent[(cn_k, mt_k, tl_k)] = k
            if (cn_k, tl_k) not in kw_30d_by_camp_ent_fallback or num(k.get("orders")) > num(kw_30d_by_camp_ent_fallback[(cn_k, tl_k)].get("orders")):
                kw_30d_by_camp_ent_fallback[(cn_k, tl_k)] = k

    tg_7d_lu = {(t.get("campaign_name") or "", str(t.get("targeting_text") or t.get("expression") or "").strip().lower()): t for t in (targets_7d or [])}
    tg_3d_lu = {(t.get("campaign_name") or "", str(t.get("targeting_text") or t.get("expression") or "").strip().lower()): t for t in (targets_3d or [])}
    tg_30d_by_camp_ent: dict[tuple[str, str], dict] = {}
    for t in (targets or []):
        if skip_total(t):
            continue
        cn_t = t.get("campaign_name") or ""
        raw_t = str(t.get("targeting_text") or t.get("expression") or "").strip().lower()
        ci_t = extract_asin_target_info(t, group_bid_lookup, camp_group_bid_lookup)
        if cn_t and raw_t:
            tg_30d_by_camp_ent[(cn_t, raw_t)] = t
        if cn_t and ci_t and ci_t.get("asin"):
            tg_30d_by_camp_ent[(cn_t, ci_t["asin"].lower())] = t

    camp_lookup_all = {c["name"]: c for c in camps if c.get("name")}

    # 11.1 Placement, OFF AMAZON & Bidding Strategy adjustments (with Real-Placement Protection, Spend Floors & Velocity-Shrinkage Guard)
    campaigns_with_placement_change = set()
    camp_new_tos_map = {}
    for c in camps:
        if c["state"] != "enabled":
            continue
        c_assessment = c.get("assessment", "")
        has_camp_min_spend = (c.get("spend_3d", 0) >= act_sp_3d or c.get("spend_7d", 0) >= min_act_sp or c.get("spend", 0) >= min_act_sp)

        c_kws = [k for k in (kws or []) if (k.get("campaign_name") or k.get("campaign")) == c["name"] and str(k.get("state") or "enabled").lower() != "paused"]
        c_targets = [t for t in (targets or []) if not skip_total(t) and (t.get("campaign_name") or t.get("campaign")) == c["name"] and str(t.get("state") or "enabled").lower() != "paused"]

        target_ent_name = "CampaignBaseBid"
        target_ent_match = "bidding"
        cur_ent_bid = camp_base_bid_map.get(c["name"], 0.30)
        sole_timing_base = 0.0
        timing_ent_desc = ""
        timing_ent_rec = ""
        single_ent_type_label = "广告活动基础竞价"
        is_single_target = False

        if len(c_kws) == 1 and not c_targets:
            is_single_target = True
            sole_k = c_kws[0]
            kw_t = sole_k.get("keyword_text") or sole_k.get("text") or c["name"]
            target_ent_name = kw_t
            target_ent_match = sole_k.get("match_type") or "exact"
            sk7 = kw_7d_lu.get((c["name"], target_ent_match.lower().strip(), kw_t.lower().strip()), {})
            sk3 = kw_3d_lu.get((c["name"], target_ent_match.lower().strip(), kw_t.lower().strip()), {})
            cur_ent_bid, _, _ = resolve_multi_window_entity_bid(sole_k, sk7, sk3, group_bid_lookup, camp_group_bid_lookup, fallback=cur_ent_bid)
            single_ent_type_label = f"投放词 {kw_t} [{target_ent_match}]"
            if sole_k.get("is_apply_time"):
                app_t = sole_k.get("applied_templates") or []
                t_name = app_t[0].get("name", "") if app_t else "分时策略"
                t_base = num(sole_k.get("timing_base_value")) if sole_k.get("timing_base_value") is not None else cur_ent_bid
                sole_timing_base = t_base
                timing_ent_desc = f" [领星分时策略运行中: 模板'{t_name}', 当前分时基准值:${t_base:.2f}]"
                timing_ent_rec = f" 【领星分时联动提示】：当前投放词已绑定分时策略'{t_name}'，请同步在领星后台将分时基准竞价(timing_base_value)更新为 ${{rec_val:.2f}}。"
        elif len(c_targets) == 1 and not c_kws:
            is_single_target = True
            sole_t = c_targets[0]
            tg_t = sole_t.get("targeting_text") or sole_t.get("expression") or sole_t.get("keyword_text") or c["name"]
            ci_sole = extract_asin_target_info(sole_t, group_bid_lookup, camp_group_bid_lookup)
            target_ent_name = ci_sole["asin"] if ci_sole else tg_t
            target_ent_match = "asinSameAs" if ci_sole else (sole_t.get("exp_type_zh") or sole_t.get("exp_type_text") or sole_t.get("match_type") or "target")
            st7_r = tg_7d_lu.get((c["name"], tg_t.lower().strip()), {})
            st3_r = tg_3d_lu.get((c["name"], tg_t.lower().strip()), {})
            cur_ent_bid, _, _ = resolve_multi_window_entity_bid(sole_t, st7_r, st3_r, group_bid_lookup, camp_group_bid_lookup, fallback=cur_ent_bid)
            single_ent_type_label = f"投放项 {target_ent_name} [{target_ent_match}]"
            if sole_t.get("is_apply_time"):
                app_t = sole_t.get("applied_templates") or []
                t_name = app_t[0].get("name", "") if app_t else "分时策略"
                t_base = num(sole_t.get("timing_base_value")) if sole_t.get("timing_base_value") is not None else cur_ent_bid
                sole_timing_base = t_base
                timing_ent_desc = f" [领星分时策略运行中: 模板'{t_name}', 当前分时基准值:${t_base:.2f}]"
                timing_ent_rec = f" 【领星分时联动提示】：当前投放项已绑定分时策略'{t_name}'，请同步在领星后台将分时基准竞价(timing_base_value)更新为 ${{rec_val:.2f}}。"

        if c_assessment == "STOCKOUT_SUSPENDED":
            base_val = cur_ent_bid
            all_final_adjustments.append({
                "action_type": "Stockout_Conserve_Hold",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${base_val:.2f}",
                "reason": "Listing断货应急休眠：FBA可售库存已归零，失去购物车并由亚马逊系统停推。严禁在无货状态下盲目提价回踩探底，维持当前基础竞价稳态，等待补货入仓。",
            })
            continue

        if c_assessment == "LOW_INVENTORY_THROTTLE":
            base_val = cur_ent_bid
            dos_val = num(profile.get("days_of_supply")) if profile else 0
            all_final_adjustments.append({
                "action_type": "Low_Inventory_Conserve_Hold",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${base_val:.2f}",
                "reason": f"极低库存告急控流：可售天数仅剩 {dos_val:.0f} 天，维持基础竞价稳态控速，平滑过渡至补货上架防断货断崖。",
            })
            continue

        if c_assessment in ("TOP_POSITION_MARKET_COOLDOWN", "DEAL_TRANSITION_COOLDOWN"):
            base_val = cur_ent_bid
            reason_txt = (
                f"广告位首屏黄金位保护：核心词广告排名高居首屏顶部（SP #{c.get('best_sp_rank', 1)}），近3天曝光变动 ({c.get('delta_imp_pct', 0):+.1f}%) 属于自然大盘冷却，绝非基础竞价过低脱靶。保持基础竞价 ${base_val:.2f} 稳态运行。"
                if c_assessment == "TOP_POSITION_MARKET_COOLDOWN"
                else f"秒杀异动保护：商品近期刚结束秒杀/促销活动，近3天曝光变动 ({c.get('delta_imp_pct', 0):+.1f}%) 属于促销红利消退后的正常均值回归，保持基础竞价 ${base_val:.2f} 稳态运行。"
            )
            t_rec = timing_ent_rec.format(rec_val=base_val) if timing_ent_rec else ""
            hierarchy_note = "【竞价生效明确】：单投放活动中按具体投放项独立竞价执行。" if is_single_target else ""
            all_final_adjustments.append({
                "action_type": "Top_Rank_Cooldown_Hold",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${base_val:.2f}",
                "reason": f"{reason_txt}{t_rec} {hierarchy_note}".strip(),
            })
            continue

        if c_assessment == "CIRCUIT_BREAKER_SHOCK":
            if stage_perms.get("block_shock_recovery"):
                continue
            base_val = cur_ent_bid
            baseline_cpc = c.get("cpc") or 0.50
            current_tos = c.get("tos", 0)
            target_clearing_bid = round(baseline_cpc / (1.0 + (current_tos / 100.0)), 2)
            probe_val = max(round(base_val * 1.15, 2), round(base_val + 0.05, 2))
            if target_clearing_bid > probe_val:
                probe_val = min(target_clearing_bid, round(base_val * 1.60, 2), round(baseline_cpc, 2))
            is_target_camp = any(x in c["name"].lower() for x in ["asin", "竞品", "b0", "target", "同类", "关联"]) or c.get("targeting") == "TARGETING" or bool(c_targets)
            is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv1)
            target_bid_floor = as_fl_frg if (is_fragile or is_high_refund) else as_fl_std
            camp_floor = target_bid_floor if is_target_camp else (kw_fl_frg if (is_fragile or is_high_refund) else kw_fl_std)
            probe_val = max(camp_floor, probe_val)
            t_rec = timing_ent_rec.format(rec_val=probe_val) if timing_ent_rec else ""
            hierarchy_note = "【竞价生效明确】：请直接修改该投放项基础竞价（及领星分时基准值），广告组默认竞价同步对齐。" if is_single_target else ""
            all_final_adjustments.append({
                "action_type": "Probe_Bid_Recovery",
                "campaign": c["name"],
                "target_entity": target_ent_name,
                "match_type": target_ent_match,
                "current_bid_num": base_val,
                "current_value": f"{single_ent_type_label} (当前基础竞价 ${base_val:.2f}){timing_ent_desc}",
                "recommended_value": f"${probe_val:.2f}",
                "reason": f"AI 熔断纠偏：调后休克断流（近3天日均曝光暴跌 {c.get('delta_imp_pct', 0):+.1f}%），基于基线CPC ${baseline_cpc:.2f}与当前TOS {current_tos:.0f}%倒推回提基础竞价至 ${probe_val:.2f} (底价保护 ${camp_floor:.2f}) 探底拉回展示池。{t_rec} {hierarchy_note}".strip(),
            })
            continue

        if c["name"] in locked_campaigns:
            if c_assessment == "CIRCUIT_BREAKER_BLEED":
                if c["tos"] >= 120:
                    step_tos, p_path = v2p.calc_placement_step(c["tos"], 60)
                    p_note_str = f"（多期阶梯路径: TOS {p_path}，单期有效降幅<=30%）" if p_path else ""
                    all_final_adjustments.append({
                        "action_type": "Emergency_Placement_Throttle",
                        "campaign": c["name"],
                        "target_entity": "PlacementTop",
                        "match_type": "placement",
                        "current_value": f"TOS={int(c['tos'])}%",
                        "recommended_value": f"TOS={step_tos}%",
                        "planned_path": f"TOS: {p_path}" if p_path else "",
                        "reason": f"观察期紧急熔断：活动持续失血（花费 ${c['spend']:.1f}, 出单 {c['ord']}），按步长上限下调顶展溢价至 TOS={step_tos}%{p_note_str}。",
                    })
                    campaigns_with_placement_change.add(c["name"])
                    camp_new_tos_map[c["name"]] = float(step_tos)
                elif c["pp"] >= 80:
                    step_pp, p_path = v2p.calc_placement_step(c["pp"], 20)
                    p_note_str = f"（多期阶梯路径: PP {p_path}，单期有效降幅<=30%）" if p_path else ""
                    c_ac_txt = fmt_acos_text(c["acos"], c["spend"], c["ord"])
                    all_final_adjustments.append({
                        "action_type": "Emergency_Placement_Throttle",
                        "campaign": c["name"],
                        "target_entity": "PlacementProductPage",
                        "match_type": "placement",
                        "current_value": f"PP={int(c['pp'])}%",
                        "recommended_value": f"PP={step_pp}%",
                        "planned_path": f"PP: {p_path}" if p_path else "",
                        "reason": f"观察期紧急熔断：商品页 PP 溢价过高持续失血（花费 ${c['spend']:.1f}, ACOS {c_ac_txt}），按步长上限下调至 PP={step_pp}%{p_note_str}。",
                    })
                    campaigns_with_placement_change.add(c["name"])
            continue

        # Item 7b / Iron Law 79: Differentiated OFF AMAZON (站外) Bleed Control
        real_off_30 = (c.get("real_placements_30d") or {}).get("OFF") or {}
        if c.get("off_amazon_status") == "OFF_AMAZON_BLEED" and real_off_30:
            off_sp = real_off_30.get("spend", 0.0)
            off_sh = real_off_30.get("spend_share", 0.0)
            off_od = real_off_30.get("orders", 0)
            off_ac_txt = fmt_acos_text(real_off_30.get("acos", 999.0), off_sp, off_od)
            all_final_adjustments.append({
                "action_type": "Update_Bidding_Strategy",
                "campaign": c["name"],
                "target_entity": "OffAmazonStrategy",
                "match_type": "strategy",
                "current_value": f"{c.get('bidding_strategy', '当前竞价策略')} | OFF AMAZON花费=${off_sp:.2f} ({off_sh:.1f}%, {off_od}单, ACOS={off_ac_txt})",
                "recommended_value": "Minimize Off-Amazon + Dynamic Bids - Down Only",
                "reason": f"[站外OFF AMAZON差异化止血] 单活动站外展位花费 ${off_sp:.2f}（占本活动总花费 {off_sh:.1f}%，{off_od}单，ACOS {off_ac_txt} > 阈值 {eval_target_acos * 1.25:.1f}%）；立即将站外广告预算控制调至最低并切换“动态竞价-仅降低”阻断站外泄洪。",
            })
            if off_sh >= 50.0 and has_camp_min_spend and not shrinkage_guard and not block_non_p0_cuts:
                off_target_name = target_ent_name if is_single_target else "CampaignDefaultBid"
                off_match = target_ent_match if is_single_target else "bidding"
                off_base = cur_ent_bid if is_single_target else camp_base_bid_map.get(c["name"], 0.35)
                is_off_asin = (str(off_match).lower().strip() == "asinsameas" or is_asin_term(off_target_name))
                off_floor = (as_fl_frg if is_off_asin else kw_fl_frg) if use_elevated_floor else (as_fl_std if is_off_asin else kw_fl_std)
                if off_base > off_floor + 0.02:
                    new_off_bid = max(off_floor, _ceil_cents(off_base * 0.85))
                    if sole_timing_base > 0 and new_off_bid >= sole_timing_base:
                        new_off_bid = max(off_floor, round(sole_timing_base - 0.02, 2))
                    if new_off_bid < off_base and (sole_timing_base <= 0 or new_off_bid < sole_timing_base):
                        off_t_rec = timing_ent_rec.format(rec_val=new_off_bid) if timing_ent_rec else ""
                        off_cur_str = f"{single_ent_type_label} (当前基础竞价 ${off_base:.2f}){timing_ent_desc}" if is_single_target else f"${off_base:.2f}"
                        all_final_adjustments.append({
                            "action_type": "Anti_Drift_Base_Bid_Throttle",
                            "campaign": c["name"],
                            "target_entity": off_target_name,
                            "match_type": off_match,
                            "current_bid_num": off_base,
                            "current_value": off_cur_str,
                            "recommended_value": f"${new_off_bid:.2f}",
                            "reason": f"[站外泄洪联动降基础竞价] 该活动 {off_sh:.1f}% 花费流向站外高亏展位（ACOS {off_ac_txt}），同步下调基础竞价 15% 至 ${new_off_bid:.2f} 压缩站外竞拍溢价空间。{off_t_rec}",
                        })

        # Bidding Strategy 2x Amplification Guard (Dynamic Up & Down + High TOS)
        if c_assessment == "UP_AND_DOWN_2X_AMPLIFICATION" or (
            c.get("is_up_and_down")
            and c.get("tos", 0) >= 30
            and c.get("spend", 0) >= min_over_sp_30d
            and (c.get("ord", 0) == 0 or c.get("acos", 0) > eval_target_acos * 1.25)
        ):
            mult_val = 2.0 * (1.0 + c["tos"] / 100.0)
            c_ac_txt = fmt_acos_text(c["acos"], c["spend"], c["ord"])
            all_final_adjustments.append({
                "action_type": "Update_Bidding_Strategy",
                "campaign": c["name"],
                "target_entity": "BiddingStrategy",
                "match_type": "strategy",
                "current_value": f"{c.get('bidding_strategy', '动态竞价-提高和降低')} (TOS={int(c['tos'])}%, 最高叠乘 {mult_val:.2f}x)",
                "recommended_value": "Dynamic Bids - Down Only",
                "reason": f"[竞价策略2倍叠乘止血] 当前开启“提高和降低”叠加 TOS={int(c['tos'])}%，首屏实际最高竞价被放大 {mult_val:.2f} 倍导致高 CPC (${c['cpc']:.2f}) 与高 ACOS ({c_ac_txt})；切换为“动态竞价-仅降低”消除溢价叠乘。",
            })

        # Velocity-Shrinkage Guard: do NOT slash placement modifiers on converting campaigns within acceptable ACOS!
        if shrinkage_guard and c.get("ord", 0) >= 2 and 0 < c.get("acos", 999.0) <= eval_target_acos * 1.35:
            continue

        c_lower = c["name"].lower()
        is_auto = "自动" in c["name"] or c.get("targeting") == "auto" or "auto" in c_lower
        is_broad = "广泛" in c["name"] or "broad" in c_lower
        is_pure_target_camp = (not is_auto and not is_broad and bool(c_targets) and not c_kws)
        is_exact = (not is_pure_target_camp) and ("exact" in c_lower or "精准" in c["name"])
        is_comp = (
            not is_auto
            and not is_broad
            and not is_exact
            and (
                is_pure_target_camp
                or any(x in c_lower for x in ["substitute", "asin", "同类", "关联", "竞品", "comp", "defense"])
                or c.get("targeting") == "targets"
            )
        )
        real_tos_30 = (c.get("real_placements_30d") or {}).get("TOS") or {}
        real_tos_profitable = (real_tos_30.get("orders", 0) >= 3 and 0 < real_tos_30.get("acos", 999.0) <= eval_target_acos * 1.05)

        if c_assessment == "REAL_TOS_BLEED" and not is_auto and not is_broad and not block_non_p0_cuts:
            goal_tos = max(20, int(round(c["tos"] * 0.50 / 5.0) * 5))
            g_pp, g_ros, p_note = _recommended_pp_ros(c, int(min(c["pp"], 15)), int(min(c["ros"], 15)), eval_target_acos)
            new_tos_val, rec_pp, rec_ros, pl_path = _step_placement_triple(c["tos"], c["pp"], c["ros"], goal_tos, g_pp, g_ros)
            path_txt = f"（多期阶梯路径: {pl_path}）" if pl_path else ""
            rt_ac_txt = fmt_acos_text(real_tos_30.get("acos", 0), real_tos_30.get("spend", 0), real_tos_30.get("orders", 0))
            rec_str = f"TOS={new_tos_val}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "planned_path": pl_path,
                    "reason": f"[真实广告位TOS止血] 真实位置报告确认 TOS 花费 ${real_tos_30.get('spend', 0):.2f} ({real_tos_30.get('orders', 0)}单, ACOS {rt_ac_txt}) 严重超标，按单期降幅<=30%步长下调 TOS 至 {new_tos_val}%{path_txt}{p_note}。",
                })
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(new_tos_val)
        elif is_auto and not real_tos_profitable and not block_non_p0_cuts and (c["tos"] >= 100 or c["pp"] >= 30 or c["ros"] >= 30):
            g_pp, g_ros, p_note = _recommended_pp_ros(c, 0, min(int(c["ros"]), 15), eval_target_acos)
            goal_tos = 40 if (is_fragile or shrinkage_guard) else 20
            target_tos, rec_pp, rec_ros, pl_path = _step_placement_triple(c["tos"], c["pp"], c["ros"], goal_tos, g_pp, g_ros)
            path_txt = f"（多期阶梯路径: {pl_path}）" if pl_path else ""
            rec_str = f"TOS={target_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(target_tos)
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "planned_path": pl_path,
                    "reason": f"自动探索仓均衡位置溢价：按单期步长上限下调虚高溢价至 {rec_str}{path_txt}，防范展位漂移至低效商品页失血{p_note}。",
                })
                if not shrinkage_guard and has_camp_min_spend:
                    drift_target_name = target_ent_name if is_single_target else "CampaignDefaultBid"
                    drift_match = target_ent_match if is_single_target else "bidding"
                    base_bid = cur_ent_bid if is_single_target else camp_base_bid_map.get(c["name"], 0.35)
                    drift_floor = kw_fl_frg if use_elevated_floor else kw_fl_std
                    if base_bid > drift_floor + 0.03 and c.get("acos", 999.0) > eval_target_acos:
                        new_base_bid = max(drift_floor, _ceil_cents(base_bid * 0.85))
                        if sole_timing_base > 0 and new_base_bid >= sole_timing_base:
                            new_base_bid = max(drift_floor, round(sole_timing_base - 0.02, 2))
                        if new_base_bid < base_bid and (sole_timing_base <= 0 or new_base_bid < sole_timing_base):
                            drift_rec = timing_ent_rec.format(rec_val=new_base_bid) if timing_ent_rec else ""
                            cur_val_str = f"{single_ent_type_label} (当前基础竞价 ${base_bid:.2f}){timing_ent_desc}" if is_single_target else f"${base_bid:.2f}"
                            all_final_adjustments.append({
                                "action_type": "Anti_Drift_Base_Bid_Throttle",
                                "campaign": c["name"],
                                "target_entity": drift_target_name,
                                "match_type": drift_match,
                                "current_bid_num": base_bid,
                                "current_value": cur_val_str,
                                "recommended_value": f"${new_base_bid:.2f}",
                                "reason": f"自动仓防展位漂移联动控价：削减 TOS 溢价时同步微调基础竞价至 ${new_base_bid:.2f}（降幅15%），防止预算向低效商品页（PP）泄洪。{drift_rec}",
                            })
        elif is_exact and (c["tos"] < 40 or c["pp"] > 15 or c["ros"] > 30):
            if real_tos_30.get("spend", 0) >= off_bleed_sp and (real_tos_30.get("orders", 0) == 0 or real_tos_30.get("acos", 0) > eval_target_acos * 1.6):
                goal_tos = int(c["tos"]) if block_non_p0_cuts else max(20, min(int(c["tos"]), 35))
            elif freeze_expansion:
                goal_tos = int(c["tos"])
            else:
                goal_tos = max(50, int(c["tos"])) if c["tos"] < 40 else int(c["tos"])
            default_exact_ros = min(int(c["ros"]), 10) if (freeze_expansion or c.get("acos", 0) > eval_target_acos) else 10
            g_pp, g_ros, p_note = _recommended_pp_ros(c, 0, default_exact_ros, eval_target_acos)
            if block_non_p0_cuts:
                g_pp = max(int(c["pp"]), g_pp)
                g_ros = max(int(c["ros"]), g_ros)
            target_tos, rec_pp, rec_ros, pl_path = _step_placement_triple(c["tos"], c["pp"], c["ros"], goal_tos, g_pp, g_ros)
            path_txt = f"（多期阶梯路径: {pl_path}）" if pl_path else ""
            rec_str = f"TOS={target_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "planned_path": pl_path,
                    "reason": f"精准收割仓聚焦首屏顶部促转化，同时基于真实广告位表现按单期步长约束非顶展溢价{path_txt}{p_note}。",
                })
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(target_tos)
        elif is_comp and (c["tos"] > 20 or c["pp"] < 20 or c["ros"] > 20):
            real_pp_30 = (c.get("real_placements_30d") or {}).get("PP") or {}
            pp_bleeding = (real_pp_30.get("spend", 0) >= off_bleed_sp and (real_pp_30.get("orders", 0) == 0 or real_pp_30.get("acos", 0) > eval_target_acos * 1.6))
            pp_profitable = (real_pp_30.get("orders", 0) >= 2 and 0 < real_pp_30.get("acos", 999.0) <= eval_target_acos * 1.15)
            if pp_bleeding:
                base_goal_pp = int(c["pp"]) if block_non_p0_cuts else 0
            elif c["pp"] < 20:
                base_goal_pp = 20 if not freeze_expansion else int(c["pp"])
            elif pp_profitable:
                base_goal_pp = int(c["pp"]) if block_non_p0_cuts else int(min(c["pp"], 50))
            else:
                base_goal_pp = int(c["pp"]) if block_non_p0_cuts else int(min(c["pp"], 40))
            base_goal_ros = int(c["ros"]) if block_non_p0_cuts else 0
            goal_pp, goal_ros, p_note = _recommended_pp_ros(c, base_goal_pp, base_goal_ros, eval_target_acos)
            if block_non_p0_cuts:
                goal_pp = max(int(c["pp"]), goal_pp)
                goal_ros = max(int(c["ros"]), goal_ros)
            goal_tos = int(c["tos"]) if block_non_p0_cuts else (int(min(c["tos"], 20)) if real_tos_profitable else 0)
            rec_tos, rec_pp, rec_ros, pl_path = _step_placement_triple(c["tos"], c["pp"], c["ros"], goal_tos, goal_pp, goal_ros)
            path_txt = f"（多期阶梯路径: {pl_path}）" if pl_path else ""
            rec_str = f"TOS={rec_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "planned_path": pl_path,
                    "reason": f"竞品防御仓基于真实广告位表现按步长上限收敛无效溢价{path_txt}，聚焦高性价比商品页拦截{p_note}。",
                })
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(rec_tos)
        elif is_broad and not real_tos_profitable and not block_non_p0_cuts and (c["tos"] >= 120 or c["pp"] >= 40 or c["ros"] >= 40):
            g_pp, g_ros, p_note = _recommended_pp_ros(c, 0, min(int(c["ros"]), 15), eval_target_acos)
            goal_tos = 50 if (is_fragile or shrinkage_guard) else 30
            target_tos, rec_pp, rec_ros, pl_path = _step_placement_triple(c["tos"], c["pp"], c["ros"], goal_tos, g_pp, g_ros)
            path_txt = f"（多期阶梯路径: {pl_path}）" if pl_path else ""
            rec_str = f"TOS={target_tos}% / PP={rec_pp}% / ROS={rec_ros}%"
            cur_str = f"TOS={int(c['tos'])}% / PP={int(c['pp'])}% / ROS={int(c['ros'])}%"
            if rec_str != cur_str:
                campaigns_with_placement_change.add(c["name"])
                camp_new_tos_map[c["name"]] = float(target_tos)
                all_final_adjustments.append({
                    "action_type": "Placement_TOS_PP_ROS",
                    "campaign": c["name"],
                    "target_entity": "CampaignPlacement",
                    "match_type": "placement",
                    "current_value": cur_str,
                    "recommended_value": rec_str,
                    "planned_path": pl_path,
                    "reason": f"广泛拓展仓按步长上限拉平畸形溢价至 {rec_str}{path_txt}，中低TOS网罗长尾词流防CPC虚高{p_note}。",
                })
                if not shrinkage_guard and has_camp_min_spend:
                    drift_target_name = target_ent_name if is_single_target else "CampaignDefaultBid"
                    drift_match = target_ent_match if is_single_target else "bidding"
                    base_bid = cur_ent_bid if is_single_target else camp_base_bid_map.get(c["name"], 0.35)
                    drift_floor = kw_fl_frg if use_elevated_floor else kw_fl_std
                    if base_bid > drift_floor + 0.03 and c.get("acos", 999.0) > eval_target_acos:
                        new_base_bid = max(drift_floor, _ceil_cents(base_bid * 0.85))
                        if sole_timing_base > 0 and new_base_bid >= sole_timing_base:
                            new_base_bid = max(drift_floor, round(sole_timing_base - 0.02, 2))
                        if new_base_bid < base_bid and (sole_timing_base <= 0 or new_base_bid < sole_timing_base):
                            drift_rec = timing_ent_rec.format(rec_val=new_base_bid) if timing_ent_rec else ""
                            cur_val_str = f"{single_ent_type_label} (当前基础竞价 ${base_bid:.2f}){timing_ent_desc}" if is_single_target else f"${base_bid:.2f}"
                            all_final_adjustments.append({
                                "action_type": "Anti_Drift_Base_Bid_Throttle",
                                "campaign": c["name"],
                                "target_entity": drift_target_name,
                                "match_type": drift_match,
                                "current_bid_num": base_bid,
                                "current_value": cur_val_str,
                                "recommended_value": f"${new_base_bid:.2f}",
                                "reason": f"广泛仓防展位漂移联动控价：削减 TOS 溢价时同步微调基础竞价至 ${new_base_bid:.2f}（降幅15%），封堵长尾预算失控泄洪。{drift_rec}",
                            })
        elif c["tos"] >= 150 and not real_tos_profitable and has_camp_min_spend and not block_non_p0_cuts:
            step_tos, p_path = v2p.calc_placement_step(c["tos"], 60)
            p_note_str = f"（多期阶梯路径: TOS {p_path}，单期有效降幅<=30%）" if p_path else ""
            all_final_adjustments.append({
                "action_type": "Placement_TOS",
                "campaign": c["name"],
                "target_entity": "PlacementTop",
                "match_type": "placement",
                "current_value": f"TOS={int(c['tos'])}%",
                "recommended_value": f"TOS={step_tos}%",
                "planned_path": f"TOS: {p_path}" if p_path else "",
                "reason": f"TOS 溢价 >=150% 且顶展投产偏弱，按步长上限阶梯回调至 TOS={step_tos}%{p_note_str} 消除点击成本通胀。",
            })
            campaigns_with_placement_change.add(c["name"])
            camp_new_tos_map[c["name"]] = float(step_tos)
        elif c["pp"] >= 80 and c_assessment == "INEFFICIENT_PP" and has_camp_min_spend and not block_non_p0_cuts:
            step_pp, p_path = v2p.calc_placement_step(c["pp"], 10)
            p_note_str = f"（多期阶梯路径: PP {p_path}，单期有效降幅<=30%）" if p_path else ""
            c_ac_txt = fmt_acos_text(c["acos"], c["spend"], c["ord"])
            all_final_adjustments.append({
                "action_type": "Placement_PP",
                "campaign": c["name"],
                "target_entity": "PlacementProductPage",
                "match_type": "placement",
                "current_value": f"PP={int(c['pp'])}%",
                "recommended_value": f"PP={step_pp}%",
                "planned_path": f"PP: {p_path}" if p_path else "",
                "reason": f"商品页 PP 高耗低效（活动 ACOS={c_ac_txt} vs 目标 {eval_target_acos:.0f}%），按步长上限下调至 PP={step_pp}%{p_note_str} 止血。",
            })
            campaigns_with_placement_change.add(c["name"])

    # 11.2 Keyword misfit pauses
    all_final_adjustments.extend(kw_misfit_adjustments)

    # 11.3 Mistaken negative recovery actions
    all_final_adjustments.extend(unnegative_adjustments)

    # 11.4 Search terms & ASIN targets adjustments with Double-Damping Mutex & Compensated Floor
    downward_bid_actions = {"Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid"}
    filtered_bulk_adjustments = []
    for b_item in bulk_adjustments:
        c_target = b_item.get("campaign", "")
        b_act = b_item.get("action_type", "")
        if c_target in campaigns_with_placement_change and b_act in downward_bid_actions:
            cur_val = b_item.get("current_value", "")
            cur_num = b_item.get("current_bid_num")
            if cur_num is None:
                m = re.search(r"(?:当前基础竞价|当前出价|当前竞价|当前基价=?)\s*\$?([\d.]+)", str(cur_val))
                cur_num = float(m.group(1)) if m else num(cur_val)

            camp_info = camp_lookup_all.get(c_target, {})
            camp_cpc = camp_info.get("cpc", 0.50)
            old_tos = float(camp_info.get("tos", 0.0))
            new_tos = float(camp_new_tos_map.get(c_target, old_tos))
            if new_tos < old_tos:
                is_comp_asin = (str(b_item.get("match_type") or "").lower().strip() == "asinsameas" or is_asin_term(str(b_item.get("target_entity") or "")))
                comp_floor = (as_fl_frg if is_comp_asin else kw_fl_frg) if use_elevated_floor else (as_fl_std if is_comp_asin else kw_fl_std)
                # §5.1 E3 rebalance invariant: compensating base bid for TOS reduction must NEVER increase
                # effective TOS bid above prior level; ASIN product-page targets hold rather than TOS-compensate.
                raw_comp_bid = round(camp_cpc / (1.0 + (new_tos / 100.0)), 2)
                max_rebalance_bid = _floor_cents(cur_num * (1.0 + (old_tos / 100.0)) / (1.0 + (new_tos / 100.0))) if cur_num > 0 else raw_comp_bid
                compensated_floor_bid = max(comp_floor, min(raw_comp_bid, max_rebalance_bid))
                if not is_comp_asin and cur_num > 0 and cur_num < compensated_floor_bid and not freeze_expansion:
                    timing_note = " 建议同步更新领星分时基准值(timing_base_value)。" if "领星分时策略运行中" in str(cur_val) else ""
                    filtered_bulk_adjustments.append({
                        "action_type": "Compensated_Base_Bid_Floor",
                        "campaign": c_target,
                        "target_entity": b_item.get("target_entity", ""),
                        "match_type": b_item.get("match_type", ""),
                        "current_bid_num": cur_num,
                        "current_value": str(cur_val),
                        "recommended_value": f"${compensated_floor_bid:.2f}",
                        "reason": f"【防双重抑制与断流休克】活动 TOS 下调至 {int(new_tos)}%，为防有效竞价跌穿拍卖底线断流，基础竞价托底补偿至 ${compensated_floor_bid:.2f}（拦截原建议 {b_act}）。{timing_note}",
                    })
                else:
                    timing_note = " 保持领星分时基准值不变。" if "领星分时策略运行中" in str(cur_val) else ""
                    filtered_bulk_adjustments.append({
                        "action_type": "Anti_Double_Damping_Hold",
                        "campaign": c_target,
                        "target_entity": b_item.get("target_entity", ""),
                        "match_type": b_item.get("match_type", ""),
                        "current_bid_num": cur_num,
                        "current_value": str(cur_val),
                        "recommended_value": f"${cur_num:.2f}",
                        "reason": f"【双重抑制互斥锁定】本期已将该活动 TOS 下调至 {int(new_tos)}%，基础竞价保持 ${cur_num:.2f} 观察（拦截原建议 {b_act}），严禁同周期既砍溢价又砍基础竞价。{timing_note}",
                    })
                continue
        filtered_bulk_adjustments.append(b_item)

    all_final_adjustments.extend(filtered_bulk_adjustments)

    # 11.45 Fragmented Single-Keyword Campaign Convergence (§7.10 with <21d / hero-exact / battlecard / LAUNCH / PEAK exemptions)
    kw_camp_counts = Counter(k.get("campaign_name") or k.get("campaign") for k in (kws or []))
    exact_prove_camp = find_exact_harvest_campaign(camps, kws or [])
    enabled_exact_texts_in_prove = {
        (k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        for k in (kws or [])
        if (k.get("campaign_name") == exact_prove_camp and (k.get("match_type") or "").lower() == "exact" and str(k.get("state") or "enabled").lower() == "enabled")
    }

    if stage_mode != "LAUNCH" and not peak_overlay and not block_non_p0_cuts:
        for c in camps:
            c_name = c.get("name") or c.get("campaign_name")
            if c.get("state") != "enabled" or c.get("is_shared_multi_asin_campaign"):
                continue
            if c_name == exact_prove_camp or any(x in c_name.lower() for x in ["exact-prove", "hero-exact", "手动-广泛", "自动", "auto", "broad", "research", "asin", "target", "竞品"]):
                continue
            # §7.10 Exemption 1: Newly created campaign < 21 days old
            sd_raw = str(c.get("start_date") or "").strip()[:10]
            if sd_raw:
                try:
                    sd_dt = datetime.strptime(sd_raw, "%Y-%m-%d").date()
                    if (today_dt.date() - sd_dt).days < 21:
                        continue
                except Exception:
                    pass
            # §7.10 Exemption 2: Dedicated hero/battlecard single-keyword exact campaign
            camp_kws = [k for k in (kws or []) if (k.get("campaign_name") or k.get("campaign")) == c_name]
            camp_kw_texts = {(k.get("keyword_text") or k.get("targeting_text") or k.get("text") or "").strip().lower() for k in camp_kws}
            if (battle_terms and (camp_kw_texts & battle_terms)) or v2p.is_dedicated_hero_campaign(c_name, kws or [], battle_terms):
                # Exempt dedicated exact campaigns if they hold a battlecard word OR have proven >=2 orders OR have high traffic/hero-exact naming
                if (battle_terms and (camp_kw_texts & battle_terms)) or int(num(c.get("ord_30d") or c.get("ord"))) >= 2:
                    continue

            kw_cnt = kw_camp_counts.get(c_name, 0)
            sp_30d = num(c.get("spend_30d") or c.get("spend"))
            ord_30d = num(c.get("ord_30d") or c.get("ord"))
            cpc_30d = num(c.get("cpc_30d") or c.get("cpc"))
            if shrinkage_guard and ord_30d >= 1:
                continue

            if kw_cnt in (1, 2) and ord_30d <= 1 and min_act_sp <= sp_30d <= max(35.0, fail_test_sp * 1.5) and c.get("acos", 999.0) > eval_target_acos:
                kw_text = (camp_kws[0].get("keyword_text") or camp_kws[0].get("text") or "").strip() if camp_kws else ""
                target_bid = max(kw_fl_frg if use_elevated_floor else kw_fl_std, round(cpc_30d / 1.40, 2)) if cpc_30d > 0 else 0.35
                fw_hit_conv, _, _ = check_global_compliance_firewall(kw_text, profile, rules)

                if not fw_hit_conv and exact_prove_camp and ord_30d >= 1 and kw_text and kw_text.lower() not in enabled_exact_texts_in_prove:
                    all_final_adjustments.append({
                        "action_type": "Converge_Harvest_To_Exact",
                        "campaign": exact_prove_camp,
                        "target_entity": kw_text,
                        "match_type": "exact",
                        "current_bid_num": target_bid,
                        "current_value": f"孤岛活动 {c_name} (30d=${sp_30d:.2f}, {int(ord_30d)}单, CPC=${cpc_30d:.2f})",
                        "recommended_value": f"${target_bid:.2f}",
                        "reason": f"【碎片化活动收敛·移入主仓】运行>=21天且非核心作战词的单活动孤岛分散预算；将出单词迁移至精准主仓 ({exact_prove_camp}) 统一控费基础竞价 ${target_bid:.2f}。",
                    })

                c_ac_txt = fmt_acos_text(c.get("acos", 0), sp_30d, ord_30d)
                all_final_adjustments.append({
                    "action_type": "Archive_Fragmented_Campaign",
                    "campaign": c_name,
                    "target_entity": c_name,
                    "match_type": "campaign",
                    "current_bid_num": num(c.get("budget")),
                    "current_value": f"日预算 ${num(c.get('budget')):.2f}, 30d=${sp_30d:.2f}, {int(ord_30d)}单, ACOS={c_ac_txt}",
                    "recommended_value": "Archive Campaign",
                    "reason": f"【碎片化低效孤岛归档】活动仅投放 {kw_cnt} 个词且低产高耗（30d仅 {int(ord_30d)} 单, ACOS {c_ac_txt}，已豁免新仓<21天及核心作战词单仓），迁移有效词后归档聚合预算至主仓。",
                })

    # 11.5 Dayparting schedule recommendations (§7.5 60-order sample-size gate)
    total_ad_orders_30d_dp = sum(int(num(c.get("ord"))) for c in camps if c.get("state") == "enabled")
    min_dp_orders = int(rcfg.get("daypart_min_orders_30d", 60))
    dayparting_gate_info = {
        "total_ad_orders_30d": total_ad_orders_30d_dp,
        "min_orders_required": min_dp_orders,
        "passed": bool(total_ad_orders_30d_dp >= min_dp_orders),
    }
    if dayparting_records and dayparting_gate_info["passed"]:
        total_active_budget = sum(num(c.get("budget")) for c in camps if c.get("state") == "enabled")
        total_daily_spend = sum(num(c.get("spend_30d") or c.get("spend")) / 30.0 for c in camps if c.get("state") == "enabled")
        budget_utilization = (total_daily_spend / total_active_budget) if total_active_budget > 0 else 0.0

        morning_peak = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR" and 6 <= r["hour"] <= 11]
        evening_peak = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR" and 15 <= r["hour"] <= 21]
        bleed_hours = [r["hour"] for r in dayparting_records if r["classification"] == "BLEEDING_OFFPEAK_HOUR"]

        if budget_utilization >= 0.80:
            if morning_peak:
                m_str = ", ".join(f"{h:02d}:00" for h in sorted(morning_peak))
                all_final_adjustments.append({
                    "action_type": "Dayparting_Budget_Pacing",
                    "campaign": "Portfolio/All_Active_SP",
                    "target_entity": f"Morning Peak [{m_str}]",
                    "match_type": "schedule",
                    "current_value": "Baseline Budget/Bid (100%)",
                    "recommended_value": "+0% ~ +5% Neutral Hold",
                    "reason": f"【预算早竭防御】：大盘日预算利用率达 {budget_utilization*100:.1f}% (>=80%)；早间保持平价续航，防止下午前打空预算错失晚间高峰。",
                })
            if evening_peak and not freeze_expansion:
                e_str = ", ".join(f"{h:02d}:00" for h in sorted(evening_peak))
                all_final_adjustments.append({
                    "action_type": "Dayparting_Boost",
                    "campaign": "Portfolio/All_Active_SP",
                    "target_entity": f"Evening Prime Peak [{e_str}]",
                    "match_type": "schedule",
                    "current_value": "Baseline Budget/Bid (100%)",
                    "recommended_value": "+15% ~ +20% Bid Schedule",
                    "reason": "晚间高转化黄金窗口（出单密集且投产优异），重点加注 +15%~+20% 抢占首屏红利。",
                })
        elif not freeze_expansion:
            peak_hours = [r["hour"] for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR"]
            if peak_hours:
                peak_str = ", ".join(f"{h:02d}:00" for h in sorted(peak_hours))
                all_final_adjustments.append({
                    "action_type": "Dayparting_Boost",
                    "campaign": "Portfolio/All_Active_SP",
                    "target_entity": f"Peak Hours [{peak_str}]",
                    "match_type": "schedule",
                    "current_value": "Baseline Budget/Bid (100%)",
                    "recommended_value": "+15% ~ +20% Bid Schedule",
                    "reason": "高转化黄金时段，分时策略配置 +15%~+20% 溢价放大首屏单量。",
                })

        if bleed_hours and not block_non_p0_cuts:
            bleed_str = ", ".join(f"{h:02d}:00" for h in sorted(bleed_hours))
            rec_sched = "-15% ~ -20% Bid Throttle" if shrinkage_guard else "-25% ~ -30% Bid Throttle"
            all_final_adjustments.append({
                "action_type": "Dayparting_Throttle",
                "campaign": "Portfolio/All_Active_SP",
                "target_entity": f"Bleeding Hours [{bleed_str}]",
                "match_type": "schedule",
                "current_value": "Baseline Budget/Bid (100%)",
                "recommended_value": rec_sched,
                "reason": f"低转化高耗散时段（仅浏览少购买，30d样本={total_ad_orders_30d_dp}单>={min_dp_orders}单门槛），分时策略降权控费，基础竞价折后底价不低于 ${(kw_fl_frg if use_elevated_floor else kw_fl_std):.2f}。",
            })

    # High Refund Unit-Economics Hard Brake (gated by minimum spend floor!)
    is_high_refund = bool(financial_prof and financial_prof.get("refund_rate", 0) >= refund_lv2)
    if is_high_refund and not shrinkage_guard and not block_non_p0_cuts:
        ref_rate = financial_prof.get("refund_rate", 0)
        for c in camps:
            c_n = c.get("name", "")
            cur_bgt_hr = num(c.get("budget"))
            new_bgt_hr = max(10.0, round(cur_bgt_hr * 0.70, 2))
            has_c_sp = (c.get("spend_3d", 0) >= act_sp_3d or c.get("spend_7d", 0) >= min_act_sp or c.get("spend", 0) >= min_act_sp)
            if c.get("state") == "enabled" and has_c_sp and cur_bgt_hr > 10.5 and new_bgt_hr < cur_bgt_hr - 0.5 and c.get("acos", 0) > eval_target_acos and any(x in c_n.lower() for x in ["自动", "auto", "broad", "广泛", "拓展"]):
                all_final_adjustments.append({
                    "action_type": "High_Refund_Throttle_Budget",
                    "campaign": c_n,
                    "target_entity": "DailyBudget",
                    "match_type": "budget",
                    "current_bid_num": cur_bgt_hr,
                    "current_value": f"日预算 ${cur_bgt_hr:.2f}",
                    "recommended_value": f"${new_bgt_hr:.2f}",
                    "reason": f"【高退款率极限制冷】退款率高达 {ref_rate:.1f}% (>={refund_lv2:.0f}%) 吞噬毛利；压缩超标泛流预算至 ${new_bgt_hr:.2f} (降幅<=30%, 保底$10) 收敛至高转化精准词，同步排查退货主因。",
                })

    # 11.55 Cross-Campaign Budget Reallocation (§7.5)
    campaigns_with_other_levers = {
        str(a.get("campaign") or "").strip()
        for a in all_final_adjustments
        if v2p.classify_action_lever(str(a.get("action_type") or "")) in ("STRATEGY_STATE", "PLACEMENT", "BUDGET")
    }
    dos_cur = num((profile or {}).get("days_of_supply"), 999.0)
    fulf_cur = int(num((profile or {}).get("fulfillable"), 999))
    is_stockout_br = (fulf_cur == 0)
    is_low_inv_br = (0 < dos_cur < v2p.get_effective_low_stock_days(rcfg)) or (0 < fulf_cur <= 25)
    budget_realloc_rows, budget_actions = v2p.build_budget_reallocation(
        camps=camps,
        stage_ctx=stage_ctx_eff,
        shrinkage_guard=shrinkage_guard,
        is_low_inventory=is_low_inv_br,
        is_stockout=is_stockout_br,
        locked_campaigns=locked_campaigns,
        campaigns_with_other_levers=campaigns_with_other_levers,
    )
    all_final_adjustments.extend(budget_actions)
    bgt_headers = "campaign\teval_window\tcurrent_budget\tspend\tsales\torders\tacos\troas\ttime_in_budget_pct\toob_hours\tsuggested_budget_api\trole\trecommended_budget\tdelta_usd\treason"

    # 11.6 Unified Dual-Track Rank Synergy Adjustments (anchored to timing_base_value via resolve_multi_window_entity_bid!)
    if xydc_map and kws:
        for k in kws:
            c_name = k.get("campaign_name") or ""
            camp_obj = camp_lookup_all.get(c_name, {})
            if camp_obj.get("state") != "enabled" or c_name in locked_campaigns:
                continue
            kw_text = (k.get("keyword_text") or k.get("targeting_text") or "").strip()
            mt_k = (k.get("match_type") or "exact").lower().strip()
            tl_k = kw_text.lower()
            if not tl_k:
                continue

            k7 = kw_7d_lu.get((c_name, mt_k, tl_k), {})
            k3 = kw_3d_lu.get((c_name, mt_k, tl_k), {})
            kw_state = str(k3.get("state") or k7.get("state") or k.get("state") or "enabled").lower().strip()
            if kw_state != "enabled":
                continue

            k_spend = num(k.get("spends"))
            k_sales = num(k.get("sales"))
            k_orders = int(num(k.get("orders")))
            k_clicks = int(num(k.get("clicks")))
            # Item 2 Fix: NEVER overwrite timing_base_value with peak-hour k3/k7 bid!
            k_bid, _, _ = resolve_multi_window_entity_bid(k, k7, k3, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)

            sp7 = num(k7.get("spends") or k7.get("spend") or k7.get("cost")) if k7 else 0.0
            od7 = int(num(k7.get("orders"))) if k7 else 0
            ac7 = acos(sp7, num(k7.get("sales"))) if k7 and num(k7.get("sales")) > 0 else (999.0 if sp7 > 0 else 0.0)
            im7 = int(num(k7.get("impressions"))) if k7 else 0
            sp3 = num(k3.get("spends") or k3.get("spend") or k3.get("cost")) if k3 else 0.0
            od3 = int(num(k3.get("orders"))) if k3 else 0
            im3 = int(num(k3.get("impressions"))) if k3 else 0

            is_ded_camp = v2p.is_dedicated_hero_campaign(c_name, kws or [], battle_terms)
            k_cpc_30 = (k_spend / k_clicks) if k_clicks > 0 else num(k.get("cpc"))
            synergy = evaluate_rank_synergy(
                term=kw_text,
                xydc_map=xydc_map,
                spend=k_spend,
                sales=k_sales,
                orders=k_orders,
                clicks=k_clicks,
                target_acos=eval_target_acos,
                current_bid=k_bid,
                scaled_thresholds=st_cfg,
                is_dedicated_camp=is_ded_camp,
                bid_floor=(kw_fl_frg if use_elevated_floor else kw_fl_std),
                stage_mode=stage_mode,
                is_fragile=is_fragile,
                ord_7d=od7,
                cpc=k_cpc_30,
                camp_tos=num(camp_obj.get("tos")),
            )
            act_type = synergy.get("action_type")
            if act_type == "Throttle_Cannibalization_Bid":
                if shrinkage_guard or block_non_p0_cuts or c_name in campaigns_with_placement_change:
                    continue
                if (im3 == 0 and sp7 <= 0.5 and im7 == 0) or (od7 >= 2 and 0 < ac7 <= eval_target_acos * 1.05):
                    continue
                if k_spend < min_over_sp_30d and sp7 < min_act_sp and sp3 < act_sp_3d:
                    continue
            elif act_type == "Boost_Ascender_TOS":
                fw_syn, _, _ = check_global_compliance_firewall(kw_text, profile, rules)
                if freeze_expansion or fw_syn or c_name in campaigns_with_placement_change:
                    continue
                if (sp7 >= cb_bleed_7d and od7 == 0) or (sp3 >= cb_bleed_3d and od3 == 0) or (od7 >= 1 and ac7 > eval_target_acos * 1.35):
                    continue
            else:
                continue

            timing_note = ""
            if k.get("is_apply_time"):
                app_t = k.get("applied_templates") or []
                tn = app_t[0].get("name", "分时策略") if app_t else "分时策略"
                timing_note = f" 【领星分时联动提示】：已绑定分时模板'{tn}'，请同步更新领星分时基准值(timing_base_value)。"

            all_final_adjustments.append({
                "action_type": act_type,
                "campaign": c_name,
                "target_entity": kw_text,
                "match_type": mt_k,
                "current_bid_num": k_bid,
                "current_value": f"当前基础竞价 ${k_bid:.2f} (自然位#{synergy['organic_rank']}, 广告位#{synergy['sp_rank'] or '--'}, 30d=${k_spend:.1f}/{k_orders}单, 7d=${sp7:.1f}/{od7}单)",
                "recommended_value": synergy["recommended_value"],
                "reason": f"西柚洞察双轨协同：{synergy['reason']}{timing_note}",
            })

    # 11.7 Three-State Recommendation Inheritance State Machine & Full-Context Entity Memory Gate (Item 4)
    entities_mem = (entity_memory or {}).get("entities") or {}
    recent_ledger_by_ent: dict[tuple[str, str, str], dict] = {}
    recent_ledger_by_camp_field: dict[tuple[str, str], dict] = {}
    for l_item in (ledger or []):
        if "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED" in str(l_item.get("effect_verdict") or ""):
            continue
        lc, mt_l, ent_txt = _parse_ledger_entity_triple(l_item)
        fld_l = str(l_item.get("field") or "").lower().strip()
        if lc and fld_l:
            recent_ledger_by_camp_field[(lc, fld_l)] = l_item
        if lc and ent_txt:
            recent_ledger_by_ent[(lc, mt_l, ent_txt)] = l_item
            if not mt_l:
                recent_ledger_by_ent[(lc, "*", ent_txt)] = l_item
            m_asin = re.search(r"(b0[0-9a-z]{8})", ent_txt)
            if m_asin:
                recent_ledger_by_ent[(lc, mt_l or "asinsameas", m_asin.group(1))] = l_item

    active_neg_pairs_set = {(str(cn_n).lower(), str(q_n).lower()) for (cn_n, q_n) in (active_neg_dict or {}).keys()}
    known_neg_set = {str(x).lower().strip() for x in (known_negatives or set())}

    def _get_live_entity_snapshot(camp_s: str, mt_s: str, ent_s: str) -> dict:
        c_obj = camp_lookup_all.get(camp_s) or {}
        c_st = c_obj.get("state", "enabled") if c_obj else ("enabled" if camp_s in ("ALL_NON_EXACT", "Portfolio/All_Active_SP") else "unknown")
        oob_hrs_s = num(c_obj.get("avg_over_budget_time"))
        tib_s = num(c_obj.get("pct_time_in_budget", 100.0))
        kw30 = kw_30d_by_camp_mt_ent.get((camp_s, mt_s, ent_s))
        k7 = kw_7d_lu.get((camp_s, mt_s, ent_s)) or {}
        k3 = kw_3d_lu.get((camp_s, mt_s, ent_s)) or {}
        if kw30:
            ebid, _, _ = resolve_multi_window_entity_bid(kw30, k7, k3, group_bid_lookup, camp_group_bid_lookup, fallback=0.35)
            est = str(k3.get("state") or k7.get("state") or kw30.get("state") or "enabled").lower().strip()
            sp30, sl30, od30 = num(kw30.get("spends")), num(kw30.get("sales")), int(num(kw30.get("orders")))
            clk30 = int(num(kw30.get("clicks")))
            cpc30 = (sp30 / clk30) if clk30 > 0 else num(kw30.get("cpc"))
            ac30 = acos(sp30, sl30)
            sp7, sl7, od7 = num(k7.get("spends") or k7.get("spend") or k7.get("cost")), num(k7.get("sales")), int(num(k7.get("orders")))
            ac7 = acos(sp7, sl7)
            sp3, sl3, od3 = num(k3.get("spends") or k3.get("spend") or k3.get("cost")), num(k3.get("sales")), int(num(k3.get("orders")))
            ac3 = acos(sp3, sl3)
            im3 = int(num(k3.get("impressions")))
            t_base_live = num(kw30.get("timing_base_value")) if kw30.get("is_apply_time") else 0.0
            return {
                "kind": "keyword", "exists": True, "camp_state": c_st, "ent_state": est, "bid": ebid, "timing_base": t_base_live,
                "sp30": sp30, "od30": od30, "ac30": ac30, "cpc30": cpc30, "sp7": sp7, "od7": od7, "ac7": ac7, "sp3": sp3, "od3": od3, "ac3": ac3, "im3": im3,
                "oob_hrs": oob_hrs_s, "tib_pct": tib_s,
            }
        tg30 = tg_30d_by_camp_ent.get((camp_s, ent_s))
        if tg30:
            raw_tk = str(tg30.get("targeting_text") or tg30.get("expression") or "").strip().lower()
            t7 = tg_7d_lu.get((camp_s, raw_tk)) or {}
            t3 = tg_3d_lu.get((camp_s, raw_tk)) or {}
            ebid, _, _ = resolve_multi_window_entity_bid(tg30, t7, t3, group_bid_lookup, camp_group_bid_lookup, fallback=0.40)
            est = str(t3.get("state") or t7.get("state") or tg30.get("state") or "enabled").lower().strip()
            sp30, sl30, od30 = num(tg30.get("spends")), num(tg30.get("sales")), int(num(tg30.get("orders")))
            clk30 = int(num(tg30.get("clicks")))
            cpc30 = (sp30 / clk30) if clk30 > 0 else num(tg30.get("cpc"))
            ac30 = acos(sp30, sl30)
            sp7, sl7, od7 = num(t7.get("spends") or t7.get("spend") or t7.get("cost")), num(t7.get("sales")), int(num(t7.get("orders")))
            ac7 = acos(sp7, sl7)
            sp3, sl3, od3 = num(t3.get("spends") or t3.get("spend") or t3.get("cost")), num(t3.get("sales")), int(num(t3.get("orders")))
            ac3 = acos(sp3, sl3)
            im3 = int(num(t3.get("impressions")))
            t_base_live = num(tg30.get("timing_base_value")) if tg30.get("is_apply_time") else 0.0
            return {
                "kind": "target", "exists": True, "camp_state": c_st, "ent_state": est, "bid": ebid, "timing_base": t_base_live,
                "sp30": sp30, "od30": od30, "ac30": ac30, "cpc30": cpc30, "sp7": sp7, "od7": od7, "ac7": ac7, "sp3": sp3, "od3": od3, "ac3": ac3, "im3": im3,
                "oob_hrs": oob_hrs_s, "tib_pct": tib_s,
            }
        return {
            "kind": "other", "exists": False, "camp_state": c_st, "ent_state": c_st,
            "bid": camp_base_bid_map.get(camp_s, 0.0), "timing_base": 0.0,
            "sp30": num(c_obj.get("spend")), "od30": int(num(c_obj.get("ord"))), "ac30": num(c_obj.get("acos")), "cpc30": num(c_obj.get("cpc")),
            "sp7": num(c_obj.get("spend_7d")), "od7": int(num(c_obj.get("ord_7d"))), "ac7": num(c_obj.get("acos_7d")),
            "sp3": num(c_obj.get("spend_3d")), "od3": int(num(c_obj.get("ord_3d"))), "ac3": num(c_obj.get("acos_3d")),
            "im3": int(num(c_obj.get("imp_3d"))), "oob_hrs": oob_hrs_s, "tib_pct": tib_s,
        }

    def _extract_prior_pending_and_executed(prev_e: dict, camp_s: str, mt_s: str, ent_s: str, live_snap: dict, l_rec: dict | None) -> tuple[dict | None, dict | None]:
        """Strictly separate `executed` from `pending_recommendation` (including migrating legacy memory files)."""
        exec_rec = prev_e.get("executed") if isinstance(prev_e.get("executed"), dict) else None
        pend_rec = prev_e.get("pending_recommendation") if isinstance(prev_e.get("pending_recommendation"), dict) else None

        # Legacy migration: if old memory recorded last_action_type without `executed`/`pending_recommendation` split
        if pend_rec is None and exec_rec is None and prev_e.get("last_action_type") and prev_e.get("last_action_date"):
            ld_s = str(prev_e.get("last_action_date") or "")[:10]
            if ld_s and ld_s < today_str:
                pend_rec = {
                    "action_type": str(prev_e.get("last_action_type") or ""),
                    "recommended_value": str(prev_e.get("last_recommended_value") or ""),
                    "current_bid_num": prev_e.get("current_bid"),
                    "first_recommended_date": ld_s,
                    "last_recommended_date": ld_s,
                }

        # Check if `pend_rec` has since been executed in backend!
        if pend_rec and str(pend_rec.get("first_recommended_date") or "")[:10] < today_str:
            p_act = str(pend_rec.get("action_type") or "")
            p_rec_val = str(pend_rec.get("recommended_value") or "")
            p_first_dt = str(pend_rec.get("first_recommended_date") or "")[:10]
            backend_executed = False
            exec_val_desc = ""

            if l_rec:
                l_dt = str(l_rec.get("change_date") or l_rec.get("changed_at") or l_rec.get("adjusted_at") or "")[:10]
                if l_dt and l_dt >= p_first_dt:
                    backend_executed = True
                    exec_val_desc = f"ledger:{l_rec.get('old_val')}->{l_rec.get('new_val')}"

            # Campaign-level ledger diff matching (tos/pp/ros/budget/bidding_strategy)
            if not backend_executed:
                camp_flds_chk: tuple[str, ...] = ()
                if p_act in ("Placement_TOS_PP_ROS", "Placement_TOS", "Placement_PP", "Placement_ROS", "Emergency_Placement_Throttle"):
                    camp_flds_chk = ("tos", "pp", "ros")
                elif p_act in ("Lower_Budget", "Increase_Budget", "Update_Budget", "High_Refund_Throttle_Budget"):
                    camp_flds_chk = ("budget",)
                elif p_act == "Update_Bidding_Strategy":
                    camp_flds_chk = ("bidding_strategy", "strategy")
                for cf_k in camp_flds_chk:
                    cf_rec = recent_ledger_by_camp_field.get((camp_s, cf_k))
                    if cf_rec:
                        cf_dt = str(cf_rec.get("change_date") or cf_rec.get("changed_at") or cf_rec.get("adjusted_at") or "")[:10]
                        if cf_dt and cf_dt >= p_first_dt:
                            backend_executed = True
                            exec_val_desc = f"ledger({cf_k}):{cf_rec.get('old_val')}->{cf_rec.get('new_val')}"
                            break

            if not backend_executed:
                m_usd_p = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", p_rec_val)
                rec_usd_p = float(m_usd_p.group(1)) if m_usd_p else None
                prev_b_mem = num(pend_rec.get("current_bid_num") if pend_rec.get("current_bid_num") is not None else prev_e.get("current_bid"))
                if p_act == "Pause_Keyword" and live_snap["ent_state"] == "paused":
                    backend_executed = True
                    exec_val_desc = "state=paused"
                elif p_act == "Unpause_Keyword" and live_snap["exists"] and live_snap["ent_state"] == "enabled":
                    backend_executed = True
                    exec_val_desc = f"state=enabled(${live_snap['bid']:.2f})"
                elif p_act == "Negative_Exact" and ((camp_s.lower(), ent_s) in active_neg_pairs_set or ent_s in known_neg_set):
                    backend_executed = True
                    exec_val_desc = "active_negative_confirmed"
                elif p_act == "Negative_Phrase" and ent_s in known_neg_set:
                    backend_executed = True
                    exec_val_desc = "negative_phrase_confirmed"
                elif p_act == "Negative_Target" and ((camp_s.lower(), ent_s) in active_neg_pairs_set or ent_s in known_neg_set):
                    backend_executed = True
                    exec_val_desc = "negative_target_confirmed"
                elif p_act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact") and (camp_s, "exact", ent_s) in kw_30d_by_camp_mt_ent:
                    backend_executed = True
                    exec_val_desc = f"exact_created(${live_snap['bid']:.2f})"
                elif p_act == "Harvest_ASIN" and (camp_s, ent_s) in tg_30d_by_camp_ent:
                    backend_executed = True
                    exec_val_desc = f"target_created(${live_snap['bid']:.2f})"
                elif p_act == "Update_Bidding_Strategy" and ent_s == "biddingstrategy":
                    c_o = camp_lookup_all.get(camp_s) or {}
                    if c_o and not c_o.get("is_up_and_down"):
                        backend_executed = True
                        exec_val_desc = "Dynamic Bids - Down Only"
                elif p_act in ("Lower_Budget", "Increase_Budget", "Update_Budget", "High_Refund_Throttle_Budget") and rec_usd_p is not None:
                    c_o = camp_lookup_all.get(camp_s) or {}
                    if c_o and abs(num(c_o.get("budget")) - rec_usd_p) <= 0.50 and (prev_b_mem <= 0 or abs(prev_b_mem - rec_usd_p) > 0.50):
                        backend_executed = True
                        exec_val_desc = f"budget=${num(c_o.get('budget')):.2f}"
                elif p_act in ("Placement_TOS_PP_ROS", "Placement_TOS", "Placement_PP", "Placement_ROS", "Emergency_Placement_Throttle"):
                    c_o = camp_lookup_all.get(camp_s) or {}
                    if c_o:
                        cur_tos_i, cur_pp_i, cur_ros_i = int(c_o.get("tos", 0)), int(c_o.get("pp", 0)), int(c_o.get("ros", 0))
                        cur_pl = f"TOS={cur_tos_i}% / PP={cur_pp_i}% / ROS={cur_ros_i}%"
                        rec_slots_p = {m.group(1).upper(): int(m.group(2)) for m in re.finditer(r"\b(TOS|PP|ROS)\s*=\s*(\d+)%", p_rec_val, re.I)}
                        cur_slots_p = {m.group(1).upper(): int(m.group(2)) for m in re.finditer(r"\b(TOS|PP|ROS)\s*=\s*(\d+)%", str(pend_rec.get("current_value") or ""), re.I)}
                        live_slots_p = {"TOS": cur_tos_i, "PP": cur_pp_i, "ROS": cur_ros_i}
                        slot_matched_change = any(
                            rec_slots_p.get(sk) == live_slots_p[sk] and cur_slots_p.get(sk) is not None and cur_slots_p[sk] != rec_slots_p[sk]
                            for sk in rec_slots_p
                        )
                        if (
                            p_rec_val == cur_pl
                            or slot_matched_change
                            or (p_act in ("Placement_TOS", "Emergency_Placement_Throttle") and f"TOS={cur_tos_i}%" == p_rec_val)
                            or (p_act == "Placement_PP" and f"PP={cur_pp_i}%" == p_rec_val)
                        ):
                            backend_executed = True
                            exec_val_desc = cur_pl
                elif rec_usd_p is not None and live_snap["exists"] and live_snap["bid"] > 0:
                    if abs(live_snap["bid"] - rec_usd_p) <= 0.015 and (prev_b_mem <= 0 or abs(prev_b_mem - rec_usd_p) > 0.015):
                        backend_executed = True
                        exec_val_desc = f"${live_snap['bid']:.2f}"

            if backend_executed:
                exec_rec = {
                    "executed_date": today_str,
                    "action_type": p_act,
                    "new_value": exec_val_desc or p_rec_val,
                    "verified_from_pending": pend_rec,
                }
                pend_rec = None

        return exec_rec, pend_rec

    def _format_five_dim_evidence(rk_d: dict, live_s: dict, extra_reason: str) -> str:
        or_part = f"OR#{rk_d['prev_organic_rank'] or '--'}→#{rk_d['organic_rank'] or '--'}"
        sp_part = f"SP#{rk_d['prev_sp_rank'] or '--'}→#{rk_d['sp_rank'] or '--'}"
        ac30_t = fmt_acos_text(live_s["ac30"], live_s["sp30"], live_s["od30"])
        ac7_t = fmt_acos_text(live_s["ac7"], live_s["sp7"], live_s["od7"])
        d1 = f"[1.双轨排名]{or_part},{sp_part}"
        d2 = f"[2.多窗实绩]30d:${live_s['sp30']:.1f}/{live_s['od30']}单({ac30_t})|7d:${live_s['sp7']:.1f}/{live_s['od7']}单({ac7_t})|3d:${live_s['sp3']:.1f}/{live_s['od3']}单"
        d3 = f"[3.状态/OOB]{live_s['camp_state']}/{live_s['ent_state']},在预算内{live_s['tib_pct']:.0f}%"
        d4 = f"[4.归因/缩量锁]lag={attribution_lag},shrink_guard={shrinkage_guard}"
        d5 = f"[5.全口径保本]trueBE={true_be_tacos:.1f}%,target={target_acos:.1f}%,cb={zero_ad_cb}"
        return f"{extra_reason} | {d1}; {d2}; {d3}; {d4}; {d5}"

    # Pre-resolve raw adjustments before memory gate so each (camp, mt, ent) has at most 1 candidate from today's fresh pass
    pre_deduped_today = resolve_entity_action_conflicts(
        all_final_adjustments, profile=profile, rules=rules, financial_prof=financial_prof, stage_ctx=stage_ctx_eff
    )

    effective_cooldown = eff_cd_days
    inheritance_records: list[dict] = []
    handled_mem_keys: set[str] = set()
    memory_vetted_adjustments = []

    for raw_adj in pre_deduped_today:
        adj = dict(raw_adj)
        act = adj.get("action_type", "")
        camp = str(adj.get("campaign") or "").strip()
        ent = str(adj.get("target_entity") or "").strip()
        ent_low = ent.lower()
        mt = str(adj.get("match_type") or "").lower().strip()
        ekey = f"{camp}::{mt}::{ent_low}"
        handled_mem_keys.add(ekey)

        rk = _compute_rank_deltas(ent_low, xydc_map, sr_map, entity_memory, today_str)
        kw_30 = kw_30d_by_camp_mt_ent.get((camp, mt, ent_low)) or (kw_30d_by_camp_ent_fallback.get((camp, ent_low)) if mt in ("", "exact", "phrase", "broad") else {}) or {}
        ord_30 = int(num(kw_30.get("orders")))
        k3 = kw_3d_lu.get((camp, mt, ent_low)) or {}
        sp_3 = num(k3.get("spends") or k3.get("spend") or k3.get("cost")) if k3 else 0.0
        od_3 = int(num(k3.get("orders"))) if k3 else 0
        k7 = kw_7d_lu.get((camp, mt, ent_low)) or {}
        od_7 = int(num(k7.get("orders"))) if k7 else 0
        sp_7 = num(k7.get("spends") or k7.get("spend") or k7.get("cost")) if k7 else 0.0
        ac_7 = acos(sp_7, num(k7.get("sales"))) if k7 and num(k7.get("sales")) > 0 else 999.0

        prev_e = entities_mem.get(ekey) or {}
        l_rec = recent_ledger_by_ent.get((camp, mt, ent_low)) or recent_ledger_by_ent.get((camp, "*", ent_low))
        live_snap = _get_live_entity_snapshot(camp, mt, ent_low)
        exec_rec, pend_rec = _extract_prior_pending_and_executed(prev_e, camp, mt, ent_low, live_snap, l_rec)

        # Record if prior pending was just confirmed executed!
        if exec_rec and exec_rec.get("verified_from_pending"):
            vp = exec_rec["verified_from_pending"]
            p_fdt = str(vp.get("first_recommended_date") or "")[:10]
            p_ldt = str(vp.get("last_recommended_date") or p_fdt)[:10]
            try:
                d_pend = max(1, (today_dt - datetime.strptime(p_fdt, "%Y-%m-%d")).days)
            except Exception:
                d_pend = 1
            inheritance_records.append({
                "first_recommended_date": p_fdt,
                "last_recommended_date": p_ldt,
                "days_pending": d_pend,
                "campaign": camp,
                "match_type": adj.get("match_type", mt),
                "target_entity": ent,
                "prior_action": vp.get("action_type", ""),
                "prior_recommended_value": vp.get("recommended_value", ""),
                "current_backend_value": exec_rec.get("new_value", ""),
                "inheritance_status": "EXECUTED_IN_COOLDOWN",
                "resolution_action": "COOLDOWN_OBSERVATION",
                "five_dim_evidence": _format_five_dim_evidence(rk, live_snap, "后台已落地执行原建议，转入实调观察冷却期"),
            })

        # A) Rank-Slide Protection Veto: Never cut bids on a Top-20 converting keyword whose organic rank dropped >= 4 positions!
        if act in DOWNWARD_BID_ACTIONS and rk["organic_rank"] is not None and rk["organic_rank"] <= 20 and rk["delta_or"] is not None and rk["delta_or"] <= -4 and ord_30 >= 1:
            cur_b = adj.get("current_bid_num") or live_snap["bid"] or 0.35
            orig_cut_act = act
            adj["action_type"] = "Rank_Slide_Protection_Hold"
            adj["recommended_value"] = f"${cur_b:.2f}"
            adj["reason"] = (
                f"【自然排名滑坡保护锁】监测到该出单词自然排名由 #{rk['prev_organic_rank']} 下滑至 #{rk['organic_rank']} (ΔOR {rk['delta_or']:+d})，"
                f"历史累计出单 {ord_30} 单；严禁在自然位滑坡期叠砍广告基础竞价加速权重崩塌（已拦截原建议 {orig_cut_act}），维持 ${cur_b:.2f} 稳位护盘。"
            )
            act = adj["action_type"]

        # B) Cooldown Gate: ONLY `executed` actions (via exec_rec or ledger) trigger cooldown! NEVER `pend_rec`!
        exec_act = str((exec_rec or {}).get("action_type") or "")
        exec_dt_str = str((exec_rec or {}).get("executed_date") or "")[:10]
        days_since_exec = 999
        if exec_dt_str:
            try:
                days_since_exec = max(0, (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(exec_dt_str, "%Y-%m-%d")).days)
            except Exception:
                days_since_exec = 999

        days_since_ledger = 999
        ledger_cut_bid = False
        ledger_raised_bid = False
        if l_rec:
            adj_dt = str(l_rec.get("change_date") or l_rec.get("changed_at") or l_rec.get("adjusted_at") or "")[:10]
            if adj_dt:
                try:
                    days_since_ledger = max(0, (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(adj_dt, "%Y-%m-%d")).days)
                except Exception:
                    days_since_ledger = 999
            if l_rec.get("field") == "bid":
                old_v = num(l_rec.get("old_val"))
                new_v = num(l_rec.get("new_val"))
                if new_v < old_v:
                    ledger_cut_bid = True
                elif new_v > old_v:
                    ledger_raised_bid = True

        mem_in_cooldown = bool(exec_rec and 0 <= days_since_exec <= effective_cooldown)
        ledger_in_cooldown = (0 <= days_since_ledger <= effective_cooldown)
        in_cooldown = mem_in_cooldown or ledger_in_cooldown

        if in_cooldown:
            is_cb_bleed_cd = (sp_7 >= cb_bleed_7d and od_7 == 0) or (sp_3 >= cb_bleed_3d and od_3 == 0)
            if ((exec_act in DOWNWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_cut_bid and ledger_in_cooldown)) and act in ("Elevate_To_TOS", "Boost_Ascender_TOS"):
                if not (od_7 >= 2 and ac_7 <= eval_target_acos * 0.85):
                    continue
            elif ((exec_act in UPWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_raised_bid and ledger_in_cooldown)) and act in DOWNWARD_BID_ACTIONS:
                if not is_cb_bleed_cd:
                    continue
            elif act in DOWNWARD_BID_ACTIONS and ((exec_act in DOWNWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_cut_bid and ledger_in_cooldown)):
                if not is_cb_bleed_cd:
                    continue
            elif act in ("Elevate_To_TOS", "Boost_Ascender_TOS") and ((exec_act in UPWARD_BID_ACTIONS and mem_in_cooldown) or (ledger_raised_bid and ledger_in_cooldown)):
                continue

        # C) Inherited Pending Tagging when prior unexecuted recommendation aligns with today's action
        inherited_prefix = ""
        if pend_rec and str(pend_rec.get("first_recommended_date") or "")[:10] < today_str and v2p.classify_action_lever(str(pend_rec.get("action_type") or "")) != "HOLD":
            p_fdt = str(pend_rec.get("first_recommended_date") or "")[:10]
            p_ldt = str(pend_rec.get("last_recommended_date") or p_fdt)[:10]
            p_act = str(pend_rec.get("action_type") or "")
            try:
                d_pend = max(1, (today_dt - datetime.strptime(p_fdt, "%Y-%m-%d")).days)
            except Exception:
                d_pend = 1
            same_family = (
                p_act == act
                or (p_act in DOWNWARD_BID_ACTIONS and act in DOWNWARD_BID_ACTIONS)
                or (p_act in UPWARD_BID_ACTIONS and act in UPWARD_BID_ACTIONS)
            )
            cur_bk_str = f"${live_snap['bid']:.2f}" if live_snap["exists"] and live_snap["bid"] > 0 else live_snap["ent_state"]
            if same_family:
                inherited_prefix = f"[待执行继承·连续{d_pend}天] "
                inheritance_records.append({
                    "first_recommended_date": p_fdt,
                    "last_recommended_date": p_ldt,
                    "days_pending": d_pend,
                    "campaign": camp,
                    "match_type": adj.get("match_type", mt),
                    "target_entity": ent,
                    "prior_action": p_act,
                    "prior_recommended_value": pend_rec.get("recommended_value", ""),
                    "current_backend_value": cur_bk_str,
                    "inheritance_status": "INHERITED_PENDING",
                    "resolution_action": f"{act} -> {adj.get('recommended_value', '')}",
                    "five_dim_evidence": _format_five_dim_evidence(rk, live_snap, "后台尚未执行原建议，经本期5维复核问题持续存在，继续继承输出"),
                })
            else:
                inheritance_records.append({
                    "first_recommended_date": p_fdt,
                    "last_recommended_date": p_ldt,
                    "days_pending": d_pend,
                    "campaign": camp,
                    "match_type": adj.get("match_type", mt),
                    "target_entity": ent,
                    "prior_action": p_act,
                    "prior_recommended_value": pend_rec.get("recommended_value", ""),
                    "current_backend_value": cur_bk_str,
                    "inheritance_status": "SUPERSEDED_BY_DATA",
                    "resolution_action": f"Replaced by {act} ({adj.get('recommended_value', '')})",
                    "five_dim_evidence": _format_five_dim_evidence(rk, live_snap, f"最新数据触发新策略 {act} 替代原建议 {p_act}"),
                })

        # D) Enrich reason with longitudinal rank delta & prior history
        ctx_badges = []
        if rk["delta_or"] is not None and rk["delta_or"] != 0:
            arrow = "↑" if rk["delta_or"] > 0 else "↓"
            ctx_badges.append(f"自然位#{rk['prev_organic_rank']}→#{rk['organic_rank']}({arrow}{abs(rk['delta_or'])})")
        if rk["delta_sp"] is not None and rk["delta_sp"] != 0:
            arrow_sp = "↑" if rk["delta_sp"] > 0 else "↓"
            ctx_badges.append(f"广告位#{rk['prev_sp_rank']}→#{rk['sp_rank']}({arrow_sp}{abs(rk['delta_sp'])})")
        if rk["aba_rank"] is not None and "ABA" not in str(adj.get("reason", "")):
            ctx_badges.append(f"ABA#{rk['aba_rank']}")

        l_old_v = l_rec.get("old_val") if (l_rec and l_rec.get("old_val") is not None) else (l_rec.get("old_value") if l_rec else None)
        l_new_v = l_rec.get("new_val") if (l_rec and l_rec.get("new_val") is not None) else (l_rec.get("new_value") if l_rec else None)
        if l_rec and l_old_v is not None and l_new_v is not None:
            l_dt_s = str(l_rec.get("change_date") or l_rec.get("changed_at") or l_rec.get("adjusted_at") or "")[:10]
            ctx_badges.append(f"前次实调({l_dt_s}):{l_old_v}→{l_new_v}[{str(l_rec.get('effect_verdict', ''))[:6]}]")

        base_rsn = str(adj.get("reason") or "")
        if ctx_badges:
            base_rsn = f"【全量追踪: {' | '.join(ctx_badges)}】{base_rsn}"
        if inherited_prefix and not base_rsn.startswith("[待执行继承"):
            base_rsn = f"{inherited_prefix}{base_rsn}"
        adj["reason"] = base_rsn

        memory_vetted_adjustments.append(adj)

    # 11.7b Re-evaluate ALL remaining prior `pending_recommendation` items not emitted in today's fresh pass (0% silent evaporation!)
    shock_recovery_camps = {
        str(a.get("campaign") or "").strip()
        for a in memory_vetted_adjustments
        if a.get("action_type") == "Probe_Bid_Recovery"
    }
    for _c_shk in camps:
        if _c_shk.get("assessment") == "CIRCUIT_BREAKER_SHOCK":
            shock_recovery_camps.add(str(_c_shk.get("name") or "").strip())

    for ekey_m, prev_e_m in sorted(entities_mem.items()):
        if ekey_m in handled_mem_keys or not isinstance(prev_e_m, dict):
            continue
        parts_m = ekey_m.split("::", 2)
        if len(parts_m) != 3:
            continue
        camp_m = str(prev_e_m.get("campaign") or parts_m[0]).strip()
        mt_disp_m = str(prev_e_m.get("match_type") or parts_m[1]).strip()
        mt_m = mt_disp_m.lower()
        ent_disp_m = str(prev_e_m.get("entity") or parts_m[2]).strip()
        ent_m = ent_disp_m.lower()
        if not camp_m or not ent_m or mt_m == "schedule":
            continue

        l_rec_m = recent_ledger_by_ent.get((camp_m, mt_m, ent_m)) or recent_ledger_by_ent.get((camp_m, "*", ent_m))
        live_m = _get_live_entity_snapshot(camp_m, mt_m, ent_m)
        exec_m, pend_m = _extract_prior_pending_and_executed(prev_e_m, camp_m, mt_m, ent_m, live_m, l_rec_m)
        rk_m = _compute_rank_deltas(ent_m, xydc_map, sr_map, entity_memory, today_str)

        if exec_m and exec_m.get("verified_from_pending"):
            vp = exec_m["verified_from_pending"]
            p_fdt = str(vp.get("first_recommended_date") or "")[:10]
            p_ldt = str(vp.get("last_recommended_date") or p_fdt)[:10]
            try:
                d_pend = max(1, (today_dt - datetime.strptime(p_fdt, "%Y-%m-%d")).days)
            except Exception:
                d_pend = 1
            inheritance_records.append({
                "first_recommended_date": p_fdt,
                "last_recommended_date": p_ldt,
                "days_pending": d_pend,
                "campaign": camp_m,
                "match_type": mt_disp_m,
                "target_entity": ent_disp_m,
                "prior_action": vp.get("action_type", ""),
                "prior_recommended_value": vp.get("recommended_value", ""),
                "current_backend_value": exec_m.get("new_value", ""),
                "inheritance_status": "EXECUTED_IN_COOLDOWN",
                "resolution_action": "HOLD_IN_COOLDOWN",
                "five_dim_evidence": _format_five_dim_evidence(rk_m, live_m, "经快照/台账确认后台已执行原建议，进入观察冷却期"),
            })
            continue

        if not pend_m:
            continue
        p_fdt = str(pend_m.get("first_recommended_date") or "")[:10]
        if not p_fdt or p_fdt >= today_str:
            continue
        p_ldt = str(pend_m.get("last_recommended_date") or p_fdt)[:10]
        p_act = str(pend_m.get("action_type") or "")
        p_rec = str(pend_m.get("recommended_value") or "")
        if not p_act or not p_rec:
            continue
        try:
            d_pend = max(1, (today_dt - datetime.strptime(p_fdt, "%Y-%m-%d")).days)
        except Exception:
            d_pend = 1

        cur_bk_str = f"${live_m['bid']:.2f}" if live_m["exists"] and live_m["bid"] > 0 else live_m["ent_state"]
        superseded_reason = ""
        reclamped_cur_val = None
        reclamped_rec_val = None
        reclamped_path = None
        reclamp_note_m = ""
        is_high_refund_m = bool(financial_prof and num(financial_prof.get("refund_rate")) >= refund_lv1)
        c_obj_m = camp_lookup_all.get(camp_m) or {}
        anchor_bid_m = live_m["bid"] if (live_m["exists"] and live_m["bid"] > 0) else num(pend_m.get("current_bid_num"))
        eff_anchor_m = live_m["timing_base"] if live_m["timing_base"] > 0 else anchor_bid_m
        fw_hit_m, _, fw_rsn_m = check_global_compliance_firewall(ent_m, profile, rules)
        bkt_m, _, _ = classify_query(ent_m, profile, rules)
        is_p0_ent_m = fw_hit_m or bkt_m in ("is_not", "competitor_brand") or str(bkt_m).startswith("is_not")

        # Evaluate 5 dimensions to decide INHERITED_PENDING vs SUPERSEDED_BY_DATA
        if v2p.classify_action_lever(p_act) == "HOLD":
            superseded_reason = "历史观察/保护锁定状态指令非待执行后台改动，已随本期实时状态重评自动销账"
        elif live_m["camp_state"] not in ("enabled",) or (live_m["exists"] and live_m["ent_state"] != "enabled" and p_act != "Unpause_Keyword"):
            superseded_reason = f"所属活动({live_m['camp_state']})或投放实体({live_m['ent_state']})已非启用状态，无需重复执行"
        elif (p_act in POSITIVE_RECOMMENDATION_ACTIONS or re.search(r"(?:Unpause|Harvest|Boost|Elevate|Probe_Bid_Recovery|Recover)", p_act, re.I)) and fw_hit_m:
            superseded_reason = f"触发全局合规/错品类防火墙({fw_rsn_m})，依法撤销正向推荐"
        elif freeze_expansion and p_act in (
            "Elevate_To_TOS", "Boost_Ascender_TOS", "Increase_Budget",
            "Harvest_To_Exact", "Harvest_ASIN", "Converge_Harvest_To_Exact",
            "Unpause_Keyword", "Unpause_Target",
        ):
            superseded_reason = "当前阶段/COGS估测/低库存/零广告熔断处于扩量冻结状态，撤销提价/收割/复活/扩预算建议"
        elif camp_m in locked_campaigns:
            superseded_reason = "所属活动近期已发生新活动层参数调整进入冷却观察，暂缓原建议"
        elif camp_m in campaigns_with_placement_change and p_act in DOWNWARD_BID_ACTIONS:
            superseded_reason = "所属活动本期已下调展位溢价(TOS/PP)，防双重抑制自动销账"
        elif p_act in ("Placement_TOS_PP_ROS", "Placement_TOS", "Placement_PP", "Placement_ROS", "Emergency_Placement_Throttle"):
            c_o_m = camp_lookup_all.get(camp_m) or {}
            c_m_low = camp_m.lower()
            is_auto_or_broad_m = (
                "自动" in camp_m or "auto" in c_m_low or c_o_m.get("targeting") == "auto"
                or "广泛" in camp_m or "broad" in c_m_low
            )
            if not c_o_m:
                superseded_reason = "所属活动未在当前在跑活动列表中，撤销历史溢价建议"
            elif is_auto_or_broad_m and "竞品防御仓" in str(pend_m.get("reason") or ""):
                superseded_reason = "历史建议系旧版自动/广泛仓误入竞品防御仓规则产生，依法撤销"
            else:
                cur_t_m, cur_p_m, cur_r_m = int(c_o_m.get("tos", 0)), int(c_o_m.get("pp", 0)), int(c_o_m.get("ros", 0))
                rec_slots_m = {m.group(1).upper(): int(m.group(2)) for m in re.finditer(r"\b(TOS|PP|ROS)\s*=\s*(\d+)%", p_rec, re.I)}
                is_down_pl = (
                    ("TOS" in rec_slots_m and rec_slots_m["TOS"] < cur_t_m)
                    or ("PP" in rec_slots_m and rec_slots_m["PP"] < cur_p_m)
                    or ("ROS" in rec_slots_m and rec_slots_m["ROS"] < cur_r_m)
                )
                is_up_pl = (
                    ("TOS" in rec_slots_m and rec_slots_m["TOS"] > cur_t_m)
                    or ("PP" in rec_slots_m and rec_slots_m["PP"] > cur_p_m)
                    or ("ROS" in rec_slots_m and rec_slots_m["ROS"] > cur_r_m)
                )
                if camp_m in shock_recovery_camps and is_down_pl:
                    superseded_reason = "所属活动本期触发休克断流回踩(CIRCUIT_BREAKER_SHOCK/Probe_Bid_Recovery)，撤回历史溢价下调建议防加剧断流"
                elif block_non_p0_cuts and is_down_pl and p_act != "Emergency_Placement_Throttle":
                    superseded_reason = "当前处于大促峰值日(PEAK_DAY)，暂缓非熔断类历史溢价下调建议"
                elif shrinkage_guard and is_down_pl and c_o_m.get("ord", 0) >= 2 and 0 < c_o_m.get("acos", 999.0) <= eval_target_acos * 1.35:
                    superseded_reason = "触发北极星缩量防线，暂缓对核心转化活动砍展位溢价"
                elif freeze_expansion and is_up_pl and not is_down_pl:
                    superseded_reason = "当前处于扩量冻结状态，撤销历史溢价上调建议"
                elif rec_slots_m:
                    # Re-clamp legacy v1 placement suggestions through v2 §5.2 step caps so cliff drops NEVER leak!
                    if p_act == "Placement_TOS_PP_ROS":
                        g_t = rec_slots_m.get("TOS", cur_t_m)
                        g_p = rec_slots_m.get("PP", cur_p_m)
                        g_r = rec_slots_m.get("ROS", cur_r_m)
                        s_t, s_p, s_r, pl_p_m = _step_placement_triple(cur_t_m, cur_p_m, cur_r_m, g_t, g_p, g_r)
                        reclamped_cur_val = f"TOS={cur_t_m}% / PP={cur_p_m}% / ROS={cur_r_m}%"
                        reclamped_rec_val = f"TOS={s_t}% / PP={s_p}% / ROS={s_r}%"
                        reclamped_path = pl_p_m
                        if reclamped_rec_val == reclamped_cur_val:
                            superseded_reason = f"当前活动展位({reclamped_cur_val})已与目标一致，无需再调"
                    else:
                        sl_k = "TOS" if "TOS" in rec_slots_m else ("PP" if "PP" in rec_slots_m else "ROS")
                        cur_sv = {"TOS": cur_t_m, "PP": cur_p_m, "ROS": cur_r_m}[sl_k]
                        g_sv = min(cur_sv, rec_slots_m[sl_k]) if freeze_expansion else rec_slots_m[sl_k]
                        s_sv, p_sv = v2p.calc_placement_step(cur_sv, g_sv)
                        reclamped_cur_val = f"{sl_k}={cur_sv}%"
                        reclamped_rec_val = f"{sl_k}={s_sv}%"
                        reclamped_path = f"{sl_k}: {p_sv}" if p_sv else ""
                        if s_sv == cur_sv:
                            superseded_reason = f"当前活动展位({reclamped_cur_val})已达标，无需再调"
        elif p_act in ("Lower_Budget", "High_Refund_Throttle_Budget"):
            c_o_b = camp_lookup_all.get(camp_m) or {}
            m_b_m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", p_rec)
            rec_b_m = float(m_b_m.group(1)) if m_b_m else 0.0
            cur_b_m = num(c_o_b.get("budget"))
            if camp_m in shock_recovery_camps:
                superseded_reason = "所属活动本期触发休克断流回踩(CIRCUIT_BREAKER_SHOCK)，暂缓历史降预算建议防加剧断流"
            elif block_non_p0_cuts:
                superseded_reason = "当前处于大促峰值日(PEAK_DAY)，暂缓常规降预算建议"
            elif shrinkage_guard and c_o_b.get("ord", 0) >= 2:
                superseded_reason = "触发北极星缩量防线，暂缓砍转化活动日预算"
            elif cur_b_m <= 10.0 or (rec_b_m > 0 and cur_b_m <= rec_b_m + 0.5):
                superseded_reason = f"当前日预算(${cur_b_m:.2f})已不高于建议值({p_rec})"
            elif rec_b_m > 0:
                clamped_b = max(10.0, round(cur_b_m * 0.70, 2), rec_b_m)
                if clamped_b < cur_b_m - 0.5:
                    reclamped_rec_val = f"${clamped_b:.2f}"
                else:
                    superseded_reason = f"当前日预算(${cur_b_m:.2f})受 -30%/保底$10 约束后已无可降空间"
        elif p_act == "Increase_Budget":
            c_o_b = camp_lookup_all.get(camp_m) or {}
            m_b_m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", p_rec)
            rec_b_m = float(m_b_m.group(1)) if m_b_m else 0.0
            cur_b_m = num(c_o_b.get("budget"))
            if not c_o_b or cur_b_m <= 0:
                superseded_reason = "所属活动未在当前在跑活动列表中，撤销历史扩预算建议"
            elif rec_b_m <= 0 or cur_b_m >= rec_b_m - 0.5:
                superseded_reason = f"当前日预算(${cur_b_m:.2f})已达或超过原扩预算目标({p_rec})"
            else:
                max_up_b = _floor_cents(cur_b_m * 1.50)
                clamped_ub = min(rec_b_m, max_up_b)
                if clamped_ub <= cur_b_m + 0.5:
                    superseded_reason = f"当前日预算(${cur_b_m:.2f})在 +50% 步长上限内已达标"
                elif abs(clamped_ub - rec_b_m) >= 0.01:
                    reclamped_rec_val = f"${clamped_ub:.2f}"
                    reclamp_note_m = f"（v2 预算+50%步长上限重校准: {p_rec}→${clamped_ub:.2f}）"
        elif p_act == "Archive_Fragmented_Campaign":
            if stage_mode == "LAUNCH" or peak_overlay or block_non_p0_cuts or shrinkage_guard:
                superseded_reason = "当前处于新品期/大促期/缩量防线保护窗口，豁免并撤销历史孤岛归档建议"
        elif p_act == "Update_Bidding_Strategy" and ent_m == "offamazonstrategy":
            if (c_obj_m or {}).get("off_amazon_status") != "OFF_AMAZON_BLEED":
                superseded_reason = "当前活动站外 OFF AMAZON 展位已非超标失血状态，显式销账"
        elif p_act in DOWNWARD_BID_ACTIONS:
            if camp_m in shock_recovery_camps:
                superseded_reason = "所属活动本期触发休克断流回踩(CIRCUIT_BREAKER_SHOCK)，撤回历史降价建议防加剧断流"
            elif rk_m["organic_rank"] is not None and rk_m["organic_rank"] <= 20 and rk_m["delta_or"] is not None and rk_m["delta_or"] <= -4 and live_m["od30"] >= 1:
                superseded_reason = f"核心出单词自然排名下滑(ΔOR={rk_m['delta_or']:+d})触发滑坡保护，撤销降价"
            elif block_non_p0_cuts and not is_p0_ent_m:
                superseded_reason = "当前处于大促峰值日(PEAK_DAY)，暂缓非P0降价建议"
            elif shrinkage_guard and (live_m["od30"] >= 1 or live_m["od7"] >= 1 or live_m["od3"] >= 1):
                superseded_reason = "触发北极星大盘缩量保护锁，暂缓出单词降价防缩量"
            elif (live_m["od7"] >= 1 and 0 < live_m["ac7"] <= eval_target_acos * 1.15) or (live_m["od3"] >= 1 and 0 < live_m["ac3"] <= eval_target_acos * 1.15) or (live_m["od30"] >= 2 and 0 < live_m["ac30"] <= eval_target_acos):
                superseded_reason = "近期新出单回流且ACOS已回落至目标安全线内，数据好转显式销账"
            elif live_m["sp7"] == 0.0 and live_m["sp3"] == 0.0 and live_m["im3"] <= 25:
                superseded_reason = "近7d与3d已自然零花费低曝光(3d曝光<=25)，无需对静默词叠加砍价"
            else:
                reclamped_rec_val, superseded_reason, reclamp_note_m = reclamp_inherited_bid_recommendation(
                    action_type=p_act,
                    recommended_value=p_rec,
                    anchor_bid=anchor_bid_m,
                    timing_base=live_m["timing_base"],
                    match_type=mt_m,
                    entity_text=ent_m,
                    is_fragile=is_fragile,
                    is_high_refund=is_high_refund_m,
                    od30=live_m["od30"],
                    od7=live_m["od7"],
                    sp_rank=rk_m.get("sp_rank"),
                    kw_fl_std=kw_fl_std,
                    kw_fl_frg=kw_fl_frg,
                    as_fl_std=as_fl_std,
                    as_fl_frg=as_fl_frg,
                    block_shock_recovery=bool(stage_perms.get("block_shock_recovery")),
                    has_paired_placement=(camp_m in campaigns_with_placement_change),
                    recent_cpc=live_m.get("cpc30", 0.0),
                    camp_tos=num(c_obj_m.get("tos")),
                )
        elif p_act in ("Negative_Exact", "Negative_Target", "Negative_Phrase"):
            if block_non_p0_cuts and p_act != "Negative_Phrase" and not is_p0_ent_m:
                superseded_reason = "当前处于大促峰值日(PEAK_DAY)，暂缓非P0精准/商品否定建议"
            elif live_m["exists"] and ((live_m["od7"] >= 1 and live_m["ac7"] <= eval_target_acos * 1.15) or (live_m["od30"] >= 1 and live_m["ac30"] <= eval_target_acos * 1.15)):
                superseded_reason = "归因延迟新出单且投产达标，撤回原否定建议保护出单词"
        elif p_act in UPWARD_BID_ACTIONS or p_act in ("Raise_Bid", "Increase_Bid"):
            if (live_m["sp7"] >= cb_bleed_7d and live_m["od7"] == 0) or (live_m["sp3"] >= cb_bleed_3d and live_m["od3"] == 0) or (live_m["od7"] >= 1 and live_m["ac7"] > eval_target_acos * 1.30):
                superseded_reason = "近7d/3d出现失血熔断无单耗散或7d ACOS超标，撤回原提价建议"
            else:
                reclamped_rec_val, superseded_reason, reclamp_note_m = reclamp_inherited_bid_recommendation(
                    action_type=p_act,
                    recommended_value=p_rec,
                    anchor_bid=anchor_bid_m,
                    timing_base=live_m["timing_base"],
                    match_type=mt_m,
                    entity_text=ent_m,
                    is_fragile=is_fragile,
                    is_high_refund=is_high_refund_m,
                    od30=live_m["od30"],
                    od7=live_m["od7"],
                    sp_rank=rk_m.get("sp_rank"),
                    kw_fl_std=kw_fl_std,
                    kw_fl_frg=kw_fl_frg,
                    as_fl_std=as_fl_std,
                    as_fl_frg=as_fl_frg,
                    block_shock_recovery=bool(stage_perms.get("block_shock_recovery")),
                    has_paired_placement=(camp_m in campaigns_with_placement_change),
                    recent_cpc=live_m.get("cpc30", 0.0),
                    camp_tos=num(c_obj_m.get("tos")),
                )

        if superseded_reason:
            inheritance_records.append({
                "first_recommended_date": p_fdt,
                "last_recommended_date": p_ldt,
                "days_pending": d_pend,
                "campaign": camp_m,
                "match_type": mt_disp_m,
                "target_entity": ent_disp_m,
                "prior_action": p_act,
                "prior_recommended_value": p_rec,
                "current_backend_value": cur_bk_str,
                "inheritance_status": "SUPERSEDED_BY_DATA",
                "resolution_action": "CANCELLED_BY_NEW_DATA",
                "five_dim_evidence": _format_five_dim_evidence(rk_m, live_m, superseded_reason),
            })
        else:
            eff_p_rec = reclamped_rec_val or p_rec
            eff_p_path = reclamped_path if reclamped_path is not None else pend_m.get("planned_path", "")
            carry_cur_val = reclamped_cur_val or str(pend_m.get("current_value") or cur_bk_str or f"${live_m['bid']:.2f}")
            if reclamped_rec_val and eff_anchor_m > 0 and (p_act in DOWNWARD_BID_ACTIONS or p_act in UPWARD_BID_ACTIONS or p_act in ("Raise_Bid", "Increase_Bid")):
                carry_cur_val = re.sub(r"当前基础竞价\s*\$[0-9]+(?:\.[0-9]+)?", f"当前基础竞价 ${eff_anchor_m:.2f}", carry_cur_val)
            raw_carry_rsn = str(pend_m.get("reason") or f"上期({p_fdt})建议尚未落地，经本期5维复核依然成立。")
            carry_rsn = sanitize_inherited_reason(raw_carry_rsn, p_act, eff_p_rec, strip_reclamp_note=bool(reclamp_note_m))
            if reclamp_note_m:
                carry_rsn = f"{carry_rsn}{reclamp_note_m}"
            carry_rsn = f"[待执行继承·连续{d_pend}天] {carry_rsn}"
            memory_vetted_adjustments.append({
                "action_type": p_act,
                "campaign": camp_m,
                "target_entity": ent_disp_m,
                "match_type": mt_disp_m,
                "current_bid_num": eff_anchor_m if eff_anchor_m > 0 else pend_m.get("current_bid_num"),
                "current_value": carry_cur_val,
                "recommended_value": eff_p_rec,
                "reason": carry_rsn,
                "lever_type": pend_m.get("lever_type", ""),
                "planned_path": eff_p_path,
                "expected_effect": pend_m.get("expected_effect", ""),
                "verify_date": pend_m.get("verify_date", ""),
                "rollback_rule": pend_m.get("rollback_rule", ""),
            })
            inheritance_records.append({
                "first_recommended_date": p_fdt,
                "last_recommended_date": p_ldt,
                "days_pending": d_pend,
                "campaign": camp_m,
                "match_type": mt_disp_m,
                "target_entity": ent_disp_m,
                "prior_action": p_act,
                "prior_recommended_value": p_rec,
                "current_backend_value": cur_bk_str,
                "inheritance_status": "INHERITED_PENDING",
                "resolution_action": f"{p_act} -> {eff_p_rec}",
                "five_dim_evidence": _format_five_dim_evidence(rk_m, live_m, "上期未执行且本期5维复核依然成立，自动继承保留至批量表"),
            })

    # 11.8 Final Deterministic Single-Winner Conflict Resolution (keyed by campaign, match_type, entity)
    deduped_adjustments = resolve_entity_action_conflicts(
        memory_vetted_adjustments, profile=profile, rules=rules, financial_prof=financial_prof, stage_ctx=stage_ctx_eff
    )

    # Bid Floor Protection SOP (standard + fragile + high-refund) + Strict Dayparting timing_base_value Downward Monotonicity Guard
    vetted_post_floor = []
    for item in deduped_adjustments:
        act_i = item.get("action_type", "")
        mt_i = item.get("match_type", "")
        mt_low_i = str(mt_i).lower().strip()
        camp_i = str(item.get("campaign") or "").strip()
        ent_low_i = str(item.get("target_entity") or "").lower().strip()
        is_asin_i = (mt_low_i == "asinsameas" or is_asin_term(ent_low_i))
        floor_v = (as_fl_frg if is_asin_i else kw_fl_frg) if use_elevated_floor else (as_fl_std if is_asin_i else kw_fl_std)

        if ("Bid" in act_i or mt_low_i in ("bidding", "exact", "phrase", "broad", "asinsameas")) and act_i not in (
            "Negative_Exact", "Negative_Phrase", "Negative_Target", "Pause_Keyword", "Remove_Negative",
            "Lower_Budget", "Increase_Budget", "Update_Budget", "High_Refund_Throttle_Budget",
        ):
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", str(item.get("recommended_value", "")))
            if m_bid:
                bid_val = float(m_bid.group(1))
                if bid_val < floor_v and "state" not in str(item.get("recommended_value", "")):
                    item["recommended_value"] = f"${floor_v:.2f}"
                    fl_lbl = "脆弱/高退款" if use_elevated_floor else "v2标准"
                    if "底价保护)" not in str(item.get("reason") or ""):
                        item["reason"] += f" ({fl_lbl} ${floor_v:.2f} 底价保护)"

        # Strict downward monotonicity: NEVER emit a downward bid cut >= timing_base_value or >= current_bid_num!
        if act_i in DOWNWARD_BID_ACTIONS:
            m_rec_chk = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", str(item.get("recommended_value", "")))
            if m_rec_chk:
                rec_num_chk = float(m_rec_chk.group(1))
                live_chk = _get_live_entity_snapshot(camp_i, mt_low_i, ent_low_i)
                cur_anchor = num(item.get("current_bid_num")) or live_chk["bid"]
                t_base_anchor = live_chk["timing_base"]
                if t_base_anchor > 0 and rec_num_chk >= t_base_anchor:
                    adj_b = max(floor_v, round(t_base_anchor - 0.02, 2))
                    if adj_b >= t_base_anchor:
                        continue
                    rec_num_chk = adj_b
                    item["recommended_value"] = f"${rec_num_chk:.2f}"
                if cur_anchor > 0 and rec_num_chk >= cur_anchor:
                    adj_b2 = max(floor_v, round(cur_anchor - 0.02, 2))
                    if adj_b2 >= cur_anchor or (t_base_anchor > 0 and adj_b2 >= t_base_anchor):
                        continue
                    item["recommended_value"] = f"${adj_b2:.2f}"

        vetted_post_floor.append(item)

    # 11.9 v2 Bidirectional Single-Lever Discipline (§5.1) + Global Change Budget Cap (§5.4) + Action Metadata (§7.8)
    kept_after_discipline, deferred_raw, change_budget_stats = v2p.enforce_single_lever_and_change_budget(
        vetted_post_floor,
        camps=camps,
        kws=kws or [],
        targets=targets or [],
        stage_ctx=stage_ctx_eff,
        is_fragile=is_fragile,
        priority_rank_map=ACTION_PRIORITY_RANK,
    )
    deferred_queue = [
        v2p.attach_action_plan_metadata(
            d_it,
            today_dt=today_dt,
            stage_ctx=stage_ctx_eff,
            scaled_thresholds=st_cfg,
            planned_path=str(d_it.get("planned_path") or ""),
        )
        for d_it in deferred_raw
    ]

    sentinel_clean_re = re.compile(r"(?<![0-9])(?:99999999(?:\.\d+)?%?|9999(?:\.0+)?%|999\.0%)(?![0-9])")

    bulk_lines = []
    detail_lines = []
    sanitized_adjustments = []
    for raw_item in kept_after_discipline:
        item = v2p.attach_action_plan_metadata(
            raw_item,
            today_dt=today_dt,
            stage_ctx=stage_ctx_eff,
            scaled_thresholds=st_cfg,
            planned_path=str(raw_item.get("planned_path") or ""),
        )
        act = item.get("action_type", "")
        rec = str(item.get("recommended_value", "")).strip()
        cur = str(item.get("current_value", "")).strip()
        rsn = str(item.get("reason", "")).strip()

        # TSV Zero-Placeholder & Pure Value Sanitizer
        if any(x in act for x in ("Bid", "Throttle", "Elevate", "Boost", "Harvest_ASIN")) and "Schedule" not in rec and "Schedule" not in act and "Placement" not in act and "Budget" not in act:
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bid:
                pure_val = f"${float(m_bid.group(1)):.2f}"
                if rec != pure_val:
                    rsn = f"[{rec}] " + rsn
                    item["recommended_value"] = pure_val
        elif act in ("Lower_Budget", "Increase_Budget", "Update_Budget", "High_Refund_Throttle_Budget"):
            m_bgt = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bgt:
                item["recommended_value"] = f"${float(m_bgt.group(1)):.2f}"

        if act == "Unpause_Keyword":
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bid:
                item["recommended_value"] = f"state=enabled (bid=${float(m_bid.group(1)):.2f})"
            else:
                item["recommended_value"] = "state=enabled"
        elif act == "Pause_Keyword":
            item["recommended_value"] = "state=paused"

        if act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact"):
            m_bid = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", rec)
            if m_bid:
                item["recommended_value"] = f"${float(m_bid.group(1)):.2f}"
                if "[Add Exact Target" not in rsn:
                    rsn = f"[Add Exact Target ${float(m_bid.group(1)):.2f}] " + rsn

        if "Current Lowered Bid" in cur:
            item["current_value"] = f"${kw_fl_frg:.2f}"
        if act == "Probe_Bid_Recovery" and not re.search(r"\$[0-9]", item["recommended_value"]):
            item["recommended_value"] = "$0.45"
            rsn = f"[{rec}] " + rsn

        # Eliminate any 999.0% / 9999.0% / 99999999 sentinel leak inside bulk_adjustments.tsv!
        item["current_value"] = sentinel_clean_re.sub("-- (0单)", str(item.get("current_value") or ""))
        rsn = sentinel_clean_re.sub("-- (0单)", rsn)

        # Strip any accidental tabs/newlines inside field strings so TSV column count is strictly 7 on every row!
        for fld in ("action_type", "campaign", "target_entity", "match_type", "current_value", "recommended_value", "lever_type", "planned_path", "expected_effect", "verify_date", "rollback_rule"):
            item[fld] = re.sub(r"[\t\r\n]+", " ", str(item.get(fld) or "")).strip()
        rsn = re.sub(r"[\t\r\n]+", " ", rsn).strip()
        item["reason"] = rsn
        sanitized_adjustments.append(item)

        bulk_lines.append("\t".join([
            item["action_type"], item["campaign"], item["target_entity"],
            item["match_type"], item["current_value"], item["recommended_value"], item["reason"]
        ]))
        detail_lines.append("\t".join([
            item["action_type"], item["campaign"], item["target_entity"],
            item["match_type"], item["current_value"], item["recommended_value"],
            item["lever_type"], item["planned_path"] or "--",
            item["expected_effect"], item["verify_date"], item["rollback_rule"],
            item["reason"],
        ]))

    bulk_header = "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\treason"
    detail_header = "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\tlever_type\tplanned_path\texpected_effect\tverify_date\trollback_rule\treason"
    tsv(dest / "bulk_adjustments.tsv", bulk_header, bulk_lines)
    tsv(dest / f"bulk_adjustments_{gen_time_str}.tsv", bulk_header, bulk_lines)
    tsv(dest / "action_plan_details.tsv", detail_header, detail_lines)

    # Write next_cycle_queue.tsv (deferred by Single-Lever Discipline or Global Change Budget Cap)
    queue_header = "action_type\tcampaign\ttarget_entity\tmatch_type\tcurrent_value\trecommended_value\tlever_type\tplanned_path\tdefer_reason\texpected_effect\tverify_date\trollback_rule\treason"
    queue_lines = [
        "\t".join(_sanitize_tsv_cell(sentinel_clean_re.sub("-- (0单)", str(q.get(k) or ""))) for k in (
            "action_type", "campaign", "target_entity", "match_type", "current_value",
            "recommended_value", "lever_type", "planned_path", "defer_reason",
            "expected_effect", "verify_date", "rollback_rule", "reason"
        ))
        for q in deferred_queue
    ]
    tsv(dest / "next_cycle_queue.tsv", queue_header, queue_lines)

    # Reconcile budget_realloc_rows with final post-discipline kept vs deferred actions (100% three-way consistency!)
    kept_bgt_by_camp = {
        str(it.get("campaign") or "").strip(): it
        for it in kept_after_discipline
        if str(it.get("match_type") or "").lower().strip() == "budget"
    }
    deferred_bgt_by_camp = {
        str(it.get("campaign") or "").strip(): it
        for it in deferred_queue
        if str(it.get("match_type") or "").lower().strip() == "budget"
    }
    for br in budget_realloc_rows:
        cn_br = str(br.get("campaign") or "").strip()
        if br.get("role") in ("DONOR_DECREASE", "RECIPIENT_INCREASE"):
            if cn_br in kept_bgt_by_camp:
                continue
            elif cn_br in deferred_bgt_by_camp:
                def_r = str(deferred_bgt_by_camp[cn_br].get("defer_reason") or "全局变更预算/单杠杆顺延")
                br["role"] = "DEFERRED_NEXT_CYCLE"
                br["reason"] = f"【顺延下期队列】{def_r}（原候选：{br['reason']}）"
            else:
                orig_r = br["reason"]
                br["role"] = "HOLD"
                br["recommended_budget"] = br["current_budget"]
                br["delta_usd"] = 0.0
                br["reason"] = f"经冷却/冲突仲裁本期保持原预算（原候选：{orig_r}）"
    bgt_lines = [
        "\t".join(_sanitize_tsv_cell(r[k]) for k in (
            "campaign", "eval_window", "current_budget", "spend", "sales", "orders",
            "acos", "roas", "time_in_budget_pct", "oob_hours", "suggested_budget_api",
            "role", "recommended_budget", "delta_usd", "reason"
        ))
        for r in budget_realloc_rows
    ]
    tsv(dest / "budget_reallocation.tsv", bgt_headers, bgt_lines)

    # Write recommendation_inheritance.tsv (100% traceability of prior recommendations)
    inh_header = (
        "first_recommended_date\tlast_recommended_date\tdays_pending\tcampaign\tmatch_type\t"
        "target_entity\tprior_action\tprior_recommended_value\tcurrent_backend_value\t"
        "inheritance_status\tresolution_action\tfive_dim_evidence"
    )
    inh_lines = [
        "\t".join(_sanitize_tsv_cell(r[k]) for k in (
            "first_recommended_date", "last_recommended_date", "days_pending", "campaign", "match_type",
            "target_entity", "prior_action", "prior_recommended_value", "current_backend_value",
            "inheritance_status", "resolution_action", "five_dim_evidence",
        ))
        for r in inheritance_records
    ]
    tsv(dest / "recommendation_inheritance.tsv", inh_header, inh_lines)

    if args.export_dir:
        exp_dir = Path(args.export_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        country_str = str(meta.get("country") or "US").upper()
        tsv(exp_dir / f"{asin_str}-{country_str}-bulk-adjustments-{gen_time_str}.tsv", bulk_header, bulk_lines)
        tsv(exp_dir / f"{asin_str}-{country_str}-bulk-adjustments.tsv", bulk_header, bulk_lines)
        tsv(exp_dir / f"{asin_str}-{country_str}-action-plan-details-{gen_time_str}.tsv", detail_header, detail_lines)
        tsv(exp_dir / f"{asin_str}-{country_str}-next-cycle-queue-{gen_time_str}.tsv", queue_header, queue_lines)
        tsv(exp_dir / f"{asin_str}-{country_str}-budget-reallocation-{gen_time_str}.tsv", bgt_headers, bgt_lines)
        tsv(exp_dir / f"{asin_str}-{country_str}-recommendation-inheritance-{gen_time_str}.tsv", inh_header, inh_lines)
        if dayparting_table_lines:
            tsv(
                exp_dir / f"{asin_str}-{country_str}-dayparting-analysis.tsv",
                "hour_window\tspend\tad_orders\tstore_orders\tcvr\tacos\tcpc\tclassification\trecommendation",
                dayparting_table_lines,
            )
        if financial_prof:
            exp_dir.joinpath(f"{asin_str}-{country_str}-financial-profile.json").write_text(
                json.dumps(financial_prof, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if traffic_prof:
            exp_dir.joinpath(f"{asin_str}-{country_str}-traffic-profile.json").write_text(
                json.dumps(traffic_prof, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return sanitized_adjustments, inheritance_records, deferred_queue, change_budget_stats, budget_realloc_rows, dayparting_gate_info


def write_observation_status(active_cooldowns, camps, dest):
    """observation_status.tsv -- what is still inside its cooling-off window."""
    # 12. Output observation_status.tsv
    obs_lines = []
    for cd in active_cooldowns:
        c_name = cd.get("campaign", "")
        matching_camp = next((c for c in camps if c["name"] == c_name), None)
        camp_assessment = matching_camp.get("assessment", "IN_OBSERVATION") if matching_camp else "IN_OBSERVATION"
        if camp_assessment == "CIRCUIT_BREAKER_BLEED":
            obs_status = "CIRCUIT_BREAKER_BLEED"
        elif camp_assessment == "CIRCUIT_BREAKER_SHOCK":
            obs_status = "CIRCUIT_BREAKER_SHOCK"
        elif camp_assessment == "ADJUSTMENT_EFFECTIVE":
            obs_status = "ADJUSTMENT_EFFECTIVE"
        else:
            obs_status = "NORMAL_MONITORING"

        obs_lines.append("\t".join([
            str(cd.get("entity_type", "")),
            str(cd.get("campaign", "")),
            str(cd.get("field", "")),
            str(cd.get("old_value", "")),
            str(cd.get("new_value", "")),
            str((cd.get("changed_at") or "")[:10]),
            str(cd.get("days_elapsed", 0)),
            str(cd.get("days_remaining", 0)),
            obs_status,
            str(cd.get("detail", "")),
        ]))
    tsv(
        dest / "observation_status.tsv",
        "entity_type\tcampaign\tfield\told_value\tnew_value\tchanged_date\tdays_elapsed\tdays_remaining\tstatus\tdetail",
        obs_lines,
    )


def write_ledger_files(args, dest, gen_time_str, ledger, ledger_dir, meta, profile, snap_key):
    """Persist the historical ledger into the db, the analysis dir, and the export dir."""
    asin_str = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
    country_str = str(meta.get("country") or "US").upper()
    file_prefix = f"{asin_str}-{country_str}"
    exp_dir_p = Path(args.export_dir) if args.export_dir else None
    save_ledger_files(ledger, ledger_dir, snap_key, dest, exp_dir_p, file_prefix, gen_time_str)


def write_summary(
    active_cooldowns,
    active_neg_dict,
    asin_campaigns,
    audit_records,
    bleeding_ng,
    camps,
    cannibal_lines,
    dayparting_records,
    deduped_adjustments,
    dest,
    diff_events,
    financial_prof,
    gen_date_str,
    gen_time_str,
    kw_misfit_adjustments,
    ledger,
    locked_campaigns,
    meta,
    placement_diagnoses,
    profile,
    snap_key,
    sr_map,
    traffic_prof,
    xydc_map=None,
    deal_info=None,
    kws=None,
    targets=None,
    macro_scorecard=None,
    real_placement_records=None,
    rules=None,
    xydc_traffic=None,
    xydc_blindspot_count=0,
    inheritance_records=None,
    target_acos=None,
    stage_ctx=None,
    scaled_thresholds=None,
    change_budget_stats=None,
    deferred_queue=None,
    battlecard=None,
    budget_realloc_rows=None,
    brand_display_rows=None,
    dayparting_gate_info=None,
    ledger_calibration=None,
):
    """summary.txt -- the headline numbers the report template reads."""
    enabled = [c for c in camps if c["state"] == "enabled"]
    total_spend = sum(c["spend"] for c in camps)
    total_sales = sum(c["sales"] for c in camps)
    total_ord = sum(c["ord"] for c in camps)
    total_clk = sum(c["clk"] for c in camps)
    effective_adj_count = sum(1 for x in ledger if "见效" in str(x.get("effect_verdict", "")))
    shock_adj_count = sum(1 for x in ledger if "休克" in str(x.get("effect_verdict", "")))
    bleed_adj_count = sum(1 for x in ledger if "失血" in str(x.get("effect_verdict", "")))
    drift_archived_count = sum(1 for x in ledger if "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED" in str(x.get("effect_verdict", "")))

    summary = []
    summary.append(f"window\t{meta.get('window')}")
    summary.append(f"shop\t{meta.get('shop_alias')}\tsid={meta.get('sid')}\tprofile={meta.get('profile_id')}")
    summary.append(f"title\t{profile.get('title')}")
    summary.append(f"price/stars/reviews\t{profile.get('price')}\t{profile.get('stars')}\t{profile.get('reviews')}")
    summary.append(f"small_rank\t{profile.get('small_rank')}")
    summary.append(f"stock\tfulfillable={profile.get('fulfillable')}\tdos={profile.get('days_of_supply')}\tinv_age_181_plus={profile.get('inv_age_181_plus', 0)}")

    if stage_ctx:
        pk_tag = f"PEAK:{stage_ctx.get('peak_phase')}" if stage_ctx.get("peak_overlay") else "NONE"
        summary.append(
            f"v2_stage_target_card\tstage={stage_ctx.get('stage_mode')}\tpeak={pk_tag}"
            f"\ttarget_acos={stage_ctx.get('target_acos', 0):.1f}%\ttarget_tacos={stage_ctx.get('target_tacos', 0):.1f}%"
            f"\ttcpa=${stage_ctx.get('tcpa', 0):.2f}\tad_unsustainable={stage_ctx.get('ad_unsustainable_zone', False)}"
            f"\tcogs_missing={stage_ctx.get('cogs_data_missing', False)}\tcogs_estimated={stage_ctx.get('cogs_is_estimated', False)}"
            f"\treason={stage_ctx.get('stage_reason', '')}"
        )
    if scaled_thresholds:
        summary.append(
            f"v2_scaled_thresholds\ttcpa=${scaled_thresholds.get('tcpa', 0):.2f}"
            f"\tcvr_base={scaled_thresholds.get('cvr_base_pct', 0):.1f}%"
            f"\tfail_clicks={scaled_thresholds.get('fail_test_clicks', 25)}"
            f"\tfail_spend=${scaled_thresholds.get('fail_test_spend', 20):.1f}"
            f"\tcb_7d=${scaled_thresholds.get('cb_bleed_spend_7d', 15):.1f}"
            f"\tcb_3d=${scaled_thresholds.get('cb_bleed_spend_3d', 15):.1f}"
            f"\tmin_over_acos_30d=${scaled_thresholds.get('min_over_acos_spend_30d', 8):.1f}"
        )
    if change_budget_stats:
        summary.append(
            f"v2_change_budget\tdaily_spend_30d=${change_budget_stats.get('portfolio_daily_spend_30d', 0):.2f}"
            f"\tcap=${change_budget_stats.get('change_budget_cap_usd', 0):.2f}({change_budget_stats.get('change_budget_share_limit_pct', 35):.0f}%)"
            f"\tused=${change_budget_stats.get('change_budget_used_usd', 0):.2f}({change_budget_stats.get('change_budget_used_pct', 0):.1f}%)"
            f"\tdeferred={change_budget_stats.get('deferred_count', 0)}\tnote={change_budget_stats.get('note', 'OK')}"
        )
    if battlecard is not None:
        summary.append(f"v2_core_keyword_battlecard_count\t{len(battlecard)}")
    if budget_realloc_rows is not None:
        b_rec = sum(1 for r in budget_realloc_rows if r.get("role") == "RECIPIENT_INCREASE")
        b_don = sum(1 for r in budget_realloc_rows if r.get("role") == "DONOR_DECREASE")
        b_def = sum(1 for r in budget_realloc_rows if r.get("role") == "DEFERRED_NEXT_CYCLE")
        summary.append(f"v2_budget_reallocation\tcampaigns_evaluated={len(budget_realloc_rows)}\trecipients={b_rec}\tdonors={b_don}\tdeferred={b_def}")
    if brand_display_rows is not None:
        summary.append(f"v2_brand_display_health_count\t{len(brand_display_rows)}")
    if dayparting_gate_info is not None:
        summary.append(
            f"v2_dayparting_sample_gate\tpassed={dayparting_gate_info.get('passed')}"
            f"\torders_30d={dayparting_gate_info.get('total_ad_orders_30d', 0)}"
            f"\tmin_required={dayparting_gate_info.get('min_orders_required', 60)}"
        )
    if ledger_calibration and ledger_calibration.get("calibration_alerts"):
        summary.append("v2_ledger_calibration_alerts\t" + " | ".join(ledger_calibration["calibration_alerts"]))

    if macro_scorecard:
        r7 = macro_scorecard.get("recent_7d") or {}
        p7 = macro_scorecard.get("prior_7d") or {}
        d7 = macro_scorecard.get("deltas_7d_vs_prior7d") or {}
        summary.append(f"macro_north_star_verdict\t{macro_scorecard.get('verdict')}\t{macro_scorecard.get('verdict_cn')}\tshrinkage_guard={macro_scorecard.get('velocity_shrinkage_guard')}")
        summary.append(
            f"macro_7d_vs_prior7d\tsales=${r7.get('daily_sales', 0):.2f}/d(vs ${p7.get('daily_sales', 0):.2f}/d, {d7.get('sales_delta_pct', 0):+.1f}%)"
            f"\tunits={r7.get('daily_units', 0):.2f}/d({d7.get('units_delta_pct', 0):+.1f}%)"
            f"\torganic_units={r7.get('daily_organic_units', 0):.2f}/d({r7.get('organic_unit_share_pct', 0):.1f}%)"
            f"\treal_tacos={r7.get('real_tacos_pct', 0):.2f}%({d7.get('tacos_delta_pp', 0):+.2f}pp)"
        )
        n_exp = macro_scorecard.get("next_7d_expectations") or {}
        summary.append(
            f"macro_next_7d_expectations\tsales_min=${n_exp.get('expected_daily_sales_min', 0):.2f}/d"
            f"\tsales_floor=${n_exp.get('circuit_breaker_daily_sales_floor', 0):.2f}/d"
            f"\ttacos_max={n_exp.get('expected_tacos_max', 0):.1f}%"
            f"\tacos_max={n_exp.get('expected_ad_acos_max', 0):.1f}%"
        )
        if macro_scorecard.get("confounders"):
            summary.append("macro_confounders\t" + " | ".join(f"{c['code']}:{c['detail']}" for c in macro_scorecard["confounders"]))

    total_cpc = (total_spend / total_clk) if total_clk > 0 else 0.0
    total_spend_14d = sum(c.get("spend_14d", 0.0) for c in camps)
    total_sales_14d = sum(c.get("sales_14d", 0.0) for c in camps)
    total_ord_14d = sum(c.get("ord_14d", 0) for c in camps)
    total_clk_14d = sum(c.get("clk_14d", 0) for c in camps)
    total_cpc_14d = (total_spend_14d / total_clk_14d) if total_clk_14d > 0 else 0.0

    total_spend_7d = sum(c.get("spend_7d", 0.0) for c in camps)
    total_sales_7d = sum(c.get("sales_7d", 0.0) for c in camps)
    total_ord_7d = sum(c.get("ord_7d", 0) for c in camps)
    total_clk_7d = sum(c.get("clk_7d", 0) for c in camps)
    total_cpc_7d = (total_spend_7d / total_clk_7d) if total_clk_7d > 0 else 0.0

    total_spend_3d = sum(c.get("spend_3d", 0.0) for c in camps)
    total_sales_3d = sum(c.get("sales_3d", 0.0) for c in camps)
    total_ord_3d = sum(c.get("ord_3d", 0) for c in camps)
    total_clk_3d = sum(c.get("clk_3d", 0) for c in camps)
    total_cpc_3d = (total_spend_3d / total_clk_3d) if total_clk_3d > 0 else 0.0

    en_spend_30d = sum(c.get("spend", 0.0) for c in enabled)
    en_sales_30d = sum(c.get("sales", 0.0) for c in enabled)
    en_ord_30d = sum(c.get("ord", 0) for c in enabled)
    en_clk_30d = sum(c.get("clk", 0) for c in enabled)
    en_cpc_30d = (en_spend_30d / en_clk_30d) if en_clk_30d > 0 else 0.0

    en_spend_14d = sum(c.get("spend_14d", 0.0) for c in enabled)
    en_sales_14d = sum(c.get("sales_14d", 0.0) for c in enabled)
    en_ord_14d = sum(c.get("ord_14d", 0) for c in enabled)
    en_clk_14d = sum(c.get("clk_14d", 0) for c in enabled)
    en_cpc_14d = (en_spend_14d / en_clk_14d) if en_clk_14d > 0 else 0.0

    en_spend_7d = sum(c.get("spend_7d", 0.0) for c in enabled)
    en_sales_7d = sum(c.get("sales_7d", 0.0) for c in enabled)
    en_ord_7d = sum(c.get("ord_7d", 0) for c in enabled)
    en_clk_7d = sum(c.get("clk_7d", 0) for c in enabled)
    en_cpc_7d = (en_spend_7d / en_clk_7d) if en_clk_7d > 0 else 0.0

    en_spend_3d = sum(c.get("spend_3d", 0.0) for c in enabled)
    en_sales_3d = sum(c.get("sales_3d", 0.0) for c in enabled)
    en_ord_3d = sum(c.get("ord_3d", 0) for c in enabled)
    en_clk_3d = sum(c.get("clk_3d", 0) for c in enabled)
    en_cpc_3d = (en_spend_3d / en_clk_3d) if en_clk_3d > 0 else 0.0

    summary.append(f"portfolio_totals_30d_all\tspend={total_spend:.2f}\tsales={total_sales:.2f}\tacos={fmt_acos_text(acos(total_spend,total_sales), total_spend, total_sales, decimals=2)}\tord={int(total_ord)}\tcpc={total_cpc:.2f}")
    summary.append(f"portfolio_totals_30d_enabled\tspend={en_spend_30d:.2f}\tsales={en_sales_30d:.2f}\tacos={fmt_acos_text(acos(en_spend_30d,en_sales_30d), en_spend_30d, en_sales_30d, decimals=2)}\tord={int(en_ord_30d)}\tcpc={en_cpc_30d:.2f}")
    if total_spend_14d > 0 or total_sales_14d > 0 or total_ord_14d > 0:
        summary.append(f"portfolio_totals_14d_all\tspend={total_spend_14d:.2f}\tsales={total_sales_14d:.2f}\tacos={fmt_acos_text(acos(total_spend_14d,total_sales_14d), total_spend_14d, total_sales_14d, decimals=2)}\tord={int(total_ord_14d)}\tcpc={total_cpc_14d:.2f}")
        summary.append(f"portfolio_totals_14d_enabled\tspend={en_spend_14d:.2f}\tsales={en_sales_14d:.2f}\tacos={fmt_acos_text(acos(en_spend_14d,en_sales_14d), en_spend_14d, en_sales_14d, decimals=2)}\tord={int(en_ord_14d)}\tcpc={en_cpc_14d:.2f}")
    summary.append(f"portfolio_totals_7d_all\tspend={total_spend_7d:.2f}\tsales={total_sales_7d:.2f}\tacos={fmt_acos_text(acos(total_spend_7d,total_sales_7d), total_spend_7d, total_sales_7d, decimals=2)}\tord={int(total_ord_7d)}\tcpc={total_cpc_7d:.2f}")
    summary.append(f"portfolio_totals_7d_enabled\tspend={en_spend_7d:.2f}\tsales={en_sales_7d:.2f}\tacos={fmt_acos_text(acos(en_spend_7d,en_sales_7d), en_spend_7d, en_sales_7d, decimals=2)}\tord={int(en_ord_7d)}\tcpc={en_cpc_7d:.2f}")
    summary.append(f"portfolio_totals_3d_all\tspend={total_spend_3d:.2f}\tsales={total_sales_3d:.2f}\tacos={fmt_acos_text(acos(total_spend_3d,total_sales_3d), total_spend_3d, total_sales_3d, decimals=2)}\tord={int(total_ord_3d)}\tcpc={total_cpc_3d:.2f}")
    summary.append(f"portfolio_totals_3d_enabled\tspend={en_spend_3d:.2f}\tsales={en_sales_3d:.2f}\tacos={fmt_acos_text(acos(en_spend_3d,en_sales_3d), en_spend_3d, en_sales_3d, decimals=2)}\tord={int(en_ord_3d)}\tcpc={en_cpc_3d:.2f}")
    summary.append(f"enabled_campaigns\t{len(enabled)} / {len(camps)}")
    summary.append(f"asin_campaigns_filtered\t{len(asin_campaigns)}")
    summary.append(f"real_placement_rows_loaded\t{len(real_placement_records or [])}")
    off_bleed_camps = [c["name"] for c in enabled if c.get("off_amazon_status") == "OFF_AMAZON_BLEED"]
    off_prot_camps = [c["name"] for c in enabled if c.get("off_amazon_status") == "OFF_AMAZON_PROTECTED"]
    if off_bleed_camps:
        summary.append("off_amazon_bleed_campaigns\t" + " | ".join(off_bleed_camps[:12]))
    if off_prot_camps:
        summary.append("off_amazon_protected_campaigns\t" + " | ".join(off_prot_camps[:12]))
    up_down_camps = [c["name"] for c in enabled if c.get("is_up_and_down")]
    if up_down_camps:
        summary.append("up_and_down_strategy_campaigns\t" + " | ".join(up_down_camps[:12]))
    high_tos = [c["name"] for c in enabled if c["tos"] >= 180]
    summary.append("high_tos>=180\t" + " | ".join(high_tos[:12]))
    if rules and rules.get("_quarantined_is_not"):
        summary.append("quarantined_self_contradictory_rules\t" + ", ".join(str(x) for x in rules["_quarantined_is_not"]))
    summary.append(f"active_negatives_total\t{len(active_neg_dict)}")
    promoted_active_count = sum(1 for r in audit_records if r["audit_status"] == "PROMOTED_MIGRATED_PROTECTED")
    summary.append(f"promoted_migrated_protected\t{promoted_active_count}")
    migrated_paused_count = sum(1 for r in audit_records if r["audit_status"] == "MIGRATED_BUT_PAUSED_IN_EXACT")
    summary.append(f"migrated_paused_in_exact\t{migrated_paused_count}")
    true_orphan_count = sum(1 for r in audit_records if r["audit_status"] == "TRUE_ORPHAN_NEGATIVE")
    summary.append(f"true_orphan_negatives\t{true_orphan_count}")
    safe_hard_count = sum(1 for r in audit_records if r["audit_status"] in ("SAFE_HARD_NEGATIVE", "SAFE_ASIN_DEFENSE"))
    summary.append(f"safe_hard_negatives_confirmed\t{safe_hard_count}")
    soft_monitor_count = sum(1 for r in audit_records if r["audit_status"] == "SOFT_MONITOR")
    summary.append(f"soft_monitor_negatives\t{soft_monitor_count}")
    summary.append(f"conflicting_keywords_paused\t{len(kw_misfit_adjustments)}")
    summary.append(f"bleeding_ngrams_count\t{len(bleeding_ng)}")
    summary.append(f"cannibalized_queries_count\t{len(cannibal_lines)}")
    summary.append(f"bulk_adjustments_count\t{len(deduped_adjustments)}")
    summary.append(f"historical_adjustments_tracked\t{len(ledger)}")
    summary.append(f"effective_adjustments_count\t{effective_adj_count}")
    summary.append(f"shock_adjustments_count\t{shock_adj_count}")
    summary.append(f"bleed_adjustments_count\t{bleed_adj_count}")
    summary.append(f"dayparting_drift_archived_count\t{drift_archived_count}")
    summary.append(f"active_cooldowns_count\t{len(active_cooldowns)}")
    summary.append(f"locked_campaigns_count\t{len(locked_campaigns)}")
    if inheritance_records is not None:
        inh_pend_cnt = sum(1 for r in inheritance_records if r.get("inheritance_status") == "INHERITED_PENDING")
        inh_sup_cnt = sum(1 for r in inheritance_records if r.get("inheritance_status") == "SUPERSEDED_BY_DATA")
        inh_exe_cnt = sum(1 for r in inheritance_records if r.get("inheritance_status") == "EXECUTED_IN_COOLDOWN")
        summary.append(f"recommendation_inheritance_summary\tinherited_pending={inh_pend_cnt}\tsuperseded_by_data={inh_sup_cnt}\texecuted_in_cooldown={inh_exe_cnt}")
    shared_brand_cnt = len(profile.get("shared_brand_campaigns") or [])
    if shared_brand_cnt > 0:
        summary.append(f"shared_brand_campaigns_count\t{shared_brand_cnt}")
    summary.append(f"database_daily_record_saved\tdb/daily_records/{snap_key}_{gen_date_str}.json")
    summary.append(f"database_ledger_saved\tdb/ledger/{snap_key}_ledger.json")
    summary.append(f"database_entity_memory_saved\tdb/entity_memory/{snap_key}_memory.json")
    summary.append(f"database_expectations_saved\tdb/expectations/{snap_key}_expectations.json")
    summary.append(f"searchrank_keywords_tracked\t{len(sr_map)}")
    trapped_count = sum(1 for d in placement_diagnoses if d.get("strategy_tag") == "TRAPPED_CORE_HERO")
    failed_tos_count = sum(1 for d in placement_diagnoses if d.get("strategy_tag") == "FAILED_AT_TOS")
    summary.append(f"placement_diagnosed_queries\t{len(placement_diagnoses)}")
    summary.append(f"trapped_core_heroes_elevate\t{trapped_count}")
    summary.append(f"premier_tos_failed_throttle\t{failed_tos_count}")
    protected_strong_count = sum(1 for a in deduped_adjustments if a.get("action_type") in ("Refine_Bid_Placement", "Elevate_To_TOS", "Throttle_TOS_Bid", "Rank_Slide_Protection_Hold"))
    summary.append(f"strong_relevance_queries_protected\t{protected_strong_count}")
    competitor_actions_count = sum(1 for a in deduped_adjustments if a.get("match_type") == "asinSameAs")
    summary.append(f"competitor_asin_actions\t{competitor_actions_count}")
    if financial_prof:
        summary.append(
            f"financial_gross_profit\t${financial_prof.get('gross_profit', 0):.2f}"
            f"\tadjusted_gp=${financial_prof.get('adjusted_gross_profit', financial_prof.get('gross_profit', 0)):.2f}"
            f"\tmargin={financial_prof.get('adjusted_gross_margin', financial_prof.get('gross_margin', 0)):.1f}%"
            f"\tcm_unit=${financial_prof.get('cm_unit', 0):.2f}\tcm_pct={financial_prof.get('cm_pct', 0):.1f}%"
            f"\tmarginal_be_acos={financial_prof.get('marginal_break_even_acos', financial_prof.get('break_even_acos', 0)):.1f}%"
            f"\ttrue_all_in_be_tacos={financial_prof.get('true_all_in_break_even_tacos', 0):.2f}%"
            f"\ttarget_acos={(target_acos or 0):.1f}%"
            f"\tzero_ad_loss_cb={financial_prof.get('zero_ad_loss_circuit_breaker', False)}"
            f"\tcogs_missing={financial_prof.get('cogs_data_missing', False)}"
            f"\ttotal_ad_cost=${financial_prof.get('total_ad_cost', 0):.2f}"
        )
        summary.append(
            f"financial_cost_structure\tcogs=${financial_prof.get('cogs_per_unit', 0):.2f}"
            f"\tfreight=${financial_prof.get('logistics_per_unit', 0):.2f}"
            f"\tfba=${financial_prof.get('fba_per_unit', 0):.2f}"
            f"\tcomm=${financial_prof.get('comm_per_unit', 0):.2f}"
            f"\tstorage=${financial_prof.get('storage_fee_total', 0):.2f}"
            f"\trefund_rate={financial_prof.get('refund_rate', 0):.2f}%"
            f"\tunit_return_rate={financial_prof.get('unit_return_rate_pct', 0):.2f}%"
        )
        if financial_prof.get("is_cross_shop_asin") and financial_prof.get("omni_shop_summary"):
            omni = financial_prof["omni_shop_summary"]
            summary.append(f"cross_shop_omni_summary\tshops={omni.get('shop_count')}\tomni_sales=${omni.get('omni_total_sales', 0):.2f}\tomni_gp=${omni.get('omni_gross_profit', 0):.2f}\tomni_tacos={omni.get('omni_tacos', 0):.2f}%")
    if traffic_prof:
        tot_sess = traffic_prof.get("sessions_total", 0)
        mob_sess = traffic_prof.get("sessions_mobile", 0)
        mob_share = (mob_sess / tot_sess * 100) if tot_sess > 0 else 0.0
        summary.append(f"listing_traffic_macro\tsessions={tot_sess}\tpageviews={traffic_prof.get('page_views_total', 0)}\tmobile_share={mob_share:.1f}%\tbuy_box={traffic_prof.get('buy_box_percentage', 0):.1f}%\tattribution_lag={traffic_prof.get('attribution_lag', False)}")
        summary.append(f"listing_cvr_comparison\tad_cvr={traffic_prof.get('ad_cvr', 0):.2f}%\tnature_cvr={traffic_prof.get('nature_cvr', 0):.2f}%\ttacos={traffic_prof.get('tacos', 0):.2f}%\tadv_sales_share={traffic_prof.get('adv_sales_share', 0):.2f}%")
    if dayparting_records:
        peak_hrs = [f"{r['hour']:02d}:00" for r in dayparting_records if r["classification"] == "PEAK_GOLDEN_HOUR"]
        bleed_hrs = [f"{r['hour']:02d}:00" for r in dayparting_records if r["classification"] == "BLEEDING_OFFPEAK_HOUR"]
        summary.append(f"dayparting_peak_hours\t{', '.join(peak_hrs) if peak_hrs else 'None'}")
        summary.append(f"dayparting_bleeding_hours\t{', '.join(bleed_hrs) if bleed_hrs else 'None'}")
    if kws:
        timing_kws = [k for k in kws if k.get("is_apply_time")]
        summary.append(f"dayparting_keywords_active\t{len(timing_kws)} / {len(kws)}")
    if targets:
        timing_targets = [t for t in targets if t.get("is_apply_time")]
        summary.append(f"dayparting_targets_active\t{len(timing_targets)} / {len(targets)}")
    all_timing_templates = set()
    for item in (kws or []) + (targets or []):
        if item.get("is_apply_time"):
            for t in item.get("applied_templates") or []:
                if t.get("name"):
                    all_timing_templates.add(t.get("name"))
    if all_timing_templates:
        summary.append(f"dayparting_active_templates\t{', '.join(sorted(all_timing_templates))}")
    if diff_events:
        summary.append(f"diff_events_detected\t{' ; '.join(diff_events[:5])}")
    if xydc_map:
        t1_cnt = sum(1 for v in xydc_map.values() if v.get("organic_rank") and v.get("organic_rank") <= 3)
        t2_cnt = sum(1 for v in xydc_map.values() if v.get("organic_rank") and 4 <= v.get("organic_rank") <= 15)
        t3_cnt = sum(1 for v in xydc_map.values() if v.get("organic_rank") and 16 <= v.get("organic_rank") <= 50)
        t4_cnt = sum(1 for v in xydc_map.values() if (v.get("organic_rank") is None or v.get("organic_rank") > 50) and (v.get("sp_rank") and v.get("sp_rank") <= 10))
        summary.append(f"xydc_ranks_total_tracked\t{len(xydc_map)}")
        summary.append(f"xydc_tier1_dominance_count\t{t1_cnt}")
        summary.append(f"xydc_tier2_ascender_count\t{t2_cnt}")
        summary.append(f"xydc_tier3_breakthrough_count\t{t3_cnt}")
        summary.append(f"xydc_tier4_disconnect_count\t{t4_cnt}")
        summary.append(f"xydc_tier5_blindspot_count\t{int(xydc_blindspot_count or 0)}")
    if xydc_traffic and isinstance(xydc_traffic, dict):
        st7 = xydc_traffic.get("self_traffic_7d") or {}
        if st7:
            summary.append(
                f"xydc_self_traffic_7d\ttotal_score={num(st7.get('total_traffic_score')):.1f}"
                f"\torg_score={num(st7.get('organic_traffic_score')):.1f}({num(st7.get('organic_share_pct')):.1f}%)"
                f"\tad_score={num(st7.get('ad_traffic_score')):.1f}({num(st7.get('ad_share_pct')):.1f}%)"
                f"\torg_growth={num(st7.get('organic_growth_pct')):+.1f}%"
                f"\tad_growth={num(st7.get('ad_growth_pct')):+.1f}%"
                f"\torg_kw_cnt={int(num(st7.get('organic_keyword_count')))}"
                f"\tad_kw_cnt={int(num(st7.get('ad_keyword_count')))}"
            )
        p_ov = xydc_traffic.get("parent_ad_overview_7d") or {}
        if p_ov:
            ch = p_ov.get("channel_distribution") or {}
            summary.append(
                f"xydc_parent_channel_split\tparent={p_ov.get('parent_asin') or '--'}"
                f"\tvariations={p_ov.get('advertised_variation_count', 0)}/{p_ov.get('total_variation_count', 0)}"
                f"\torganic={num((ch.get('organic') or {}).get('share_pct')):.1f}%"
                f"\tsp={num((ch.get('sp') or {}).get('share_pct')):.1f}%"
                f"\tsb={num((ch.get('sb') or {}).get('share_pct')):.1f}%"
                f"\tsbv={num((ch.get('sbv') or {}).get('share_pct')):.1f}%"
            )
        kc_tr = xydc_traffic.get("keyword_count_trend") or {}
        if kc_tr:
            snap_kc = kc_tr.get("latest_snapshot") or {}
            summary.append(
                f"xydc_keyword_count_14d_trend\torg_7d_avg={num(kc_tr.get('recent_7d_avg_organic_keywords')):.1f}"
                f"({num(kc_tr.get('organic_keywords_delta_pct')):+.1f}%)"
                f"\tad_7d_avg={num(kc_tr.get('recent_7d_avg_ad_keywords')):.1f}"
                f"({num(kc_tr.get('ad_keywords_delta_pct')):+.1f}%)"
                f"\thead={snap_kc.get('head_count', 0)}\tmid={snap_kc.get('mid_tail_count', 0)}\tlong={snap_kc.get('long_tail_count', 0)}"
            )
        bsr_tr = xydc_traffic.get("bsr_trend") or {}
        leaf_b = bsr_tr.get("leaf_category") if isinstance(bsr_tr, dict) else None
        if isinstance(leaf_b, dict) and leaf_b.get("latest_rank"):
            summary.append(
                f"xydc_bsr_leaf_14d_trend\tcategory={leaf_b.get('category_name')}"
                f"\tlatest=#{leaf_b.get('latest_rank')}\tavg_7d=#{leaf_b.get('recent_7d_avg_rank')}"
                f"\tprior_7d=#{leaf_b.get('prior_7d_avg_rank')}\tchange={num(leaf_b.get('rank_change_7d')):+.1f}"
            )
    if deal_info:
        summary.append(f"deal_status_badge\t{deal_info.get('summary_badge', 'NONE')}")
        summary.append(f"deal_status_desc\t{deal_info.get('summary_desc', '无近期秒杀/促销')}")
        summary.append(f"deal_is_transition\t{deal_info.get('is_deal_transition', False)}")
        summary.append(f"deal_last_date\t{deal_info.get('last_deal_date') or '--'}")
    summary.append(f"generation_timestamp\t{gen_time_str}")
    dest.joinpath("summary.txt").write_text("\n".join(summary), encoding="utf-8")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--rules", default=None, help="Optional path to product_rules.json / rules.json")
    parser.add_argument("--snapshots-dir", default=None, help="Optional path to snapshots directory")
    parser.add_argument("--db-dir", default=None, help="Optional path to database directory (default: workspace db/)")
    parser.add_argument("--cooldown-days", type=int, default=None, help="Cooldown observation period in days (default: rules.json cooldown_days or 7)")
    parser.add_argument("--target-acos", type=float, default=None, help="Target ACOS percent threshold (e.g. 25.0)")
    parser.add_argument("--mode", default=None, help="Stage mode: AUTO|LAUNCH|GROWTH|PROFIT|DEFEND|STOP_LOSS|CLEARANCE (optionally +PEAK)")
    parser.add_argument("--peak", action="store_true", help="Force PEAK promotional overlay")
    parser.add_argument("--no-peak", action="store_true", help="Disable auto-detected PEAK promotional overlay")
    parser.add_argument("--timestamp", default=None, help="Optional timestamp string (e.g. 20260926-104901). Defaults to current local time.")
    parser.add_argument("--known-negatives", default=None, help="Comma-separated or file path of already negated keywords/phrases")
    parser.add_argument("--export-dir", default=None, help="Optional export directory (e.g. outputs) to export timestamped bulk adjustments table.")
    parser.add_argument("--shop", default=None, help="Optional shop alias/name for multi-shop portfolio isolation.")
    args = parser.parse_args()

    src = Path(args.indir)
    dest = Path(args.outdir)
    dest.mkdir(parents=True, exist_ok=True)

    known_negatives = load_known_negatives(args=args, src=src)
    dayparting_records, dayparting_table_lines, financial_prof, gen_time_str, meta, profile, rules, target_acos, traffic_prof, deal_info = load_product_context(
        args=args,
        dest=dest,
        src=src,
    )
    asin_campaigns, camps, kws, kws_3d, kws_7d, targets, targets_3d, targets_7d = load_report_frames(
        meta=meta,
        profile=profile,
        src=src,
    )
    kws_14d, targets_14d, _ = load_optional_14d_frames(src=src, asin_campaigns=asin_campaigns)
    group_bid_lookup, camp_group_bid_lookup = build_adgroup_bid_lookups(src=src, asin_campaigns=asin_campaigns)
    sr_map = load_searchrank_map(src=src)
    xydc_map = load_xydc_ranks(src=src)
    xydc_traffic = load_xydc_traffic(src=src)

    active_cooldowns, daily_records_dir, db_root, diff_events, gen_date_str, ledger, ledger_dir, legacy_key, locked_campaigns, snap_key = run_state_tracking(
        args=args,
        camps=camps,
        dayparting_records=dayparting_records,
        financial_prof=financial_prof,
        kws=kws,
        meta=meta,
        profile=profile,
        src=src,
        targets=targets,
        traffic_prof=traffic_prof,
        kws_3d=kws_3d,
        kws_7d=kws_7d,
        targets_3d=targets_3d,
        targets_7d=targets_7d,
        xydc_map=xydc_map,
        deal_info=deal_info,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        target_acos=target_acos,
        gen_time_str=gen_time_str,
        rules=rules,
    )
    entity_memory = load_entity_memory(db_root=db_root, snap_key=snap_key, legacy_key=legacy_key)

    # Load prior expectations (read-only) & run preliminary macro scorecard (no disk overwrite yet)
    exp_dir_ro = db_root / "expectations"
    prev_exp_path = _find_case_insensitive_db_file(
        exp_dir_ro,
        [f"{snap_key}_expectations.json", f"{legacy_key}_expectations.json"],
    ) if exp_dir_ro.exists() else None
    prior_expectations = load_json(prev_exp_path) if prev_exp_path else None

    macro_prelim = evaluate_macro_scorecard(
        src=src,
        dest=dest,
        export_dir=None,
        db_root=db_root,
        snap_key=snap_key,
        legacy_key=legacy_key,
        gen_time_str=gen_time_str,
        meta=meta,
        profile=profile,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        camps=camps,
        deal_info=deal_info,
        target_acos=target_acos,
        write_expectations=False,
    )

    # Settle final v2 stage_ctx & TCPA/CVR_base scaled thresholds with full xydc_map + macro shrinkage context
    en_sp_sum = sum(c.get("spend", 0.0) for c in camps if c.get("state") == "enabled")
    en_sl_sum = sum(c.get("sales", 0.0) for c in camps if c.get("state") == "enabled")
    if en_sp_sum <= 0:
        en_sp_sum = sum(c.get("spend", 0.0) for c in camps)
        en_sl_sum = sum(c.get("sales", 0.0) for c in camps)
    portfolio_acos_30d = acos(en_sp_sum, en_sl_sum) if en_sl_sum > 0 else None

    _, win_end_main = _parse_window_bounds(meta or {})
    stage_ctx = v2p.determine_stage_and_targets(
        raw_mode_arg=getattr(args, "mode", None),
        peak_flag=bool(getattr(args, "peak", False)),
        no_peak_flag=bool(getattr(args, "no_peak", False)),
        rules=rules,
        profile=profile,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        deal_info=deal_info,
        xydc_map=xydc_map,
        shrinkage_signal=bool(macro_prelim.get("velocity_shrinkage_guard")),
        explicit_target_acos=args.target_acos,
        anchor_date_str=win_end_main or (deal_info or {}).get("anchor_date"),
        portfolio_acos_30d=float(portfolio_acos_30d or 0.0),
    )
    target_acos = float(stage_ctx["target_acos"])
    cvr_base_ratio = v2p.compute_portfolio_base_cvr(camps, traffic_prof)
    scaled_thresholds = v2p.compute_scaled_thresholds(
        tcpa=float(stage_ctx["tcpa"]),
        cvr_base_ratio=cvr_base_ratio,
        cfg=stage_ctx["config"],
    )

    # Sync settled v2 stage_ctx & thresholds back to profile & financial_profile.json
    profile["stage_context"] = {k: v for k, v in stage_ctx.items() if k != "config"}
    profile["stage_mode"] = stage_ctx["stage_mode"]
    profile["peak_overlay"] = stage_ctx["peak_overlay"]
    profile["peak_phase"] = stage_ctx["peak_phase"]
    profile["tcpa"] = stage_ctx["tcpa"]
    profile["critical_refund_alert"] = bool(stage_ctx["critical_refund_lv1"])
    profile["scaled_thresholds"] = scaled_thresholds
    profile["target_acos"] = target_acos
    profile["target_tacos"] = stage_ctx["target_tacos"]
    profile["zero_ad_loss_circuit_breaker"] = stage_ctx["zero_ad_loss_circuit_breaker"]
    profile["ad_unsustainable_zone"] = stage_ctx["ad_unsustainable_zone"]
    if isinstance(profile.get("rules_loaded"), dict):
        profile["rules_loaded"]["target_acos"] = target_acos
        profile["rules_loaded"]["target_tacos"] = stage_ctx["target_tacos"]
        profile["rules_loaded"]["stage_mode"] = stage_ctx["stage_mode"]
        profile["rules_loaded"]["peak_overlay"] = stage_ctx["peak_overlay"]
        profile["rules_loaded"]["zero_ad_loss_circuit_breaker"] = stage_ctx["zero_ad_loss_circuit_breaker"]
        profile["rules_loaded"]["ad_unsustainable_zone"] = stage_ctx["ad_unsustainable_zone"]
    if financial_prof:
        financial_prof["effective_target_acos"] = target_acos
        financial_prof["target_tacos"] = stage_ctx["target_tacos"]
        financial_prof["target_cpa"] = stage_ctx["tcpa"]
        financial_prof["stage_mode"] = stage_ctx["stage_mode"]
        financial_prof["peak_overlay"] = stage_ctx["peak_overlay"]
        financial_prof["peak_phase"] = stage_ctx["peak_phase"]
        financial_prof["ad_unsustainable_zone"] = stage_ctx["ad_unsustainable_zone"]
        financial_prof["zero_ad_loss_circuit_breaker"] = stage_ctx["zero_ad_loss_circuit_breaker"]
        dest.joinpath("financial_profile.json").write_text(
            json.dumps(financial_prof, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    dest.joinpath("product_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    real_placement_records = load_real_placement_report(
        src=src, camps=camps, dest=dest,
        target_acos=float(stage_ctx["effective_eval_acos"]),
        scaled_thresholds=scaled_thresholds,
    )

    # Build v2 Core Keyword Battlecard (§6.3) & SB/SBV/SD Independent Health Check (§7.10)
    st_rows_bc = [
        r for r in as_rows(load_json(src / "search_terms.json"))
        if not skip_total(r) and (not asin_campaigns or r.get("campaign_name") in asin_campaigns)
    ]
    battlecard = v2p.build_core_keyword_battlecard(
        xydc_map=xydc_map,
        sr_map=sr_map,
        kws=kws,
        kws_7d=kws_7d,
        kws_14d=kws_14d,
        search_terms=st_rows_bc,
        camps=camps,
        profile=profile,
        rules=rules,
        entity_memory=entity_memory,
        prior_expectations=prior_expectations,
        stage_ctx=stage_ctx,
        scaled_thresholds=scaled_thresholds,
        gen_date_str=gen_date_str,
        classify_fn=classify_query,
        firewall_fn=check_global_compliance_firewall,
        get_searchrank_fn=get_searchrank,
        compute_rank_deltas_fn=_compute_rank_deltas,
    )
    bc_header = (
        "keyword\tbucket\taba_rank\taba_tier\tcurr_org_rank\tdelta_or\tcurr_sp_rank\tdelta_sp\t"
        "conv_window\tspend_recent\torders_recent\tacos_recent\tspend_30d\torders_30d\tacos_30d\t"
        "cvr_adj_pct\tactive_campaign\tis_dedicated_campaign\trole\ttarget_or\tverify_window_days\tprior_target_status"
    )
    bc_lines = [
        "\t".join(_sanitize_tsv_cell(
            fmt_acos_text(b[k], b["spend_recent"], b["orders_recent"]) if k == "acos_recent"
            else (fmt_acos_text(b[k], b["spend_30d"], b["orders_30d"]) if k == "acos_30d"
                  else ("--" if b.get(k) is None else b.get(k)))
        ) for k in (
            "keyword", "bucket", "aba_rank", "aba_tier", "curr_org_rank", "delta_or", "curr_sp_rank", "delta_sp",
            "conv_window", "spend_recent", "orders_recent", "acos_recent", "spend_30d", "orders_30d", "acos_30d",
            "cvr_adj_pct", "active_campaign", "is_dedicated_campaign", "role", "target_or", "verify_window_days", "prior_target_status"
        ))
        for b in battlecard
    ]
    tsv(dest / "core_keyword_battlecard.tsv", bc_header, bc_lines)

    brand_display_rows = v2p.build_brand_display_health(
        camps=camps,
        shared_brand_campaigns=profile.get("shared_brand_campaigns"),
        kws=kws,
        profile=profile,
        battlecard=battlecard,
    )
    bd_header = (
        "campaign\tad_format\tsource\tstate\tattribution_window\tspend_30d\tsales_30d\torders_30d\t"
        "acos_30d\troas_30d\tntb_metrics\tbrand_defense_covered\tbattlecard_covered_count\tbattlecard_total\t"
        "is_shared_multi_asin_campaign\thealth_note"
    )
    bd_lines = [
        "\t".join(_sanitize_tsv_cell(
            fmt_acos_text(r["acos_30d"], r["spend_30d"], r["sales_30d"]) if k == "acos_30d" else r.get(k)
        ) for k in (
            "campaign", "ad_format", "source", "state", "attribution_window", "spend_30d", "sales_30d", "orders_30d",
            "acos_30d", "roas_30d", "ntb_metrics", "brand_defense_covered", "battlecard_covered_count", "battlecard_total",
            "is_shared_multi_asin_campaign", "health_note"
        ))
        for r in brand_display_rows
    ]
    tsv(dest / "brand_display_health.tsv", bd_header, bd_lines)

    if args.export_dir:
        exp_dir_bc = Path(args.export_dir)
        exp_dir_bc.mkdir(parents=True, exist_ok=True)
        asin_bc = str(meta.get("asin") or profile.get("asin") or "UNKNOWN").upper()
        ctry_bc = str(meta.get("country") or "US").upper()
        tsv(exp_dir_bc / f"{asin_bc}-{ctry_bc}-core-keyword-battlecard-{gen_time_str}.tsv", bc_header, bc_lines)
        tsv(exp_dir_bc / f"{asin_bc}-{ctry_bc}-brand-display-health-{gen_time_str}.tsv", bd_header, bd_lines)

    write_campaigns_tsv(camps=camps, dest=dest)
    assess_and_write_placements(
        active_cooldowns=active_cooldowns,
        args=args,
        camps=camps,
        dest=dest,
        locked_campaigns=locked_campaigns,
        target_acos=target_acos,
        traffic_prof=traffic_prof,
        kws=kws,
        xydc_map=xydc_map,
        deal_info=deal_info,
        financial_prof=financial_prof,
        profile=profile,
        stage_ctx=stage_ctx,
        scaled_thresholds=scaled_thresholds,
    )
    now_iso = datetime.now().isoformat()
    archive_daily_record(
        daily_records_dir, snap_key, gen_date_str, now_iso,
        meta, profile, financial_prof, traffic_prof, camps, dayparting_records
    )
    write_keywords_tsv(
        dest=dest,
        kws=kws,
        sr_map=sr_map,
        xydc_map=xydc_map,
        kws_7d=kws_7d,
        kws_3d=kws_3d,
        target_acos=target_acos,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        scaled_thresholds=scaled_thresholds,
        stage_ctx=stage_ctx,
    )
    active_neg_dict, all_neg_queries, audit_records, bucket_stats, bulk_adjustments, camp_lookup, cannibal_map, harvest, kw_misfit_adjustments, ngram_stats, placement_diagnoses, st_lines, unnegative_adjustments, waste = analyze_search_terms(
        asin_campaigns=asin_campaigns,
        camps=camps,
        known_negatives=known_negatives,
        kws=kws,
        kws_3d=kws_3d,
        kws_7d=kws_7d,
        profile=profile,
        rules=rules,
        sr_map=sr_map,
        src=src,
        target_acos=target_acos,
        xydc_map=xydc_map,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        targets=targets,
        targets_7d=targets_7d,
        targets_3d=targets_3d,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        macro_scorecard=macro_prelim,
        entity_memory=entity_memory,
        stage_ctx=stage_ctx,
        scaled_thresholds=scaled_thresholds,
    )
    write_search_term_outputs(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        harvest=harvest,
        meta=meta,
        placement_diagnoses=placement_diagnoses,
        profile=profile,
        st_lines=st_lines,
        waste=waste,
    )
    _, blindspot_rows = write_rank_synergy_tsv(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        kws=kws,
        meta=meta,
        profile=profile,
        target_acos=target_acos,
        xydc_map=xydc_map,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        camps=camps,
        rules=rules,
        sr_map=sr_map,
        entity_memory=entity_memory,
        gen_date_str=gen_date_str,
        scaled_thresholds=scaled_thresholds,
        stage_ctx=stage_ctx,
    )
    write_xydc_traffic_and_competitor_tsvs(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        meta=meta,
        profile=profile,
        target_acos=target_acos,
        targets=targets,
        xydc_traffic=xydc_traffic,
        camp_lookup=camp_lookup,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        src=src,
        asin_campaigns=asin_campaigns,
        scaled_thresholds=scaled_thresholds,
        stage_ctx=stage_ctx,
    )
    write_buckets_tsv(bucket_stats=bucket_stats, dest=dest)
    bleeding_ng = write_ngram_outputs(
        all_neg_queries=all_neg_queries,
        bulk_adjustments=bulk_adjustments,
        dest=dest,
        known_negatives=known_negatives,
        ngram_stats=ngram_stats,
        profile=profile,
        rules=rules,
        financial_prof=financial_prof,
        target_acos=target_acos,
        scaled_thresholds=scaled_thresholds,
    )
    cannibal_lines = write_cannibalization_tsv(cannibal_map=cannibal_map, dest=dest)
    write_groups_tsv(asin_campaigns=asin_campaigns, dest=dest, src=src)
    write_targets_tsv(
        bulk_adjustments=bulk_adjustments,
        camp_lookup=camp_lookup,
        dest=dest,
        profile=profile,
        target_acos=target_acos,
        targets=targets,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        targets_7d=targets_7d,
        targets_3d=targets_3d,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        macro_scorecard=macro_prelim,
        locked_campaigns=locked_campaigns,
        stage_ctx=stage_ctx,
        scaled_thresholds=scaled_thresholds,
    )
    write_negative_tsvs(active_neg_dict=active_neg_dict, audit_records=audit_records, dest=dest)
    deduped_adjustments, inheritance_records, deferred_queue, change_budget_stats, budget_realloc_rows, dayparting_gate_info = assemble_and_write_adjustments(
        args=args,
        bulk_adjustments=bulk_adjustments,
        camps=camps,
        dayparting_records=dayparting_records,
        dayparting_table_lines=dayparting_table_lines,
        dest=dest,
        financial_prof=financial_prof,
        gen_time_str=gen_time_str,
        kw_misfit_adjustments=kw_misfit_adjustments,
        locked_campaigns=locked_campaigns,
        meta=meta,
        profile=profile,
        target_acos=target_acos,
        traffic_prof=traffic_prof,
        unnegative_adjustments=unnegative_adjustments,
        kws=kws,
        kws_7d=kws_7d,
        kws_3d=kws_3d,
        xydc_map=xydc_map,
        sr_map=sr_map,
        deal_info=deal_info,
        targets=targets,
        targets_7d=targets_7d,
        targets_3d=targets_3d,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        macro_scorecard=macro_prelim,
        entity_memory=entity_memory,
        ledger=ledger,
        rules=rules,
        active_neg_dict=active_neg_dict,
        known_negatives=known_negatives,
        stage_ctx=stage_ctx,
        scaled_thresholds=scaled_thresholds,
        battlecard=battlecard,
    )

    # Persist final macro scorecard & expectations.json with settled v2 Target Card, battlecard targets & change budget
    macro_scorecard = evaluate_macro_scorecard(
        src=src,
        dest=dest,
        export_dir=getattr(args, "export_dir", None),
        db_root=db_root,
        snap_key=snap_key,
        legacy_key=legacy_key,
        gen_time_str=gen_time_str,
        meta=meta,
        profile=profile,
        financial_prof=financial_prof,
        traffic_prof=traffic_prof,
        camps=camps,
        deal_info=deal_info,
        target_acos=target_acos,
        stage_ctx=stage_ctx,
        battlecard=battlecard,
        change_budget_stats=change_budget_stats,
        write_expectations=True,
    )

    sync_and_write_entity_full_context(
        db_root=db_root,
        dest=dest,
        snap_key=snap_key,
        gen_date_str=gen_date_str,
        entity_memory=entity_memory,
        kws=kws,
        kws_7d=kws_7d,
        kws_3d=kws_3d,
        targets=targets,
        targets_7d=targets_7d,
        targets_3d=targets_3d,
        xydc_map=xydc_map,
        sr_map=sr_map,
        group_bid_lookup=group_bid_lookup,
        camp_group_bid_lookup=camp_group_bid_lookup,
        ledger=ledger,
        deduped_adjustments=deduped_adjustments,
        inheritance_records=inheritance_records,
        deferred_queue=deferred_queue,
    )
    ledger_calibration = v2p.compute_ledger_calibration(ledger)
    dest.joinpath("ledger_calibration.json").write_text(
        json.dumps(ledger_calibration, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_observation_status(active_cooldowns=active_cooldowns, camps=camps, dest=dest)
    write_ledger_files(
        args=args,
        dest=dest,
        gen_time_str=gen_time_str,
        ledger=ledger,
        ledger_dir=ledger_dir,
        meta=meta,
        profile=profile,
        snap_key=snap_key,
    )
    summary = write_summary(
        active_cooldowns=active_cooldowns,
        active_neg_dict=active_neg_dict,
        asin_campaigns=asin_campaigns,
        audit_records=audit_records,
        bleeding_ng=bleeding_ng,
        camps=camps,
        cannibal_lines=cannibal_lines,
        dayparting_records=dayparting_records,
        deduped_adjustments=deduped_adjustments,
        dest=dest,
        diff_events=diff_events,
        financial_prof=financial_prof,
        gen_date_str=gen_date_str,
        gen_time_str=gen_time_str,
        kw_misfit_adjustments=kw_misfit_adjustments,
        ledger=ledger,
        locked_campaigns=locked_campaigns,
        meta=meta,
        placement_diagnoses=placement_diagnoses,
        profile=profile,
        snap_key=snap_key,
        sr_map=sr_map,
        traffic_prof=traffic_prof,
        xydc_map=xydc_map,
        deal_info=deal_info,
        kws=kws,
        targets=targets,
        macro_scorecard=macro_scorecard,
        real_placement_records=real_placement_records,
        rules=rules,
        xydc_traffic=xydc_traffic,
        xydc_blindspot_count=len(blindspot_rows),
        inheritance_records=inheritance_records,
        target_acos=target_acos,
        stage_ctx=stage_ctx,
        scaled_thresholds=scaled_thresholds,
        change_budget_stats=change_budget_stats,
        deferred_queue=deferred_queue,
        battlecard=battlecard,
        budget_realloc_rows=budget_realloc_rows,
        brand_display_rows=brand_display_rows,
        dayparting_gate_info=dayparting_gate_info,
        ledger_calibration=ledger_calibration,
    )

    print(f"Analysis complete. Tables saved to {dest}")
    print("\n".join(summary[:22]))


if __name__ == "__main__":
    main()
