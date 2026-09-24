# -*- coding: utf-8 -*-
"""Independent-variable config snapshots for cooldown locks.

Amazon/Lingxing reports always return CURRENT bid/TOS/budget/state.
Metrics (spend, CPC, clicks) are historical; attributes are not.
Persist a tiny control-variable snapshot across runs and diff it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def _num(x, default=0.0) -> float:
    if x is None or x == "" or x == "--":
        return default
    try:
        return float(str(x).replace("%", "").replace(",", ""))
    except Exception:
        return default


def state_dir() -> Path:
    override = os.environ.get("LINGXING_ADS_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "skills" / "lingxing-ads-playbook" / "state"


def snapshot_path(profile_id: str, asin: str) -> Path:
    pid = re.sub(r"[^0-9A-Za-z_-]", "_", str(profile_id or "unknown"))
    aid = re.sub(r"[^0-9A-Za-z_-]", "_", str(asin or "unknown").upper())
    return state_dir() / f"{pid}_{aid}.json"


def load_snapshot(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "updated_at": "",
        "campaigns": {},
        "groups": {},
        "keywords": {},
        "targets": {},
        "active_cooldowns": [],
    }


def save_snapshot(path: Path, snap: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_config(camps: list[dict], groups: list[dict], kws: list[dict], targets: list[dict]) -> dict:
    camp_map = {}
    for c in camps:
        name = c.get("name")
        if not name:
            continue
        camp_map[name] = {
            "tos": int(_num(c.get("tos"))),
            "pp": int(_num(c.get("pp"))),
            "ros": int(_num(c.get("ros"))),
            "budget": round(_num(c.get("budget")), 2),
            "state": str(c.get("state") or ""),
        }

    group_map = {}
    for g in groups:
        campaign = g.get("campaign_name") or ""
        name = g.get("name") or g.get("ad_group_name") or ""
        if not campaign and not name:
            continue
        key = f"{campaign}::{name}"
        group_map[key] = {
            "campaign": campaign,
            "name": name,
            "default_bid": round(_num(g.get("default_bid") or g.get("bid")), 2),
            "state": str(g.get("state") or ""),
        }

    kw_map = {}
    for k in kws:
        campaign = k.get("campaign_name") or ""
        match = k.get("match_type") or ""
        text = (k.get("keyword_text") or k.get("targeting_text") or "").strip()
        if not text:
            continue
        if _num(k.get("spends")) < 1 and _num(k.get("orders")) < 1:
            continue
        key = f"{campaign}::{match}::{text.lower()}"
        kw_map[key] = {
            "campaign": campaign,
            "match": match,
            "text": text,
            "bid": round(_num(k.get("bid")), 2),
            "state": str(k.get("state") or ""),
        }

    target_map = {}
    for t in targets:
        campaign = t.get("campaign_name") or ""
        ttype = t.get("exp_type_zh") or t.get("exp_type_text") or t.get("match_type") or ""
        text = (t.get("targeting_text") or t.get("expression") or t.get("keyword_text") or "").strip()
        if not text:
            continue
        if _num(t.get("spends")) < 0.5 and _num(t.get("orders")) < 1:
            continue
        key = f"{campaign}::{ttype}::{text.lower()}"
        target_map[key] = {
            "campaign": campaign,
            "type": ttype,
            "text": text,
            "bid": round(_num(t.get("bid")), 2),
            "state": str(t.get("state") or ""),
        }

    return {
        "campaigns": camp_map,
        "groups": group_map,
        "keywords": kw_map,
        "targets": target_map,
    }


def _parse_dt(raw: str):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d")
        except Exception:
            return None


def diff_independent_vars(prev_snap: dict, curr: dict) -> list[dict]:
    """Return control-variable diffs. Never uses CPC/spend."""
    diffs = []
    prev_camps = prev_snap.get("campaigns") or {}
    for name, curr_c in (curr.get("campaigns") or {}).items():
        prev = prev_camps.get(name)
        if not prev:
            continue
        for field, lever, thresh in (
            ("tos", "placement_tos", 1),
            ("pp", "placement_pp", 1),
            ("ros", "placement_ros", 1),
            ("budget", "daily_budget", 0.5),
        ):
            old = _num(prev.get(field))
            new = _num(curr_c.get(field))
            if abs(new - old) >= thresh:
                diffs.append({
                    "campaign": name,
                    "entity": name,
                    "lever": lever,
                    "field": field,
                    "old": old,
                    "new": new,
                    "note": f"{field} {old} -> {new}",
                })
        if str(curr_c.get("state") or "") != str(prev.get("state") or ""):
            diffs.append({
                "campaign": name,
                "entity": name,
                "lever": "campaign_state",
                "field": "state",
                "old": prev.get("state"),
                "new": curr_c.get("state"),
                "note": f"state {prev.get('state')} -> {curr_c.get('state')}",
            })

    prev_groups = prev_snap.get("groups") or {}
    for key, curr_g in (curr.get("groups") or {}).items():
        prev = prev_groups.get(key)
        if not prev:
            continue
        old = _num(prev.get("default_bid"))
        new = _num(curr_g.get("default_bid"))
        if old > 0 and new > 0 and abs(new - old) >= 0.01:
            diffs.append({
                "campaign": curr_g.get("campaign"),
                "entity": key,
                "lever": "default_bid",
                "field": "default_bid",
                "old": old,
                "new": new,
                "note": f"default_bid {old:.2f} -> {new:.2f}",
            })

    prev_kws = prev_snap.get("keywords") or {}
    for key, curr_k in (curr.get("keywords") or {}).items():
        prev = prev_kws.get(key)
        if not prev:
            continue
        old = _num(prev.get("bid"))
        new = _num(curr_k.get("bid"))
        if old > 0 and new > 0 and abs(new - old) >= 0.02:
            diffs.append({
                "campaign": curr_k.get("campaign"),
                "entity": curr_k.get("text"),
                "lever": "keyword_target_bid",
                "field": "bid",
                "old": old,
                "new": new,
                "note": f"keyword {curr_k.get('text')} bid {old:.2f} -> {new:.2f}",
            })

    prev_targets = prev_snap.get("targets") or {}
    for key, curr_t in (curr.get("targets") or {}).items():
        prev = prev_targets.get(key)
        if not prev:
            continue
        old = _num(prev.get("bid"))
        new = _num(curr_t.get("bid"))
        if old > 0 and new > 0 and abs(new - old) >= 0.02:
            diffs.append({
                "campaign": curr_t.get("campaign"),
                "entity": curr_t.get("text"),
                "lever": "keyword_target_bid",
                "field": "bid",
                "old": old,
                "new": new,
                "note": f"target {curr_t.get('text')} bid {old:.2f} -> {new:.2f}",
            })
    return diffs


def keep_active_cooldowns(prev_snap: dict, today: date, cooldown_days: int = 7) -> list[dict]:
    out = []
    for item in prev_snap.get("active_cooldowns") or []:
        until_raw = item.get("lock_until") or item.get("cooldown_until") or ""
        until = None
        if until_raw:
            try:
                until = date.fromisoformat(str(until_raw)[:10])
            except ValueError:
                dt = _parse_dt(str(until_raw))
                until = dt.date() if dt else None
        if until and today <= until:
            rec = dict(item)
            rec["status"] = "cooling"
            rec["lock_until"] = until.isoformat()
            out.append(rec)
    return out


def diffs_to_cooldowns(
    diffs: list[dict],
    prev_snap: dict,
    ops_events: list[dict],
    today: date,
    cooldown_days: int = 7,
) -> list[dict]:
    prev_dt = _parse_dt(str(prev_snap.get("updated_at") or ""))
    prev_age_days = (today - prev_dt.date()).days if prev_dt else 999

    ops_by_campaign = {}
    for ev in ops_events:
        if ev.get("lever") == "negatives":
            continue
        for name in ev.get("campaigns") or []:
            ops_by_campaign.setdefault(name, []).append(ev)
        if not ev.get("campaigns"):
            ops_by_campaign.setdefault("*", []).append(ev)

    locks = []
    for diff in diffs:
        campaign = diff.get("campaign") or "*"
        changed_on = None
        if prev_age_days <= cooldown_days and prev_dt:
            changed_on = prev_dt.date()
        related = ops_by_campaign.get(campaign) or ops_by_campaign.get("*") or []
        lever = diff.get("lever")
        for ev in related:
            ev_lever = ev.get("lever")
            if ev_lever in (lever, "placement_budget_state") or (
                lever in ("placement_tos", "placement_pp", "placement_ros", "daily_budget")
                and ev_lever == "placement_budget_state"
            ) or (
                lever == "default_bid" and ev_lever == "default_bid"
            ) or (
                lever == "keyword_target_bid" and ev_lever == "keyword_target_bid"
            ):
                try:
                    d0 = date.fromisoformat(str(ev.get("date"))[:10])
                    if (today - d0).days <= cooldown_days:
                        changed_on = d0
                except ValueError:
                    pass
        if changed_on is None:
            if prev_age_days > cooldown_days:
                continue
            changed_on = today
        until = changed_on + timedelta(days=cooldown_days)
        locks.append({
            "campaign": campaign,
            "lever": lever,
            "source": "config_diff",
            "changed_on": changed_on.isoformat(),
            "lock_until": until.isoformat(),
            "status": "cooling" if today <= until else "expired",
            "old": diff.get("old"),
            "new": diff.get("new"),
            "note": diff.get("note"),
        })
    return locks


def merge_locks(*groups: list[dict]) -> list[dict]:
    best = {}
    for group in groups:
        for rec in group:
            key = (rec.get("campaign"), rec.get("lever"))
            old = best.get(key)
            if not old or str(rec.get("changed_on") or "") > str(old.get("changed_on") or ""):
                best[key] = rec
    return sorted(best.values(), key=lambda x: (x.get("lock_until") or "", x.get("campaign") or "", x.get("lever") or ""), reverse=True)
