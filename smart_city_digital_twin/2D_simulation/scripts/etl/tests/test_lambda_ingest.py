"""
Tests for the Lambda entrypoint — moto mocks Secrets Manager; run_ingest is
stubbed so the test stays hermetic (no Drive, no parser). We verify the handler
wires config + secret correctly and returns the summary.

    python -m pytest etl/tests/test_lambda_ingest.py   # or: python etl/tests/test_lambda_ingest.py
"""
from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REGION = "us-east-1"


@contextlib.contextmanager
def mocked_env(max_files_env=None):
    from moto import mock_aws
    import boto3

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ["AWS_DEFAULT_REGION"] = REGION
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name=REGION)
        arn = sm.create_secret(Name="drive-key", SecretString="FAKE-DRIVE-KEY")["ARN"]
        os.environ["DATA_BUCKET"] = "etl-bucket"
        os.environ["DRIVE_SECRET_ARN"] = arn
        os.environ["DRIVE_FOLDER_ID"] = "FOLDER123"
        os.environ.pop("MAX_FILES", None)
        if max_files_env is not None:
            os.environ["MAX_FILES"] = str(max_files_env)

        import importlib

        import etl.lambda_ingest as li
        li = importlib.reload(li)
        yield li


class _FakeSummary:
    def as_dict(self):
        return {"scanned": 1451, "selected": 2, "processed": 2, "rejected": 0, "errors": 0, "rejects": []}


def _capture_run_ingest(calls):
    def _fake(store, key, *, top_folder_id, max_files=None):
        calls.append({"bucket": store.bucket, "key": key, "folder": top_folder_id, "max_files": max_files})
        return _FakeSummary()
    return _fake


def test_handler_reads_secret_builds_store_and_returns_summary():
    with mocked_env() as li:
        calls = []
        li.run_ingest = _capture_run_ingest(calls)
        resp = li.lambda_handler({})
        assert resp["statusCode"] == 200
        assert resp["summary"]["processed"] == 2
        assert calls == [{"bucket": "etl-bucket", "key": "FAKE-DRIVE-KEY",
                          "folder": "FOLDER123", "max_files": None}]


def test_event_max_files_overrides():
    with mocked_env(max_files_env="10") as li:
        calls = []
        li.run_ingest = _capture_run_ingest(calls)
        li.lambda_handler({"max_files": 3})     # event beats env
        assert calls[0]["max_files"] == 3


def test_env_max_files_used_when_no_event_override():
    with mocked_env(max_files_env="5") as li:
        calls = []
        li.run_ingest = _capture_run_ingest(calls)
        li.lambda_handler({})
        assert calls[0]["max_files"] == 5


if __name__ == "__main__":
    try:
        import moto  # noqa: F401
        import boto3  # noqa: F401
    except ImportError:
        print('SKIP test_lambda_ingest: pip install boto3 "moto[secretsmanager]"')
        raise SystemExit(0)
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
