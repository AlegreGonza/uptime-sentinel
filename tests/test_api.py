import io
import json
import os
from decimal import Decimal
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CHECKER_FUNCTION", "uptime-checker-dev")
os.environ.setdefault("TARGETS_TABLE", "uptime-targets-dev")
os.environ.setdefault("RESULTS_TABLE", "uptime-checks-dev")

import api

ACEPTADO = {
    "url": "https://example.com/",
    "timestamp": "2026-01-01T00:00:00+00:00",
    "status_code": 200,
    "error": None,
    "elapsed_ms": 50,
}
RECHAZADO = {"status_code": 400, "error": "Destination not allowed (private or internal IP)", "rejected": True}


def _evento(route, method, body=None, query=None):
    return {
        "httpMethod": method,
        "path": route,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
    }


def _evento_crudo(route, method, body_crudo):
    return {"httpMethod": method, "path": route, "body": body_crudo}


def _checker(body, function_error=None):
    response = {"Payload": io.BytesIO(json.dumps(body).encode())}
    if function_error:
        response["FunctionError"] = function_error
    return response


def _cliente_error(code):
    return ClientError({"Error": {"Code": code, "Message": "simulado"}}, "Operacion")


@pytest.fixture
def mocks():
    with patch.object(api, "lambda_client") as lam, \
         patch.object(api, "targets_db") as targets, \
         patch.object(api, "checks_db") as results:
        yield lam, targets, results


def test_probe_returns_checker_result(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    r = api.handler(_evento("/probe", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == ACEPTADO


def test_probe_invokes_checker_with_expected_function_and_payload(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/probe", "POST", {"url": "https://example.com/"}), None)
    kwargs = lam.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == api.CHECKER_FUNCTION
    assert json.loads(kwargs["Payload"]) == {"url": "https://example.com/"}


def test_probe_never_writes_to_tables(mocks):
    lam, targets, results = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/probe", "POST", {"url": "https://example.com/"}), None)
    targets.put_item.assert_not_called()
    results.put_item.assert_not_called()


def test_probe_rejected_url_returns_400(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(RECHAZADO)
    r = api.handler(_evento("/probe", "POST", {"url": "http://10.0.0.1"}), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"]) == RECHAZADO


@pytest.mark.parametrize(
    "body",
    [{}, {"url": ""}, {"url": "   "}, {"url": 123}, {"url": None}, {"url": ["https://example.com/"]}, [], "https://example.com/", None],
    ids=["no_key", "empty", "only_spaces", "number", "null", "list", "root_list", "root_string", "no_body"],
)
def test_invalid_body_returns_400_without_calling_checker(mocks, body):
    lam, _, _ = mocks
    r = api.handler(_evento("/probe", "POST", body), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"]) == {"error": "URL is required"}
    lam.invoke.assert_not_called()


def test_invalid_json_body_returns_400(mocks):
    lam, _, _ = mocks
    r = api.handler(_evento_crudo("/probe", "POST", "{no json"), None)
    assert r["statusCode"] == 400
    lam.invoke.assert_not_called()


def test_url_is_stripped_before_calling_checker(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/probe", "POST", {"url": "  https://example.com/  "}), None)
    assert json.loads(lam.invoke.call_args.kwargs["Payload"]) == {"url": "https://example.com/"}


def test_save_saves_accepted_url_as_enabled(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    r = api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 201
    item = targets.put_item.call_args.kwargs["Item"]
    assert item["url"] == "https://example.com/"
    assert item["enabled"] is True
    assert "created_at" in item


def test_save_uses_condition_to_avoid_overwrite(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    kwargs = targets.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "attribute_not_exists(#u)"
    assert kwargs["ExpressionAttributeNames"] == {"#u": "url"}


def test_save_does_not_save_rejected_url(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker(RECHAZADO)
    r = api.handler(_evento("/targets", "POST", {"url": "http://10.0.0.1"}), None)
    assert r["statusCode"] == 400
    targets.put_item.assert_not_called()


def test_save_duplicate_url_returns_409_with_url(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    targets.put_item.side_effect = _cliente_error("ConditionalCheckFailedException")
    r = api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 409
    assert json.loads(r["body"])["url"] == "https://example.com/"


def test_save_other_dynamodb_error_returns_500(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    targets.put_item.side_effect = _cliente_error("ProvisionedThroughputExceededException")
    r = api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500


def test_save_checker_response_without_url_returns_500_and_writes_nothing(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker({"status_code": 200})
    r = api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500
    targets.put_item.assert_not_called()


def test_save_checker_non_json_payload_returns_500_and_writes_nothing(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = {"Payload": io.BytesIO(b"no es json")}
    r = api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500
    targets.put_item.assert_not_called()


def test_checker_function_error_returns_502(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker({"errorMessage": "boom"}, function_error="Unhandled")
    r = api.handler(_evento("/probe", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 502


def test_checker_crash_does_not_write_to_targets(mocks):
    lam, targets, _ = mocks
    lam.invoke.return_value = _checker({"errorMessage": "boom"}, function_error="Unhandled")
    api.handler(_evento("/targets", "POST", {"url": "https://example.com/"}), None)
    targets.put_item.assert_not_called()


def test_checker_crash_details_are_not_returned_to_client(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker({"errorMessage": "secreto-interno"}, function_error="Unhandled")
    r = api.handler(_evento("/probe", "POST", {"url": "https://example.com/"}), None)
    assert "secreto-interno" not in r["body"]


def test_lambda_invoke_failure_returns_502(mocks):
    lam, _, _ = mocks
    lam.invoke.side_effect = _cliente_error("TooManyRequestsException")
    r = api.handler(_evento("/probe", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 502


def test_body_too_large_is_rejected_before_parsing():
    with pytest.raises(api.ClientRequestError) as error:
        api._read_url({"body": '{"url":"' + "a" * 5000 + '"}'})
    assert error.value.status == 400


def test_checker_lambda_timeout_returns_504():
    with pytest.raises(api.ClientRequestError) as error:
        api._check_response(
            {"FunctionError": "Unhandled"},
            {"errorMessage": "2026-11-07T21:00:00Z Task timed out after 25.00 seconds"},
        )
    assert error.value.status == 504


def test_checker_non_object_response_gives_controlled_error():
    with pytest.raises(RuntimeError):
        api._check_response({}, [1, 2])


def test_list_follows_all_pages(mocks):
    _, targets, _ = mocks
    targets.scan.side_effect = [
        {"Items": [{"url": "https://a.com/"}], "LastEvaluatedKey": {"url": "https://a.com/"}},
        {"Items": [{"url": "https://b.com/"}]},
    ]
    r = api.handler(_evento("/targets", "GET"), None)
    urls = [o["url"] for o in json.loads(r["body"])["targets"]]
    assert urls == ["https://a.com/", "https://b.com/"]
    assert targets.scan.call_count == 2


def test_list_empty_table_returns_empty_list(mocks):
    _, targets, _ = mocks
    targets.scan.return_value = {"Items": []}
    r = api.handler(_evento("/targets", "GET"), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == {"targets": []}


def test_list_dynamodb_error_returns_500(mocks):
    _, targets, _ = mocks
    targets.scan.side_effect = _cliente_error("ProvisionedThroughputExceededException")
    r = api.handler(_evento("/targets", "GET"), None)
    assert r["statusCode"] == 500


def test_results_queries_newest_first_with_limit(mocks):
    _, _, results = mocks
    results.query.return_value = {"Items": [{"url": "https://example.com/"}]}
    r = api.handler(_evento("/results", "GET", query={"url": "https://example.com/"}), None)
    assert r["statusCode"] == 200
    kwargs = results.query.call_args.kwargs
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == api.MAX_RESULTS


@pytest.mark.parametrize("entrada,esperada", [
    ("https://Example.com", "https://example.com/"),
    ("https://example.com:443/a?x=1", "https://example.com/a?x=1"),
])
def test_normalizar_url_coincide_con_lo_que_guarda_el_checker(entrada, esperada):
    assert api._normalizar_url(entrada) == esperada


@pytest.mark.parametrize("query", [{}, {"url": ""}, {"url": "   "}, None], ids=["no_key", "empty", "only_spaces", "no_query"])
def test_results_without_url_returns_400_without_querying(mocks, query):
    _, _, results = mocks
    r = api.handler(_evento("/results", "GET", query=query), None)
    assert r["statusCode"] == 400
    results.query.assert_not_called()


def test_results_dynamodb_error_returns_500(mocks):
    _, _, results = mocks
    results.query.side_effect = _cliente_error("ResourceNotFoundException")
    r = api.handler(_evento("/results", "GET", query={"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500


def test_results_con_decimal_de_dynamodb_no_falla(mocks):
    _, _, results = mocks
    results.query.return_value = {
        "Items": [
            {
                "url": "https://example.com/",
                "timestamp": "t",
                "status_code": Decimal(200),
                "elapsed_ms": Decimal(373),
                "error": None,
            }
        ]
    }
    r = api.handler(_evento("/results", "GET", query={"url": "https://example.com/"}), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["results"][0]["status_code"] == 200


@pytest.mark.parametrize(
    "method, route",
    [("GET", "/probe"), ("POST", "/results"), ("DELETE", "/targets"), ("PUT", "/targets")],
    ids=["probe_con_get", "results_con_post", "targets_con_delete", "targets_con_put"],
)
def test_wrong_method_returns_404(mocks, method, route):
    r = api.handler(_evento(route, method), None)
    assert r["statusCode"] == 404


def test_unknown_route_returns_404(mocks):
    r = api.handler(_evento("/no-existe", "GET"), None)
    assert r["statusCode"] == 404


def test_event_without_method_or_path_returns_404(mocks):
    r = api.handler({}, None)
    assert r["statusCode"] == 404


def test_unexpected_error_returns_500_without_leaking_details(mocks):
    _, targets, _ = mocks
    targets.scan.side_effect = ValueError("detalle interno")
    r = api.handler(_evento("/targets", "GET"), None)
    assert r["statusCode"] == 500
    assert "detalle interno" not in r["body"]


def test_responses_are_json_with_content_type(mocks):
    _, targets, _ = mocks
    targets.scan.return_value = {"Items": []}
    r = api.handler(_evento("/targets", "GET"), None)
    assert r["headers"]["Content-Type"] == "application/json"
    json.loads(r["body"])