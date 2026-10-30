"""
AWS Lambda entrypoint for the query API (query-traffic-counts).

Fronted by API Gateway (HTTP API):  GET /traffic-counts?intersection=…&date=…

Thin glue: pull the query params off the event, call the AWS-free
``query_counts`` core against an S3-backed store, return a JSON HTTP response with
open CORS (tighten allow-origin to the dashboard domain later).
"""
from __future__ import annotations

import json
import os
from typing import Any

from etl.query import query_counts
from etl.s3_store import S3ObjectStore

_CORS = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,OPTIONS",
}


def _params(event: Any) -> dict[str, str]:
    if not isinstance(event, dict):
        return {}
    return event.get("queryStringParameters") or {}


def lambda_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    params = _params(event)
    intersection = (params.get("intersection") or "").strip() or None
    date = (params.get("date") or "").strip() or None

    store = S3ObjectStore(os.environ["DATA_BUCKET"])
    result = query_counts(store, intersection=intersection, date=date)

    status = 200
    if intersection and date and not result.get("found", True):
        status = 404
    return {
        "statusCode": status,
        "headers": _CORS,
        "body": json.dumps(result, default=str),
    }
