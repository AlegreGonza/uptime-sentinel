import io
import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("CHECKER_FUNCTION", "uptime-checker-dev")
os.environ.setdefault("TARGETS_TABLE", "uptime-targets-dev")
os.environ.setdefault("RESULTS_TABLE", "uptime-checks-dev")

import monitor


def _payload(data):
    return {"Payload": io.BytesIO(json.dumps(data).encode())}


def _run(pages, invoke_results, put_results=None):
    targets = MagicMock()
    targets.scan.side_effect = pages
    results = MagicMock()
    if put_results is not None:
        results.put_item.side_effect = put_results
    lambda_client = MagicMock()
    # Checker calls run in parallel, so answer by URL instead of by call order.
    urls = [item["url"] for page in pages for item in page.get("Items", []) if item.get("url")]
    by_url = dict(zip(urls, invoke_results))

    def invoke(FunctionName, Payload):
        value = by_url[json.loads(Payload)["url"]]
        if isinstance(value, Exception):
            raise value
        return value

    lambda_client.invoke.side_effect = invoke
    with patch.object(monitor, "TARGETS_DB", targets), \
            patch.object(monitor, "CHECKS_DB", results), \
            patch.object(monitor, "LAMBDA", lambda_client), \
            patch.object(monitor, "SNS", MagicMock()):
        summary = monitor.handler({}, None)
    return summary, results, lambda_client


def test_saves_accepted_result():
    record = {"url": "https://a.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 5}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}]}],
        invoke_results=[_payload(record)],
    )
    assert summary == {"checked": 1, "rejected": 0, "failed": 0}
    results.put_item.assert_called_once_with(Item=record)


def test_rejected_result_is_recorded_as_failure():
    rejection = {"status_code": 400, "error": "Only HTTPS is allowed", "rejected": True}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "http://a.com/"}]}],
        invoke_results=[_payload(rejection)],
    )
    assert summary == {"checked": 0, "rejected": 1, "failed": 0}
    results.put_item.assert_called_once()
    failure = results.put_item.call_args.kwargs["Item"]
    assert failure["url"] == "http://a.com/"
    assert failure["status_code"] == 0
    assert failure["error"] == "URL rejected by the checker"


def test_failed_invocation_is_counted_and_run_continues():
    record = {"url": "https://b.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 5}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}, {"url": "https://b.com/"}]}],
        invoke_results=[Exception("timeout"), _payload(record)],
    )
    assert summary == {"checked": 1, "rejected": 0, "failed": 1}
    assert results.put_item.call_count == 2
    results.put_item.assert_any_call(Item=record)
    failure = results.put_item.call_args_list[0].kwargs["Item"]
    assert failure["url"] == "https://a.com/"
    assert failure["status_code"] == 0
    assert failure["error"].startswith("The checker did not respond")


def test_scan_follows_all_pages():
    first = {"url": "https://a.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 5}
    second = {"url": "https://b.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 7}
    summary, results, lambda_client = _run(
        pages=[
            {"Items": [{"url": "https://a.com/"}], "LastEvaluatedKey": {"url": "https://a.com/"}},
            {"Items": [{"url": "https://b.com/"}]},
        ],
        invoke_results=[_payload(first), _payload(second)],
    )
    assert summary == {"checked": 2, "rejected": 0, "failed": 0}
    assert lambda_client.invoke.call_count == 2
    assert results.put_item.call_count == 2


def test_rejected_url_is_recorded_as_failure():
    rejection = {"status_code": 400, "error": "Destination not allowed (private or internal IP)", "rejected": True}
    _, results, _ = _run(
        pages=[{"Items": [{"url": "https://127.0.0.1/"}]}],
        invoke_results=[_payload(rejection)],
    )
    results.put_item.assert_called_once()
    assert results.put_item.call_args.kwargs["Item"]["status_code"] == 0


def test_empty_targets_do_nothing():
    summary, results, lambda_client = _run(
        pages=[{"Items": []}],
        invoke_results=[],
    )
    assert summary == {"checked": 0, "rejected": 0, "failed": 0}
    lambda_client.invoke.assert_not_called()
    results.put_item.assert_not_called()


def test_checker_crash_is_recorded_as_failure():
    crash = {
        "Payload": io.BytesIO(json.dumps({"errorMessage": "boom"}).encode()),
        "FunctionError": "Unhandled",
    }
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}]}],
        invoke_results=[crash],
    )
    assert summary == {"checked": 0, "rejected": 0, "failed": 1}
    results.put_item.assert_called_once()
    failure = results.put_item.call_args.kwargs["Item"]
    assert failure["url"] == "https://a.com/"
    assert failure["status_code"] == 0


def test_put_item_failure_is_counted_and_run_continues():
    record_a = {"url": "https://a.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 5}
    record_b = {"url": "https://b.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 7}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}, {"url": "https://b.com/"}]}],
        invoke_results=[_payload(record_a), _payload(record_b)],
        put_results=[Exception("ProvisionedThroughputExceeded"), None],
    )
    assert summary == {"checked": 1, "rejected": 0, "failed": 1}
    assert results.put_item.call_count == 2


def test_incomplete_checker_response_is_recorded_as_failure():
    incomplete = {"status_code": 200, "error": None}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}]}],
        invoke_results=[_payload(incomplete)],
    )
    assert summary == {"checked": 0, "rejected": 0, "failed": 1}
    results.put_item.assert_called_once()
    saved = results.put_item.call_args.kwargs["Item"]
    assert saved["status_code"] == 0
    assert saved["error"] == "The checker returned an invalid response"


def test_target_without_url_is_skipped():
    record = {"url": "https://b.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 7}
    summary, _, lambda_client = _run(
        pages=[{"Items": [{"enabled": True}, {"url": "https://b.com/"}]}],
        invoke_results=[_payload(record)],
    )
    assert summary == {"checked": 1, "rejected": 0, "failed": 0}
    assert lambda_client.invoke.call_count == 1