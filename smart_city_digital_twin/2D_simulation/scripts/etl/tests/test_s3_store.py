"""
Tests for S3ObjectStore — DynamoDB-free, uses moto to mock S3.

    pip install boto3 "moto[s3]"
    python -m pytest etl/tests/test_s3_store.py   # or: python etl/tests/test_s3_store.py
"""
from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BUCKET = "test-etl-bucket"
REGION = "us-east-1"


@contextlib.contextmanager
def mocked_store():
    from moto import mock_aws
    import boto3

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ["AWS_DEFAULT_REGION"] = REGION
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        from etl.s3_store import S3ObjectStore

        yield S3ObjectStore(BUCKET)


def test_get_json_missing_returns_none():
    with mocked_store() as store:
        assert store.get_json("manifest/drive_manifest.json") is None


def test_put_json_get_json_roundtrip():
    with mocked_store() as store:
        store.put_json("manifest/drive_manifest.json", {"a": "2026-01-01T00:00:00Z"})
        assert store.get_json("manifest/drive_manifest.json") == {"a": "2026-01-01T00:00:00Z"}


def test_put_bytes_is_retrievable_raw():
    import boto3

    with mocked_store() as store:
        store.put_bytes("raw-traffic-data/2026 Intersection/x.xlsx", b"PK\x03\x04data")
        body = boto3.client("s3", region_name=REGION).get_object(
            Bucket=BUCKET, Key="raw-traffic-data/2026 Intersection/x.xlsx"
        )["Body"].read()
        assert body == b"PK\x03\x04data"


if __name__ == "__main__":
    try:
        import moto  # noqa: F401
        import boto3  # noqa: F401
    except ImportError:
        print('SKIP test_s3_store: pip install boto3 "moto[s3]"')
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
