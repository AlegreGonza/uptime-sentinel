import json
import os

import boto3
import httpx
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGION = "us-east-1"
CHECKER = os.environ.get("CHECKER_FUNCTION", "uptime-checker-dev")
MONITOR = os.environ.get("MONITOR_FUNCTION", "uptime-monitor-dev")
CHECKS_TABLE = os.environ.get("RESULTS_TABLE", "uptime-checks-dev")
API_URL = os.environ.get("UPTIME_API_URL", "")
API_KEY = os.environ.get("UPTIME_API_KEY", "")
REGISTRO = {"url", "timestamp", "status_code", "error", "elapsed_ms"}

pytestmark = pytest.mark.integration


def _invocar(function, payload=None):
    client = boto3.client("lambda", region_name=REGION)
    try:
        client.get_function(FunctionName=function)
    except ClientError:
        pytest.skip(f"{function} is not deployed")
    response = client.invoke(FunctionName=function, Payload=json.dumps(payload or {}).encode())
    body = json.loads(response["Payload"].read())
    assert response.get("FunctionError") is None, body
    return body


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/",
    "https://10.0.0.1/",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/",
    "https://localhost/",
])
def test_checker_rechaza_destinos_internos(url):
    body = _invocar(CHECKER, {"url": url})
    assert body["rejected"] is True
    assert body["status_code"] == 400


@pytest.mark.parametrize("url", [
    "http://example.com/",
    "ftp://example.com/",
    "https://user:pass@example.com/",
    "https://example.com:8443/",
])
def test_checker_rechaza_esquemas_y_puertos_no_permitidos(url):
    body = _invocar(CHECKER, {"url": url})
    assert body["rejected"] is True


@pytest.mark.parametrize("payload", [{}, {"url": None}, {"url": 123}])
def test_checker_rechaza_payload_malformado(payload):
    body = _invocar(CHECKER, payload)
    assert body["rejected"] is True


def test_checker_checks_a_public_site():
    body = _invocar(CHECKER, {"url": "https://example.com/"})
    assert body["error"] is None
    assert body["status_code"] == 200
    assert REGISTRO <= set(body)


def test_monitor_runs_without_failures():
    resumen = _invocar(MONITOR)
    assert resumen["failed"] == 0
    assert resumen["checked"] >= 1


def test_monitor_guarda_el_resultado():
    _invocar(MONITOR)
    table = boto3.resource("dynamodb", region_name=REGION).Table(CHECKS_TABLE)
    response = table.query(
        KeyConditionExpression=Key("url").eq("https://example.com/"),
        ScanIndexForward=False,
        Limit=1,
    )
    assert response["Count"] >= 1
    assert REGISTRO <= set(response["Items"][0])


def _url(route):
    return API_URL.rstrip("/") + route


def _requiere_api():
    if not API_URL or not API_KEY:
        pytest.skip("Faltan UPTIME_API_URL y UPTIME_API_KEY")


@pytest.fixture
def api():
    _requiere_api()
    with httpx.Client(headers={"x-api-key": API_KEY}, timeout=30) as client:
        yield client


def test_api_without_key_returns_403():
    _requiere_api()
    assert httpx.get(_url("/targets"), timeout=30).status_code == 403


def test_api_list_con_key(api):
    response = api.get(_url("/targets"))
    assert response.status_code == 200
    assert "targets" in response.json()


def test_api_probe_sitio_publico(api):
    response = api.post(_url("/probe"), json={"url": "https://example.com/"})
    assert response.status_code == 200
    assert response.json()["status_code"] == 200


def test_api_probe_rechaza_ip_interna(api):
    response = api.post(_url("/probe"), json={"url": "https://169.254.169.254/"})
    assert response.status_code == 400
    assert response.json()["rejected"] is True


def test_api_save_repetida_da_409(api):
    response = api.post(_url("/targets"), json={"url": "https://example.com/"})
    assert response.status_code == 409


def test_api_results_without_url_returns_400(api):
    assert api.get(_url("/results")).status_code == 400


def test_api_results_returns_history(api):
    response = api.get(_url("/results"), params={"url": "https://example.com/"})
    assert response.status_code == 200
    assert "results" in response.json()