#!/usr/bin/env python3
"""
validate_playbook_report.py — Hard quality gate for Lingxing Ads Playbook reports & bulk TSVs.

Usage:
  python validate_playbook_report.py --report <playbook.md> [--analysis-dir <analysis_dir>] [--bulk-tsv <bulk_adjustments.tsv>] [--json]

Exit codes:
  0 — PASS (zero violations)
  1 — FAIL (prints exact file, line number, rule code, and violation details)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_BULK_HEADER = [
    "action_type",
    "campaign",
    "target_entity",
    "match_type",
    "current_value",
    "recommended_value",
    "reason",
]

REQUIRED_SUBSECTIONS: list[tuple[str, re.Pattern[str], str]] = [
    ("0.0", re.compile(r"^#{3,4}\s+0\.0(?:\b|\s|\.|：|:)"), "### 0.0 阶段模式判定与本期目标卡"),
    ("0.2", re.compile(r"^#{3,4}\s+0\.2(?:\b|\s|\.|：|:)"), "### 0.2 竞品竞争力与西柚流量护城河评估表"),
    ("0.3", re.compile(r"^#{3,4}\s+0\.3(?:\b|\s|\.|：|:)"), "### 0.3 真实财务利润核算与保本锚定"),
    ("0.4", re.compile(r"^#{3,4}\s+0\.4(?:\b|\s|\.|：|:)"), "### 0.4 北极星总览预期闭环对账卡"),
    ("1.3", re.compile(r"^#{3,4}\s+1\.3(?:\b|\s|\.|：|:)"), "### 1.3 24小时与周度分时转化画像"),
    ("1.4", re.compile(r"^#{3,4}\s+1\.4(?:\b|\s|\.|：|:)"), "### 1.4 秒杀、优惠券与日常价格异动排查"),
    ("1.8", re.compile(r"^#{3,4}\s+1\.8(?:\b|\s|\.|：|:)"), "### 1.8 多维广告位指纹与真实广告位验证"),
    ("1.9.1", re.compile(r"^#{3,5}\s+1\.9\.1(?:\b|\s|\.|：|:)"), "#### 1.9.1 本品流量得分、父体四渠道与14d趋势"),
    ("1.9.2", re.compile(r"^#{3,5}\s+1\.9\.2(?:\b|\s|\.|：|:)"), "#### 1.9.2 自然排名 vs 广告排名双轨协同五阶矩阵"),
    ("1.9.3", re.compile(r"^#{3,5}\s+1\.9\.3(?:\b|\s|\.|：|:)"), "#### 1.9.3 Tier 5 自然流量盲区高潜拓词清单"),
    ("2.1", re.compile(r"^#{3,4}\s+2\.1(?:\b|\s|\.|：|:)"), "### 2.1 跨期实调动作效果对账总表"),
    ("2.2", re.compile(r"^#{3,4}\s+2\.2(?:\b|\s|\.|：|:)"), "### 2.2 核心实体全量历史记忆与建议继承复盘"),
]

# Single-lever classification for v2 bulk conflict gate (excluding E1~E5, holds, harvests, schedules)
LEVER_CLASS_MAP: dict[str, str] = {
    "Update_Bidding_Strategy": "STRATEGY_STATE",
    "Archive_Fragmented_Campaign": "STRATEGY_STATE",
    "Pause_Campaign": "STRATEGY_STATE",
    "Placement_TOS_PP_ROS": "PLACEMENT",
    "Placement_TOS": "PLACEMENT",
    "Placement_PP": "PLACEMENT",
    "Placement_ROS": "PLACEMENT",
    "Lower_Budget": "BUDGET",
    "Increase_Budget": "BUDGET",
    "Update_Budget": "BUDGET",
    "High_Refund_Throttle_Budget": "BUDGET",
    "Lower_Bid": "BID",
    "Raise_Bid": "BID",
    "Increase_Bid": "BID",
    "Update_Bid": "BID",
    "Throttle_TOS_Bid": "BID",
    "Throttle_Cannibalization_Bid": "BID",
    "Elevate_To_TOS": "BID",
    "Boost_Ascender_TOS": "BID",
}
PLACEMENT_STEP_ACTIONS = {
    "Placement_TOS_PP_ROS",
    "Placement_TOS",
    "Placement_PP",
    "Placement_ROS",
    "Emergency_Placement_Throttle",
}
PLACEMENT_SLOT_RE = re.compile(r"\b(TOS|PP|ROS)\s*=\s*(\d+)%", re.IGNORECASE)

# Actions whose recommended_value MUST be a pure USD dollar value ($X.XX)
PURE_USD_ACTIONS = {
    "Lower_Bid",
    "Raise_Bid",
    "Increase_Bid",
    "Update_Bid",
    "Throttle_TOS_Bid",
    "Throttle_Cannibalization_Bid",
    "Anti_Drift_Base_Bid_Throttle",
    "Compensated_Base_Bid_Floor",
    "Probe_Bid_Recovery",
    "Elevate_To_TOS",
    "Boost_Ascender_TOS",
    "Harvest_To_Exact",
    "Converge_Harvest_To_Exact",
    "Harvest_ASIN",
    "High_Refund_Throttle_Budget",
    "Update_Budget",
    "Lower_Budget",
    "Increase_Budget",
    "Anti_Double_Damping_Hold",
    "Top_Rank_Cooldown_Hold",
    "Stockout_Conserve_Hold",
    "Low_Inventory_Conserve_Hold",
    "Rank_Slide_Protection_Hold",
    "Defensive_Dominance_Maintain",
}

NON_USD_ACTIONS = {
    "Placement_TOS_PP_ROS",
    "Placement_TOS",
    "Placement_PP",
    "Placement_ROS",
    "Emergency_Placement_Throttle",
    "Update_Bidding_Strategy",
    "Archive_Fragmented_Campaign",
    "Pause_Keyword",
    "Pause_Target",
    "Pause_Campaign",
    "Unpause_Keyword",
    "Unpause_Target",
    "Negative_Exact",
    "Negative_Phrase",
    "Negative_Target",
    "Remove_Negative",
    "Dayparting_Budget_Pacing",
    "Dayparting_Boost",
    "Dayparting_Throttle",
}

PURE_USD_RE = re.compile(r"^\$(\d+(?:\.\d{1,2})?)$")
POSITIVE_ACTION_RE = re.compile(
    r"(?:Unpause|Harvest|Boost|Elevate|Raise_Bid|Increase_Bid|Probe_Bid_Recovery|Recover)",
    re.IGNORECASE,
)
PROHIBITED_TERMS_RE = re.compile(
    r"\b(?:weed|weeds|marijuana|cannabis|thc|cbd|bong|bongs|kief|kratom|hashish|psilocybin|shroom|shrooms|crack\s+pipe|meth)\b",
    re.IGNORECASE,
)

# Formatting & sentinel regexes
FENCE_OPEN_RE = re.compile(r"^\s*(?:>\s*)*(`{3,}|~{3,})(.*)$")
BARE_DOLLAR_RE = re.compile(r"(?<!\\)\$\s*[+-]?\d[\d,.]*")
LATEX_ARROW_RE = re.compile(r"\\(?:to|rightarrow|longrightarrow|Rightarrow)\b")
BARE_BID_RE = re.compile(r"(?<![A-Za-z0-9_./\\-])(?:BID|bid)(?![A-Za-z0-9_./\\-])")
SENTINEL_RE = re.compile(
    r"(?<![0-9])(?:99999999(?:\.\d+)?%?|9999(?:\.0+)?%|999\.0%)(?![0-9])"
)
META_RULE_EXEMPTION_RE = re.compile(r"(?:严禁|禁止|杜绝|清洗|拦截|不得出现)")
MD_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")


def strip_inline_code_preserve_len(line: str) -> str:
    """
    Mask CommonMark inline code spans (matching exact N-backtick opener & closer)
    with spaces of equal length so column positions remain intact.
    Unclosed backticks are intentionally NOT masked because they expose raw `$` to KaTeX.
    """
    n = len(line)
    chars = list(line)
    i = 0
    while i < n:
        if line[i] == "`":
            j = i
            while j < n and line[j] == "`":
                j += 1
            tick_len = j - i
            # Search forward for exact matching run of `tick_len` backticks
            k = j
            closed = False
            while k < n:
                if line[k] == "`":
                    m = k
                    while m < n and line[m] == "`":
                        m += 1
                    if (m - k) == tick_len:
                        for p in range(i, m):
                            chars[p] = " "
                        i = m
                        closed = True
                        break
                    k = m
                else:
                    k += 1
            if not closed:
                i = j
        else:
            i += 1
    return "".join(chars)


def make_violation(
    file_path: Path,
    line: int,
    rule: str,
    message: str,
    snippet: str = "",
) -> dict[str, Any]:
    return {
        "file": str(file_path),
        "line": line,
        "rule": rule,
        "message": message,
        "snippet": snippet.strip()[:160],
    }


def validate_report_markdown(report_path: Path, legacy_v1: bool = False) -> tuple[list[dict[str, Any]], str]:
    violations: list[dict[str, Any]] = []
    text = report_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    if not lines:
        violations.append(make_violation(report_path, 1, "EMPTY_REPORT", "报告文件为空"))
        return violations, ""

    # 1. Check outer triple-backtick code fence wrapping
    first_nonempty_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    last_nonempty_idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if first_nonempty_idx is not None and re.match(r"^\s*`{3,}", lines[first_nonempty_idx]):
        violations.append(
            make_violation(
                report_path,
                first_nonempty_idx + 1,
                "OUTER_CODE_FENCE",
                "报告首行严禁包裹在外层三反引号代码围栏 (```) 中",
                lines[first_nonempty_idx],
            )
        )

    in_fence = False
    fence_marker = ""
    fence_open_line = 0
    non_fenced_lines: list[str] = []

    h2_found: dict[int, int] = {}
    h2_sequence: list[tuple[int, int]] = []
    sub_found: dict[str, int] = {}
    has_exec_card = False

    h2_patterns = {
        sec_num: re.compile(rf"^##\s+{sec_num}(?:\.\s*|\s+|：|:)")
        for sec_num in range(13)
    }

    for idx, raw_line in enumerate(lines, 1):
        m_fence = FENCE_OPEN_RE.match(raw_line)
        if m_fence:
            ticks = m_fence.group(1)
            rest = m_fence.group(2).strip()
            if not in_fence:
                in_fence = True
                fence_marker = ticks
                fence_open_line = idx
                non_fenced_lines.append("")
                continue
            elif (
                ticks[0] == fence_marker[0]
                and len(ticks) >= len(fence_marker)
                and not rest
            ):
                in_fence = False
                fence_marker = ""
                non_fenced_lines.append("")
                continue

        if in_fence:
            non_fenced_lines.append("")
            continue

        non_fenced_lines.append(raw_line)
        stripped = raw_line.strip()

        # Check Executive Quick-Reference Card
        if "首屏复核速览卡" in stripped:
            has_exec_card = True

        # Check H2 sections ## 0. .. ## 12.
        if stripped.startswith("## ") and not stripped.startswith("### "):
            for sec_num, pat in h2_patterns.items():
                if pat.match(stripped):
                    if sec_num not in h2_found:
                        h2_found[sec_num] = idx
                    h2_sequence.append((sec_num, idx))
                    break

        # Check mandatory subsections
        if stripped.startswith("###"):
            for sub_key, sub_pat, _ in REQUIRED_SUBSECTIONS:
                if sub_pat.match(stripped) and sub_key not in sub_found:
                    sub_found[sub_key] = idx

        # 2. Formatting & sentinel checks on non-fenced lines
        masked_line = strip_inline_code_preserve_len(raw_line)

        # 2a. Bare unescaped $ currency amounts (KaTeX crash prevention)
        for m_dlr in BARE_DOLLAR_RE.finditer(masked_line):
            violations.append(
                make_violation(
                    report_path,
                    idx,
                    "BARE_DOLLAR_AMOUNT",
                    f"检测到未用反引号包裹或未转义的裸美元符号 '{m_dlr.group(0).strip()}'（易触发 KaTeX 渲染崩溃，请改为 `{m_dlr.group(0).strip()}`）",
                    raw_line,
                )
            )

        # 2b. LaTeX arrow escapes \to / \rightarrow
        for m_arr in LATEX_ARROW_RE.finditer(masked_line):
            violations.append(
                make_violation(
                    report_path,
                    idx,
                    "LATEX_ARROW_ESCAPE",
                    f"检测到 LaTeX 转义箭头 '{m_arr.group(0)}'（请统一使用 Unicode 箭头 '→'）",
                    raw_line,
                )
            )

        # 2c. User-facing bare BID / bid (ignoring inline code and link URLs/paths)
        masked_no_links = MD_LINK_TARGET_RE.sub("]( )", masked_line)
        for m_bid in BARE_BID_RE.finditer(masked_no_links):
            violations.append(
                make_violation(
                    report_path,
                    idx,
                    "BARE_BID_TERM",
                    f"检测到面向用户的裸词 '{m_bid.group(0)}'（对外一律写「基础竞价」或「默认基础竞价」）",
                    raw_line,
                )
            )

        # 2d. Extreme sentinel leaks (99999999 / 9999% / 9999.0% / 999.0%)
        # Checked on raw_line (even inside backtick table cells!) unless explicitly stating prohibition/sanitization rule
        if not META_RULE_EXEMPTION_RE.search(raw_line):
            for m_sen in SENTINEL_RE.finditer(raw_line):
                violations.append(
                    make_violation(
                        report_path,
                        idx,
                        "SENTINEL_LEAK",
                        f"检测到泄漏的 0 单极值哨兵 '{m_sen.group(0)}'（面向人类报告 0 订单有花费 ACOS 必须展示为 '-- (0单)'）",
                        raw_line,
                    )
                )

    if in_fence:
        # Check if the unclosed fence actually wrapped the whole file
        msg = f"第 {fence_open_line} 行开启的三反引号代码围栏 (```) 未闭合"
        if last_nonempty_idx is not None and fence_open_line <= 3:
            msg += "（疑似外层代码围栏包裹整份报告）"
        violations.append(
            make_violation(
                report_path,
                fence_open_line,
                "UNCLOSED_CODE_FENCE",
                msg,
                lines[fence_open_line - 1],
            )
        )

    # Verify Executive Card
    if not has_exec_card:
        violations.append(
            make_violation(
                report_path,
                1,
                "MISSING_EXEC_CARD",
                "缺少首屏『首屏复核速览卡（Executive Quick-Reference Card）』区块",
            )
        )

    # Verify 13 H2 sections (## 0. through ## 12.)
    for sec_num in range(13):
        if sec_num not in h2_found:
            violations.append(
                make_violation(
                    report_path,
                    1,
                    "MISSING_H2_SECTION",
                    f"缺少必选一级章节 '## {sec_num}.'（必须完整保留 ## 0. 至 ## 12. 全部 13 个章节，且不可包裹在代码块内）",
                )
            )

    # Verify H2 ascending order
    prev_sec = -1
    for sec_num, line_no in h2_sequence:
        if sec_num < prev_sec:
            violations.append(
                make_violation(
                    report_path,
                    line_no,
                    "OUT_OF_ORDER_H2_SECTION",
                    f"一级章节顺序错乱：'## {sec_num}.' 出现在 '## {prev_sec}.' 之后",
                    lines[line_no - 1],
                )
            )
        prev_sec = max(prev_sec, sec_num)

    # Verify mandatory subsections
    for sub_key, _, sub_desc in REQUIRED_SUBSECTIONS:
        if legacy_v1 and sub_key == "0.0":
            continue
        if sub_key not in sub_found:
            violations.append(
                make_violation(
                    report_path,
                    1,
                    "MISSING_SUBSECTION",
                    f"缺少必选关键子章节 '{sub_desc}'（编号 {sub_key}）",
                )
            )

    return violations, "\n".join(non_fenced_lines)


def _load_custom_restricted_re(rules_path: Path | None) -> re.Pattern[str] | None:
    if not rules_path or not rules_path.exists():
        return None
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    terms: list[Any] = []
    for src_d in (
        data.get("params") if isinstance(data.get("params"), dict) else {},
        data.get("v2_config") if isinstance(data.get("v2_config"), dict) else {},
        data,
    ):
        rt = src_d.get("restricted_terms")
        if isinstance(rt, list):
            terms.extend(rt)
    parts: list[str] = []
    for t in terms:
        s = str(t or "").strip()
        if s:
            esc = re.escape(s).replace(r"\ ", r"\s+")
            parts.append(rf"\b{esc}\b")
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def validate_bulk_tsv(bulk_path: Path, rules_path: Path | None = None) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    raw_lines = bulk_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not raw_lines:
        violations.append(
            make_violation(bulk_path, 1, "BULK_EMPTY", "bulk_adjustments.tsv 文件为空")
        )
        return violations

    header_cols = raw_lines[0].split("\t")
    if header_cols != EXPECTED_BULK_HEADER:
        violations.append(
            make_violation(
                bulk_path,
                1,
                "BULK_HEADER_INVALID",
                f"表头必须严格为 7 列 {EXPECTED_BULK_HEADER}，实际为 {len(header_cols)} 列: {header_cols}",
                raw_lines[0],
            )
        )

    custom_restricted_re = _load_custom_restricted_re(rules_path)
    seen_exact_action_keys: dict[tuple[str, str, str, str], int] = {}
    seen_entity_keys: dict[tuple[str, str, str], tuple[int, str]] = {}
    camp_levers: dict[str, dict[str, tuple[int, str]]] = {}
    placement_camps_in_bulk: set[str] = set()
    paired_exempt_rows: list[tuple[int, str, str, str, str]] = []

    for idx, raw_line in enumerate(raw_lines[1:], 2):
        if not raw_line.strip():
            continue
        cols = raw_line.split("\t")
        if len(cols) != 7:
            violations.append(
                make_violation(
                    bulk_path,
                    idx,
                    "BULK_COLUMN_COUNT",
                    f"严格要求 7 列（无内嵌制表符），当前行实测为 {len(cols)} 列",
                    raw_line,
                )
            )
            continue

        action_type, campaign, target_entity, match_type, current_value, recommended_value, reason = [
            c.strip() for c in cols
        ]

        # Non-empty core columns
        for col_name, col_val in (
            ("action_type", action_type),
            ("campaign", campaign),
            ("target_entity", target_entity),
            ("match_type", match_type),
            ("recommended_value", recommended_value),
        ):
            if not col_val:
                violations.append(
                    make_violation(
                        bulk_path,
                        idx,
                        "BULK_EMPTY_FIELD",
                        f"必填列 '{col_name}' 不能为空",
                        raw_line,
                    )
                )

        # Sentinel check in bulk TSV
        for m_sen in SENTINEL_RE.finditer(raw_line):
            violations.append(
                make_violation(
                    bulk_path,
                    idx,
                    "BULK_SENTINEL_LEAK",
                    f"批量表中存在未清洗的极值哨兵 '{m_sen.group(0)}'",
                    raw_line,
                )
            )

        # Pure USD numeric check for bid/budget actions
        is_bid_or_budget_action = (
            action_type in PURE_USD_ACTIONS
            or (
                action_type not in NON_USD_ACTIONS
                and match_type not in ("schedule", "placement", "strategy", "campaign", "negativeExact", "negativePhrase")
                and any(
                    kw in action_type
                    for kw in ("Bid", "Budget", "Throttle", "Elevate", "Boost", "Harvest", "Probe")
                )
            )
        )
        if is_bid_or_budget_action:
            m_usd = PURE_USD_RE.match(recommended_value)
            if not m_usd:
                violations.append(
                    make_violation(
                        bulk_path,
                        idx,
                        "BULK_NON_NUMERIC_USD",
                        f"竞价/预算类动作 '{action_type}' 的 recommended_value 必须为纯美元绝对数值（如 $0.44），严禁文本占位符: '{recommended_value}'",
                        raw_line,
                    )
                )
            elif float(m_usd.group(1)) <= 0:
                violations.append(
                    make_violation(
                        bulk_path,
                        idx,
                        "BULK_NON_POSITIVE_USD",
                        f"竞价/预算类动作 '{action_type}' 的 recommended_value 必须大于 $0.00，实际为 '{recommended_value}'",
                        raw_line,
                    )
                )
        elif action_type in ("Unpause_Keyword", "Unpause_Target") and "bid=" in recommended_value.lower():
            if not re.match(r"^state=enabled\s*\(bid=\$\d+(?:\.\d{1,2})?\)$", recommended_value):
                violations.append(
                    make_violation(
                        bulk_path,
                        idx,
                        "BULK_INVALID_UNPAUSE_VALUE",
                        f"复活类动作 '{action_type}' 的 recommended_value 格式不规范: '{recommended_value}'",
                        raw_line,
                    )
                )

        # Duplicate / conflict check
        norm_camp = campaign.lower()
        norm_mt = match_type.lower()
        norm_ent = target_entity.lower()
        norm_act = action_type.lower()

        exact_key = (norm_camp, norm_mt, norm_ent, norm_act)
        if exact_key in seen_exact_action_keys:
            prev_ln = seen_exact_action_keys[exact_key]
            violations.append(
                make_violation(
                    bulk_path,
                    idx,
                    "BULK_DUPLICATE_ACTION",
                    f"与第 {prev_ln} 行存在重复的 (campaign, match_type, target_entity, action_type) 记录: ({campaign}, {match_type}, {target_entity}, {action_type})",
                    raw_line,
                )
            )
        else:
            seen_exact_action_keys[exact_key] = idx

        entity_key = (norm_camp, norm_mt, norm_ent)
        if entity_key in seen_entity_keys:
            prev_ln, prev_act = seen_entity_keys[entity_key]
            if prev_act.lower() != norm_act:
                violations.append(
                    make_violation(
                        bulk_path,
                        idx,
                        "BULK_ENTITY_CONFLICT",
                        f"同一 (campaign, match_type, target_entity) 与第 {prev_ln} 行动作 '{prev_act}' 发生冲突（当前动作 '{action_type}'）",
                        raw_line,
                    )
                )
        else:
            seen_entity_keys[entity_key] = (idx, action_type)

        # Contraband / prohibited term firewall on positive actions (default + custom rules.json restricted_terms)
        if POSITIVE_ACTION_RE.search(action_type):
            m_prohib = PROHIBITED_TERMS_RE.search(target_entity)
            if m_prohib:
                violations.append(
                    make_violation(
                        bulk_path,
                        idx,
                        "BULK_PROHIBITED_POSITIVE_TERM",
                        f"正向推荐动作 '{action_type}' 严禁包含违禁词 '{m_prohib.group(0)}' (target_entity='{target_entity}')",
                        raw_line,
                    )
                )
            elif custom_restricted_re:
                m_cust = custom_restricted_re.search(target_entity)
                if m_cust:
                    violations.append(
                        make_violation(
                            bulk_path,
                            idx,
                            "BULK_PROHIBITED_POSITIVE_TERM",
                            f"正向推荐动作 '{action_type}' 命中 rules.json 自定义敏感词 '{m_cust.group(0)}' (target_entity='{target_entity}')",
                            raw_line,
                        )
                    )

        # v2 Single-Lever Discipline check per campaign
        if norm_camp not in ("all_non_exact", "portfolio/all_active_sp"):
            lv_cls = LEVER_CLASS_MAP.get(action_type)
            if lv_cls:
                prev_map = camp_levers.setdefault(norm_camp, {})
                for other_lv, (other_ln, other_act) in prev_map.items():
                    if other_lv != lv_cls:
                        violations.append(
                            make_violation(
                                bulk_path,
                                idx,
                                "BULK_SINGLE_LEVER_CONFLICT",
                                f"活动 '{campaign}' 违反单杠杆原则：第 {other_ln} 行已动 {other_lv} ({other_act})，第 {idx} 行又动 {lv_cls} ({action_type})",
                                raw_line,
                            )
                        )
                prev_map.setdefault(lv_cls, (idx, action_type))

        # Track placement campaigns (specifically TOS decreases!) & paired E3/E4 rows for orphan integrity validation
        if action_type in PLACEMENT_STEP_ACTIONS:
            cur_slots_chk = {m.group(1).upper(): int(m.group(2)) for m in PLACEMENT_SLOT_RE.finditer(current_value)}
            rec_slots_chk = {m.group(1).upper(): int(m.group(2)) for m in PLACEMENT_SLOT_RE.finditer(recommended_value)}
            tos_was_cut = (
                "TOS" in cur_slots_chk and "TOS" in rec_slots_chk and rec_slots_chk["TOS"] < cur_slots_chk["TOS"]
            )
            any_slot_cut = any(
                sk in cur_slots_chk and rec_slots_chk[sk] < cur_slots_chk[sk]
                for sk in rec_slots_chk
            )
            if tos_was_cut or (not cur_slots_chk and action_type == "Emergency_Placement_Throttle"):
                placement_camps_in_bulk.add(norm_camp)
            elif any_slot_cut and action_type == "Placement_TOS_PP_ROS":
                # Auto/Broad E4 anti-drift can also trigger when PP/ROS/TOS is reduced on that campaign
                placement_camps_in_bulk.add(f"ANYCUT::{norm_camp}")
        elif action_type == "Compensated_Base_Bid_Floor":
            paired_exempt_rows.append((idx, norm_camp, campaign, action_type, raw_line))
        elif action_type == "Anti_Drift_Base_Bid_Throttle":
            if "站外" not in reason and "OFF AMAZON" not in reason.upper():
                paired_exempt_rows.append((idx, norm_camp, campaign, action_type, raw_line))

        # v2 Placement step-size cap check (down effective drop <= 30% [mult >= 0.69], up <= +30pp)
        if action_type in PLACEMENT_STEP_ACTIONS:
            cur_slots = {m.group(1).upper(): int(m.group(2)) for m in PLACEMENT_SLOT_RE.finditer(current_value)}
            rec_slots = {m.group(1).upper(): int(m.group(2)) for m in PLACEMENT_SLOT_RE.finditer(recommended_value)}
            for slot_k, rec_v in rec_slots.items():
                if slot_k not in cur_slots:
                    continue
                cur_v = cur_slots[slot_k]
                if rec_v < cur_v and (cur_v - rec_v) > 5:
                    eff_ratio = (100.0 + rec_v) / (100.0 + cur_v)
                    if eff_ratio < 0.685:
                        violations.append(
                            make_violation(
                                bulk_path,
                                idx,
                                "BULK_PLACEMENT_STEP_EXCEEDED",
                                f"广告位溢价单期下调超步长上限 ({slot_k}: {cur_v}% → {rec_v}%，有效系数比 {eff_ratio:.2f} < 0.70，单期降幅超30%)",
                                raw_line,
                            )
                        )
                elif rec_v > cur_v and (rec_v - cur_v) > 30:
                    violations.append(
                        make_violation(
                            bulk_path,
                            idx,
                            "BULK_PLACEMENT_STEP_EXCEEDED",
                            f"广告位溢价单期上调超步长上限 ({slot_k}: {cur_v}% → {rec_v}%，+{rec_v - cur_v}pp > +30pp)",
                            raw_line,
                        )
                    )

    # Post-pass: verify paired E3/E4 exceptions actually have a qualifying placement reduction on the same campaign
    for idx_ex, nc_ex, c_ex, act_ex, raw_ex in paired_exempt_rows:
        has_valid_pair = (
            nc_ex in placement_camps_in_bulk
            or (act_ex == "Anti_Drift_Base_Bid_Throttle" and f"ANYCUT::{nc_ex}" in placement_camps_in_bulk)
        )
        if not has_valid_pair:
            violations.append(
                make_violation(
                    bulk_path,
                    idx_ex,
                    "BULK_ORPHAN_EXEMPT_ACTION",
                    f"活动 '{c_ex}' 出现孤立的配套例外动作 '{act_ex}'，但本批量表中并无该活动的展位溢价下调动作（严禁未降TOS却单独执行托底提价或E4联动降价）",
                    raw_ex,
                )
            )

    return violations


def validate_three_way_consistency(
    report_path: Path,
    non_fenced_report_text: str,
    analysis_dir: Path,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    campaigns_tsv = analysis_dir / "campaigns.tsv"
    if not campaigns_tsv.exists():
        violations.append(
            make_violation(
                campaigns_tsv,
                1,
                "MISSING_CAMPAIGNS_TSV",
                f"analysis-dir 下未找到 campaigns.tsv: {campaigns_tsv}",
            )
        )
        return violations

    with campaigns_tsv.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        all_camps: list[tuple[int, str, str]] = []
        for row_idx, row in enumerate(reader, 2):
            c_name = (row.get("name") or row.get("campaign_name") or row.get("campaign") or "").strip()
            c_state = (row.get("state") or "").strip().lower()
            if not c_name or c_name.lower() in ("none", "total", "汇总", "合计"):
                continue
            all_camps.append((row_idx, c_name, c_state))

    # Mask longer campaign names first so shorter campaign names (e.g. "*-coffee grinder" vs
    # "*-coffee grinder electric" or "*-手动-广泛" vs "*-手动-广泛-出单词") cannot falsely match
    # substrings inside longer campaign names!
    working_text = non_fenced_report_text
    camps_sorted_by_len = sorted(all_camps, key=lambda x: len(x[1]), reverse=True)
    present_campaigns: set[str] = set()

    for _, c_name, _ in camps_sorted_by_len:
        # Match c_name when not immediately attached to alphanumeric/hyphen/underscore/CJK characters
        pat = re.compile(
            rf"(?<![A-Za-z0-9_\u4e00-\u9fff-]){re.escape(c_name)}(?![A-Za-z0-9_\u4e00-\u9fff-])"
        )
        if pat.search(working_text):
            present_campaigns.add(c_name)
            working_text = pat.sub(lambda m: "\x00" * len(m.group(0)), working_text)

    for row_idx, c_name, c_state in all_camps:
        if c_state == "enabled" and c_name not in present_campaigns:
            violations.append(
                make_violation(
                    report_path,
                    1,
                    "MISSING_ENABLED_CAMPAIGN",
                    f"campaigns.tsv 第 {row_idx} 行的启用活动 (state=enabled) 完整名称未出现在报告正文中（防漏活动或后缀缩写串流）: '{c_name}'",
                    c_name,
                )
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Lingxing Ads Playbook Markdown report and bulk_adjustments.tsv against hard quality gates."
    )
    parser.add_argument("--report", required=True, help="Path to playbook Markdown report (.md)")
    parser.add_argument(
        "--analysis-dir",
        default=None,
        help="Optional path to analysis output directory containing campaigns.tsv / bulk_adjustments.tsv",
    )
    parser.add_argument(
        "--bulk-tsv",
        default=None,
        help="Optional path to bulk_adjustments.tsv (auto-detected from --analysis-dir if omitted)",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help="Optional path to product_rules.json / rules.json (for custom restricted_terms validation)",
    )
    parser.add_argument(
        "--legacy-v1",
        action="store_true",
        help="Validate legacy pre-v2 report without mandatory ### 0.0 subsection",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit validation result as structured JSON",
    )
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    analysis_dir = Path(args.analysis_dir) if args.analysis_dir else None
    bulk_path = Path(args.bulk_tsv) if args.bulk_tsv else None
    rules_path = Path(args.rules) if args.rules else None

    if bulk_path is None and analysis_dir is not None:
        candidate_bulk = analysis_dir / "bulk_adjustments.tsv"
        if candidate_bulk.exists():
            bulk_path = candidate_bulk

    all_violations: list[dict[str, Any]] = []
    non_fenced_text = ""

    if not report_path.exists():
        all_violations.append(
            make_violation(report_path, 1, "REPORT_NOT_FOUND", f"报告文件不存在: {report_path}")
        )
    else:
        rep_violations, non_fenced_text = validate_report_markdown(report_path, legacy_v1=args.legacy_v1)
        all_violations.extend(rep_violations)

    if bulk_path is not None:
        if not bulk_path.exists():
            all_violations.append(
                make_violation(bulk_path, 1, "BULK_NOT_FOUND", f"批量表文件不存在: {bulk_path}")
            )
        else:
            all_violations.extend(validate_bulk_tsv(bulk_path, rules_path=rules_path))
    elif analysis_dir is not None:
        # --analysis-dir was provided, so bulk_adjustments.tsv is expected inside it
        missing_bulk = analysis_dir / "bulk_adjustments.tsv"
        all_violations.append(
            make_violation(
                missing_bulk,
                1,
                "BULK_NOT_FOUND",
                f"analysis-dir 下未找到 bulk_adjustments.tsv: {missing_bulk}",
            )
        )

    if analysis_dir is not None:
        if not analysis_dir.exists():
            all_violations.append(
                make_violation(
                    analysis_dir,
                    1,
                    "ANALYSIS_DIR_NOT_FOUND",
                    f"analysis-dir 目录不存在: {analysis_dir}",
                )
            )
        elif report_path.exists():
            all_violations.extend(
                validate_three_way_consistency(report_path, non_fenced_text, analysis_dir)
            )

    passed = len(all_violations) == 0
    result = {
        "passed": passed,
        "status": "PASS" if passed else "FAIL",
        "report": str(report_path),
        "analysis_dir": str(analysis_dir) if analysis_dir else None,
        "bulk_tsv": str(bulk_path) if bulk_path else None,
        "violation_count": len(all_violations),
        "violations": all_violations,
    }

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if passed:
            checked = [f"report={report_path.name}"]
            if bulk_path:
                checked.append(f"bulk_tsv={bulk_path.name}")
            if analysis_dir:
                checked.append(f"analysis_dir={analysis_dir}")
            print(f"[PASS] Playbook report validation succeeded with 0 violations ({', '.join(checked)}).")
        else:
            print(f"[FAIL] Playbook report validation failed with {len(all_violations)} violation(s):")
            for v in all_violations:
                loc = f"{v['file']}:{v['line']}"
                print(f"  - [{v['rule']}] {loc} — {v['message']}")
                if v.get("snippet"):
                    print(f"      > {v['snippet']}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
