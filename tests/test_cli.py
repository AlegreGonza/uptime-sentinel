import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

sys.path.insert(0, "tools")

import cli


@pytest.fixture
def config():
    with patch.object(cli, "API_URL", "https://api.example.com/dev"), \
         patch.object(cli, "API_KEY", "clave-de-prueba"):
        yield


@pytest.fixture
def runner():
    return CliRunner()


def _respuesta(status, cuerpo):
    respuesta = MagicMock()
    respuesta.status_code = status
    respuesta.json.return_value = cuerpo
    return respuesta


def test_version_muestra_numero_y_sale_con_0(runner):
    resultado = runner.invoke(cli.app, ["--version"])
    assert resultado.exit_code == 0
    assert "uptime 1.0.0" in resultado.output


def test_help_lista_los_comandos(runner):
    resultado = runner.invoke(cli.app, ["--help"])
    for comando in ("probar", "guardar", "listar", "resultados"):
        assert comando in resultado.output


def test_sin_variables_de_entorno_sale_con_2(runner):
    with patch.object(cli, "API_URL", ""), patch.object(cli, "API_KEY", ""):
        resultado = runner.invoke(cli.app, ["listar"])
    assert resultado.exit_code == 2
    assert "Faltan" in resultado.output


def test_probar_envia_post_a_probar_con_la_url(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, {"status_code": 200})) as pedido:
        runner.invoke(cli.app, ["probar", "https://example.com/"])
    assert pedido.call_args.args[0] == "POST"
    assert pedido.call_args.args[1] == "https://api.example.com/dev/probar"
    assert pedido.call_args.kwargs["json"] == {"url": "https://example.com/"}


def test_probar_muestra_el_resultado(config, runner):
    cuerpo = {"url": "https://example.com/", "status_code": 200, "error": None}
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, cuerpo)):
        resultado = runner.invoke(cli.app, ["probar", "https://example.com/"])
    assert resultado.exit_code == 0
    assert "status_code" in resultado.output
    assert "200" in resultado.output


def test_guardar_envia_post_a_objetivos(config, runner):
    cuerpo = {"url": "https://example.com/", "enabled": True}
    with patch.object(cli.httpx, "request", return_value=_respuesta(201, cuerpo)) as pedido:
        resultado = runner.invoke(cli.app, ["guardar", "https://example.com/"])
    assert resultado.exit_code == 0
    assert pedido.call_args.args[1] == "https://api.example.com/dev/objetivos"
    assert "Objetivo guardado" in resultado.output


def test_listar_muestra_las_urls(config, runner):
    cuerpo = {"objetivos": [{"url": "https://a.com/", "created_at": "2026-01-01", "enabled": True}]}
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, cuerpo)):
        resultado = runner.invoke(cli.app, ["listar"])
    assert resultado.exit_code == 0
    assert "https://a.com/" in resultado.output
    assert "sí" in resultado.output


def test_listar_sin_objetivos_sale_bien(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, {"objetivos": []})):
        resultado = runner.invoke(cli.app, ["listar"])
    assert resultado.exit_code == 0


def test_resultados_envia_la_url_como_query(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, {"resultados": []})) as pedido:
        runner.invoke(cli.app, ["resultados", "https://example.com/"])
    assert pedido.call_args.args[1] == "https://api.example.com/dev/resultados"
    assert pedido.call_args.kwargs["params"] == {"url": "https://example.com/"}


def test_resultados_muestra_el_error_de_cada_chequeo(config, runner):
    cuerpo = {"resultados": [{"timestamp": "2026-01-01T00:00:00", "status_code": 503, "elapsed_ms": 9, "error": "Timeout"}]}
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, cuerpo)):
        resultado = runner.invoke(cli.app, ["resultados", "https://example.com/"])
    assert "Timeout" in resultado.output


def test_resultados_muestra_guion_sin_error(config, runner):
    cuerpo = {"resultados": [{"timestamp": "2026-01-01T00:00:00", "status_code": 200, "elapsed_ms": 5, "error": None}]}
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, cuerpo)):
        resultado = runner.invoke(cli.app, ["resultados", "https://example.com/"])
    assert "-" in resultado.output


def test_api_key_viaja_en_el_header(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(200, {"objetivos": []})) as pedido:
        runner.invoke(cli.app, ["listar"])
    assert pedido.call_args.kwargs["headers"] == {"x-api-key": "clave-de-prueba"}


def test_no_duplica_la_barra_final(config, runner):
    with patch.object(cli, "API_URL", "https://api.example.com/dev/"), \
         patch.object(cli.httpx, "request", return_value=_respuesta(200, {"objetivos": []})) as pedido:
        runner.invoke(cli.app, ["listar"])
    assert pedido.call_args.args[1] == "https://api.example.com/dev/objetivos"


def test_error_400_muestra_el_motivo_y_sale_con_1(config, runner):
    cuerpo = {"error": "Destino no permitido (IP privada o interna)", "rechazado": True}
    with patch.object(cli.httpx, "request", return_value=_respuesta(400, cuerpo)):
        resultado = runner.invoke(cli.app, ["probar", "http://10.0.0.1"])
    assert resultado.exit_code == 1
    assert "Destino no permitido" in resultado.output


def test_error_409_de_url_repetida(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(409, {"error": "La URL ya está guardada"})):
        resultado = runner.invoke(cli.app, ["guardar", "https://example.com/"])
    assert resultado.exit_code == 1
    assert "ya está guardada" in resultado.output


def test_403_de_api_gateway_muestra_forbidden(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(403, {"message": "Forbidden"})):
        resultado = runner.invoke(cli.app, ["listar"])
    assert resultado.exit_code == 1
    assert "Forbidden" in resultado.output


def test_502_del_checker_muestra_mensaje(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(502, {"error": "No se pudo consultar el checker"})):
        resultado = runner.invoke(cli.app, ["probar", "https://example.com/"])
    assert resultado.exit_code == 1
    assert "No se pudo consultar el checker" in resultado.output


def test_error_sin_campo_error_usa_mensaje_generico(config, runner):
    with patch.object(cli.httpx, "request", return_value=_respuesta(500, {})):
        resultado = runner.invoke(cli.app, ["listar"])
    assert resultado.exit_code == 1
    assert "respuesta inesperada" in resultado.output


def test_sin_conexion_muestra_mensaje(config, runner):
    with patch.object(cli.httpx, "request", side_effect=httpx.ConnectError("sin red")):
        resultado = runner.invoke(cli.app, ["listar"])
    assert resultado.exit_code == 1
    assert "No se pudo conectar" in resultado.output


def test_probar_sin_url_sale_con_2(config, runner):
    resultado = runner.invoke(cli.app, ["probar"])
    assert resultado.exit_code == 2


def test_comando_inexistente_sale_con_2(config, runner):
    resultado = runner.invoke(cli.app, ["borrar"])
    assert resultado.exit_code == 2