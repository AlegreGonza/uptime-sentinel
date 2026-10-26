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
    dynamodb = MagicMock()
    dynamodb.Table.side_effect = [targets, results]
    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = invoke_results
    with patch("monitor.boto3") as boto3:
        boto3.resource.return_value = dynamodb
        boto3.client.return_value = lambda_client
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


def test_rejected_result_is_not_saved():
    rejection = {"status_code": 400, "error": "Solo se permite HTTPS", "rechazado": True}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "http://a.com/"}]}],
        invoke_results=[_payload(rejection)],
    )
    assert summary == {"checked": 0, "rejected": 1, "failed": 0}
    results.put_item.assert_not_called()


def test_failed_invocation_is_counted_and_run_continues():
    record = {"url": "https://b.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 5}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}, {"url": "https://b.com/"}]}],
        invoke_results=[Exception("timeout"), _payload(record)],
    )
    assert summary == {"checked": 1, "rejected": 0, "failed": 1}
    results.put_item.assert_called_once_with(Item=record)


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


def test_rejected_url_never_reaches_put_item():
    rejection = {"status_code": 400, "error": "Destino no permitido (IP privada o interna)", "rechazado": True}
    _, results, _ = _run(
        pages=[{"Items": [{"url": "https://127.0.0.1/"}]}],
        invoke_results=[_payload(rejection)],
    )
    results.put_item.assert_not_called()


def test_empty_targets_do_nothing():
    summary, results, lambda_client = _run(
        pages=[{"Items": []}],
        invoke_results=[],
    )
    assert summary == {"checked": 0, "rejected": 0, "failed": 0}
    lambda_client.invoke.assert_not_called()
    results.put_item.assert_not_called()


def test_checker_crash_is_counted_as_failed_not_saved():
    crash = {
        "Payload": io.BytesIO(json.dumps({"errorMessage": "boom"}).encode()),
        "FunctionError": "Unhandled",
    }
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}]}],
        invoke_results=[crash],
    )
    assert summary == {"checked": 0, "rejected": 0, "failed": 1}
    results.put_item.assert_not_called()


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


def test_incomplete_checker_response_is_not_saved():
    incomplete = {"status_code": 200, "error": None}
    summary, results, _ = _run(
        pages=[{"Items": [{"url": "https://a.com/"}]}],
        invoke_results=[_payload(incomplete)],
    )
    assert summary == {"checked": 0, "rejected": 0, "failed": 1}
    results.put_item.assert_not_called()


def test_target_without_url_is_skipped():
    record = {"url": "https://b.com/", "status_code": 200, "error": None, "timestamp": "t", "elapsed_ms": 7}
    summary, results, lambda_client = _run(
        pages=[{"Items": [{"enabled": True}, {"url": "https://b.com/"}]}],
        invoke_results=[_payload(record)],
    )
    assert summary == {"checked": 1, "rejected": 0, "failed": 0}
    assert lambda_client.invoke.call_count == 1