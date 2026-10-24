# -*- coding: utf-8 -*-
"""
Portfolio-First Advertising Audit Runner for Lingxing Ads Playbook
Automates dedicated portfolio workspace isolation, fetching, analysis, and index registration.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Paths
SKILL_DIR = Path(r"C:\Users\Administrator\.gemini\config\skills\lingxing-ads-playbook")
SKILL_SCRIPTS = SKILL_DIR / "scripts"
DEFAULT_BASE_DIR = Path(r"E:\shardor\antigravity\领星广告")

sys.path.insert(0, str(SKILL_SCRIPTS))
import lx_client as lx


def ensure_portfolio_structure(portfolio_dir: Path, portfolio_name: str, asin: str = "", shop: str = "") -> dict[str, Path]:
    """Ensure standard 4-module subdirectories exist inside the portfolio workspace."""
    outputs_dir = portfolio_dir / "outputs"
    db_dir = portfolio_dir / "db"
    daily_records_dir = db_dir / "daily_records"
    ledger_dir = db_dir / "ledger"
    snapshots_dir = db_dir / "snapshots"
    work_dir = portfolio_dir / "work" / "lx-ads"

    for d in [outputs_dir, db_dir, daily_records_dir, ledger_dir, snapshots_dir, work_dir]:
        d.mkdir(parents=True, exist_ok=True)

    readme_file = portfolio_dir / "README.md"
    if not readme_file.exists():
        readme_content = f"""# {portfolio_name} - 广告组合专属工作区

> **组合元数据**：
> - **广告组合名称**：`{portfolio_name}`
> - **主推 ASIN**：`{asin or "待定"}`
> - **店铺**：`{shop or "待定"}`
> - **创建时间**：`{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}`

---

## 目录结构说明

- `outputs/`：存放所有带时间戳的诊断手册报告（`.md`）、领星批量调整表（`.tsv`）、广告位与分时分析等交付物；
- `db/`：
  - `daily_records/`：每日全域切片与抓取日志快照；
  - `ledger/`：持久化跨会话调控历史台账（`portfolio_*_ledger.json` 与 `.tsv`），供后续诊断自动复盘对账；
  - `snapshots/`：活动状态快照；
- `work/lx-ads/`：领星活数据与西柚洞察原始数据缓存。
"""
        readme_file.write_text(readme_content, encoding="utf-8")

    return {
        "portfolio_dir": portfolio_dir,
        "outputs_dir": outputs_dir,
        "db_dir": db_dir,
        "daily_records_dir": daily_records_dir,
        "ledger_dir": ledger_dir,
        "snapshots_dir": snapshots_dir,
        "work_dir": work_dir,
    }


def register_in_portfolio_index(base_dir: Path, portfolio_name: str, asin: str, shop: str):
    """Automatically append newly created portfolio to PORTFOLIO_INDEX.md and README.md if not present."""
    index_file = base_dir / "PORTFOLIO_INDEX.md"
    readme_file = base_dir / "README.md"
    for target in [index_file, readme_file]:
        if not target.exists():
            continue
        content = target.read_text(encoding="utf-8")
        if f"`{portfolio_name}`" in content:
            continue
        # Find table end or insert new line
        lines = content.splitlines()
        insert_idx = -1
        for i, l in enumerate(lines):
            if l.strip().startswith("|") and f"[`" in l:
                insert_idx = i + 1
        if insert_idx > 0:
            new_row = f"| + | [`{portfolio_name}`]({portfolio_name}/) | `{asin}` | {shop or 'DILU'} | 自动建档-新增组合 | 0 份 | 0 份 | 0 份 | 已就绪 |"
            lines.insert(insert_idx, new_row)
            target.write_text("\n".join(lines), encoding="utf-8")


def resolve_portfolio_name(asin: str, country: str, shop_alias: str | None = None) -> tuple[str, str, str]:
    """Resolve portfolio name, shop, and ASIN from Lingxing API if portfolio name was not provided."""
    print(f"Auto-probing portfolio for ASIN {asin} ({country})...", file=sys.stderr)
    try:
        from fetch_ads_bundle import resolve_shop, normalize_country
        c_code = normalize_country(country)
        resolved = resolve_shop(asin, c_code, shop_alias)
        shop = resolved["shop"]
        profile_id = str(shop.get("profile_id"))
        sid = str(shop.get("sid"))
        
        # Probe campaigns to see if they belong to a portfolio
        camps = lx.action("get_sp_campaign_list", {
            "profile_id": profile_id,
            "sid": sid,
            "offset": 0,
            "length": 50,
        })
        list_c = camps.get("list", []) if isinstance(camps, dict) else (camps if isinstance(camps, list) else [])
        for c in list_c:
            pf_name = c.get("portfolio_name")
            if pf_name and pf_name.strip():
                return pf_name.strip(), shop.get("alias", "DILU"), asin
    except Exception as exc:
        print(f"Warning: auto-probe failed ({exc})", file=sys.stderr)
    
    # Fallback name if no portfolio was attached
    return f"{asin}-{country}-portfolio", shop_alias or "DILU", asin


def main():
    parser = argparse.ArgumentParser(description="Portfolio-First Advertising Audit Runner")
    parser.add_argument("--portfolio", default=None, help="Advertising portfolio name (folder name)")
    parser.add_argument("--asin", default=None, help="Target ASIN (e.g. B0FPC98VJ1)")
    parser.add_argument("--country", default="US", help="Target marketplace/country (default: US)")
    parser.add_argument("--shop", default=None, help="Shop alias (e.g. dilu)")
    parser.add_argument("--target-acos", type=float, default=None, help="Target ACOS threshold (e.g. 25.0)")
    parser.add_argument("--base-dir", default=str(DEFAULT_BASE_DIR), help="Base directory for advertising portfolios")
    parser.add_argument("--concurrency", type=int, default=6, help="Report fetching concurrency")
    args = parser.parse_args()

    base_dir = Path(args.base_dir)
    portfolio_name = args.portfolio
    asin = args.asin
    country = args.country
    shop = args.shop

    if not portfolio_name and not asin:
        parser.error("Either --portfolio or --asin must be provided.")

    if not portfolio_name and asin:
        portfolio_name, auto_shop, asin = resolve_portfolio_name(asin, country, shop)
        if not shop:
            shop = auto_shop

    # Sanitize portfolio directory name
    clean_portfolio_name = portfolio_name.replace("/", "-").replace("\\", "-").strip()
    portfolio_dir = base_dir / clean_portfolio_name

    print(f"\n=======================================================")
    print(f"[*] Initializing Portfolio Workspace: {portfolio_dir}")
    print(f"[*] Portfolio Name: {portfolio_name}")
    print(f"[*] ASIN: {asin} | Country: {country} | Shop: {shop}")
    print(f"=======================================================\n")

    dirs = ensure_portfolio_structure(portfolio_dir, clean_portfolio_name, asin or "", shop or "")
    if asin:
        register_in_portfolio_index(base_dir, clean_portfolio_name, asin, shop or "DILU")

    # Step 1: Run fetch_ads_bundle.py
    fetch_script = SKILL_SCRIPTS / "fetch_ads_bundle.py"
    fetch_cmd = [
        sys.executable,
        str(fetch_script),
        "--asin", asin or clean_portfolio_name,
        "--country", country,
        "--outdir", str(dirs["work_dir"]),
        "--db-dir", str(dirs["db_dir"]),
        "--concurrency", str(args.concurrency),
    ]
    if shop:
        fetch_cmd.extend(["--shop", shop])
    if portfolio_name:
        fetch_cmd.extend(["--portfolio", portfolio_name])

    print(f"[1/2] Fetching live data into: {dirs['work_dir']}")
    res_fetch = subprocess.run(fetch_cmd, text=True)
    if res_fetch.returncode != 0:
        print(f"Error during fetch_ads_bundle (code {res_fetch.returncode})", file=sys.stderr)
        return res_fetch.returncode

    # Step 2: Run analyze_ads_bundle.py
    analyze_script = SKILL_SCRIPTS / "analyze_ads_bundle.py"
    analysis_outdir = dirs["work_dir"] / "analysis"
    analyze_cmd = [
        sys.executable,
        str(analyze_script),
        "--indir", str(dirs["work_dir"]),
        "--outdir", str(analysis_outdir),
        "--db-dir", str(dirs["db_dir"]),
        "--snapshots-dir", str(dirs["snapshots_dir"]),
        "--export-dir", str(dirs["outputs_dir"]),
    ]
    if args.target_acos is not None:
        analyze_cmd.extend(["--target-acos", str(args.target_acos)])
    if shop:
        analyze_cmd.extend(["--shop", str(shop)])

    print(f"\n[2/2] Analyzing data & outputting to: {dirs['outputs_dir']}")
    res_analyze = subprocess.run(analyze_cmd, text=True)
    if res_analyze.returncode != 0:
        print(f"Error during analyze_ads_bundle (code {res_analyze.returncode})", file=sys.stderr)
        return res_analyze.returncode

    print(f"\n=======================================================")
    print(f"[√] Portfolio Audit Completed Successfully!")
    print(f"[*] Workspace Root: {portfolio_dir}")
    print(f"[*] Outputs: {dirs['outputs_dir']}")
    print(f"[*] Ledger & Database: {dirs['db_dir']}")
    print(f"[*] Raw Data Cache: {dirs['work_dir']}")
    print(f"=======================================================\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
