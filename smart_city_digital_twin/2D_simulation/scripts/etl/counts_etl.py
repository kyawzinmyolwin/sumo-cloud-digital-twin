"""
Parse/clean core for the CCC traffic-count ETL.

Turns one downloaded workbook into cleaned records, and — crucially — handles the
non-vehicle files the Drive folder mixes in (pedestrian counts, blank "Turning
Count Template" workbooks, summaries) that the parser legitimately can't read.
The probe showed 10/11 sampled years parse cleanly; the misses are those other
file types, so one odd file must never break a batch.

Wraps ``traffic_counts_parser.parse_xlsx`` (validated across 2016-2026 for the
standard intersection vehicle-count workbooks). ``parse_xlsx`` raises when a
workbook has no vehicle metric grid; ``clean_workbook`` catches that and returns
a REJECTED result carrying the reason, instead of raising.

Design notes:
- ``parse_xlsx`` is *injectable* so this module is unit-testable with a fake and
  no openpyxl/parser import (see tests/test_counts_etl.py). The real parser is
  imported lazily inside ``_load_parser`` only when actually parsing.
- The real parser module runs a venv re-exec at import; the Lambda packaging step
  (Step 3) neutralises that. This module doesn't trigger it until parse time.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Where the id -> human-name lookup lives in the store (maintained by ingest,
# read by the query discovery so the dropdown can show names).
NAMES_INDEX_KEY = "index/intersection_names.json"

# Filename -> name parsing pieces. CCC workbook names look like
#   I0007_Kirk___Miners___West_Coast_339745_08-24-2016.xls
# i.e. <study id>_<streets>_<study number>_<MM-DD-YYYY>.<ext>, with '___'
# separating streets and single '_' standing in for a space within a name.
_STUDY_ID_PREFIX = re.compile(r"^I?\d+[_\s.\-]+", re.I)
_TRAILING_META = re.compile(r"[_\s]+\d{3,}(?:[_\s]+\d{1,2}-\d{1,2}-\d{2,4})?$")
_TRAILING_DATE = re.compile(r"[_\s]+\d{1,2}-\d{1,2}-\d{2,4}$")
_TRAILING_DESC = re.compile(
    r"[_\s]+(?:intersection|counts?|ped\w*|turning\s*counts?|traffic\s*counts?)\s*$", re.I
)


def intersection_name_from_filename(filename: str) -> str:
    """Best-effort human name from a CCC workbook filename.

    'I0007_Kirk___Miners___West_Coast_339745_08-24-2016.xls'
        -> 'Kirk / Miners / West Coast'

    Heuristic and imperfect across the different yearly filename conventions —
    good enough to label a dropdown; returns '' if nothing usable remains.
    """
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = _STUDY_ID_PREFIX.sub("", stem)
    stem = _TRAILING_META.sub("", stem)
    stem = _TRAILING_DATE.sub("", stem)
    stem = _TRAILING_DESC.sub("", stem)
    # Runs of '_' separate streets; a single '_' is a space inside a street name.
    stem = re.sub(r"_{2,}", "\x00", stem).replace("_", " ").replace("\x00", " / ")
    return re.sub(r"\s{2,}", " ", stem).strip(" /")


@dataclass
class CleanResult:
    """Outcome of cleaning one workbook."""

    path: str
    status: str                              # "ok" | "rejected"
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    reason: str = ""                         # populated when status == "rejected"

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _load_parser() -> Callable[..., list[dict[str, Any]]]:
    """Import the real parser lazily (keeps this module import-light/testable).

    The parser re-execs into a project ``.venv`` at import time; that's meant for
    interactive/IDE use and must not fire in a Lambda (or any headless run), so we
    set the parser's documented opt-out before importing.
    """
    os.environ.setdefault("TRAFFIC_PARSER_NO_VENV_REEXEC", "1")
    from traffic_counts_parser import parse_xlsx

    return parse_xlsx


def _first_line(text: str) -> str:
    return (text.splitlines()[0] if text else "").strip()[:200]


def clean_workbook(
    path,
    *,
    parse_xlsx: Callable[..., list[dict[str, Any]]] | None = None,
) -> CleanResult:
    """Parse one workbook into records, or reject it with a reason.

    A standard vehicle-count workbook yields rows -> ``status="ok"``. A file the
    parser can't read (pedestrian count, template, summary, corrupt) is returned
    as ``status="rejected"`` with the reason — never raised — so a batch run can
    quarantine it and carry on.

    ``parse_xlsx`` is injectable for testing; defaults to the real parser.
    """
    p = Path(path)
    fn = parse_xlsx or _load_parser()
    try:
        rows = fn(p)
    except Exception as exc:  # noqa: BLE001 - parser raises ValueError on layout mismatch
        return CleanResult(path=str(p), status="rejected", reason=_first_line(str(exc)))
    if not rows:
        return CleanResult(path=str(p), status="rejected", reason="parser returned 0 rows")
    return CleanResult(path=str(p), status="ok", rows=rows, row_count=len(rows))


# Row fields the parser emits (see the CSV header in traffic_counts_parser.py) that
# the query endpoint keys on. Kept as constants so the layout has one source of truth.
INTERSECTION_FIELD = "intersection_id"
DATE_FIELD = "survey_date"


def group_by_intersection_date(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group rows by (intersection_id, survey_date) so each group can be stored
    under a query-friendly key. Missing values become "" — the caller decides the
    fallback key so nothing is silently dropped."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        iid = str(r.get(INTERSECTION_FIELD, "") or "")
        date = str(r.get(DATE_FIELD, "") or "")
        groups.setdefault((iid, date), []).append(r)
    return groups


def to_processed_payload(result: CleanResult, *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shape an OK result into the processed-data document written to S3."""
    return {
        "source": source or {},          # drive id/name/folder/modifiedTime, set by the Lambda
        "rowCount": result.row_count,
        "rows": result.rows,
    }


def to_json_bytes(result: CleanResult, *, source: dict[str, Any] | None = None) -> bytes:
    """Serialise the processed payload to compact UTF-8 JSON bytes (for S3)."""
    return json.dumps(
        to_processed_payload(result, source=source), separators=(",", ":"), default=str
    ).encode("utf-8")
