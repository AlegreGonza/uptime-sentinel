import json
import logging
import os
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import takewhile

import boto3
from boto3.dynamodb.conditions import Attr, Key

from risk import RISK_WINDOW, is_failure, risk_index

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKER_FUNCTION = os.environ["CHECKER_FUNCTION"]
TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
ALERTS_TOPIC = os.environ["ALERTS_TOPIC"]

DYNAMODB = boto3.resource("dynamodb")
LAMBDA = boto3.client("lambda")
SNS = boto3.client("sns")
TARGETS_DB = DYNAMODB.Table(TARGETS_TABLE)
CHECKS_DB = DYNAMODB.Table(RESULTS_TABLE)
REQUIRED_FIELDS = ("url", "timestamp", "status_code", "error", "elapsed_ms")
DOWN_THRESHOLD = 3
HISTORY_SIZE = DOWN_THRESHOLD + 1
RISK_THRESHOLD = 50
RISK_HISTORY_SIZE = RISK_WINDOW + 1  # one extra check for the previous index
MAX_WORKERS = 8  # concurrent checker invocations
REASON_NO_RESPONSE = "The checker did not respond"
REASON_REJECTED = "URL rejected by the checker"
REASON_INVALID_RESPONSE = "The checker returned an invalid response"


def _paginate(operation: Callable, **kwargs) -> Iterator[dict]:
    """Yields every page of a DynamoDB scan() or query()."""
    while True:
        page = operation(**kwargs)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _run_step(step: str, url: str, func: Callable, *args):
    """Runs one check step; if it fails, logs it and returns None."""
    try:
        return func(*args)
    except Exception:
        logger.exception("Failed %s for %s", step, url)
        return None


def _publish(sns_client, subject: str, message: str) -> None:
    sns_client.publish(TopicArn=ALERTS_TOPIC, Subject=subject, Message=message)


def _enabled_targets(table):
    for item in _paginate(table.scan, FilterExpression=Attr("enabled").eq(True)):
        if item.get("url"):
            yield item
        else:
            logger.warning("Target without url: %s", item)


def _invoke_checker(lambda_client, url):
    response = lambda_client.invoke(
        FunctionName=CHECKER_FUNCTION,
        Payload=json.dumps({"url": url}),
    )
    body = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise RuntimeError(f"The checker failed: {body}")
    return body


def _invoke_safely(lambda_client, url):
    """Returns (result, None) or (None, reason). Safe to run in a worker thread."""
    try:
        return _invoke_checker(lambda_client, url), None
    except Exception:
        logger.exception("The checker did not respond for %s", url)
        return None, REASON_NO_RESPONSE


def _record_failure(checks_db, url, reason):
    """Stores a failed check so the history reflects the outage."""
    checks_db.put_item(Item={
        "url": url,
        "timestamp": datetime.now(UTC).isoformat(),
        "status_code": 0,
        "error": reason,
        "elapsed_ms": 0,
    })


def _bad_reason(result):
    if result.get("rejected"):
        return REASON_REJECTED
    if [f for f in REQUIRED_FIELDS if f not in result]:
        return REASON_INVALID_RESPONSE
    return None


def _store_check(checks_db, url, result, error):
    """Saves a valid check, or records the failure. Runs on the main thread only."""
    reason = error or _bad_reason(result)
    if reason is None:
        checks_db.put_item(Item=result)
        return "checked"
    logger.warning("Check failed for %s: %s", url, reason)
    _record_failure(checks_db, url, reason)
    return "rejected" if reason == REASON_REJECTED else "failed"


def _recent_checks(checks_db, url, count):
    """Returns the most recent checks first.

    A single query with Limit: without filters, DynamoDB returns up to `count`
    items, so pagination is not needed. Paginating here loaded the whole history.
    """
    response = checks_db.query(
        KeyConditionExpression=Key("url").eq(url),
        ScanIndexForward=False,
        Limit=count,
        ConsistentRead=True,
    )
    return list(response.get("Items", []))[:count]


def _alert_type(failures):
    if len(failures) == HISTORY_SIZE and not failures[0] and all(failures[1:]):
        return "RECOVERED"
    streak = len(list(takewhile(bool, failures)))
    return {1: "PRE-ALERT", DOWN_THRESHOLD: "DOWN"}.get(streak)


def _alert_message(kind, url):
    messages = {
        "PRE-ALERT": (
            f"{url} failed the last check. "
            f"If it fails {DOWN_THRESHOLD} times in a row, a down alert is sent."
        ),
        "DOWN": f"{url} failed {DOWN_THRESHOLD} checks in a row.",
        "RECOVERED": f"{url} is responding again.",
    }
    return messages[kind]


def _evaluate_alerts(sns_client, history, url, expected_status=None):
    checks = history[:HISTORY_SIZE]
    kind = _alert_type([is_failure(c, expected_status) for c in checks])
    if kind:
        _publish(sns_client, f"{kind} uptime", _alert_message(kind, url))


def _evaluate_risk(sns_client, history, url, expected_status=None):
    current = risk_index(history[:RISK_WINDOW], expected_status)
    previous = risk_index(history[1:RISK_WINDOW + 1], expected_status)
    crossed_threshold = (
        current is not None
        and previous is not None
        and previous["index"] < RISK_THRESHOLD <= current["index"]
    )
    if crossed_threshold:
        _publish(
            sns_client,
            "RISK uptime",
            f"{url} exceeded the risk index {RISK_THRESHOLD}: it is now {current['index']}.",
        )


def _evaluate(sns_client, checks_db, url, expected_status=None):
    history = _recent_checks(checks_db, url, RISK_HISTORY_SIZE)
    _run_step("alert evaluation", url, _evaluate_alerts, sns_client, history, url, expected_status)
    _run_step("risk evaluation", url, _evaluate_risk, sns_client, history, url, expected_status)


def handler(event, context):
    targets = list(_enabled_targets(TARGETS_DB))
    urls = [target["url"] for target in targets]
    # Only the checker calls run in parallel; DynamoDB and SNS stay on this thread.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        responses = list(pool.map(lambda url: _invoke_safely(LAMBDA, url), urls))
    counts = Counter()
    for target, (result, error) in zip(targets, responses):
        url = target["url"]
        state = _run_step("the check", url, _store_check, CHECKS_DB, url, result, error)
        if state is None:
            counts["failed"] += 1
            continue
        counts[state] += 1
        _run_step("evaluation", url, _evaluate, SNS, CHECKS_DB, url, target.get("expected_status"))
    return {key: counts[key] for key in ("checked", "rejected", "failed")}
