# -*- coding: utf-8 -*-
"""Scope campaigns to one ASIN so snapshots do not mix categories."""

from __future__ import annotations

import re


def belonging_tokens(asin: str | None, listing: dict | None = None) -> list[str]:
    listing = listing or {}
    tokens: list[str] = []

    def add(raw):
        text = str(raw or "").strip()
        if not text:
            return
        if text not in tokens:
            tokens.append(text)
        for part in re.split(r"[-_\s/]+", text):
            part = part.strip()
            if len(part) >= 6 and re.search(r"[A-Za-z]", part) and re.search(r"\d", part):
                if part not in tokens:
                    tokens.append(part)

    add(asin)
    add(listing.get("asin"))
    add(listing.get("msku") or listing.get("seller_sku"))
    add(listing.get("local_sku") or listing.get("sku"))
    parent = listing.get("parent_asin")
    if parent and str(parent).upper() != str(asin or "").upper():
        add(parent)
    return tokens


def campaign_belongs(name, tokens: list[str]) -> bool:
    text = str(name or "")
    if not text or not tokens:
        return False
    upper = text.upper()
    for tok in tokens:
        needle = str(tok).strip()
        if len(needle) < 4:
            continue
        if needle.upper() in upper:
            return True
    return False


def filter_named_rows(rows: list[dict], tokens: list[str], name_keys=("name", "campaign_name")) -> list[dict]:
    if not tokens:
        return rows
    out = []
    for row in rows:
        name = ""
        for key in name_keys:
            if row.get(key):
                name = row.get(key)
                break
        if campaign_belongs(name, tokens):
            out.append(row)
    return out


def matched_campaign_names(rows: list[dict], tokens: list[str]) -> set[str]:
    names = set()
    for row in filter_named_rows(rows, tokens):
        name = row.get("name") or row.get("campaign_name")
        if name:
            names.add(str(name))
        if row.get("campaign_id"):
            names.add(str(row.get("campaign_id")))
    return names
