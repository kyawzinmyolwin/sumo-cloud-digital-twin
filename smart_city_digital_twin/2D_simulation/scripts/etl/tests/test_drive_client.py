"""
Unit tests for the pure parts of drive_client — no network, no AWS.

The change-detection logic is what the scheduled Lambda relies on to fetch only
new/updated files, so it's tested here with plain DriveFile objects and fake
manifests.

    python -m pytest etl/tests/test_drive_client.py   # or: python etl/tests/test_drive_client.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from etl.drive_client import (  # noqa: E402
    DriveFile,
    merge_manifest,
    select_new_or_updated,
)


def _f(fid, mtime, name="x.xlsx"):
    return DriveFile(id=fid, name=name, mime_type="…sheet", modified_time=mtime, folder="2026")


def test_empty_manifest_selects_everything():
    files = [_f("a", "2026-01-01T00:00:00Z"), _f("b", "2026-02-01T00:00:00Z")]
    assert {f.id for f in select_new_or_updated(files, {})} == {"a", "b"}


def test_unchanged_files_are_skipped():
    files = [_f("a", "2026-01-01T00:00:00Z")]
    manifest = {"a": "2026-01-01T00:00:00Z"}
    assert select_new_or_updated(files, manifest) == []


def test_updated_file_is_selected():
    files = [_f("a", "2026-03-01T00:00:00Z")]  # newer than manifest
    manifest = {"a": "2026-01-01T00:00:00Z"}
    got = select_new_or_updated(files, manifest)
    assert [f.id for f in got] == ["a"]


def test_mixed_new_updated_unchanged():
    files = [
        _f("new", "2026-05-01T00:00:00Z"),                 # absent -> select
        _f("upd", "2026-05-02T00:00:00Z"),                 # newer  -> select
        _f("same", "2026-01-01T00:00:00Z"),                # equal  -> skip
    ]
    manifest = {"upd": "2026-04-01T00:00:00Z", "same": "2026-01-01T00:00:00Z"}
    assert {f.id for f in select_new_or_updated(files, manifest)} == {"new", "upd"}


def test_iso_strings_sort_chronologically():
    # A later wall-clock time must compare greater as a plain string.
    assert "2026-05-27T02:25:53.212Z" > "2025-02-18T23:51:21.547Z"


def test_merge_manifest_records_all_and_preserves_others():
    files = [_f("a", "2026-06-01T00:00:00Z"), _f("b", "2026-06-02T00:00:00Z")]
    manifest = {"a": "2026-01-01T00:00:00Z", "c": "2020-01-01T00:00:00Z"}
    merged = merge_manifest(files, manifest)
    assert merged["a"] == "2026-06-01T00:00:00Z"   # updated
    assert merged["b"] == "2026-06-02T00:00:00Z"   # added
    assert merged["c"] == "2020-01-01T00:00:00Z"   # untouched
    assert manifest["a"] == "2026-01-01T00:00:00Z"  # input not mutated


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
