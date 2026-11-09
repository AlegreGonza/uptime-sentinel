import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote, urlsplit

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKER_FUNCTION = os.environ["CHECKER_FUNCTION"]
TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
MAX_RESULTS = 100
PATH_SAFE = "/?=&%:@!$'()*+,;~-._"  # must match checker.validate_url
HEADERS = {"Content-Type": "application/json"}

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
targets_db = dynamodb.Table(TARGETS_TABLE)
checks_db = dynamodb.Table(RESULTS_TABLE)


class ClientRequestError(Exception):
    def __init__(self, status, body):
        super().__init__(str(body))
        self.status = status
        self.body = body


def _to_json(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Type is not serializable: {type(value)}")


def _response(status, body):
    return {
        "statusCode": status,
        "headers": HEADERS,
        "body": json.dumps(body, ensure_ascii=False, default=_to_json),
    }


MAX_BODY_CHARS = 4096


def _read_url(event):
    body = event.get("body") or "{}"
    if len(body) > MAX_BODY_CHARS:
        raise ClientRequestError(400, {"error": "Request body is too large"})
    try:
        url = json.loads(body)["url"].strip()
        if not url:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ClientRequestError(400, {"error": "URL is required"})
    return url


def _check_response(response, body):
    if response.get("FunctionError"):
        logger.error("The checker failed: %s", body)
        if "timed out" in str(body).lower():
            raise ClientRequestError(504, {"error": "The site took too long to respond"})
        raise RuntimeError("The checker failed to check the URL")
    if not isinstance(body, dict):
        raise RuntimeError("The checker returned an invalid response")  # noqa: TRY004 caught as RuntimeError below
    if body.get("rejected"):
        raise ClientRequestError(400, body)
    return body


def _invoke_checker(url):
    try:
        response = lambda_client.invoke(
            FunctionName=CHECKER_FUNCTION,
            Payload=json.dumps({"url": url}),
        )
        body = json.loads(response["Payload"].read())
    except ClientError:
        logger.exception("Could not invoke the checker")
        raise RuntimeError("Could not reach the checker")
    return _check_response(response, body)


def _probe(event):
    return _response(200, _invoke_checker(_read_url(event)))


def _expected_status(event):
    """Optional status code that counts as OK for this target (None = any 2xx or 3xx)."""
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return None  # _read_url reports the malformed body
    value = body.get("expected_status") if isinstance(body, dict) else None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        raise ClientRequestError(400, {"error": "expected_status must be an integer from 100 to 599"})
    return value


def _save(event):
    expected = _expected_status(event)
    result = _invoke_checker(_read_url(event))
    target = {
        "url": result["url"],
        "created_at": datetime.now(UTC).isoformat(),
        "enabled": True,
    }
    if expected is not None:
        target["expected_status"] = expected
    try:
        targets_db.put_item(
            Item=target,
            ConditionExpression="attribute_not_exists(#u)",
            ExpressionAttributeNames={"#u": "url"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise ClientRequestError(409, {"error": "URL is already saved", "url": target["url"]})
    return _response(201, target)


def _list(event):
    targets = []
    kwargs = {}
    while True:
        page = targets_db.scan(**kwargs)
        targets.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return _response(200, {"targets": targets})


def _normalizar_url(url):
    """Same shape the checker stores: https://host/path?query (see checker.validate_url)."""
    parts = urlsplit(url)
    if not parts.hostname:
        return url
    host = parts.hostname.rstrip(".")
    raw = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return f"https://{host}{quote(raw, safe=PATH_SAFE)}"


def _results(event):
    url = ((event.get("queryStringParameters") or {}).get("url") or "").strip()
    if not url:
        raise ClientRequestError(400, {"error": "url query parameter is required"})
    url = _normalizar_url(url)
    response = checks_db.query(
        KeyConditionExpression=Key("url").eq(url),
        ScanIndexForward=False,
        Limit=MAX_RESULTS,
    )
    return _response(200, {"results": response.get("Items", [])})


ROUTES = {
    ("POST", "/probe"): _probe,
    ("POST", "/targets"): _save,
    ("GET", "/targets"): _list,
    ("GET", "/results"): _results,
}


def handler(event, context):
    func = ROUTES.get((event.get("httpMethod"), event.get("path")))
    if func is None:
        return _response(404, {"error": "Route not found"})
    try:
        return func(event)
    except ClientRequestError as e:
        return _response(e.status, e.body)
    except RuntimeError as e:
        return _response(502, {"error": str(e)})
    except Exception:
        logger.exception("Error inesperado en %s %s", event.get("httpMethod"), event.get("path"))
        return _response(500, {"error": "Internal server error"})