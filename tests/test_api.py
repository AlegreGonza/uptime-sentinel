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
RECHAZADO = {"status_code": 400, "error": "Destino no permitido (IP privada o interna)", "rechazado": True}


def _evento(ruta, metodo, body=None, query=None):
    return {
        "httpMethod": metodo,
        "path": ruta,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
    }


def _evento_crudo(ruta, metodo, body_crudo):
    return {"httpMethod": metodo, "path": ruta, "body": body_crudo}


def _checker(cuerpo, function_error=None):
    respuesta = {"Payload": io.BytesIO(json.dumps(cuerpo).encode())}
    if function_error:
        respuesta["FunctionError"] = function_error
    return respuesta


def _cliente_error(code):
    return ClientError({"Error": {"Code": code, "Message": "simulado"}}, "Operacion")


@pytest.fixture
def mocks():
    with patch.object(api, "lambda_client") as lam, \
         patch.object(api, "tabla_objetivos") as objetivos, \
         patch.object(api, "tabla_resultados") as resultados:
        yield lam, objetivos, resultados


def test_probar_returns_checker_result(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    r = api.handler(_evento("/probar", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == ACEPTADO


def test_probar_invokes_checker_with_expected_function_and_payload(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/probar", "POST", {"url": "https://example.com/"}), None)
    kwargs = lam.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == api.CHECKER_FUNCTION
    assert json.loads(kwargs["Payload"]) == {"url": "https://example.com/"}


def test_probar_never_writes_to_tables(mocks):
    lam, objetivos, resultados = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/probar", "POST", {"url": "https://example.com/"}), None)
    objetivos.put_item.assert_not_called()
    resultados.put_item.assert_not_called()


def test_probar_rejected_url_returns_400(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(RECHAZADO)
    r = api.handler(_evento("/probar", "POST", {"url": "http://10.0.0.1"}), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"]) == RECHAZADO


@pytest.mark.parametrize(
    "body",
    [{}, {"url": ""}, {"url": "   "}, {"url": 123}, {"url": None}, {"url": ["https://example.com/"]}, [], "https://example.com/", None],
    ids=["sin_clave", "vacia", "solo_espacios", "numero", "null", "lista", "lista_raiz", "string_raiz", "sin_body"],
)
def test_invalid_body_returns_400_without_calling_checker(mocks, body):
    lam, _, _ = mocks
    r = api.handler(_evento("/probar", "POST", body), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"]) == {"error": "Falta la URL"}
    lam.invoke.assert_not_called()


def test_invalid_json_body_returns_400(mocks):
    lam, _, _ = mocks
    r = api.handler(_evento_crudo("/probar", "POST", "{no json"), None)
    assert r["statusCode"] == 400
    lam.invoke.assert_not_called()


def test_url_is_stripped_before_calling_checker(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/probar", "POST", {"url": "  https://example.com/  "}), None)
    assert json.loads(lam.invoke.call_args.kwargs["Payload"]) == {"url": "https://example.com/"}


def test_guardar_saves_accepted_url_as_enabled(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    r = api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 201
    item = objetivos.put_item.call_args.kwargs["Item"]
    assert item["url"] == "https://example.com/"
    assert item["enabled"] is True
    assert "created_at" in item


def test_guardar_uses_condition_to_avoid_overwrite(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    kwargs = objetivos.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "attribute_not_exists(#u)"
    assert kwargs["ExpressionAttributeNames"] == {"#u": "url"}


def test_guardar_does_not_save_rejected_url(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker(RECHAZADO)
    r = api.handler(_evento("/objetivos", "POST", {"url": "http://10.0.0.1"}), None)
    assert r["statusCode"] == 400
    objetivos.put_item.assert_not_called()


def test_guardar_duplicate_url_returns_409_with_url(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    objetivos.put_item.side_effect = _cliente_error("ConditionalCheckFailedException")
    r = api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 409
    assert json.loads(r["body"])["url"] == "https://example.com/"


def test_guardar_other_dynamodb_error_returns_500(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker(ACEPTADO)
    objetivos.put_item.side_effect = _cliente_error("ProvisionedThroughputExceededException")
    r = api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500


def test_guardar_checker_response_without_url_returns_500_and_writes_nothing(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker({"status_code": 200})
    r = api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500
    objetivos.put_item.assert_not_called()


def test_guardar_checker_non_json_payload_returns_500_and_writes_nothing(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = {"Payload": io.BytesIO(b"no es json")}
    r = api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500
    objetivos.put_item.assert_not_called()


def test_checker_function_error_returns_502(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker({"errorMessage": "boom"}, function_error="Unhandled")
    r = api.handler(_evento("/probar", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 502


def test_checker_crash_does_not_write_to_targets(mocks):
    lam, objetivos, _ = mocks
    lam.invoke.return_value = _checker({"errorMessage": "boom"}, function_error="Unhandled")
    api.handler(_evento("/objetivos", "POST", {"url": "https://example.com/"}), None)
    objetivos.put_item.assert_not_called()


def test_checker_crash_details_are_not_returned_to_client(mocks):
    lam, _, _ = mocks
    lam.invoke.return_value = _checker({"errorMessage": "secreto-interno"}, function_error="Unhandled")
    r = api.handler(_evento("/probar", "POST", {"url": "https://example.com/"}), None)
    assert "secreto-interno" not in r["body"]


def test_lambda_invoke_failure_returns_502(mocks):
    lam, _, _ = mocks
    lam.invoke.side_effect = _cliente_error("TooManyRequestsException")
    r = api.handler(_evento("/probar", "POST", {"url": "https://example.com/"}), None)
    assert r["statusCode"] == 502


def test_listar_follows_all_pages(mocks):
    _, objetivos, _ = mocks
    objetivos.scan.side_effect = [
        {"Items": [{"url": "https://a.com/"}], "LastEvaluatedKey": {"url": "https://a.com/"}},
        {"Items": [{"url": "https://b.com/"}]},
    ]
    r = api.handler(_evento("/objetivos", "GET"), None)
    urls = [o["url"] for o in json.loads(r["body"])["objetivos"]]
    assert urls == ["https://a.com/", "https://b.com/"]
    assert objetivos.scan.call_count == 2


def test_listar_empty_table_returns_empty_list(mocks):
    _, objetivos, _ = mocks
    objetivos.scan.return_value = {"Items": []}
    r = api.handler(_evento("/objetivos", "GET"), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"]) == {"objetivos": []}


def test_listar_dynamodb_error_returns_500(mocks):
    _, objetivos, _ = mocks
    objetivos.scan.side_effect = _cliente_error("ProvisionedThroughputExceededException")
    r = api.handler(_evento("/objetivos", "GET"), None)
    assert r["statusCode"] == 500


def test_resultados_queries_newest_first_with_limit(mocks):
    _, _, resultados = mocks
    resultados.query.return_value = {"Items": [{"url": "https://example.com/"}]}
    r = api.handler(_evento("/resultados", "GET", query={"url": "https://example.com/"}), None)
    assert r["statusCode"] == 200
    kwargs = resultados.query.call_args.kwargs
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == api.MAX_RESULTS


@pytest.mark.parametrize("query", [{}, {"url": ""}, {"url": "   "}, None], ids=["sin_clave", "vacia", "solo_espacios", "sin_query"])
def test_resultados_without_url_returns_400_without_querying(mocks, query):
    _, _, resultados = mocks
    r = api.handler(_evento("/resultados", "GET", query=query), None)
    assert r["statusCode"] == 400
    resultados.query.assert_not_called()


def test_resultados_dynamodb_error_returns_500(mocks):
    _, _, resultados = mocks
    resultados.query.side_effect = _cliente_error("ResourceNotFoundException")
    r = api.handler(_evento("/resultados", "GET", query={"url": "https://example.com/"}), None)
    assert r["statusCode"] == 500


def test_resultados_con_decimal_de_dynamodb_no_falla(mocks):
    _, _, resultados = mocks
    resultados.query.return_value = {
        "Items": [
            {
                "url": "https://example.com/",
                "timestamp": "t",
                "status_code": Decimal("200"),
                "elapsed_ms": Decimal("373"),
                "error": None,
            }
        ]
    }
    r = api.handler(_evento("/resultados", "GET", query={"url": "https://example.com/"}), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["resultados"][0]["status_code"] == 200


@pytest.mark.parametrize(
    "metodo, ruta",
    [("GET", "/probar"), ("POST", "/resultados"), ("DELETE", "/objetivos"), ("PUT", "/objetivos")],
    ids=["probar_con_get", "resultados_con_post", "objetivos_con_delete", "objetivos_con_put"],
)
def test_wrong_method_returns_404(mocks, metodo, ruta):
    r = api.handler(_evento(ruta, metodo), None)
    assert r["statusCode"] == 404


def test_unknown_route_returns_404(mocks):
    r = api.handler(_evento("/no-existe", "GET"), None)
    assert r["statusCode"] == 404


def test_event_without_method_or_path_returns_404(mocks):
    r = api.handler({}, None)
    assert r["statusCode"] == 404


def test_unexpected_error_returns_500_without_leaking_details(mocks):
    _, objetivos, _ = mocks
    objetivos.scan.side_effect = ValueError("detalle interno")
    r = api.handler(_evento("/objetivos", "GET"), None)
    assert r["statusCode"] == 500
    assert "detalle interno" not in r["body"]


def test_responses_are_json_with_content_type(mocks):
    _, objetivos, _ = mocks
    objetivos.scan.return_value = {"Items": []}
    r = api.handler(_evento("/objetivos", "GET"), None)
    assert r["headers"]["Content-Type"] == "application/json"
    json.loads(r["body"])