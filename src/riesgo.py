import os

import boto3

RECENT_WINDOW = 4
MIN_BASELINE = 20


def _failed(check):
    return bool(check.get("error")) or check.get("status_code") != 200


def _failure_rate(checks):
    return sum(1 for check in checks if _failed(check)) / len(checks)


def risk_index(checks):
    checks = sorted(checks, key=lambda check: check["timestamp"])
    if len(checks) < RECENT_WINDOW + MIN_BASELINE:
        return None
    baseline = checks[:-RECENT_WINDOW]
    recent = checks[-RECENT_WINDOW:]
    baseline_rate = _failure_rate(baseline)
    recent_rate = _failure_rate(recent)
    return {
        "index": max(0, min(100, round(100 * (recent_rate - baseline_rate)))),
        "baseline_rate": round(baseline_rate, 2),
        "recent_rate": round(recent_rate, 2),
    }


def _urls(table):
    urls = []
    kwargs = {}
    while True:
        page = table.scan(**kwargs)
        urls.extend(item["url"] for item in page["Items"])
        if "LastEvaluatedKey" not in page:
            return urls
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _checks_for(table, url):
    items = []
    kwargs = {
        "KeyConditionExpression": "#u = :u",
        "ExpressionAttributeNames": {"#u": "url"},
        "ExpressionAttributeValues": {":u": url},
        "ScanIndexForward": True,
    }
    while True:
        page = table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    dynamodb = boto3.resource("dynamodb")
    targets = dynamodb.Table(os.environ["TARGETS_TABLE"])
    checks = dynamodb.Table(os.environ["RESULTS_TABLE"])
    for url in _urls(targets):
        result = risk_index(_checks_for(checks, url))
        if result is None:
            print(f"{url}: sin datos suficientes")
        else:
            print(
                f"{url}: riesgo {result['index']} "
                f"(base {result['baseline_rate']}, reciente {result['recent_rate']})"
            )


if __name__ == "__main__":
    main()