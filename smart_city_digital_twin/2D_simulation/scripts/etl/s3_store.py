"""
S3-backed ObjectStore for the ETL ingest core.

Implements the ``etl.ingest.ObjectStore`` contract (get_json / put_bytes /
put_json) on top of an S3 bucket, so ``run_ingest`` can persist raw workbooks,
processed JSON, rejects, and the manifest. boto3 is provided by the Lambda
runtime; a client can be injected for testing (moto).
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Codes S3 returns when an object (or bucket) isn't there — treated as "no value".
_MISSING = {"NoSuchKey", "404", "NoSuchBucket"}


class S3ObjectStore:
    def __init__(self, bucket: str, client: Any = None) -> None:
        self.bucket = bucket
        self._s3 = client or boto3.client("s3")

    def get_json(self, key: str) -> dict[str, Any] | None:
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise
        return json.loads(obj["Body"].read().decode("utf-8"))

    def put_bytes(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def put_json(self, key: str, obj: Any) -> None:
        self.put_bytes(
            key, json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
        )

    def list_keys(self, prefix: str) -> list[str]:
        """All object keys under ``prefix`` (paginated)."""
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                return keys
            token = resp.get("NextContinuationToken")
