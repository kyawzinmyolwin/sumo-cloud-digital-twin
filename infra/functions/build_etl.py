#!/usr/bin/env python3
"""
Assemble the ETL Lambda bundles that etl.tf zips.

Terraform's archive_file can only zip an existing directory — it can't pip-install
or reach across the repo — so this script stages each function's bundle under
infra/build/ first. Run it before `terraform apply`:

    python infra/functions/build_etl.py
    terraform -chdir=infra apply

Bundles:
  build/etl_query   — the etl package only (query path; boto3 is in the runtime).
  build/etl_ingest  — the etl package + the workbook parser (traffic_counts_parser,
                      _sim_root, pipeline_progress) + openpyxl/xlrd, which the
                      parser needs and the Lambda runtime does not provide.

Both are pure-Python, so the vendored deps work on the Lambda runtime regardless
of the machine this is built on.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # infra/functions
INFRA = HERE.parent                               # infra
REPO = INFRA.parent                               # repo root
SCRIPTS = REPO / "smart_city_digital_twin" / "2D_simulation" / "scripts"
ETL_PKG = SCRIPTS / "etl"
BUILD = INFRA / "build"

# Parser modules the ingest Lambda imports (all stdlib-only except openpyxl/xlrd,
# which the parser imports lazily and we vendor below).
PARSER_MODULES = ["traffic_counts_parser.py", "_sim_root.py", "pipeline_progress.py"]
INGEST_PIP_DEPS = ["openpyxl", "xlrd"]

_IGNORE = shutil.ignore_patterns("tests", "__pycache__", "*.pyc")


def _fresh(d: Path) -> None:
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)


def _copy_etl(dest: Path) -> None:
    shutil.copytree(ETL_PKG, dest / "etl", ignore=_IGNORE)


def build_query() -> Path:
    d = BUILD / "etl_query"
    _fresh(d)
    _copy_etl(d)
    return d


def build_ingest() -> Path:
    d = BUILD / "etl_ingest"
    _fresh(d)
    _copy_etl(d)
    for module in PARSER_MODULES:
        shutil.copy2(SCRIPTS / module, d / module)
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(d), *INGEST_PIP_DEPS]
    )
    return d


def main() -> int:
    if not ETL_PKG.is_dir():
        print(f"ERROR: etl package not found at {ETL_PKG}", file=sys.stderr)
        return 2
    BUILD.mkdir(exist_ok=True)
    q = build_query()
    i = build_ingest()
    print(f"built {q}")
    print(f"built {i}")
    print("done — now: terraform -chdir=infra apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
