"""
Unit tests for the ingest orchestration core — no AWS, no network, no parser.

Everything is injected: a fake Drive walk/download, a fake clean, and an
in-memory store. That lets us assert the full flow (select -> raw -> clean ->
processed/rejected -> manifest) deterministically.

    python -m pytest etl/tests/test_ingest.py   # or: python etl/tests/test_ingest.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from etl.counts_etl import CleanResult  # noqa: E402
from etl.drive_client import DriveFile  # noqa: E402
from etl.ingest import MANIFEST_KEY, run_ingest  # noqa: E402


class MemStore:
    """In-memory ObjectStore: bytes blobs + JSON docs in two dicts."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.docs: dict[str, object] = {}

    def get_json(self, key):
        return self.docs.get(key)

    def put_bytes(self, key, data):
        self.blobs[key] = data

    def put_json(self, key, obj):
        self.docs[key] = obj

    def list_keys(self, prefix):
        return [k for k in {**self.blobs, **self.docs} if k.startswith(prefix)]


def _file(fid, name, folder="2026 Intersection", mtime="2026-01-01T00:00:00Z"):
    return DriveFile(id=fid, name=name, mime_type="…sheet", modified_time=mtime, folder=folder)


def _fake_walk(files):
    return lambda top, key: iter(files)


def _fake_download(dest_bytes=b"PK\x03\x04fake-xlsx"):
    def _dl(file_id, key, dest):
        from pathlib import Path
        Path(dest).write_bytes(dest_bytes)
        return dest
    return _dl


def _fake_clean(mapping):
    """mapping: filename -> ('ok', rows) | ('reject', reason)."""
    def _clean(path):
        from pathlib import Path
        name = Path(path).name
        kind, val = mapping[name]
        if kind == "ok":
            return CleanResult(path=str(path), status="ok", rows=val, row_count=len(val))
        return CleanResult(path=str(path), status="rejected", reason=val)
    return _clean


def test_full_pass_processes_and_rejects_and_writes_manifest():
    files = [
        _file("a", "good1.xlsx"),
        _file("b", "good2.xlsx"),
        _file("c", "pedestrian.xlsx"),
    ]
    clean = _fake_clean({
        "good1.xlsx": ("ok", [{"intersection_id": "I1", "survey_date": "2020-02-17", "totals": 5}]),
        "good2.xlsx": ("ok", [{"intersection_id": "I2", "survey_date": "2019-03-01", "totals": 9}]),
        "pedestrian.xlsx": ("reject", "Expected at least one Miovision metric sheet"),
    })
    store = MemStore()
    s = run_ingest(store, "KEY", top_folder_id="TOP",
                   walk=_fake_walk(files), download=_fake_download(), clean=clean)

    assert (s.scanned, s.selected, s.processed, s.rejected, s.errors) == (3, 3, 2, 1, 0)
    # raw (put_bytes) for all 3; processed + rejected + manifest are JSON docs
    assert len(store.blobs) == 3
    assert "raw-traffic-data/2026 Intersection/good1.xlsx" in store.blobs
    # processed objects keyed by intersection/date (query-friendly)
    assert store.docs["processed-traffic-data/I1/2020-02-17.json"]["rowCount"] == 1
    assert "processed-traffic-data/I2/2019-03-01.json" in store.docs
    assert "rejected-traffic-data/2026 Intersection/pedestrian.xlsx.json" in store.docs
    # manifest records all 3 handled files
    assert set(store.docs[MANIFEST_KEY]) == {"a", "b", "c"}
    assert s.rejects == [{"name": "pedestrian.xlsx", "reason": "Expected at least one Miovision metric sheet"}]


def test_second_pass_is_a_noop_when_nothing_changed():
    files = [_file("a", "good1.xlsx")]
    clean = _fake_clean({"good1.xlsx": ("ok", [{"x": 1}])})
    store = MemStore()
    run_ingest(store, "K", top_folder_id="TOP", walk=_fake_walk(files), download=_fake_download(), clean=clean)
    s2 = run_ingest(store, "K", top_folder_id="TOP", walk=_fake_walk(files), download=_fake_download(), clean=clean)
    assert (s2.selected, s2.processed, s2.rejected) == (0, 0, 0)


def test_updated_file_is_reprocessed():
    store = MemStore()
    clean = _fake_clean({"good1.xlsx": ("ok", [{"x": 1}])})
    run_ingest(store, "K", top_folder_id="TOP",
               walk=_fake_walk([_file("a", "good1.xlsx", mtime="2026-01-01T00:00:00Z")]),
               download=_fake_download(), clean=clean)
    # same id, newer modifiedTime -> selected again
    s2 = run_ingest(store, "K", top_folder_id="TOP",
                    walk=_fake_walk([_file("a", "good1.xlsx", mtime="2026-06-01T00:00:00Z")]),
                    download=_fake_download(), clean=clean)
    assert s2.selected == 1 and s2.processed == 1


def test_download_error_is_counted_and_not_manifested():
    def _boom(file_id, key, dest):
        raise RuntimeError("network down")

    store = MemStore()
    s = run_ingest(store, "K", top_folder_id="TOP",
                   walk=_fake_walk([_file("a", "good1.xlsx")]),
                   download=_boom, clean=_fake_clean({}))
    assert s.errors == 1 and s.processed == 0
    assert store.docs[MANIFEST_KEY] == {}          # not recorded -> retried next pass


def test_max_files_bounds_the_pass():
    files = [_file(str(i), f"f{i}.xlsx") for i in range(5)]
    clean = _fake_clean({f"f{i}.xlsx": ("ok", [{"x": i}]) for i in range(5)})
    store = MemStore()
    s = run_ingest(store, "K", top_folder_id="TOP", max_files=2,
                   walk=_fake_walk(files), download=_fake_download(), clean=clean)
    assert s.scanned == 5 and s.selected == 2 and s.processed == 2


def test_run_ingest_writes_name_index():
    from etl.counts_etl import NAMES_INDEX_KEY
    name = "I0007_Kirk___Miners___West_Coast_339745_08-24-2016.xlsx"
    clean = _fake_clean({name: ("ok", [{"intersection_id": "I0007", "survey_date": "2016-08-24"}])})
    store = MemStore()
    run_ingest(store, "K", top_folder_id="T",
               walk=_fake_walk([_file("a", name)]), download=_fake_download(), clean=clean)
    assert store.docs[NAMES_INDEX_KEY]["I0007"] == "Kirk / Miners / West Coast"


def test_rebuild_name_index_from_processed_objects():
    from etl.counts_etl import NAMES_INDEX_KEY
    from etl.ingest import rebuild_name_index
    store = MemStore()
    store.docs["processed-traffic-data/I0007/2016-08-24.json"] = {
        "source": {"name": "I0007_Kirk___Miners___West_Coast_339745_08-24-2016.xlsx"}
    }
    store.docs["processed-traffic-data/I0007/2020-02-17.json"] = {"source": {"name": "other.xlsx"}}
    n = rebuild_name_index(store)
    assert n == 1                                   # one read per intersection id
    assert store.docs[NAMES_INDEX_KEY]["I0007"] == "Kirk / Miners / West Coast"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
