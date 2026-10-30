"""
Tests for the query Lambda — moto mocks S3; real query_counts + S3ObjectStore.

    python -m pytest etl/tests/test_lambda_query.py   # or: python etl/tests/test_lambda_query.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BUCKET = "etl-bucket"
REGION = "us-east-1"
P = "processed-traffic-data"


@contextlib.contextmanager
def mocked():
    from moto import mock_aws
    import boto3

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ["AWS_DEFAULT_REGION"] = REGION
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{P}/I0007/2016-08-24.json",
            Body=json.dumps({"rowCount": 2, "rows": [{"m": 1}, {"m": 2}]}).encode(),
        )
        os.environ["DATA_BUCKET"] = BUCKET
        import importlib

        import etl.lambda_query as lq
        yield importlib.reload(lq)


def test_hit_returns_rows_200_and_cors():
    with mocked() as lq:
        resp = lq.lambda_handler({"queryStringParameters": {"intersection": "I0007", "date": "2016-08-24"}})
        assert resp["statusCode"] == 200
        assert resp["headers"]["access-control-allow-origin"] == "*"
        body = json.loads(resp["body"])
        assert body["found"] is True and body["rowCount"] == 2


def test_missing_survey_returns_404():
    with mocked() as lq:
        resp = lq.lambda_handler({"queryStringParameters": {"intersection": "I0007", "date": "1900-01-01"}})
        assert resp["statusCode"] == 404
        assert json.loads(resp["body"])["found"] is False


def test_intersection_only_lists_dates():
    with mocked() as lq:
        resp = lq.lambda_handler({"queryStringParameters": {"intersection": "I0007"}})
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["dates"] == ["2016-08-24"]


def test_no_params_is_discovery():
    with mocked() as lq:
        resp = lq.lambda_handler({})
        assert json.loads(resp["body"])["intersections"] == [{"id": "I0007", "name": ""}]


if __name__ == "__main__":
    try:
        import moto  # noqa: F401
        import boto3  # noqa: F401
    except ImportError:
        print('SKIP test_lambda_query: pip install boto3 "moto[s3]"')
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
