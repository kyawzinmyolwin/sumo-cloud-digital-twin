"""
Ingest orchestration for the CCC traffic-count ETL — AWS-free core.

``run_ingest`` ties the Drive client and the clean core together against an
abstract object store, so the whole flow is testable with an in-memory store and
even runnable end-to-end locally (real Drive + parser, no AWS):

    list workbooks  ->  select new/updated (vs manifest)  ->  for each:
        download  ->  store raw  ->  clean  ->  store processed | rejected
    ->  update manifest

The Lambda handler (Step 3b) supplies an S3-backed store and the Drive key from
Secrets Manager; nothing here imports boto3.

Store contract (see ObjectStore): get_json(key)->dict|None, put_bytes(key,bytes),
put_json(key,obj). Keys are S3-style paths under the configured prefixes.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from etl.counts_etl import (
    NAMES_INDEX_KEY,
    clean_workbook,
    group_by_intersection_date,
    intersection_name_from_filename,
)
from etl.drive_client import (
    download as drive_download,
    merge_manifest,
    select_new_or_updated,
    walk_workbooks,
)

MANIFEST_KEY = "manifest/drive_manifest.json"


class ObjectStore(Protocol):
    def get_json(self, key: str) -> dict[str, Any] | None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def put_json(self, key: str, obj: Any) -> None: ...


@dataclass
class IngestSummary:
    scanned: int = 0
    selected: int = 0
    processed: int = 0
    rejected: int = 0
    errors: int = 0
    rejects: list[dict[str, str]] = field(default_factory=list)  # {name, reason}

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "selected": self.selected,
            "processed": self.processed,
            "rejected": self.rejected,
            "errors": self.errors,
            "rejects": self.rejects,
        }


def _folder_seg(folder: str) -> str:
    return folder or "root"


def _processed_key(prefix: str, iid: str, date: str, workbook: str) -> str:
    """Query-friendly key: processed-traffic-data/<intersection>/<date>.json.

    Falls back to placeholders when a group lacks an id/date, appending the source
    workbook name in that case so distinct sources can't collide into one key.
    """
    if iid and date:
        return f"{prefix}/{iid}/{date}.json"
    iid = iid or "unknown-intersection"
    date = date or "unknown-date"
    stem = workbook.rsplit(".", 1)[0].replace("/", "_")
    return f"{prefix}/{iid}/{date}__{stem}.json"


def run_ingest(
    store: ObjectStore,
    key: str,
    *,
    top_folder_id: str,
    max_files: int | None = None,
    raw_prefix: str = "raw-traffic-data",
    processed_prefix: str = "processed-traffic-data",
    rejected_prefix: str = "rejected-traffic-data",
    # injected for testing; default to the real implementations
    walk: Callable[..., Any] = walk_workbooks,
    download: Callable[..., Any] = drive_download,
    clean: Callable[..., Any] = clean_workbook,
) -> IngestSummary:
    """Run one ETL pass and return a summary.

    ``max_files`` bounds how many new/updated files are handled this pass (handy
    for a first backfill or a smoke run). Files that download+handle successfully
    (processed OR rejected) are recorded in the manifest so they aren't reworked;
    a download error leaves the file out of the manifest so it retries next pass.
    """
    summary = IngestSummary()

    files = list(walk(top_folder_id, key))
    summary.scanned = len(files)

    manifest: dict[str, str] = store.get_json(MANIFEST_KEY) or {}
    todo = select_new_or_updated(files, manifest)
    if max_files is not None:
        todo = todo[:max_files]
    summary.selected = len(todo)

    handled = []  # only these advance the manifest
    names_seen: dict[str, str] = {}  # intersection id -> human name (this pass)
    with tempfile.TemporaryDirectory() as tmp:
        for f in todo:
            seg = _folder_seg(f.folder)
            dest = Path(tmp) / f.name.replace("/", "_")
            try:
                download(f.id, key, dest)
                store.put_bytes(f"{raw_prefix}/{seg}/{f.name}", dest.read_bytes())
                result = clean(dest)
            except Exception:  # noqa: BLE001 - a bad download/parse must not abort the batch
                summary.errors += 1
                continue

            source = {
                "driveId": f.id,
                "name": f.name,
                "folder": f.folder,
                "modifiedTime": f.modified_time,
            }
            if result.ok:
                # Store one query-friendly object per (intersection, date) group,
                # keyed so the query Lambda can fetch/prefix-list without scanning.
                for (iid, date), rows in group_by_intersection_date(result.rows).items():
                    store.put_json(
                        _processed_key(processed_prefix, iid, date, f.name),
                        {
                            "source": source,
                            "intersection": iid,
                            "date": date,
                            "rowCount": len(rows),
                            "rows": rows,
                        },
                    )
                    if iid:
                        names_seen.setdefault(iid, intersection_name_from_filename(f.name))
                summary.processed += 1
            else:
                store.put_json(
                    f"{rejected_prefix}/{seg}/{f.name}.json",
                    {"source": source, "reason": result.reason},
                )
                summary.rejected += 1
                summary.rejects.append({"name": f.name, "reason": result.reason})
            handled.append(f)

    if names_seen:
        merged_names = {**(store.get_json(NAMES_INDEX_KEY) or {}), **names_seen}
        store.put_json(NAMES_INDEX_KEY, merged_names)

    store.put_json(MANIFEST_KEY, merge_manifest(handled, manifest))
    return summary


def rebuild_name_index(store, *, processed_prefix: str = "processed-traffic-data") -> int:
    """Populate the id -> name index from already-processed objects.

    A one-off for data ingested before names were tracked: reads one object per
    intersection (its ``source.name``), derives a name, and writes the index —
    no need to re-download or re-parse the workbooks. Returns the id count.
    """
    seen: dict[str, str] = {}
    for k in store.list_keys(f"{processed_prefix}/"):
        rel = k[len(processed_prefix) + 1 :]
        if "/" not in rel:
            continue
        iid = rel.split("/", 1)[0]
        if iid in seen:
            continue  # one read per intersection is enough
        doc = store.get_json(k) or {}
        seen[iid] = intersection_name_from_filename((doc.get("source") or {}).get("name", ""))
    merged = {**(store.get_json(NAMES_INDEX_KEY) or {}), **seen}
    store.put_json(NAMES_INDEX_KEY, merged)
    return len(seen)
