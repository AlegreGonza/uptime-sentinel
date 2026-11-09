import os

import boto3
from boto3.dynamodb.conditions import Attr

RECENT_WINDOW = 4
MIN_BASELINE = 20
RISK_WINDOW = 60


def is_failure(check, expected_status=None):
    """A check fails on an error or a missing status.

    Without expected_status, any 2xx or 3xx is OK. With it, only that exact status is OK.
    """
    status = check.get("status_code")
    if check.get("error") or status is None:
        return True
    if expected_status is None:
        return not (200 <= status < 400)
    return status != expected_status


def _failure_rate(checks, expected_status=None):
    return sum(1 for check in checks if is_failure(check, expected_status)) / len(checks)


def risk_index(checks, expected_status=None):
    checks = sorted(checks, key=lambda check: check["timestamp"])
    if len(checks) < RECENT_WINDOW + MIN_BASELINE:
        return None
    baseline = checks[:-RECENT_WINDOW]
    recent = checks[-RECENT_WINDOW:]
    baseline_rate = _failure_rate(baseline, expected_status)
    recent_rate = _failure_rate(recent, expected_status)
    return {
        "index": max(0, min(100, round(100 * (recent_rate - baseline_rate)))),
        "baseline_rate": round(baseline_rate, 2),
        "recent_rate": round(recent_rate, 2),
    }


def _urls(table):
    urls = []
    kwargs = {"FilterExpression": Attr("enabled").eq(True)}
    while True:
        page = table.scan(**kwargs)
        urls.extend(item["url"] for item in page["Items"])
        if "LastEvaluatedKey" not in page:
            return urls
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _checks_for(table, url, limit=RISK_WINDOW):
    """Returns the last `limit` checks, oldest first."""
    response = table.query(
        KeyConditionExpression="#u = :u",
        ExpressionAttributeNames={"#u": "url"},
        ExpressionAttributeValues={":u": url},
        ScanIndexForward=False,
        Limit=limit,
    )
    return list(reversed(response.get("Items", [])))


def main():
    dynamodb = boto3.resource("dynamodb")
    targets = dynamodb.Table(os.environ["TARGETS_TABLE"])
    checks = dynamodb.Table(os.environ["RESULTS_TABLE"])
    for url in _urls(targets):
        result = risk_index(_checks_for(checks, url))
        if result is None:
            print(f"{url}: not enough data")
        else:
            print(
                f"{url}: risk {result['index']} "
                f"(baseline {result['baseline_rate']}, recent {result['recent_rate']})"
            )


if __name__ == "__main__":
    main()