# -*- coding: utf-8 -*-
"""v2_policy.py — Stage-aware, profit-constrained, statistically calibrated decision engine
for Lingxing Ads Playbook v2.

Implements:
- Appendix A configurable rules loader (100% backward-compatible with legacy product_rules.json)
- §1 Product Stage Mode (LAUNCH/GROWTH/PROFIT/DEFEND/STOP_LOSS/CLEARANCE) + PEAK overlay
- §2 Unit contribution margin (CM_unit / CM%), TCPA, and ad-unsustainable zone (< 8% BE / CM <= 0)
- §3 TCPA & CVR_base scaled thresholds + Bayesian smoothed CVR_adj
- §5 Single-lever discipline (E1~E5), unified step-size caps + multi-period planned_path,
     and Global Change Budget Cap (35% / 20% fragile) with deferred next-cycle queue
- §6.3 Core Keyword Battlecard (5~15 target keywords with role & target OR reconciliation)
- §7.5 Budget Reallocation across enabled campaigns + 60-order dayparting sample gate
- §7.8 Action-plan metadata (lever_type, planned_path, expected_effect, verify_date, rollback_rule)
- §7.10 SB/SBV/SD independent health check (14d attribution & NTB separation)
- §10 Historical ledger action-type achievement rate calibration
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any


VALID_STAGE_MODES = ("LAUNCH", "GROWTH", "PROFIT", "DEFEND", "STOP_LOSS", "CLEARANCE")
VALID_PEAK_PHASES = ("NONE", "PRE_HEAT", "PEAK_DAY", "POST_COOLDOWN")

DEFAULT_RESTRICTED_TERMS = [
    "weed", "weeds", "marijuana", "cannabis", "thc", "cbd", "bong", "bongs",
    "kief", "herb weed", "kratom", "psilocybin", "shroom", "shrooms",
    "tobacco", "cigarette", "vape", "vaping", "joint roller",
    "blunt grinder", "hash grinder", "hashish", "crack pipe", "meth",
]

DEFAULT_RULE_CONFIG: dict[str, Any] = {
    "mode": "AUTO",
    "peak_mode": "AUTO",
    "launch_acos_multiplier": 1.30,
    "launch_weeks": 6,
    "launch_budget_cap": None,
    "growth_acos_coef": 0.85,
    "profit_acos_coef": 0.68,
    "profit_acos_min": 12.0,
    "profit_acos_max": 35.0,
    "target_net_margin": 10.0,
    "refund_lv1": 12.0,
    "refund_lv2": 15.0,
    "restricted_terms": [],
    "bid_floor_kw": 0.25,
    "bid_floor_kw_fragile": 0.30,
    "bid_floor_asin": 0.36,
    "bid_floor_asin_fragile": 0.42,
    "cvr_prior_k": 20.0,
    "fail_test_clicks_factor": 3.0,
    "fail_test_clicks_min": 15,
    "fail_test_clicks_legacy_floor": 25,
    "change_budget_share": 0.35,
    "change_budget_share_fragile": 0.20,
    "cooldown_days": 7,
    "daypart_min_orders_30d": 60,
    "organic_flywheel_cvr": 5.0,
    "organic_flywheel_adv_share": 75.0,
    "organic_flywheel_orders": 45,
    "organic_flywheel_sessions": 1500,
    "organic_fragile_cvr": 1.0,
    "organic_fragile_adv_share": 90.0,
    "organic_fragile_orders": 15,
    "low_stock_buffer_days": 14,
    "lead_time_days": None,
    "clearance": False,
}


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "" or val == "--" or isinstance(val, bool):
        return default
    try:
        s = str(val).replace("%", "").replace(",", "").replace("$", "").strip()
        fv = float(s)
        if not math.isfinite(fv):
            return default
        return fv
    except Exception:
        return default


def get_effective_low_stock_days(cfg: dict[str, Any] | None) -> float:
    """Effective low-stock warning threshold in days (§7.2 & Appendix A):
    uses max(low_stock_buffer_days, lead_time_days + 7) when lead_time_days is set, else low_stock_buffer_days (default 14)."""
    c = cfg or DEFAULT_RULE_CONFIG
    buf = max(3.0, min(90.0, _safe_float(c.get("low_stock_buffer_days"), 14.0)))
    lt = c.get("lead_time_days")
    if lt is not None:
        lt_v = _safe_float(lt, 0.0)
        if lt_v > 0.0:
            return max(buf, lt_v + 7.0)
    return buf


def resolve_rule_config(rules: dict | None) -> dict[str, Any]:
    """Merge Appendix A defaults with overrides from rules.json (fully compatible with legacy rules)."""
    cfg = dict(DEFAULT_RULE_CONFIG)
    if not isinstance(rules, dict):
        return cfg

    # Support both top-level keys and nested "v2_config" / "params" dict in rules.json
    nested = rules.get("v2_config") if isinstance(rules.get("v2_config"), dict) else {}
    params = rules.get("params") if isinstance(rules.get("params"), dict) else {}
    merged_src = {**params, **nested, **rules}

    for k, default_v in DEFAULT_RULE_CONFIG.items():
        if k in merged_src and merged_src[k] is not None:
            v = merged_src[k]
            if k == "restricted_terms":
                if isinstance(v, list):
                    cfg[k] = [str(x).strip().lower() for x in v if str(x).strip()]
            elif k in ("mode", "peak_mode"):
                cfg[k] = v
            elif k == "clearance":
                cfg[k] = bool(v)
            elif k in ("launch_budget_cap", "lead_time_days"):
                if v not in (None, "", False):
                    fv_opt = _safe_float(v, -1.0)
                    cfg[k] = fv_opt if fv_opt > 0.0 else None
                else:
                    cfg[k] = None
            elif isinstance(default_v, int):
                cfg[k] = int(round(_safe_float(v, float(default_v))))
            elif isinstance(default_v, float):
                fv = _safe_float(v, default_v)
                # Normalize percentage shares if user passed 35 instead of 0.35 for change_budget_share
                if k in ("change_budget_share", "change_budget_share_fragile") and fv > 1.0:
                    fv = fv / 100.0
                cfg[k] = fv
            else:
                cfg[k] = v

    # Guard reasonable finite domain bounds against malformed/negative/NaN/Inf overrides
    cfg["launch_acos_multiplier"] = max(0.5, min(3.0, float(cfg["launch_acos_multiplier"])))
    cfg["launch_weeks"] = max(1, min(26, int(cfg["launch_weeks"])))
    cfg["growth_acos_coef"] = max(0.5, min(1.2, float(cfg["growth_acos_coef"])))
    cfg["profit_acos_coef"] = max(0.3, min(1.0, float(cfg["profit_acos_coef"])))
    cfg["profit_acos_min"] = max(8.0, min(50.0, float(cfg["profit_acos_min"])))
    cfg["profit_acos_max"] = max(cfg["profit_acos_min"], min(80.0, float(cfg["profit_acos_max"])))
    cfg["target_net_margin"] = max(0.0, min(50.0, float(cfg["target_net_margin"])))
    cfg["refund_lv1"] = max(3.0, min(50.0, float(cfg["refund_lv1"])))
    cfg["refund_lv2"] = max(cfg["refund_lv1"], min(60.0, float(cfg["refund_lv2"])))
    for fl_k in ("bid_floor_kw", "bid_floor_kw_fragile", "bid_floor_asin", "bid_floor_asin_fragile"):
        cfg[fl_k] = max(0.05, min(5.0, float(cfg[fl_k])))
    cfg["cvr_prior_k"] = max(1.0, min(200.0, float(cfg["cvr_prior_k"])))
    cfg["fail_test_clicks_factor"] = max(1.0, min(10.0, float(cfg["fail_test_clicks_factor"])))
    cfg["fail_test_clicks_min"] = max(5, min(200, int(cfg["fail_test_clicks_min"])))
    cfg["fail_test_clicks_legacy_floor"] = max(5, min(200, int(cfg["fail_test_clicks_legacy_floor"])))
    cfg["cooldown_days"] = max(3, min(14, int(cfg["cooldown_days"])))
    cfg["daypart_min_orders_30d"] = max(10, min(500, int(cfg["daypart_min_orders_30d"])))
    cfg["low_stock_buffer_days"] = max(3, min(90, int(cfg["low_stock_buffer_days"])))
    cfg["change_budget_share"] = max(0.05, min(1.0, float(cfg["change_budget_share"])))
    cfg["change_budget_share_fragile"] = max(0.05, min(1.0, float(cfg["change_budget_share_fragile"])))
    cfg["organic_flywheel_cvr"] = max(0.5, min(50.0, float(cfg["organic_flywheel_cvr"])))
    cfg["organic_flywheel_adv_share"] = max(10.0, min(100.0, float(cfg["organic_flywheel_adv_share"])))
    cfg["organic_flywheel_orders"] = max(1, min(10000, int(cfg["organic_flywheel_orders"])))
    cfg["organic_flywheel_sessions"] = max(10, min(1000000, int(cfg["organic_flywheel_sessions"])))
    cfg["organic_fragile_cvr"] = max(0.0, min(20.0, float(cfg["organic_fragile_cvr"])))
    cfg["organic_fragile_adv_share"] = max(20.0, min(100.0, float(cfg["organic_fragile_adv_share"])))
    cfg["organic_fragile_orders"] = max(1, min(1000, int(cfg["organic_fragile_orders"])))
    return cfg


def build_restricted_terms_regex(rules: dict | None) -> re.Pattern[str] | None:
    """Compile optional custom restricted_terms from rules.json into a word-boundary regex."""
    cfg = resolve_rule_config(rules)
    custom = cfg.get("restricted_terms") or []
    if not custom:
        return None
    escaped = [re.escape(t).replace(r"\ ", r"\s+") for t in custom if t]
    if not escaped:
        return None
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.I)


def parse_mode_arg(raw_mode: str | None, peak_flag: bool = False, no_peak_flag: bool = False) -> tuple[str | None, bool | None]:
    """Parse --mode argument supporting e.g. 'PROFIT', 'GROWTH+PEAK', 'AUTO', 'LAUNCH'.

    Returns (stage_mode_or_None_for_auto, explicit_peak_bool_or_None).
    """
    explicit_peak: bool | None = True if peak_flag else (False if no_peak_flag else None)
    if not raw_mode:
        return None, explicit_peak

    s = str(raw_mode).strip().upper()
    if not s or s == "AUTO":
        return None, explicit_peak

    parts = [p.strip() for p in re.split(r"[+,/|\s]+", s) if p.strip()]
    stage: str | None = None
    for p in parts:
        if p == "PEAK":
            explicit_peak = True
        elif p in VALID_STAGE_MODES:
            stage = p
        elif p == "AUTO":
            stage = None
    return stage, explicit_peak


def detect_peak_overlay(
    anchor_date_str: str | None,
    deal_info: dict | None,
    cfg: dict[str, Any],
    explicit_peak: bool | None = None,
) -> tuple[bool, str, str]:
    """Determine PEAK overlay status and phase (PRE_HEAT / PEAK_DAY / POST_COOLDOWN / NONE).

    Uses data window anchor_date (NOT wall-clock time) so historical replays remain deterministic.
    """
    if explicit_peak is False:
        return False, "NONE", "显式关闭大促叠加态 (--no-peak)"

    peak_cfg = cfg.get("peak_mode", "AUTO")
    if peak_cfg is False or str(peak_cfg).upper() == "FALSE" or str(peak_cfg).upper() == "OFF":
        if explicit_peak is not True:
            return False, "NONE", "rules.json 关闭大促叠加态"
    if isinstance(peak_cfg, str) and peak_cfg.upper() in ("PRE_HEAT", "PEAK_DAY", "POST_COOLDOWN"):
        ph = peak_cfg.upper()
        return True, ph, f"rules.json 显式指定大促阶段 {ph}"

    ref_dt = None
    if anchor_date_str:
        try:
            ref_dt = datetime.strptime(str(anchor_date_str)[:10], "%Y-%m-%d").date()
        except Exception:
            ref_dt = None

    in_cal_peak_day = False
    if ref_dt is not None:
        _m, _d = ref_dt.month, ref_dt.day
        in_cal_peak_day = (
            (_m == 11 and _d >= 23) or (_m == 12 and _d <= 2)
            or (_m == 7 and 11 <= _d <= 18)
            or (_m == 10 and 7 <= _d <= 11)
        )

    if explicit_peak is True or peak_cfg is True or str(peak_cfg).upper() == "ON":
        if (
            deal_info
            and deal_info.get("is_deal_transition")
            and not deal_info.get("is_active_deal")
            and not in_cal_peak_day
        ):
            return True, "POST_COOLDOWN", "显式开启 PEAK 且处于大促后冷却过渡期"
        return True, "PEAK_DAY", "显式开启大促叠加态 (PEAK)"

    # Auto-detect from deal_info and anchor_date calendar (BFCM / Prime Day / PBDD)
    if deal_info and deal_info.get("is_active_deal"):
        return True, "PEAK_DAY", f"检测到进行中的秒杀/大促活动 ({deal_info.get('active_deal_window') or deal_info.get('last_deal_date')})"

    if ref_dt is not None:
        m, d = ref_dt.month, ref_dt.day
        # BFCM window: Nov 10 - Nov 22 PRE_HEAT, Nov 23 - Dec 2 PEAK_DAY, Dec 3 - Dec 14 POST_COOLDOWN
        if m == 11 and 10 <= d <= 22:
            return True, "PRE_HEAT", f"数据基准日 {ref_dt.isoformat()} 处于黑五/网一预热期 (大促前 7~14 天)"
        if (m == 11 and d >= 23) or (m == 12 and d <= 2):
            return True, "PEAK_DAY", f"数据基准日 {ref_dt.isoformat()} 处于黑五/网一大促窗口"
        if m == 12 and 3 <= d <= 14:
            return True, "POST_COOLDOWN", f"数据基准日 {ref_dt.isoformat()} 处于黑五/网一大促后 10~14 天回落冷却期"
        # Prime Day window (mid-July): Jul 1-10 PRE_HEAT, Jul 11-18 PEAK_DAY, Jul 19-28 POST_COOLDOWN
        if m == 7 and 1 <= d <= 10:
            return True, "PRE_HEAT", f"数据基准日 {ref_dt.isoformat()} 处于 Prime Day 预热窗口"
        if m == 7 and 11 <= d <= 18:
            return True, "PEAK_DAY", f"数据基准日 {ref_dt.isoformat()} 处于 Prime Day 窗口"
        if m == 7 and 19 <= d <= 28:
            return True, "POST_COOLDOWN", f"数据基准日 {ref_dt.isoformat()} 处于 Prime Day 大促后 10 天回落冷却期"
        # Prime Big Deal Days window (October): Oct 1-6 PRE_HEAT, Oct 7-11 PEAK_DAY, Oct 12-22 POST_COOLDOWN
        if m == 10 and 1 <= d <= 6:
            return True, "PRE_HEAT", f"数据基准日 {ref_dt.isoformat()} 处于 Prime Big Deal Days (秋季大促) 预热窗口"
        if m == 10 and 7 <= d <= 11:
            return True, "PEAK_DAY", f"数据基准日 {ref_dt.isoformat()} 处于 Prime Big Deal Days (秋季大促) 窗口"
        if m == 10 and 12 <= d <= 22:
            return True, "POST_COOLDOWN", f"数据基准日 {ref_dt.isoformat()} 处于 Prime Big Deal Days 大促后 10 天回落冷却期"

    if deal_info and deal_info.get("is_deal_transition"):
        days_since = deal_info.get("days_since_last_deal")
        return True, "POST_COOLDOWN", f"秒杀/大促结束第 {days_since} 天（10~14 天促后回落冷却期）"

    return False, "NONE", "常规非大促期"


def determine_stage_and_targets(
    *,
    raw_mode_arg: str | None,
    peak_flag: bool,
    no_peak_flag: bool,
    rules: dict | None,
    profile: dict | None,
    financial_prof: dict | None,
    traffic_prof: dict | None,
    deal_info: dict | None,
    xydc_map: dict | None,
    shrinkage_signal: bool,
    explicit_target_acos: float | None,
    anchor_date_str: str | None = None,
    portfolio_acos_30d: float = 0.0,
) -> dict[str, Any]:
    """Determine product lifecycle stage mode, PEAK overlay, target ACOS, target TACOS, TCPA,
    and mode-level permissions strictly following §1 and §2."""
    cfg = resolve_rule_config(rules)
    fin = financial_prof or {}
    trf = traffic_prof or {}
    prof = profile or {}

    cli_stage, explicit_peak = parse_mode_arg(raw_mode_arg, peak_flag=peak_flag, no_peak_flag=no_peak_flag)
    rule_stage, rule_peak = parse_mode_arg(str(cfg.get("mode") or ""), False, False)
    forced_stage = cli_stage or rule_stage
    if explicit_peak is None and rule_peak is not None:
        explicit_peak = rule_peak

    peak_overlay, peak_phase, peak_reason = detect_peak_overlay(
        anchor_date_str, deal_info, cfg, explicit_peak=explicit_peak
    )

    total_sales = _safe_float(fin.get("total_sales"))
    avg_price = _safe_float(fin.get("avg_price")) or _safe_float(prof.get("price")) or 25.0
    marginal_be = _safe_float(fin.get("marginal_break_even_acos"))
    true_all_in_be = _safe_float(fin.get("true_all_in_break_even_tacos"))
    pre_ad_contrib = _safe_float(fin.get("pre_ad_net_contribution"))
    refund_rate = _safe_float(fin.get("refund_rate"))
    cogs_is_estimated = bool(fin.get("cogs_is_estimated"))
    cogs_data_missing = bool(fin.get("cogs_data_missing"))

    # Decision Break-Even (CM%) = all-in break-even when sales exist, else marginal BE
    has_fin_sales = total_sales > 0.0
    cm_pct = true_all_in_be if has_fin_sales else marginal_be
    cm_unit = _safe_float(fin.get("cm_unit"))
    if cm_unit == 0.0 and avg_price > 0 and cm_pct != 0.0:
        cm_unit = round(avg_price * cm_pct / 100.0, 2)

    ad_unsustainable_zone = bool(
        has_fin_sales and (pre_ad_contrib <= 0.0 or true_all_in_be < 8.0)
    )
    zero_ad_loss_cb = bool(has_fin_sales and (pre_ad_contrib <= 0.0 or true_all_in_be <= 0.0))

    reviews = int(_safe_float(prof.get("reviews")))
    fulfillable = _safe_float(prof.get("fulfillable"), -1.0)
    dos = _safe_float(prof.get("days_of_supply"), -1.0)
    inv_age_181_plus = _safe_float(prof.get("inv_age_181_plus"), 0.0)
    if inv_age_181_plus <= 0.0:
        inv_age_181_plus = (
            _safe_float(prof.get("inv_age_181_270"), 0.0)
            + _safe_float(prof.get("inv_age_271_365"), 0.0)
            + _safe_float(prof.get("inv_age_365_plus"), 0.0)
        )
    organic_readiness = str(trf.get("organic_readiness") or "ORGANIC_BALANCED")

    # Median organic rank of top xydc keywords (for LAUNCH / PROFIT hints)
    top_or_ranks = sorted(
        int(v["organic_rank"])
        for v in (xydc_map or {}).values()
        if isinstance(v, dict) and v.get("organic_rank") is not None and int(v["organic_rank"]) > 0
    )[:10]
    median_core_or = top_or_ranks[len(top_or_ranks) // 2] if top_or_ranks else None
    best_core_or = top_or_ranks[0] if top_or_ranks else None

    # Stage Auto-Detection (§1.1)
    auto_stage = "GROWTH"
    auto_reasons: list[str] = []
    inv_denom = max(fulfillable, _safe_float(prof.get("available_total"), 0.0))
    if ad_unsustainable_zone:
        auto_stage = "STOP_LOSS"
        if zero_ad_loss_cb:
            auto_reasons.append(f"零广告即亏 (广告前净贡献 ${pre_ad_contrib:.2f} / 全口径保本 {true_all_in_be:.2f}%)")
        else:
            auto_reasons.append(f"全口径保本 {true_all_in_be:.2f}% < 8%（广告不可持续区，须先修价格/成本/费用结构）")
    elif cfg.get("clearance") or (
        inv_denom > 0 and inv_age_181_plus > 0 and (inv_age_181_plus / inv_denom >= 0.50 and dos >= 180)
    ):
        auto_stage = "CLEARANCE"
        if cfg.get("clearance"):
            auto_reasons.append("rules.json 显式声明清仓 (clearance=true)")
        else:
            age_pct = min(100.0, inv_age_181_plus / inv_denom * 100.0)
            auto_reasons.append(f"超180天高库龄清仓特征 (DOS={dos:.0f}天, >180天库龄={int(inv_age_181_plus)}/{int(inv_denom)}件={age_pct:.0f}%)")
    elif reviews < 30 and (prof.get("title") or reviews > 0):
        auto_stage = "LAUNCH"
        auto_reasons.append(f"评论数 {reviews} < 30（新品冷启动/推排名窗口）")
        if median_core_or is not None and median_core_or > 50:
            auto_reasons.append(f"核心词中位自然位 #{median_core_or} > 50")
    elif shrinkage_signal or organic_readiness == "ORGANIC_FRAGILE":
        auto_stage = "DEFEND"
        if shrinkage_signal:
            auto_reasons.append("北极星大盘缩量防线激活")
        if organic_readiness == "ORGANIC_FRAGILE":
            auto_reasons.append("自然承接底盘脆弱 (ORGANIC_FRAGILE)")
    elif organic_readiness == "ORGANIC_FLYWHEEL" and (best_core_or is None or best_core_or <= 10):
        auto_stage = "PROFIT"
        auto_reasons.append(
            f"成熟自然飞轮 (ORGANIC_FLYWHEEL, 自然CVR={_safe_float(trf.get('nature_cvr')):.1f}%"
            + (f", 头部词自然位 #{best_core_or}" if best_core_or else "")
            + ")"
        )
    else:
        auto_stage = "GROWTH"
        auto_reasons.append(
            f"健康均衡成长态 ({organic_readiness}, 全口径保本 {cm_pct:.1f}%"
            + (f", 头部词自然位 #{best_core_or}" if best_core_or else "")
            + ")"
        )

    stage_mode = forced_stage or auto_stage
    stage_source = "user_cli" if cli_stage else ("rules_json" if rule_stage else "auto_detected")
    stage_reason = (
        f"显式指定 {stage_mode}（自动判别参考: {auto_stage} — {'; '.join(auto_reasons)}）"
        if forced_stage
        else "; ".join(auto_reasons)
    )

    # Target ACOS & Target TACOS derivation by Stage Mode (§1.1 & §2.1)
    target_net_margin = _safe_float(cfg.get("target_net_margin"), 10.0)
    refund_lv1 = _safe_float(cfg.get("refund_lv1"), 12.0)
    refund_lv2 = _safe_float(cfg.get("refund_lv2"), 15.0)
    critical_refund_lv1 = refund_rate >= refund_lv1
    critical_refund_lv2 = refund_rate >= refund_lv2

    be_ref = cm_pct if cm_pct > 0 else (marginal_be if marginal_be > 0 else 25.0)
    target_acos_note = ""
    investment_or_clearance_note = ""

    if explicit_target_acos is not None:
        target_acos = round(float(explicit_target_acos), 1)
        target_acos_note = f"显式命令行指定 --target-acos {target_acos:.1f}%"
    else:
        rules_ta = rules.get("target_acos") if (rules and rules.get("target_acos") is not None) else None
        if stage_mode == "STOP_LOSS":
            # Do NOT let stale legacy rules.json target_acos defeat STOP_LOSS financial guardrail!
            target_acos = round(max(8.0, min(15.0, cm_pct * 0.85)), 1) if cm_pct >= 8.0 else 8.0
            target_acos_note = (
                f"STOP_LOSS 止损区（全口径保本 {cm_pct:.2f}% < 8% 或零广告即亏）："
                f"不强压无意义 3% 地板，也不虚设 12% 目标；广告仅保留品牌防守与已验证盈利精准词，请优先修复售价/COGS/退货结构"
            )
            if rules_ta is not None:
                target_acos_note += f"（已覆盖 rules.json 静态值 {_safe_float(rules_ta):.1f}%）"
        elif rules_ta is not None:
            target_acos = round(_safe_float(rules_ta, 25.0), 1)
            target_acos_note = f"rules.json 指定初始 target_acos {target_acos:.1f}%"
        elif stage_mode == "LAUNCH":
            mult = _safe_float(cfg.get("launch_acos_multiplier"), 1.30)
            base_be = cm_pct if cm_pct >= 8.0 else (marginal_be if marginal_be >= 8.0 else 25.0)
            target_acos = round(min(80.0, max(15.0, base_be * mult)), 1)
            cap_str = f"${cfg['launch_budget_cap']:.0f}" if cfg.get("launch_budget_cap") else "未设定（请运营在 rules.json 设置 launch_budget_cap）"
            investment_or_clearance_note = (
                f"LAUNCH 投资期允许 ACOS <= BE × {mult:.2f} = {target_acos:.1f}%（默认周期 {cfg['launch_weeks']} 周，累计投资上限: {cap_str}）"
            )
            target_acos_note = investment_or_clearance_note
        elif stage_mode == "GROWTH":
            g_coef = max(0.75, min(1.0, _safe_float(cfg.get("growth_acos_coef"), 0.85)))
            target_acos = round(be_ref * g_coef, 1)
            target_acos_note = f"GROWTH 成长期目标 = 全口径保本 {be_ref:.1f}% × {g_coef:.2f} = {target_acos:.1f}%"
        elif stage_mode == "PROFIT":
            p_coef = _safe_float(cfg.get("profit_acos_coef"), 0.68)
            p_min = _safe_float(cfg.get("profit_acos_min"), 12.0)
            p_max = _safe_float(cfg.get("profit_acos_max"), 35.0)
            base_marg = marginal_be if marginal_be > 0 else be_ref
            target_acos = max(p_min, min(p_max, round(base_marg * p_coef, 1)))
            target_acos_note = f"PROFIT 成熟收割起点 = clamp(边际保本 {base_marg:.1f}% × {p_coef:.2f}, {p_min:.0f}%, {p_max:.0f}%) = {target_acos:.1f}%"
        elif stage_mode == "DEFEND":
            # Do not aggressively compress ACOS during DEFEND; stay near sustainable floor up to BE * 0.85
            cur_ref = portfolio_acos_30d if 8.0 <= portfolio_acos_30d < 200.0 else be_ref * 0.85
            target_acos = round(min(be_ref * 0.85, max(be_ref * 0.78, cur_ref)), 1) if has_fin_sales else round(min(30.0, max(15.0, cur_ref)), 1)
            target_acos_note = f"DEFEND 护盘期以守住单量地板为先，维持可持续 ACOS 参考线 {target_acos:.1f}%"
        else:  # CLEARANCE
            target_acos = round(max(12.0, min(60.0, be_ref)), 1)
            investment_or_clearance_note = (
                f"CLEARANCE 清仓期允许 ACOS 放宽至全口径保本 {target_acos:.1f}%（须满足单件清仓损失 < 长期仓储/移除成本）"
            )
            target_acos_note = investment_or_clearance_note

        # Apply refund LV1 contraction (§2.1 & §2.3)
        if critical_refund_lv1 and stage_mode not in ("STOP_LOSS",):
            target_acos = round(target_acos * 0.82, 1)
            target_acos_note += f"；触发退款一级警戒 ({refund_rate:.1f}% >= {refund_lv1:.0f}%) × 0.82 -> {target_acos:.1f}%"

        # Apply All-In BE × 0.85 hard ceiling (except LAUNCH and CLEARANCE, §2.1)
        if has_fin_sales and stage_mode not in ("LAUNCH", "CLEARANCE", "STOP_LOSS"):
            if true_all_in_be <= 0.0:
                ad_unsustainable_zone = True
                zero_ad_loss_cb = True
                stage_mode = "STOP_LOSS"
                stage_reason += " -> 触发全口径保本<=0自动转入 STOP_LOSS"
                target_acos = 8.0
                target_acos_note = "全口径真实保本 <= 0，自动转 STOP_LOSS（广告不可持续区），仅做最小防守"
            else:
                all_in_cap = math.floor(true_all_in_be * 0.85 * 10.0 + 1e-6) / 10.0
                if all_in_cap < 8.0:
                    ad_unsustainable_zone = True
                    zero_ad_loss_cb = True
                    if not forced_stage:
                        stage_mode = "STOP_LOSS"
                        stage_reason += f" -> 全口径保本封顶 {all_in_cap:.1f}% < 8%，转入 STOP_LOSS"
                    target_acos = 8.0
                    target_acos_note += f"；全口径封顶 ({all_in_cap:.1f}%) < 8% 触发广告不可持续区，转 STOP_LOSS（拒绝硬压至 3%）"
                elif target_acos > all_in_cap:
                    target_acos = all_in_cap
                    target_acos_note += f"；受全口径保本 × 0.85 硬封顶至 {target_acos:.1f}%"

    # Check explicit < 8% even after rules override (unless CLI explicit or LAUNCH/CLEARANCE)
    if (
        explicit_target_acos is None
        and stage_mode not in ("LAUNCH", "CLEARANCE", "STOP_LOSS")
        and target_acos < 8.0
    ):
        ad_unsustainable_zone = True
        zero_ad_loss_cb = True
        stage_mode = "STOP_LOSS"
        target_acos = 8.0
        target_acos_note += "；目标 ACOS < 8% 转入广告不可持续区 STOP_LOSS"

    # Target TACOS by Stage Mode (§1.1 & §2.1)
    if stage_mode == "LAUNCH":
        target_tacos = round(max(12.0, cm_pct + 10.0 if cm_pct > 0 else 25.0), 1)
    elif stage_mode == "GROWTH":
        target_tacos = round(max(8.0, cm_pct - 5.0), 1) if cm_pct > 5.0 else round(max(8.0, target_acos * 0.8), 1)
    elif stage_mode == "PROFIT":
        target_tacos = round(max(6.0, cm_pct - target_net_margin), 1) if cm_pct > target_net_margin else round(max(6.0, cm_pct * 0.70), 1)
    elif stage_mode == "DEFEND":
        target_tacos = round(max(8.0, cm_pct * 0.85), 1) if cm_pct > 0 else round(max(8.0, target_acos * 0.80), 1)
    elif stage_mode == "CLEARANCE":
        target_tacos = round(max(10.0, cm_pct), 1) if cm_pct > 0 else 25.0
    else:  # STOP_LOSS
        target_tacos = round(max(0.0, min(8.0, cm_pct)), 1) if cm_pct > 0 else 0.0

    # PEAK_DAY ACOS tolerance multiplier (§7.9: × 1.2 on peak day)
    eval_acos_multiplier = 1.20 if (peak_overlay and peak_phase == "PEAK_DAY") else 1.0
    effective_eval_acos = round(target_acos * eval_acos_multiplier, 1)

    # TCPA = Avg Price × Target ACOS (§2.1, scaling anchor for all thresholds)
    tcpa = max(1.0, round(avg_price * (max(target_acos, 8.0) / 100.0), 2))

    # Stage & Conservative Permissions (§1.1, §2.2, §7.2, §7.9)
    effective_low_stock_days = get_effective_low_stock_days(cfg)
    is_stockout_stage = bool(fulfillable == 0.0)
    is_low_stock_warning = bool(
        not is_stockout_stage
        and ((0.0 < dos < effective_low_stock_days) or (0.0 < fulfillable <= 25.0))
    )
    freeze_expansion = bool(
        stage_mode == "STOP_LOSS"
        or ad_unsustainable_zone
        or zero_ad_loss_cb
        or cogs_is_estimated
        or is_stockout_stage
        or is_low_stock_warning
        or (peak_overlay and peak_phase == "POST_COOLDOWN")
    )
    allow_aggressive_acos_cuts = stage_mode not in ("DEFEND", "LAUNCH") and not (peak_overlay and peak_phase == "PEAK_DAY")
    block_non_p0_neg_and_cuts = bool(peak_overlay and peak_phase == "PEAK_DAY")
    block_shock_recovery = bool(peak_overlay and peak_phase == "POST_COOLDOWN")

    return {
        "stage_mode": stage_mode,
        "stage_source": stage_source,
        "stage_reason": stage_reason,
        "auto_detected_stage": auto_stage,
        "peak_overlay": peak_overlay,
        "peak_phase": peak_phase,
        "peak_reason": peak_reason,
        "mode_display": f"{stage_mode}+PEAK({peak_phase})" if peak_overlay else stage_mode,
        "target_acos": target_acos,
        "effective_eval_acos": effective_eval_acos,
        "target_acos_note": target_acos_note,
        "target_tacos": target_tacos,
        "target_net_margin": target_net_margin,
        "cm_unit": cm_unit,
        "cm_pct": round(cm_pct, 2),
        "marginal_break_even_acos": round(marginal_be, 2),
        "true_all_in_break_even_tacos": round(true_all_in_be, 2),
        "tcpa": tcpa,
        "avg_price": round(avg_price, 2),
        "ad_unsustainable_zone": ad_unsustainable_zone,
        "zero_ad_loss_circuit_breaker": zero_ad_loss_cb,
        "cogs_data_missing": cogs_data_missing,
        "cogs_is_estimated": cogs_is_estimated,
        "critical_refund_lv1": critical_refund_lv1,
        "critical_refund_lv2": critical_refund_lv2,
        "is_low_stock_warning": is_low_stock_warning,
        "effective_low_stock_days": effective_low_stock_days,
        "investment_or_clearance_note": investment_or_clearance_note,
        "permissions": {
            "freeze_expansion": freeze_expansion,
            "allow_aggressive_acos_cuts": allow_aggressive_acos_cuts,
            "block_non_p0_neg_and_cuts": block_non_p0_neg_and_cuts,
            "block_shock_recovery": block_shock_recovery,
            "conservative_only_estimated_cogs": cogs_is_estimated,
            "low_stock_freeze_expansion": is_low_stock_warning,
        },
        "config": cfg,
    }


def calc_smoothed_cvr(orders: float, clicks: float, cvr_base_ratio: float, prior_k: float = 20.0) -> float:
    """Bayesian smoothed CVR (§3.3): CVR_adj = (orders + k * CVR_base) / (clicks + k).
    Returns percentage in [0, 100] (e.g. 11.3 for 11.3%).
    """
    o = max(0.0, float(orders))
    c = max(0.0, float(clicks))
    base = max(0.005, min(0.60, float(cvr_base_ratio)))
    k = max(1.0, float(prior_k))
    return round(((o + k * base) / (c + k)) * 100.0, 2)


def compute_portfolio_base_cvr(camps: list[dict], traffic_prof: dict | None = None) -> float:
    """Estimate baseline ad CVR ratio in (0.01, 0.50) from enabled campaigns or traffic profile."""
    tot_ord = sum(_safe_float(c.get("ord")) for c in camps if c.get("state") == "enabled")
    tot_clk = sum(_safe_float(c.get("clk")) for c in camps if c.get("state") == "enabled")
    if tot_clk >= 15 and tot_ord > 0:
        return max(0.01, min(0.50, tot_ord / tot_clk))
    all_ord = sum(_safe_float(c.get("ord")) for c in camps)
    all_clk = sum(_safe_float(c.get("clk")) for c in camps)
    if all_clk >= 15 and all_ord > 0:
        return max(0.01, min(0.50, all_ord / all_clk))
    ad_cvr_pct = _safe_float((traffic_prof or {}).get("ad_cvr"))
    if ad_cvr_pct > 0:
        return max(0.01, min(0.50, ad_cvr_pct / 100.0))
    return 0.08


def compute_scaled_thresholds(
    tcpa: float,
    cvr_base_ratio: float,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scale conversion/bleed/circuit-breaker thresholds by TCPA & CVR_base (§3.3),
    preserving legacy fixed values as stricter lower-bound protections."""
    c = cfg or DEFAULT_RULE_CONFIG
    t = max(1.0, float(tcpa))
    cvr_b = max(0.01, min(0.50, float(cvr_base_ratio)))
    k_clicks = _safe_float(c.get("fail_test_clicks_factor"), 3.0)
    min_clicks = int(_safe_float(c.get("fail_test_clicks_min"), 15))
    legacy_clicks = int(_safe_float(c.get("fail_test_clicks_legacy_floor"), 25))

    # Failed test (0 orders in 14d/30d): clicks >= max(legacy 25, max(15, ceil(3/CVR_base))) OR spend >= max($20, 2*TCPA)
    stat_clicks = max(min_clicks, int(math.ceil(k_clicks / cvr_b)))
    fail_test_clicks = max(legacy_clicks, stat_clicks)
    fail_test_spend = max(20.0, round(2.0 * t, 2))

    # Active bleeding (necessary precondition for lowering bids, using 7d conversion + 3d activity)
    active_bleed_spend_7d = max(3.0, round(0.5 * t, 2))
    activity_spend_3d = max(1.50, round(0.20 * t, 2))

    # Moderate vs Severe bleeding (for -15% vs -20% 0-order bid steps)
    severe_bleed_spend = max(15.0, round(1.5 * t, 2))

    # Bleed circuit breaker (penetrates cooldown):
    # 7d spend >= max($15, 2.5*TCPA) & 0 ord | OR 3d spend >= max($15, 3.0*TCPA) & 0 ord | OR 7d ACOS >= 2x target & spend >= max($25, 2*TCPA)
    cb_bleed_spend_7d = max(15.0, round(2.5 * t, 2))
    cb_bleed_spend_3d = max(15.0, round(3.0 * t, 2))
    cb_bleed_over_acos_spend_7d = max(25.0, round(2.0 * t, 2))

    # Minimum spend before non-P0 over-ACOS actions or N-gram phrase negatives
    min_action_spend = max(5.0, round(0.8 * t, 2))
    min_over_acos_spend_30d = max(8.0, round(1.0 * t, 2))
    ngram_bleed_spend = max(10.0, round(1.5 * t, 2))
    ngram_bleed_clicks = max(8, min(fail_test_clicks, int(math.ceil(1.5 / cvr_b))))
    off_amazon_bleed_spend = max(15.0, round(1.0 * t, 2))
    hero_proven_spend = max(10.0, round(1.0 * t, 2))

    return {
        "tcpa": round(t, 2),
        "cvr_base_pct": round(cvr_b * 100.0, 2),
        "cvr_base_ratio": round(cvr_b, 4),
        "cvr_prior_k": float(c.get("cvr_prior_k", 20.0)),
        "fail_test_clicks": fail_test_clicks,
        "fail_test_spend": fail_test_spend,
        "active_bleed_spend_7d": active_bleed_spend_7d,
        "activity_spend_3d": activity_spend_3d,
        "severe_bleed_spend": severe_bleed_spend,
        "cb_bleed_spend_7d": cb_bleed_spend_7d,
        "cb_bleed_spend_3d": cb_bleed_spend_3d,
        "cb_bleed_over_acos_spend_7d": cb_bleed_over_acos_spend_7d,
        "min_action_spend": min_action_spend,
        "min_over_acos_spend_30d": min_over_acos_spend_30d,
        "ngram_bleed_spend": ngram_bleed_spend,
        "ngram_bleed_clicks": ngram_bleed_clicks,
        "off_amazon_bleed_spend": off_amazon_bleed_spend,
        "hero_proven_spend": hero_proven_spend,
    }


def calc_placement_step(current_pct: float, goal_pct: float, max_up_pp: int = 30) -> tuple[int, str]:
    """Enforce §5.2 unified placement step-size caps and generate multi-period planned_path.

    - Downward: max step = current * 0.70 (rounded to 5pp, guaranteeing effective bid drop <= 30%).
      e.g. 250 -> 175 -> 120 -> 85 -> 60.
    - Upward: max step = +30pp per cycle.
    Returns (next_step_pct, planned_path_str).
    """
    cur = int(round(max(0.0, float(current_pct))))
    goal = int(round(max(0.0, float(goal_pct))))
    if cur == goal:
        return cur, ""

    path = [cur]
    val = cur
    guard = 0
    if goal < cur:
        while val > goal and guard < 12:
            guard += 1
            nxt = int(round((val * 0.70) / 5.0) * 5)
            if nxt >= val:
                nxt = max(goal, val - 5)
            nxt = max(goal, nxt)
            # Snap last small tail <= 5pp directly to goal if effective drop stays <= 30%
            if nxt > goal and (nxt - goal) <= 5 and (100.0 + goal) / (100.0 + val) >= 0.70:
                nxt = goal
            path.append(nxt)
            val = nxt
    else:
        while val < goal and guard < 12:
            guard += 1
            nxt = min(goal, val + max_up_pp)
            path.append(nxt)
            val = nxt

    next_step = path[1] if len(path) >= 2 else goal
    planned_path = " → ".join(f"{x}%" for x in path) if len(path) > 2 else ""
    return next_step, planned_path


def is_dedicated_hero_campaign(
    camp_name: str,
    kws: list[dict],
    battlecard_terms: set[str] | None = None,
) -> bool:
    """§5.5: Placement modifiers are campaign-level. A single-keyword TOS strategy is ONLY valid
    when the keyword sits in a dedicated single/few-word exact campaign (<=3 enabled exact keywords)."""
    cn = (camp_name or "").strip()
    if not cn:
        return False
    enabled_kws = [
        k for k in (kws or [])
        if (k.get("campaign_name") or k.get("campaign")) == cn
        and str(k.get("state") or "enabled").lower() == "enabled"
    ]
    if not enabled_kws:
        return False
    all_exact = all(str(k.get("match_type") or "").lower() == "exact" for k in enabled_kws)
    if all_exact and len(enabled_kws) <= 3:
        return True
    cn_low = cn.lower()
    if ("hero-exact" in cn_low or "exact-prove" in cn_low) and len(enabled_kws) <= 3:
        return True
    if battlecard_terms and all_exact and len(enabled_kws) <= 5:
        kw_texts = {str(k.get("keyword_text") or k.get("targeting_text") or "").strip().lower() for k in enabled_kws}
        if kw_texts and kw_texts.issubset(battlecard_terms):
            return True
    return False


def build_core_keyword_battlecard(
    *,
    xydc_map: dict | None,
    sr_map: dict | None,
    kws: list[dict],
    kws_7d: list[dict] | None,
    kws_14d: list[dict] | None,
    search_terms: list[dict] | None = None,
    camps: list[dict],
    profile: dict,
    rules: dict,
    entity_memory: dict | None,
    prior_expectations: dict | None,
    stage_ctx: dict[str, Any],
    scaled_thresholds: dict[str, Any],
    gen_date_str: str,
    classify_fn,
    firewall_fn,
    get_searchrank_fn,
    compute_rank_deltas_fn,
) -> list[dict[str, Any]]:
    """§6.3 Core Keyword Battlecard: pick 5~15 highest-priority IS core keywords with ABA tier,
    OR/ΔOR, SP/ΔSP, 14d/7d conversion, smoothed CVR_adj, role, target OR, verify window & reconciliation."""
    camp_state = {str(c.get("name") or ""): str(c.get("state") or "enabled").lower() for c in (camps or [])}
    kw_count_by_camp: dict[str, int] = defaultdict(int)
    for k in (kws or []):
        if str(k.get("state") or "enabled").lower() == "enabled":
            kw_count_by_camp[str(k.get("campaign_name") or "")] += 1

    # Aggregate conversion performance per keyword across 14d (preferred) -> 7d -> 30d
    def _agg_kws(rows: list[dict] | None) -> dict[str, dict]:
        out: dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "camps": set(), "enabled_camps": set()})
        for r in (rows or []):
            t = str(r.get("keyword_text") or r.get("targeting_text") or "").strip().lower()
            if not t:
                continue
            cn = str(r.get("campaign_name") or "").strip()
            out[t]["spend"] += _safe_float(r.get("spends") or r.get("spend") or r.get("cost"))
            out[t]["sales"] += _safe_float(r.get("sales"))
            out[t]["orders"] += int(_safe_float(r.get("orders")))
            out[t]["clicks"] += int(_safe_float(r.get("clicks")))
            if cn:
                out[t]["camps"].add(cn)
                if str(r.get("state") or "enabled").lower() == "enabled" and camp_state.get(cn, "enabled") == "enabled":
                    out[t]["enabled_camps"].add(cn)
        return out

    perf_30 = _agg_kws(kws)
    perf_14 = _agg_kws(kws_14d) if kws_14d else {}
    perf_7 = _agg_kws(kws_7d)
    conv_window_lbl = "14d" if kws_14d else "7d"

    # Also aggregate converting non-ASIN search terms as fallback when portfolio relies on Auto/Broad or lacks xydc_ranks
    perf_st: dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "camps": set(), "enabled_camps": set()})
    for sr in (search_terms or []):
        q = str(sr.get("query") or "").strip().lower()
        if not q or re.fullmatch(r"b0[0-9a-z]{8}", q):
            continue
        cn = str(sr.get("campaign_name") or "").strip()
        perf_st[q]["spend"] += _safe_float(sr.get("spends") or sr.get("spend") or sr.get("cost"))
        perf_st[q]["sales"] += _safe_float(sr.get("sales"))
        perf_st[q]["orders"] += int(_safe_float(sr.get("orders")))
        perf_st[q]["clicks"] += int(_safe_float(sr.get("clicks")))
        if cn:
            perf_st[q]["camps"].add(cn)
            if camp_state.get(cn, "enabled") == "enabled":
                perf_st[q]["enabled_camps"].add(cn)

    prior_targets_map: dict[str, dict] = {}
    if isinstance(prior_expectations, dict):
        for item in (prior_expectations.get("core_keyword_targets") or []):
            if isinstance(item, dict) and item.get("keyword"):
                prior_targets_map[str(item["keyword"]).strip().lower()] = item

    # Gather candidate universe from xydc_map + converting keywords + converting search terms
    cand_terms: set[str] = set()
    for t in (xydc_map or {}).keys():
        tl = str(t).strip().lower()
        if tl:
            cand_terms.add(tl)
    for t, p in perf_30.items():
        if p["orders"] >= 1 or p["spend"] >= 5.0:
            cand_terms.add(t)
    for q, ps in perf_st.items():
        if ps["orders"] >= 2 or (ps["orders"] >= 1 and ps["spend"] >= 3.0):
            cand_terms.add(q)
            if q not in perf_30:
                perf_30[q] = ps

    cvr_base = scaled_thresholds["cvr_base_ratio"]
    prior_k = scaled_thresholds["cvr_prior_k"]
    target_acos = float(stage_ctx["target_acos"])
    stage_mode = stage_ctx["stage_mode"]

    scored: list[tuple[float, dict[str, Any]]] = []
    for tl in cand_terms:
        if re.fullmatch(r"b0[0-9a-z]{8}", tl):
            continue
        fw_hit, _, _ = firewall_fn(tl, profile, rules)
        if fw_hit:
            continue
        bucket, _, is_strong = classify_fn(tl, profile, rules)
        if bucket in ("is_not", "competitor_brand", "asin") or str(bucket).startswith("is_not"):
            continue

        x_info = (xydc_map or {}).get(tl) or {}
        rk = compute_rank_deltas_fn(tl, xydc_map, sr_map, entity_memory, gen_date_str)
        org_rank = rk.get("organic_rank")
        sp_rank = rk.get("sp_rank")
        aba_rank, aba_tier = get_searchrank_fn(tl, sr_map or {})
        tot_trf = _safe_float(x_info.get("total_traffic"))

        p30 = perf_30.get(tl, {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "camps": set(), "enabled_camps": set()})
        p_rec = (perf_14.get(tl) if perf_14 else perf_7.get(tl)) or {"spend": 0.0, "sales": 0.0, "orders": 0, "clicks": 0, "camps": set(), "enabled_camps": set()}

        # Must be relevant OR have real orders OR meaningful xydc traffic with strong relevance
        if not is_strong and bucket not in ("brand", "core_feature", "core_or_title") and p30["orders"] < 2:
            continue
        if p30["orders"] == 0 and p30["spend"] < 3.0 and tot_trf < 50.0 and (org_rank is None or org_rank > 50):
            continue

        # Importance score
        score = 0.0
        score += min(500.0, p30["orders"] * 25.0 + p_rec["orders"] * 35.0)
        score += min(300.0, tot_trf * 0.15)
        if aba_tier == "MEGA_HEAD":
            score += 140.0
        elif aba_tier == "CORE_HEAD":
            score += 100.0
        elif aba_tier == "MID_TAIL":
            score += 55.0
        if bucket == "brand":
            score += 90.0
        elif is_strong:
            score += 60.0
        if org_rank is not None:
            if org_rank <= 3:
                score += 80.0
            elif org_rank <= 15:
                score += 110.0  # Highest flywheel leverage!
            elif org_rank <= 45:
                score += 45.0

        enabled_camps = sorted(p30.get("enabled_camps") or [])
        primary_camp = enabled_camps[0] if enabled_camps else (sorted(p30.get("camps") or [])[0] if p30.get("camps") else "--")
        is_dedicated = bool(enabled_camps and any(kw_count_by_camp.get(c_n, 99) <= 3 for c_n in enabled_camps))

        rec_clk = p_rec["clicks"] if p_rec["clicks"] > 0 else p30["clicks"]
        rec_ord = p_rec["orders"] if p_rec["clicks"] > 0 else p30["orders"]
        cvr_adj = calc_smoothed_cvr(rec_ord, rec_clk, cvr_base, prior_k)
        rec_acos = (p_rec["spend"] / p_rec["sales"] * 100.0) if p_rec["sales"] > 0 else (999.0 if p_rec["spend"] > 0 else 0.0)
        acos_30 = (p30["spend"] / p30["sales"] * 100.0) if p30["sales"] > 0 else (999.0 if p30["spend"] > 0 else 0.0)

        # Role & Target OR
        if bucket == "brand":
            role = "防守"
            target_or = 1
        elif not enabled_camps and org_rank is not None and org_rank <= 45:
            role = "补盲"
            target_or = max(1, min(15, org_rank - 5))
        elif org_rank is not None and org_rank <= 3:
            role = "守位"
            target_or = min(3, org_rank)
        elif org_rank is not None and 4 <= org_rank <= 15:
            role = "冲刺" if stage_mode != "STOP_LOSS" else "防守"
            target_or = max(1, min(3, org_rank - 3))
        elif org_rank is not None and 16 <= org_rank <= 50:
            role = "冲刺" if (stage_mode in ("LAUNCH", "GROWTH") and p30["orders"] >= 1) else "测试"
            target_or = max(5, org_rank - 10)
        else:
            role = "测试" if stage_mode in ("LAUNCH", "GROWTH") else "防守"
            target_or = 30 if stage_mode == "LAUNCH" else 45

        verify_days = 14 if role in ("冲刺", "补盲", "测试") else 7
        prior_t = prior_targets_map.get(tl)
        prior_status = "首期建档"
        if prior_t:
            pt_or = prior_t.get("target_or")
            if org_rank is not None and pt_or is not None:
                if org_rank <= int(pt_or):
                    prior_status = f"达标 (目标<=#{pt_or}, 实测#{org_rank})"
                else:
                    prior_status = f"未达标 (目标<=#{pt_or}, 实测#{org_rank})"
            else:
                prior_status = f"追踪中 (上期目标<=#{pt_or or '--'})"

        scored.append((score, {
            "keyword": tl,
            "bucket": bucket,
            "aba_rank": aba_rank,
            "aba_tier": aba_tier,
            "curr_org_rank": org_rank,
            "delta_or": rk.get("delta_or"),
            "curr_sp_rank": sp_rank,
            "delta_sp": rk.get("delta_sp"),
            "conv_window": conv_window_lbl,
            "spend_recent": round(p_rec["spend"], 2),
            "orders_recent": int(p_rec["orders"]),
            "acos_recent": round(rec_acos, 1),
            "spend_30d": round(p30["spend"], 2),
            "orders_30d": int(p30["orders"]),
            "acos_30d": round(acos_30, 1),
            "cvr_adj_pct": cvr_adj,
            "active_campaign": primary_camp,
            "is_dedicated_campaign": is_dedicated,
            "role": role,
            "target_or": target_or,
            "verify_window_days": verify_days,
            "prior_target_status": prior_status,
            "score": round(score, 1),
        }))

    scored.sort(key=lambda kv: (-kv[0], kv[1]["keyword"]))
    return [item for _, item in scored[:15]]


def build_budget_reallocation(
    *,
    camps: list[dict],
    stage_ctx: dict[str, Any],
    shrinkage_guard: bool,
    is_low_inventory: bool,
    is_stockout: bool,
    locked_campaigns: set[str],
    campaigns_with_other_levers: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """§7.5 Budget Reallocation: rank enabled campaigns by 14d/7d marginal efficiency,
    move budget from ACOS > target*1.3 & non-OOB donors to OOB & ACOS <= target recipients.
    Step caps: donor -30% (floor $10), recipient +50%.
    """
    enabled = [c for c in camps if c.get("state") == "enabled"]
    if not enabled or is_stockout:
        return [], []

    target_acos = float(stage_ctx["effective_eval_acos"])
    freeze_exp = bool(stage_ctx["permissions"]["freeze_expansion"])
    stage_mode = stage_ctx["stage_mode"]

    donors: list[dict[str, Any]] = []
    recipients: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []

    for c in enabled:
        cn = str(c.get("name") or "")
        budget = _safe_float(c.get("budget"))
        if budget <= 0:
            continue
        # Prefer 14d -> 7d -> 30d marginal efficiency
        if _safe_float(c.get("spend_14d")) >= 5.0:
            eval_win, eval_sp, eval_sl, eval_od, eval_ac = (
                "14d", _safe_float(c["spend_14d"]), _safe_float(c.get("sales_14d")),
                int(_safe_float(c.get("ord_14d"))), _safe_float(c.get("acos_14d")),
            )
        elif _safe_float(c.get("spend_7d")) >= 5.0:
            eval_win, eval_sp, eval_sl, eval_od, eval_ac = (
                "7d", _safe_float(c["spend_7d"]), _safe_float(c.get("sales_7d")),
                int(_safe_float(c.get("ord_7d"))), _safe_float(c.get("acos_7d")),
            )
        else:
            eval_win, eval_sp, eval_sl, eval_od, eval_ac = (
                "30d", _safe_float(c.get("spend")), _safe_float(c.get("sales")),
                int(_safe_float(c.get("ord"))), _safe_float(c.get("acos")),
            )

        tib = _safe_float(c.get("pct_time_in_budget"), 100.0)
        oob_hrs = _safe_float(c.get("avg_over_budget_time"), 0.0)
        sug_bgt = _safe_float(c.get("suggestion_budget"), 0.0)
        is_oob = (tib < 90.0 or oob_hrs >= 1.0)
        roas_eff = round(eval_sl / eval_sp, 2) if eval_sp > 0 else 0.0

        row = {
            "campaign": cn,
            "eval_window": eval_win,
            "current_budget": round(budget, 2),
            "spend": round(eval_sp, 2),
            "sales": round(eval_sl, 2),
            "orders": eval_od,
            "acos": round(eval_ac, 1),
            "roas": roas_eff,
            "time_in_budget_pct": round(tib, 1),
            "oob_hours": round(oob_hrs, 2),
            "suggested_budget_api": round(sug_bgt, 2),
            "role": "HOLD",
            "recommended_budget": round(budget, 2),
            "delta_usd": 0.0,
            "reason": "效率正常或在单杠杆/冷却锁内保持",
        }

        if (
            cn in locked_campaigns
            or cn in campaigns_with_other_levers
            or c.get("is_shared_multi_asin_campaign")
            or c.get("assessment") == "CIRCUIT_BREAKER_SHOCK"
        ):
            if c.get("assessment") == "CIRCUIT_BREAKER_SHOCK":
                row["reason"] = "活动处于休克断流回踩期(CIRCUIT_BREAKER_SHOCK)，禁止削减日预算"
            elif cn in campaigns_with_other_levers:
                row["reason"] = "本期该活动已调整其他杠杆（单杠杆原则），预算顺延下期"
            elif cn in locked_campaigns:
                row["reason"] = "活动处于冷却观察期内，预算保持不变"
            elif c.get("is_shared_multi_asin_campaign"):
                row["reason"] = "跨组合多品共享品牌广告，禁止单品视角调整预算"
            report_rows.append(row)
            continue

        # Check Recipient: OOB & efficient (ACOS <= target & orders >= 2)
        if is_oob and eval_od >= 2 and 0.0 < eval_ac <= target_acos and not freeze_exp and not is_low_inventory:
            max_up = round(budget * 1.50, 2)
            target_b = round(min(max_up, max(budget * 1.25, sug_bgt if sug_bgt > budget else budget * 1.30)), 2)
            if target_b > budget + 1.0:
                row["role"] = "RECIPIENT_INCREASE"
                row["recommended_budget"] = target_b
                row["delta_usd"] = round(target_b - budget, 2)
                row["reason"] = (
                    f"高效受预算约束 ({eval_win} ACOS {eval_ac:.1f}% <= 目标 {target_acos:.1f}%, {eval_od}单, "
                    f"在预算内仅 {tib:.0f}%/断流 {oob_hrs:.1f}h)，扩预算至 ${target_b:.2f} (步长上限 +50%)"
                )
                recipients.append(row)
        # Check Donor: High ACOS (> target*1.3) & non-OOB & budget > $10
        elif (
            not is_oob
            and eval_sp >= 10.0
            and (eval_od == 0 or eval_ac > target_acos * 1.30)
            and budget > 10.0
            and not (shrinkage_guard and eval_od >= 2)
            and not (stage_mode == "LAUNCH" and eval_od >= 2)
            and not stage_ctx["permissions"]["block_non_p0_neg_and_cuts"]
        ):
            new_b = max(10.0, round(budget * 0.75, 2))
            new_b = max(10.0, max(round(budget * 0.70, 2), new_b))
            if new_b < budget - 1.0:
                row["role"] = "DONOR_DECREASE"
                row["recommended_budget"] = new_b
                row["delta_usd"] = round(new_b - budget, 2)
                ac_str = f"{eval_ac:.1f}%" if eval_od > 0 else "-- (0单)"
                row["reason"] = (
                    f"低效非 OOB 预算回收 ({eval_win} 花费 ${eval_sp:.1f}, {eval_od}单, ACOS {ac_str} > {target_acos * 1.30:.1f}%, "
                    f"在预算内 {tib:.0f}%)，回收至 ${new_b:.2f} (降幅<=30%, 保底$10)"
                )
                donors.append(row)

        report_rows.append(row)

    # Emit budget actions only when there is clear donor or OOB recipient efficiency gain;
    # keep report_rows (`budget_reallocation.tsv`) 100% consistent with emitted budget_actions!
    allow_unilateral_donor_cuts = bool(recipients or stage_mode in ("STOP_LOSS", "PROFIT", "GROWTH"))
    budget_actions: list[dict[str, Any]] = []
    for rec in recipients:
        budget_actions.append({
            "action_type": "Increase_Budget",
            "campaign": rec["campaign"],
            "target_entity": "DailyBudget",
            "match_type": "budget",
            "current_bid_num": rec["current_budget"],
            "current_value": f"日预算 ${rec['current_budget']:.2f} ({rec['eval_window']} ACOS {rec['acos']:.1f}%, 在预算内 {rec['time_in_budget_pct']:.0f}%)",
            "recommended_value": f"${rec['recommended_budget']:.2f}",
            "reason": f"[预算再分配·高效OOB扩容] {rec['reason']}",
        })
    for don in donors:
        if allow_unilateral_donor_cuts:
            budget_actions.append({
                "action_type": "Lower_Budget",
                "campaign": don["campaign"],
                "target_entity": "DailyBudget",
                "match_type": "budget",
                "current_bid_num": don["current_budget"],
                "current_value": f"日预算 ${don['current_budget']:.2f} ({don['eval_window']} 花费 ${don['spend']:.1f}, {don['orders']}单)",
                "recommended_value": f"${don['recommended_budget']:.2f}",
                "reason": f"[预算再分配·低效回收] {don['reason']}",
            })
        else:
            orig_reason = don["reason"]
            don["role"] = "HOLD"
            don["recommended_budget"] = don["current_budget"]
            don["delta_usd"] = 0.0
            don["reason"] = f"当前处于 {stage_mode} 阶段且无高效 OOB 承接仓，暂不单边削减日预算（原回收候选：{orig_reason}）"

    report_rows.sort(key=lambda r: (-r["roas"], -r["spend"]))
    return report_rows, budget_actions


def build_brand_display_health(
    *,
    camps: list[dict],
    shared_brand_campaigns: list[dict] | None,
    kws: list[dict],
    profile: dict,
    battlecard: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """§7.10 Independent SB/SBV/SD Health Check:
    - Evaluates 14d attribution separately from SP (never compared directly against SP ACOS)
    - Checks brand-term defense coverage & SBV coverage of Core Keyword Battlecard terms
    - Reports NTB (New-To-Brand) metrics if present, or flags DATA_MISSING: NTB
    - Preserves is_shared_multi_asin_campaign anti-false-pause guard
    """
    brand = str((profile or {}).get("brand") or "").strip().lower()
    battle_terms = {str(b.get("keyword") or "").strip().lower() for b in (battlecard or []) if b.get("keyword")}

    # Collect SB/SBV/SD campaigns from portfolio + cross-portfolio shared list
    seen_ids: set[str] = set()
    bd_rows: list[dict[str, Any]] = []

    def _detect_fmt(c: dict) -> str:
        raw = str(c.get("ad_format") or c.get("sponsored") or c.get("sponsored_type") or "").upper()
        name_u = str(c.get("name") or c.get("campaign_name") or "").upper()
        if "SBV" in raw or "VIDEO" in raw or "SBV" in name_u or "视频" in name_u:
            return "SBV"
        if "SD" in raw or "DISPLAY" in raw or re.search(r"(?:^|[-_])SD(?:[-_]|$)", name_u):
            return "SD"
        if "SB" in raw or "HSA" in raw or "BRAND" in raw or re.search(r"(?:^|[-_])SB(?:[-_]|$)", name_u):
            return "SB"
        return "SP"

    def _add_campaign(c: dict, source: str) -> None:
        fmt = _detect_fmt(c)
        if fmt == "SP":
            return
        cid = str(c.get("id") or c.get("campaign_id") or "")
        cname = str(c.get("name") or c.get("campaign_name") or "").strip()
        dedup_k = cid or cname
        if not dedup_k or dedup_k in seen_ids:
            return
        seen_ids.add(dedup_k)

        sp = _safe_float(c.get("spend_30d") if c.get("spend_30d") is not None else c.get("spend"))
        sl = _safe_float(c.get("sales_30d") if c.get("sales_30d") is not None else c.get("sales"))
        od = int(_safe_float(c.get("orders_30d") if c.get("orders_30d") is not None else c.get("ord")))
        ac = round(sp / sl * 100.0, 1) if sl > 0 else (999.0 if sp > 0 else 0.0)
        ro = round(sl / sp, 2) if sp > 0 else 0.0
        state = str(c.get("state") or "enabled").lower()
        is_shared = bool(c.get("is_shared_multi_asin_campaign"))

        # Check keyword coverage for this campaign
        c_kws = {
            str(k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
            for k in (kws or [])
            if (k.get("campaign_name") or "") == cname and str(k.get("state") or "enabled").lower() == "enabled"
        }
        brand_covered = bool(brand and any(brand in kt for kt in c_kws)) or (brand and brand in cname.lower())
        battle_hits = sorted(c_kws & battle_terms)

        # NTB telemetry check
        ntb_ord = c.get("ntb_orders") or c.get("new_to_brand_orders") or c.get("units_sold_new_to_brand")
        ntb_pct = c.get("ntb_orders_pct") or c.get("percent_of_orders_new_to_brand")
        ntb_display = (
            f"{int(_safe_float(ntb_ord))}单 ({_safe_float(ntb_pct):.1f}%)"
            if ntb_ord is not None
            else "DATA_MISSING: NTB"
        )

        notes: list[str] = ["归因窗口=14天(不与SP 7天ACOS直接横比)"]
        if is_shared:
            notes.append(f"多品共享保护({int(_safe_float(c.get('shared_asin_count'), 2))}个ASIN，禁止单品视角误关)")
        if fmt == "SBV":
            notes.append(
                f"核心词作战表覆盖 {len(battle_hits)}/{len(battle_terms)} 词"
                + (f" ({', '.join(battle_hits[:4])})" if battle_hits else "（建议优先覆盖作战表核心词）")
            )
        if brand:
            notes.append("品牌词防守=已覆盖" if brand_covered else "品牌词防守=未覆盖")

        bd_rows.append({
            "campaign": cname,
            "ad_format": fmt,
            "source": source,
            "state": state,
            "attribution_window": "14d",
            "spend_30d": round(sp, 2),
            "sales_30d": round(sl, 2),
            "orders_30d": od,
            "acos_30d": ac,
            "roas_30d": ro,
            "ntb_metrics": ntb_display,
            "brand_defense_covered": brand_covered,
            "battlecard_covered_count": len(battle_hits),
            "battlecard_total": len(battle_terms),
            "is_shared_multi_asin_campaign": is_shared,
            "health_note": "；".join(notes),
        })

    for c in (camps or []):
        _add_campaign(c, "portfolio")
    for sc in (shared_brand_campaigns or []):
        _add_campaign(sc, "cross_portfolio_shared")

    # If zero SB/SBV/SD campaigns exist, emit a diagnostic gap row so the report never omits SB/SD check!
    if not bd_rows:
        bd_rows.append({
            "campaign": "-- (当前无 SB/SBV/SD 在投活动)",
            "ad_format": "NONE",
            "source": "audit_gap",
            "state": "missing",
            "attribution_window": "14d",
            "spend_30d": 0.0,
            "sales_30d": 0.0,
            "orders_30d": 0,
            "acos_30d": 0.0,
            "roas_30d": 0.0,
            "ntb_metrics": "DATA_MISSING: NTB",
            "brand_defense_covered": False,
            "battlecard_covered_count": 0,
            "battlecard_total": len(battle_terms),
            "is_shared_multi_asin_campaign": False,
            "health_note": "未检测到 SB/SBV/SD 活动；若已品牌备案且非 STOP_LOSS 阶段，建议补建品牌词防守及核心词作战表 SBV 视频位",
        })

    return bd_rows


def classify_action_lever(action_type: str) -> str:
    """Map an action_type into §5.1 lever classes + P0/exempt classes."""
    act = (action_type or "").strip()
    if act in ("Pause_Keyword", "Pause_Target", "Negative_Exact", "Negative_Phrase", "Negative_Target", "Remove_Negative"):
        return "P0_COMPLIANCE_NEGATIVE"
    if act in ("Stockout_Conserve_Hold", "Low_Inventory_Conserve_Hold", "Top_Rank_Cooldown_Hold", "Rank_Slide_Protection_Hold", "Anti_Double_Damping_Hold", "Defensive_Dominance_Maintain"):
        return "HOLD"
    if act == "Probe_Bid_Recovery":
        return "P0_CIRCUIT_BREAKER"
    if act == "Compensated_Base_Bid_Floor":
        return "EXEMPT_E3_FLOOR"
    if act == "Anti_Drift_Base_Bid_Throttle":
        return "EXEMPT_E4_E5_DRIFT"
    if act in ("Update_Bidding_Strategy", "Archive_Fragmented_Campaign", "Pause_Campaign"):
        return "STRATEGY_STATE"
    if act in ("Placement_TOS_PP_ROS", "Placement_TOS", "Placement_PP", "Placement_ROS", "Emergency_Placement_Throttle"):
        return "PLACEMENT"
    if act in ("Lower_Budget", "Increase_Budget", "Update_Budget", "High_Refund_Throttle_Budget"):
        return "BUDGET"
    if act in ("Dayparting_Boost", "Dayparting_Throttle", "Dayparting_Budget_Pacing"):
        return "SCHEDULE"
    if act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact", "Harvest_ASIN", "Unpause_Keyword", "Unpause_Target"):
        return "HARVEST"
    return "BID"


def is_p0_or_p1_exempt_from_change_budget(item: dict, stage_ctx: dict[str, Any]) -> bool:
    """§5.4: P0/P1 safety, compliance, survival, circuit-breaker, and hold items bypass the global change budget cap."""
    act = str(item.get("action_type") or "")
    lever = classify_action_lever(act)
    if lever in ("P0_COMPLIANCE_NEGATIVE", "P0_CIRCUIT_BREAKER", "HOLD", "EXEMPT_E3_FLOOR", "EXEMPT_E4_E5_DRIFT"):
        return True
    if act in ("Emergency_Placement_Throttle", "High_Refund_Throttle_Budget"):
        return True
    if stage_ctx.get("stage_mode") == "STOP_LOSS" and act in ("Update_Bidding_Strategy", "Lower_Budget"):
        return True
    return False


def attach_action_plan_metadata(
    item: dict,
    *,
    today_dt: datetime,
    stage_ctx: dict[str, Any],
    scaled_thresholds: dict[str, Any],
    planned_path: str = "",
) -> dict:
    """§7.8: Ensure every action carries lever_type, planned_path, expected_effect, verify_date, and rollback_rule."""
    out = dict(item)
    act = str(out.get("action_type") or "")
    lever = classify_action_lever(act)
    target_acos = float(stage_ctx.get("target_acos", 25.0))
    cb_7d = scaled_thresholds.get("cb_bleed_spend_7d", 15.0)
    cb_3d = scaled_thresholds.get("cb_bleed_spend_3d", 15.0)

    out["lever_type"] = out.get("lever_type") or lever
    out["planned_path"] = str(out.get("planned_path") or planned_path or "")

    # Verify date & expected effect & rollback rule defaults by action family
    # Recompute verify_date whenever missing OR stale (<= today) so inherited/deferred actions never carry expired dates!
    today_ymd = today_dt.strftime("%Y-%m-%d")
    cur_vdate = str(out.get("verify_date") or "").strip()[:10]
    if not cur_vdate or cur_vdate <= today_ymd:
        if act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact", "Harvest_ASIN", "Unpause_Keyword", "Increase_Budget", "Lower_Budget", "High_Refund_Throttle_Budget"):
            v_days = 7
        elif lever == "P0_COMPLIANCE_NEGATIVE":
            v_days = 3
        elif lever in ("PLACEMENT", "STRATEGY_STATE", "EXEMPT_E3_FLOOR"):
            v_days = 5
        else:
            v_days = 3
        out["verify_date"] = (today_dt + timedelta(days=v_days)).strftime("%Y-%m-%d")

    if not out.get("expected_effect"):
        if lever == "P0_COMPLIANCE_NEGATIVE":
            out["expected_effect"] = "立即阻断违禁/错品/流血词根无效花费，无负向转化损失"
        elif act == "Update_Bidding_Strategy":
            out["expected_effect"] = f"拆除提高和降低 2x 放大或站外泄洪，3~5天 CPC 下降 >=10%，ACOS 向 {target_acos:.1f}% 收敛"
        elif lever == "PLACEMENT":
            out["expected_effect"] = f"展位溢价按步长平滑回归，3~5天 CPC 下降 10%~20%，日均曝光跌幅控制在 <50%"
        elif act in ("Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid", "Anti_Drift_Base_Bid_Throttle"):
            out["expected_effect"] = f"3~7天 CPC 下降 8%~18%，ACOS 向 {target_acos:.1f}% 收敛，日均曝光保持稳定(ΔImp > -60%)"
        elif act in ("Boost_Ascender_TOS", "Elevate_To_TOS", "Raise_Bid", "Increase_Bid"):
            out["expected_effect"] = f"3~7天顶展份额与曝光提升，自然位 OR 向前推进，7d ACOS 受控在 <= {target_acos * 1.2:.1f}%"
        elif act == "Probe_Bid_Recovery":
            out["expected_effect"] = "3天内恢复有效曝光池（日均曝光回到基线 >=40%），恢复出单流速"
        elif act == "Compensated_Base_Bid_Floor":
            out["expected_effect"] = "下调 TOS 同时保持有效顶展竞价稳定，防止休克断流"
        elif act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact", "Harvest_ASIN", "Unpause_Keyword"):
            out["expected_effect"] = f"3~7天精准仓跑出稳定曝光与成交（ACOS <= {target_acos:.1f}%），达标后二阶段隔离源仓"
        elif act in ("Increase_Budget", "Lower_Budget", "High_Refund_Throttle_Budget"):
            out["expected_effect"] = "7天内高效仓 OOB 断流时长下降或低效仓花费收缩，全店贡献利润提升"
        elif lever == "HOLD":
            out["expected_effect"] = "维持稳态观察，避免过度优化或自然权重滑坡"
        else:
            out["expected_effect"] = f"3~7天投产向目标 ACOS {target_acos:.1f}% 收敛且总单量稳定"

    if not out.get("rollback_rule"):
        if lever == "P0_COMPLIANCE_NEGATIVE":
            out["rollback_rule"] = "违禁/硬冲突永久不回滚；边缘否定若发现自然位急跌>=5名需人工复核"
        elif act in ("Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid", "Placement_TOS_PP_ROS", "Placement_TOS", "Placement_PP", "Placement_ROS", "Emergency_Placement_Throttle"):
            out["rollback_rule"] = "若3天日均曝光跌>=70%(排除假休克)则按倒推公式回踩；若关联自然+广告总单跌>=15%立即回调至改前值"
        elif act in ("Boost_Ascender_TOS", "Elevate_To_TOS", "Raise_Bid", "Increase_Bid", "Increase_Budget", "Probe_Bid_Recovery"):
            out["rollback_rule"] = f"若7天花费>={cb_7d:.0f}美元0单，或3天花费>={cb_3d:.0f}美元0单，或7天ACOS>={target_acos * 2.0:.0f}%，立即回滚至改前值"
        elif act in ("Harvest_To_Exact", "Converge_Harvest_To_Exact", "Harvest_ASIN", "Unpause_Keyword"):
            out["rollback_rule"] = f"若精准仓14天累计花费>={scaled_thresholds.get('fail_test_spend', 20.0):.0f}美元0单或CPC暴涨>=+40%，回调竞价或暂停"
        elif lever == "HOLD":
            out["rollback_rule"] = f"观察期内若触发失血熔断(7d>=${cb_7d:.0f} 0单或3d>=${cb_3d:.0f} 0单)或真休克(ΔImp<=-70%)则穿透冷却纠偏"
        else:
            out["rollback_rule"] = "7天验收未达预期且击穿日均销额地板或TACOS护栏时回调至改前值"

    return out


def enforce_single_lever_and_change_budget(
    adjustments: list[dict],
    *,
    camps: list[dict],
    kws: list[dict],
    targets: list[dict],
    stage_ctx: dict[str, Any],
    is_fragile: bool,
    priority_rank_map: dict[str, int],
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Enforce §5.1 Single-Lever Discipline (bidirectional!) AND §5.4 Global Change Budget Cap.

    Returns (kept_adjustments, deferred_next_cycle_queue, budget_stats).
    """
    cfg = stage_ctx["config"]
    share_cap = float(cfg["change_budget_share_fragile"] if is_fragile else cfg["change_budget_share"])
    enabled_camps = [c for c in camps if c.get("state") == "enabled"]
    camp_daily_spend = {str(c.get("name") or ""): _safe_float(c.get("spend")) / 30.0 for c in enabled_camps}
    portfolio_daily_spend = sum(camp_daily_spend.values())
    change_budget_cap_usd = round(portfolio_daily_spend * share_cap, 2)

    # Keyword & target 30d daily spend maps for accurate entity-level change budget accounting
    kw_daily_spend: dict[tuple[str, str, str], float] = {}
    for k in (kws or []):
        cn = str(k.get("campaign_name") or "").strip()
        mt = str(k.get("match_type") or "").lower().strip()
        tl = str(k.get("keyword_text") or k.get("targeting_text") or "").strip().lower()
        if cn and tl:
            kw_daily_spend[(cn, mt, tl)] = _safe_float(k.get("spends")) / 30.0

    tg_daily_spend: dict[tuple[str, str], float] = {}
    for t in (targets or []):
        cn = str(t.get("campaign_name") or "").strip()
        raw_t = str(t.get("targeting_text") or t.get("expression") or "").strip().lower()
        sp_d = _safe_float(t.get("spends")) / 30.0
        if cn and raw_t:
            tg_daily_spend[(cn, raw_t)] = sp_d
            m_a = re.search(r"(b0[0-9a-z]{8})", raw_t, re.I)
            if m_a:
                tg_daily_spend[(cn, m_a.group(1).lower())] = sp_d

    # ---- Pass 1: Bidirectional Single-Lever Discipline per Campaign (§5.1) ----
    # Lever hierarchy within a campaign: STRATEGY_STATE > PLACEMENT > BUDGET > BID
    lever_order = {"STRATEGY_STATE": 4, "PLACEMENT": 3, "BUDGET": 2, "BID": 1}
    camp_primary_lever: dict[str, str] = {}
    for item in adjustments:
        cn = str(item.get("campaign") or "").strip()
        lv = classify_action_lever(str(item.get("action_type") or ""))
        if not cn or cn in ("ALL_NON_EXACT", "Portfolio/All_Active_SP") or lv not in lever_order:
            continue
        prev_lv = camp_primary_lever.get(cn)
        if prev_lv is None or lever_order[lv] > lever_order[prev_lv]:
            camp_primary_lever[cn] = lv

    single_lever_kept: list[dict] = []
    deferred_queue: list[dict] = []

    for raw_item in adjustments:
        item = dict(raw_item)
        act = str(item.get("action_type") or "")
        cn = str(item.get("campaign") or "").strip()
        lv = classify_action_lever(act)
        rsn = str(item.get("reason") or "")

        # Annotate explicit legal exceptions E1~E5 (§5.1) — idempotent across multi-cycle inheritance!
        if lv == "P0_COMPLIANCE_NEGATIVE":
            if "[例外 E1" not in rsn:
                item["reason"] = f"[例外 E1·P0合规/错品] {rsn}"
            single_lever_kept.append(item)
            continue
        if lv == "P0_CIRCUIT_BREAKER" or act == "Emergency_Placement_Throttle":
            if "[例外 E2" not in rsn:
                item["reason"] = f"[例外 E2·熔断纠偏] {rsn}"
            single_lever_kept.append(item)
            continue
        if lv == "EXEMPT_E3_FLOOR":
            if camp_primary_lever.get(cn) != "PLACEMENT":
                def_item = dict(item)
                def_item["defer_reason"] = f"SINGLE_LEVER_HOLD: 本期 {cn} 未执行展位下调(由 {camp_primary_lever.get(cn)} 主导)，E3 托底随展位顺延下期"
                deferred_queue.append(def_item)
                continue
            if "[例外 E3" not in rsn:
                item["reason"] = f"[例外 E3·降TOS托底再平衡] {rsn}"
            single_lever_kept.append(item)
            continue
        if lv == "EXEMPT_E4_E5_DRIFT":
            is_e5_off = ("站外" in rsn or "OFF AMAZON" in rsn.upper())
            if not is_e5_off and camp_primary_lever.get(cn) != "PLACEMENT":
                def_item = dict(item)
                def_item["defer_reason"] = f"SINGLE_LEVER_HOLD: 本期 {cn} 未执行展位下调(由 {camp_primary_lever.get(cn)} 主导)，E4 防漂移随展位顺延下期"
                deferred_queue.append(def_item)
                continue
            ex_tag = "[例外 E5·站外泄洪微降]" if is_e5_off else "[例外 E4·自动/广泛防漂移]"
            if "[例外 E4" not in rsn and "[例外 E5" not in rsn:
                item["reason"] = f"{ex_tag} {rsn}"
            single_lever_kept.append(item)
            continue
        if lv in ("HOLD", "SCHEDULE", "HARVEST"):
            single_lever_kept.append(item)
            continue

        # Check conflict against primary lever on same campaign
        prim = camp_primary_lever.get(cn)
        if prim and lv in lever_order and lv != prim:
            # Bidirectional single-lever hold / defer!
            if lv == "BID" and prim in ("PLACEMENT", "STRATEGY_STATE"):
                cur_b = _safe_float(item.get("current_bid_num"))
                if cur_b > 0 and act in ("Lower_Bid", "Throttle_TOS_Bid", "Throttle_Cannibalization_Bid"):
                    item["_orig_before_hold"] = dict(raw_item)
                    item["action_type"] = "Anti_Double_Damping_Hold"
                    item["recommended_value"] = f"${cur_b:.2f}"
                    item["reason"] = f"【单杠杆原则拦截】本期该活动已执行 {prim} 杠杆调整，基础竞价保持 ${cur_b:.2f} 观察（原 {act} 顺延至下期）。"
                    single_lever_kept.append(item)
                else:
                    # Upward bid (Boost_Ascender_TOS / Elevate_To_TOS) or budget deferred to next cycle
                    def_item = dict(item)
                    def_item["defer_reason"] = f"SINGLE_LEVER_HOLD: 本期同活动已动 {prim} 杠杆，按单杠杆原则顺延下期"
                    deferred_queue.append(def_item)
            else:
                def_item = dict(item)
                def_item["defer_reason"] = f"SINGLE_LEVER_HOLD: 本期同活动已动 {prim} 杠杆，按单杠杆原则顺延下期"
                deferred_queue.append(def_item)
            continue

        single_lever_kept.append(item)

    # ---- Pass 2: Global Change Budget Cap (§5.4) ----
    if portfolio_daily_spend <= 0.0:
        for it_c in single_lever_kept:
            it_c.pop("_orig_before_hold", None)
        stats = {
            "portfolio_daily_spend_30d": 0.0,
            "change_budget_share_limit_pct": round(share_cap * 100.0, 1),
            "change_budget_cap_usd": 0.0,
            "change_budget_used_usd": 0.0,
            "change_budget_used_pct": 0.0,
            "deferred_count": len(deferred_queue),
            "note": "组合30d日均花费为0，跳过变更预算限额",
        }
        return single_lever_kept, deferred_queue, stats

    def _entity_daily_spend(it: dict) -> tuple[str, float]:
        cn = str(it.get("campaign") or "").strip()
        mt = str(it.get("match_type") or "").lower().strip()
        ent = str(it.get("target_entity") or "").strip().lower()
        lv = classify_action_lever(str(it.get("action_type") or ""))
        if lv in ("PLACEMENT", "BUDGET", "STRATEGY_STATE"):
            return f"CAMP::{cn}", camp_daily_spend.get(cn, 0.0)
        if (cn, mt, ent) in kw_daily_spend:
            return f"KW::{cn}::{mt}::{ent}", kw_daily_spend[(cn, mt, ent)]
        if (cn, ent) in tg_daily_spend:
            return f"TG::{cn}::{ent}", tg_daily_spend[(cn, ent)]
        # New harvest / schedule / unindexed entity: low incremental footprint
        return f"OTHER::{cn}::{mt}::{ent}", 0.0

    p0_p1_items: list[dict] = []
    budgeted_items: list[tuple[int, float, int, dict]] = []
    for idx, it in enumerate(single_lever_kept):
        if is_p0_or_p1_exempt_from_change_budget(it, stage_ctx):
            p0_p1_items.append(it)
        else:
            prio = priority_rank_map.get(str(it.get("action_type") or ""), 50)
            _, sp_d = _entity_daily_spend(it)
            budgeted_items.append((prio, sp_d, idx, it))

    # Highest priority first, then largest entity spend (highest business impact)
    budgeted_items.sort(key=lambda x: (-x[0], -x[1], x[2]))

    used_usd = 0.0
    counted_keys: set[str] = set()
    kept_budgeted: list[tuple[int, dict]] = []
    allow_one_dominance_used = False
    budget_note = "OK"

    for prio, sp_d, orig_idx, it in budgeted_items:
        ek, _ = _entity_daily_spend(it)
        inc = 0.0 if ek in counted_keys else sp_d
        if inc <= 0.0 or (used_usd + inc <= change_budget_cap_usd + 1e-6):
            kept_budgeted.append((orig_idx, it))
            if ek not in counted_keys:
                counted_keys.add(ek)
                used_usd += inc
        elif used_usd == 0.0 and not allow_one_dominance_used:
            # Anti-deadlock for 1~2 campaign portfolios where a single top campaign exceeds 35% of spend
            allow_one_dominance_used = True
            kept_budgeted.append((orig_idx, it))
            counted_keys.add(ek)
            used_usd += inc
            budget_note = "SINGLE_CAMPAIGN_DOMINANCE_ALLOW_ONE"
        else:
            def_it = dict(it)
            def_it.pop("_orig_before_hold", None)
            def_it["defer_reason"] = (
                f"GLOBAL_CHANGE_BUDGET_CAP: 超过本期全局变更预算上限 "
                f"(${change_budget_cap_usd:.2f}/天 = {share_cap*100:.0f}% × ${portfolio_daily_spend:.2f}/天)，顺延下期队列"
            )
            deferred_queue.append(def_it)

    kept_budgeted.sort(key=lambda x: x[0])
    candidate_kept = p0_p1_items + [it for _, it in kept_budgeted]

    # ---- Pass 3: Post-Change-Budget Paired-Exception Integrity Check ----
    # Never execute E3 (Compensated_Base_Bid_Floor), non-E5 E4 (Anti_Drift_Base_Bid_Throttle),
    # or Anti_Double_Damping_Hold if the campaign's parent PLACEMENT/STRATEGY_STATE lever was deferred in Pass 2!
    surviving_placement_camps = {
        str(it.get("campaign") or "").strip()
        for it in candidate_kept
        if classify_action_lever(str(it.get("action_type") or "")) == "PLACEMENT"
    }
    surviving_strategy_camps = {
        str(it.get("campaign") or "").strip()
        for it in candidate_kept
        if classify_action_lever(str(it.get("action_type") or "")) == "STRATEGY_STATE"
    }

    final_kept: list[dict] = []
    for it in candidate_kept:
        act = str(it.get("action_type") or "")
        cn = str(it.get("campaign") or "").strip()
        lv = classify_action_lever(act)
        rsn = str(it.get("reason") or "")
        orig_held = it.pop("_orig_before_hold", None)

        if lv == "EXEMPT_E3_FLOOR" and cn not in surviving_placement_camps:
            def_it = dict(it)
            def_it["defer_reason"] = (
                f"PAIRED_PLACEMENT_DEFERRED: 本期 {cn} 的展位溢价下调因全局变更预算顺延，"
                "E3 托底补偿同步顺延下期（严禁未降TOS却单独抬高基础竞价）"
            )
            deferred_queue.append(def_it)
            continue
        if lv == "EXEMPT_E4_E5_DRIFT":
            is_e5_off = ("站外" in rsn or "OFF AMAZON" in rsn.upper())
            if not is_e5_off and cn not in surviving_placement_camps:
                def_it = dict(it)
                def_it["defer_reason"] = (
                    f"PAIRED_PLACEMENT_DEFERRED: 本期 {cn} 的展位溢价下调因全局变更预算顺延，"
                    "E4 防漂移联动微降同步顺延下期"
                )
                deferred_queue.append(def_it)
                continue
        if act == "Anti_Double_Damping_Hold" and cn not in surviving_placement_camps and cn not in surviving_strategy_camps:
            def_it = dict(orig_held) if isinstance(orig_held, dict) else dict(it)
            def_it["defer_reason"] = (
                f"PAIRED_LEVER_DEFERRED: 本期 {cn} 的主杠杆因全局变更预算顺延，"
                "原关联基础竞价下调同步顺延至下期队列"
            )
            deferred_queue.append(def_it)
            continue

        final_kept.append(it)

    used_pct = round(min(100.0, (used_usd / portfolio_daily_spend * 100.0)), 1) if portfolio_daily_spend > 0 else 0.0
    stats = {
        "portfolio_daily_spend_30d": round(portfolio_daily_spend, 2),
        "change_budget_share_limit_pct": round(share_cap * 100.0, 1),
        "change_budget_cap_usd": change_budget_cap_usd,
        "change_budget_used_usd": round(used_usd, 2),
        "change_budget_used_pct": used_pct,
        "deferred_count": len(deferred_queue),
        "note": budget_note,
    }
    return final_kept, deferred_queue, stats


def compute_ledger_calibration(ledger: list[dict]) -> dict[str, Any]:
    """§10 Continuous Calibration: compute achievement rate by action type across historical ledger entries.
    Flags any action category with < 50% achievement rate across >= 3 tracked adjustments."""
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "achieved": 0, "failed": 0, "pending": 0})

    for row in (ledger or []):
        if not isinstance(row, dict):
            continue
        verdict = str(row.get("effect_verdict") or "")
        if any(skip_kw in verdict for skip_kw in ("归档", "DAYPARTING_SNAPSHOT_DRIFT_ARCHIVED", "未匹配")):
            continue

        fld = str(row.get("field") or "").lower().strip()
        direction = str(row.get("adjustment_direction") or "").upper()
        if fld == "bid":
            if direction not in ("UP", "DOWN"):
                raw_old = row.get("old_val")
                if raw_old in (None, ""):
                    raw_old = row.get("old_value")
                raw_new = row.get("new_val")
                if raw_new in (None, ""):
                    raw_new = row.get("new_value")
                if raw_old not in (None, "", "--") and raw_new not in (None, "", "--"):
                    old_num = _safe_float(raw_old, -1.0)
                    new_num = _safe_float(raw_new, -1.0)
                    if old_num >= 0.0 and new_num >= 0.0 and max(old_num, new_num) > 0.0:
                        if new_num > old_num:
                            direction = "UP"
                        elif new_num < old_num:
                            direction = "DOWN"
            cat = "提价" if direction == "UP" else ("降价" if direction == "DOWN" else "竞价调整")
        elif fld in ("tos", "pp", "ros"):
            cat = "溢价调整"
        elif fld == "budget":
            cat = "预算调整"
        elif fld in ("bidding_strategy", "strategy"):
            cat = "竞价策略"
        elif fld == "state":
            cat = "暂停/状态"
        else:
            cat = "其他动作"

        is_achieved = any(ok in verdict for ok in ("见效保持", "投产达标", "稳态运行", "合规意向暂停", "否定拦截生效", "首屏黄金位", "常态冷却"))
        is_failed = any(bad in verdict for bad in ("休克断流", "失血暴冲"))
        if is_achieved:
            buckets[cat]["total"] += 1
            buckets[cat]["achieved"] += 1
        elif is_failed:
            buckets[cat]["total"] += 1
            buckets[cat]["failed"] += 1
        else:
            buckets[cat]["pending"] += 1

    by_category: dict[str, Any] = {}
    calibration_alerts: list[str] = []
    for cat, st in sorted(buckets.items()):
        evaluated = st["total"]
        rate = round((st["achieved"] / evaluated * 100.0), 1) if evaluated > 0 else None
        needs_cal = bool(evaluated >= 3 and rate is not None and rate < 50.0)
        by_category[cat] = {
            "evaluated": evaluated,
            "achieved": st["achieved"],
            "failed": st["failed"],
            "observing": st["pending"],
            "achievement_rate_pct": rate,
            "needs_calibration": needs_cal,
        }
        if needs_cal:
            calibration_alerts.append(
                f"动作类型【{cat}】已追踪 {evaluated} 笔，预期达成率仅 {rate:.1f}% (<50%)，建议在 rules.json 校准对应步长/阈值（需人工确认）"
            )

    return {
        "by_category": by_category,
        "calibration_alerts": calibration_alerts,
    }
