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
    cliente = boto3.client("lambda", region_name=REGION)
    try:
        cliente.get_function(FunctionName=function)
    except ClientError:
        pytest.skip(f"{function} no está desplegada")
    respuesta = cliente.invoke(FunctionName=function, Payload=json.dumps(payload or {}).encode())
    cuerpo = json.loads(respuesta["Payload"].read())
    assert respuesta.get("FunctionError") is None, cuerpo
    return cuerpo


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/",
    "https://10.0.0.1/",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/",
    "https://localhost/",
])
def test_checker_rechaza_destinos_internos(url):
    cuerpo = _invocar(CHECKER, {"url": url})
    assert cuerpo["rechazado"] is True
    assert cuerpo["status_code"] == 400


@pytest.mark.parametrize("url", [
    "http://example.com/",
    "ftp://example.com/",
    "https://user:pass@example.com/",
    "https://example.com:8443/",
])
def test_checker_rechaza_esquemas_y_puertos_no_permitidos(url):
    cuerpo = _invocar(CHECKER, {"url": url})
    assert cuerpo["rechazado"] is True


@pytest.mark.parametrize("payload", [{}, {"url": None}, {"url": 123}])
def test_checker_rechaza_payload_malformado(payload):
    cuerpo = _invocar(CHECKER, payload)
    assert cuerpo["rechazado"] is True


def test_checker_revisa_un_sitio_publico():
    cuerpo = _invocar(CHECKER, {"url": "https://example.com/"})
    assert cuerpo["error"] is None
    assert cuerpo["status_code"] == 200
    assert set(cuerpo) == REGISTRO


def test_monitor_corre_sin_fallos():
    resumen = _invocar(MONITOR)
    assert resumen["failed"] == 0
    assert resumen["checked"] >= 1


def test_monitor_guarda_el_resultado():
    _invocar(MONITOR)
    tabla = boto3.resource("dynamodb", region_name=REGION).Table(CHECKS_TABLE)
    respuesta = tabla.query(
        KeyConditionExpression=Key("url").eq("https://example.com/"),
        ScanIndexForward=False,
        Limit=1,
    )
    assert respuesta["Count"] >= 1
    assert REGISTRO <= set(respuesta["Items"][0])


def _url(ruta):
    return API_URL.rstrip("/") + ruta


def _requiere_api():
    if not API_URL or not API_KEY:
        pytest.skip("Faltan UPTIME_API_URL y UPTIME_API_KEY")


@pytest.fixture
def api():
    _requiere_api()
    with httpx.Client(headers={"x-api-key": API_KEY}, timeout=30) as cliente:
        yield cliente


def test_api_sin_key_responde_403():
    _requiere_api()
    assert httpx.get(_url("/objetivos"), timeout=30).status_code == 403


def test_api_listar_con_key(api):
    respuesta = api.get(_url("/objetivos"))
    assert respuesta.status_code == 200
    assert "objetivos" in respuesta.json()


def test_api_probar_sitio_publico(api):
    respuesta = api.post(_url("/probar"), json={"url": "https://example.com/"})
    assert respuesta.status_code == 200
    assert respuesta.json()["status_code"] == 200


def test_api_probar_rechaza_ip_interna(api):
    respuesta = api.post(_url("/probar"), json={"url": "https://169.254.169.254/"})
    assert respuesta.status_code == 400
    assert respuesta.json()["rechazado"] is True


def test_api_guardar_repetida_da_409(api):
    respuesta = api.post(_url("/objetivos"), json={"url": "https://example.com/"})
    assert respuesta.status_code == 409


def test_api_resultados_sin_url_da_400(api):
    assert api.get(_url("/resultados")).status_code == 400


def test_api_resultados_devuelve_historial(api):
    respuesta = api.get(_url("/resultados"), params={"url": "https://example.com/"})
    assert respuesta.status_code == 200
    assert "resultados" in respuesta.json()