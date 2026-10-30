"""
traffic-ingest — WebSocket API Gateway handler.

Routes (selected by the "action" field of the message body):
  $connect     record the connectionId so we can broadcast to it later
  $disconnect  forget the connectionId
  sendmessage  the producer channel: fan the snapshot out to every connected
               browser, and async-invoke traffic-metrics to store it
  $default     anything else — ignored

Message shape the emitter sends on sendmessage:
  {"action": "sendmessage", "data": { ...emitter snapshot... }}

Uses only boto3, which is present in the Lambda runtime.
"""
import json
import os
import time

import boto3

CONNECTIONS_TABLE = os.environ["CONNECTIONS_TABLE"]
METRICS_FUNCTION = os.environ["METRICS_FUNCTION"]
CONNECTION_TTL_SECONDS = 2 * 60 * 60  # rows self-expire after 2h (DynamoDB TTL)

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(CONNECTIONS_TABLE)
_lambda = boto3.client("lambda")


def lambda_handler(event, context):
    route = event.get("requestContext", {}).get("routeKey")
    connection_id = event.get("requestContext", {}).get("connectionId")

    if route == "$connect":
        _table.put_item(Item={
            "connectionId": connection_id,
            "expireAt": int(time.time()) + CONNECTION_TTL_SECONDS,
        })
        return {"statusCode": 200}

    if route == "$disconnect":
        _table.delete_item(Key={"connectionId": connection_id})
        return {"statusCode": 200}

    if route == "sendmessage":
        return _handle_sendmessage(event, connection_id)

    return {"statusCode": 200}  # $default


def _handle_sendmessage(event, sender_id):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid JSON"}

    snapshot = body.get("data")
    if snapshot is None:
        return {"statusCode": 400, "body": "missing 'data'"}

    # 1) Hand the snapshot to traffic-metrics (fire-and-forget).
    _lambda.invoke(
        FunctionName=METRICS_FUNCTION,
        InvocationType="Event",  # async — don't block the broadcast
        Payload=json.dumps(snapshot).encode("utf-8"),
    )

    # 2) Fan the snapshot out to every connected browser.
    _broadcast(event, json.dumps(snapshot), skip=sender_id)
    return {"statusCode": 200}


def _broadcast(event, message, skip=None):
    rc = event["requestContext"]
    endpoint = f"https://{rc['domainName']}/{rc['stage']}"
    api = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)

    scan = _table.scan(ProjectionExpression="connectionId")
    for item in scan.get("Items", []):
        cid = item["connectionId"]
        if cid == skip:
            continue
        try:
            api.post_to_connection(ConnectionId=cid, Data=message.encode("utf-8"))
        except api.exceptions.GoneException:
            # Client vanished without a $disconnect — clean up the stale row.
            _table.delete_item(Key={"connectionId": cid})
        except Exception as exc:  # noqa: BLE001 — one bad client shouldn't fail the tick
            print(f"post_to_connection failed for {cid}: {exc}")
