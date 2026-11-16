# -*- coding: utf-8 -*-
"""
Portfolio-First Advertising Audit Runner for Lingxing Ads Playbook
Automates dedicated portfolio workspace isolation, fetching, analysis, and index registration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SKILL_SCRIPTS.parent
DEFAULT_BASE_DIR = Path(r"E:\shardor\antigravity\领星广告")

sys.path.insert(0, str(SKILL_SCRIPTS))
import lx_client as lx


def ensure_portfolio_structure(
    portfolio_dir: Path,
    portfolio_name: str,
    asin: str = "",
    shop: str = "",
) -> dict[str, Path]:
    """Ensure standard subdirectories exist inside the portfolio workspace."""
    outputs_dir = portfolio_dir / "outputs"
    db_dir = portfolio_dir / "db"
    daily_records_dir = db_dir / "daily_records"
    ledger_dir = db_dir / "ledger"
    snapshots_dir = db_dir / "snapshots"
    expectations_dir = db_dir / "expectations"
    entity_memory_dir = db_dir / "entity_memory"
    work_dir = portfolio_dir / "work" / "lx-ads"

    for d in [
        outputs_dir,
        db_dir,
        daily_records_dir,
        ledger_dir,
        snapshots_dir,
        expectations_dir,
        entity_memory_dir,
        work_dir,
    ]:
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

- `outputs/`：存放所有带时间戳的诊断手册报告（`.md`）、领星批量调整表（`.tsv`）等交付物；
- `db/`：
  - `daily_records/`：每日全域切片与抓取日志快照；
  - `ledger/`：持久化跨会话调控历史台账（`*_ledger.json` 与 `.tsv`），供后续诊断自动复盘对账；
  - `snapshots/`：活动、关键词与排名状态快照；
  - `expectations/`：上期量化预期目标区间，用于下期北极星总览自动验收；
  - `entity_memory/`：单实体历史建议记忆与移词软交接第1/2阶段追踪；
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
        "expectations_dir": expectations_dir,
        "entity_memory_dir": entity_memory_dir,
        "work_dir": work_dir,
    }


def register_in_portfolio_index(base_dir: Path, portfolio_name: str, asin: str, shop: str) -> None:
    """Automatically append newly created portfolio to PORTFOLIO_INDEX.md and README.md if not present."""
    index_file = base_dir / "PORTFOLIO_INDEX.md"
    readme_file = base_dir / "README.md"
    for target in [index_file, readme_file]:
        if not target.exists():
            continue
        content = target.read_text(encoding="utf-8")
        if f"`{portfolio_name}`" in content:
            continue
        lines = content.splitlines()
        insert_idx = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("|") and "[`" in line:
                insert_idx = i + 1
        if insert_idx > 0:
            new_row = (
                f"| + | [`{portfolio_name}`]({portfolio_name}/) | `{asin}` | {shop or 'DILU'} "
                f"| 自动建档-新增组合 | 0 份 | 0 份 | 0 份 | 已就绪 |"
            )
            lines.insert(insert_idx, new_row)
            target.write_text("\n".join(lines), encoding="utf-8")


def resolve_portfolio_name(asin: str, country: str, shop_alias: str | None = None) -> tuple[str, str, str]:
    """Resolve portfolio name, shop, and ASIN from Lingxing API using real report tools."""
    print(f"Auto-probing portfolio for ASIN {asin} ({country})...", file=sys.stderr)
    try:
        from fetch_ads_bundle import base_report_params, normalize_country, report_window, resolve_shop

        c_code = normalize_country(country)
        win = report_window(30)
        resolved = resolve_shop(asin, c_code, shop_alias, window=win)
        shop = resolved["shop"]
        profile_id = str(shop.get("profile_id"))
        pid_int = int(profile_id) if profile_id.isdigit() else profile_id

        # 1. Find campaigns advertising this ASIN via ad_campaign_product_report
        prod_rows = lx.paginate(
            "ad_campaign_product_report",
            {
                "report_date": win,
                "profile_id": pid_int,
                "page": 1,
                "length": 100,
                "sort_field": "spends",
                "sort_type": "desc",
                "with_ring": False,
            },
            max_pages=10,
        )
        asin_camp_ids = {
            str(r.get("campaign_id"))
            for r in prod_rows
            if str(r.get("asin") or r.get("advertised_asin") or "").upper() == asin.upper()
            and r.get("campaign_id")
        }
        asin_camp_names = {
            str(r.get("campaign_name"))
            for r in prod_rows
            if str(r.get("asin") or r.get("advertised_asin") or "").upper() == asin.upper()
            and r.get("campaign_name")
        }

        # 2. Match against ad_campaign_report to pick the portfolio with highest spend on this ASIN
        camps = lx.paginate("ad_campaign_report", base_report_params(profile_id, win), max_pages=10)
        pf_spend: dict[str, float] = {}
        for c in camps:
            cid = str(c.get("campaign_id") or "")
            cname = str(c.get("name") or c.get("campaign_name") or "")
            if asin_camp_ids or asin_camp_names:
                if cid not in asin_camp_ids and cname not in asin_camp_names:
                    continue
            pf_name = str(c.get("portfolio_name") or "").strip()
            if pf_name:
                try:
                    sp = float(c.get("spends") or c.get("cost") or c.get("spend") or 0.0)
                except Exception:
                    sp = 0.0
                pf_spend[pf_name] = pf_spend.get(pf_name, 0.0) + sp + 0.01

        if pf_spend:
            best_pf = max(pf_spend.items(), key=lambda kv: kv[1])[0]
            return best_pf, str(shop.get("alias") or "DILU"), asin
    except Exception as exc:
        print(f"Warning: auto-probe failed ({exc})", file=sys.stderr)

    return f"{asin}-{country}-portfolio", shop_alias or "DILU", asin


def _recover_meta_from_existing_workspace(portfolio_dir: Path) -> dict:
    meta_file = portfolio_dir / "work" / "lx-ads" / "meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def main() -> int:
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
    asin = args.asin.strip().upper() if args.asin else None
    country = args.country
    shop = args.shop

    if not portfolio_name and not asin:
        parser.error("Either --portfolio or --asin must be provided.")

    if not portfolio_name and asin:
        portfolio_name, auto_shop, asin = resolve_portfolio_name(asin, country, shop)
        if not shop:
            shop = auto_shop

    clean_portfolio_name = (portfolio_name or "").replace("/", "-").replace("\\", "-").strip()
    portfolio_dir = base_dir / clean_portfolio_name

    if not asin:
        prev_meta = _recover_meta_from_existing_workspace(portfolio_dir)
        asin = str(prev_meta.get("asin") or "").strip().upper() or None
        if not shop and prev_meta.get("shop_alias"):
            shop = str(prev_meta.get("shop_alias"))
        if not asin:
            parser.error(
                f"Could not recover ASIN from {portfolio_dir / 'work/lx-ads/meta.json'}; please pass --asin explicitly."
            )

    print("\n=======================================================")
    print(f"[*] Initializing Portfolio Workspace: {portfolio_dir}")
    print(f"[*] Portfolio Name: {portfolio_name}")
    print(f"[*] ASIN: {asin} | Country: {country} | Shop: {shop or 'auto'}")
    print("=======================================================\n")

    dirs = ensure_portfolio_structure(portfolio_dir, clean_portfolio_name, asin or "", shop or "")
    if asin:
        register_in_portfolio_index(base_dir, clean_portfolio_name, asin, shop or "DILU")

    fetch_script = SKILL_SCRIPTS / "fetch_ads_bundle.py"
    fetch_cmd = [
        sys.executable,
        str(fetch_script),
        "--asin",
        asin,
        "--country",
        country,
        "--outdir",
        str(dirs["work_dir"]),
        "--db-dir",
        str(dirs["db_dir"]),
        "--concurrency",
        str(args.concurrency),
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

    analyze_script = SKILL_SCRIPTS / "analyze_ads_bundle.py"
    analysis_outdir = dirs["work_dir"] / "analysis"
    analyze_cmd = [
        sys.executable,
        str(analyze_script),
        "--indir",
        str(dirs["work_dir"]),
        "--outdir",
        str(analysis_outdir),
        "--db-dir",
        str(dirs["db_dir"]),
        "--snapshots-dir",
        str(dirs["snapshots_dir"]),
        "--export-dir",
        str(dirs["outputs_dir"]),
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

    print("\n=======================================================")
    print("[OK] Portfolio Audit Completed Successfully!")
    print(f"[*] Workspace Root: {portfolio_dir}")
    print(f"[*] Outputs: {dirs['outputs_dir']}")
    print(f"[*] Ledger & Database: {dirs['db_dir']}")
    print(f"[*] Raw Data Cache: {dirs['work_dir']}")
    print("=======================================================\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
