"""
AWS Lambda entrypoint for the scheduled ETL ingest (etl-traffic-counts).

Thin glue: read config from the environment, fetch the Drive API key from Secrets
Manager, build an S3-backed store, and run the AWS-free ``run_ingest`` core. All
the real logic (walk / select / download / clean / store / manifest) lives in
``etl.ingest`` and is unit-tested without AWS.

Environment (set by Terraform in Step 5):
    DATA_BUCKET       S3 bucket for raw/processed/rejected/manifest objects.
    DRIVE_SECRET_ARN  Secrets Manager secret whose value is the Drive API key.
    DRIVE_FOLDER_ID   Top Drive folder id (defaults to the CCC counts folder).
    MAX_FILES         Optional cap on files handled per run (first backfill).

Importing this module does NOT import the workbook parser (that happens lazily
inside clean_workbook at parse time), so it stays light for tests.
"""
from __future__ import annotations

import os
from typing import Any

import boto3

from etl.ingest import rebuild_name_index, run_ingest
from etl.s3_store import S3ObjectStore

DEFAULT_FOLDER_ID = "1oP5gcuKR1bHB9Xn2B4ILZZd_LhpMggxU"


def _get_secret(arn: str, client: Any = None) -> str:
    sm = client or boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=arn)["SecretString"]


def _resolve_max_files(event: Any) -> int | None:
    # An event override lets you trigger a small bounded run by hand.
    if isinstance(event, dict) and event.get("max_files") is not None:
        return int(event["max_files"])
    env = os.environ.get("MAX_FILES", "").strip()
    return int(env) if env else None


def lambda_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    bucket = os.environ["DATA_BUCKET"]

    # One-off: rebuild the id -> name index from already-processed objects (no
    # Drive access / re-parse). Trigger with {"rebuild_names": true}.
    if isinstance(event, dict) and event.get("rebuild_names"):
        count = rebuild_name_index(S3ObjectStore(bucket))
        return {"statusCode": 200, "rebuiltNames": count}

    secret_arn = os.environ["DRIVE_SECRET_ARN"]
    folder_id = os.environ.get("DRIVE_FOLDER_ID") or DEFAULT_FOLDER_ID

    key = _get_secret(secret_arn)
    store = S3ObjectStore(bucket)
    summary = run_ingest(
        store, key, top_folder_id=folder_id, max_files=_resolve_max_files(event)
    )
    return {"statusCode": 200, "summary": summary.as_dict()}
