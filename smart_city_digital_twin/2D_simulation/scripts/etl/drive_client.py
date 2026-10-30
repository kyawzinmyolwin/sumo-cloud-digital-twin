"""
Google Drive client for the CCC traffic-count ETL — AWS-free and unit-testable.

Talks to the Drive REST API with an API key (the CCC folder is public, so a key
is sufficient — no OAuth/service account). Deliberately split so the two network
calls (``walk_workbooks`` / ``download``) are separate from the pure
change-detection logic (``select_new_or_updated`` / ``merge_manifest``): the
Lambda handler (Step 3) wires the pure parts to S3 + Secrets Manager, and the
pure parts are unit-tested with no network (see tests/test_drive_client.py).

Standard library only, so it bundles into a Lambda with no extra dependencies.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Iterator

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
# Workbook extensions the parser can consume; other files (PDFs, Google Sheets,
# pedestrian-count variants) are ignored at the walk stage.
EXCEL_EXTS = (".xlsx", ".xlsm", ".xls", ".xltx", ".xltm")


@dataclass(frozen=True)
class DriveFile:
    """One workbook found in the folder tree."""

    id: str
    name: str
    mime_type: str
    modified_time: str  # RFC-3339 UTC, e.g. "2026-05-27T02:25:53.212Z"
    folder: str = ""    # the (year) folder name it was found under


class DriveError(RuntimeError):
    """Any Drive API failure, with the response body attached where available."""


def _api(params: dict, key: str, timeout: int = 60) -> dict:
    q = dict(params)
    q["key"] = key
    q.setdefault("supportsAllDrives", "true")
    q.setdefault("includeItemsFromAllDrives", "true")
    url = DRIVE_API + "?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise DriveError(f"Drive API {e.code}: {body}") from e


def list_children(folder_id: str, key: str) -> list[dict]:
    """Every child of ``folder_id`` (following pagination)."""
    out: list[dict] = []
    token: str | None = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "1000",
            "orderBy": "name",
        }
        if token:
            params["pageToken"] = token
        data = _api(params, key)
        out.extend(data.get("files", []))
        token = data.get("nextPageToken")
        if not token:
            return out


def walk_workbooks(top_folder_id: str, key: str, max_depth: int = 4) -> Iterator[DriveFile]:
    """Yield a DriveFile for every Excel workbook under ``top_folder_id``.

    The CCC folder is nested (top -> per-year folders -> workbooks), so this
    recurses into subfolders up to ``max_depth`` and reports each workbook with
    the folder it was found under.
    """
    def _walk(folder_id: str, folder_name: str, depth: int) -> Iterator[DriveFile]:
        for f in list_children(folder_id, key):
            if f["mimeType"] == FOLDER_MIME:
                if depth < max_depth:
                    yield from _walk(f["id"], f["name"], depth + 1)
            elif f["name"].lower().endswith(EXCEL_EXTS):
                yield DriveFile(
                    id=f["id"],
                    name=f["name"],
                    mime_type=f["mimeType"],
                    modified_time=f.get("modifiedTime", ""),
                    folder=folder_name,
                )

    yield from _walk(top_folder_id, "", 0)


def select_new_or_updated(
    files: Iterable[DriveFile], manifest: dict[str, str]
) -> list[DriveFile]:
    """Pure: files not in the manifest, or whose modifiedTime advanced.

    ``manifest`` maps ``fileId -> last-seen modifiedTime``. RFC-3339 UTC strings
    sort chronologically as plain strings, so a lexicographic ``>`` is a correct
    "is newer" test without parsing dates.
    """
    selected: list[DriveFile] = []
    for f in files:
        previous = manifest.get(f.id)
        if previous is None or f.modified_time > previous:
            selected.append(f)
    return selected


def merge_manifest(
    files: Iterable[DriveFile], manifest: dict[str, str]
) -> dict[str, str]:
    """Pure: return a new manifest with each file's modifiedTime recorded."""
    updated = dict(manifest)
    for f in files:
        updated[f.id] = f.modified_time
    return updated


def download(file_id: str, key: str, dest, timeout: int = 180):
    """Download a file's bytes to ``dest`` via alt=media (public file + key)."""
    url = DRIVE_API + "/" + urllib.parse.quote(file_id) + "?" + urllib.parse.urlencode(
        {"alt": "media", "key": key, "supportsAllDrives": "true"}
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as fh:
            fh.write(r.read())
    except urllib.error.HTTPError as e:
        raise DriveError(f"download {file_id}: {e.code}") from e
    return dest
