"""
Query core for the CCC traffic-count ETL — AWS-free and unit-testable.

Reads the processed objects the ingest wrote under
``processed-traffic-data/<intersection_id>/<survey_date>.json``, so a query is a
direct get (intersection + date) or a prefix list (intersection, or discovery) —
no scanning of every file, no separate index.

Needs an ObjectStore with ``get_json`` and ``list_keys`` (see etl.s3_store); the
Lambda (lambda_query) supplies the S3-backed one.
"""
from __future__ import annotations

from typing import Any, Protocol

from etl.counts_etl import NAMES_INDEX_KEY

PROCESSED_PREFIX = "processed-traffic-data"


class ReadableStore(Protocol):
    def get_json(self, key: str) -> dict[str, Any] | None: ...
    def list_keys(self, prefix: str) -> list[str]: ...


def _date_from_key(key: str) -> str:
    """`.../<intersection>/<date>.json` -> `<date>` (handles the __workbook suffix)."""
    leaf = key.rsplit("/", 1)[-1]
    if leaf.endswith(".json"):
        leaf = leaf[: -len(".json")]
    return leaf.split("__", 1)[0]


def query_counts(
    store: ReadableStore,
    *,
    intersection: str | None = None,
    date: str | None = None,
    prefix: str = PROCESSED_PREFIX,
) -> dict[str, Any]:
    """Resolve a `/traffic-counts` query.

    - intersection + date -> that survey's rows (``found`` False if absent).
    - intersection only    -> the list of dates available for it.
    - neither              -> the list of intersections (discovery).
    """
    if intersection and date:
        doc = store.get_json(f"{prefix}/{intersection}/{date}.json")
        if doc is None:
            return {"intersection": intersection, "date": date, "found": False, "rows": []}
        return {
            "intersection": intersection,
            "date": date,
            "found": True,
            "rowCount": doc.get("rowCount", len(doc.get("rows", []))),
            "rows": doc.get("rows", []),
        }

    if intersection:
        keys = store.list_keys(f"{prefix}/{intersection}/")
        dates = sorted({_date_from_key(k) for k in keys if k.endswith(".json")})
        return {"intersection": intersection, "dates": dates}

    keys = store.list_keys(f"{prefix}/")
    ids = sorted(
        {k[len(prefix) + 1 :].split("/", 1)[0] for k in keys if "/" in k[len(prefix) + 1 :]}
    )
    names = store.get_json(NAMES_INDEX_KEY) or {}
    return {"intersections": [{"id": i, "name": names.get(i, "")} for i in ids]}
