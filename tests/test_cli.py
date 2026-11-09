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
         patch.object(cli, "API_KEY", "test-key"):
        yield


@pytest.fixture
def runner():
    return CliRunner()


def _response(status, body):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body
    return response


def test_version_prints_number_and_exits_0(runner):
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "uptime 1.0.0" in result.output


def test_help_lista_los_comandos(runner):
    result = runner.invoke(cli.app, ["--help"])
    for comando in ("probe", "save", "list", "results"):
        assert comando in result.output


def test_missing_env_vars_exit_2(runner):
    with patch.object(cli, "API_URL", ""), patch.object(cli, "API_KEY", ""):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 2
    assert "are missing" in result.output


def test_probe_envia_post_a_probe_con_la_url(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(200, {"status_code": 200})) as pedido:
        runner.invoke(cli.app, ["probe", "https://example.com/"])
    assert pedido.call_args.args[0] == "POST"
    assert pedido.call_args.args[1] == "https://api.example.com/dev/probe"
    assert pedido.call_args.kwargs["json"] == {"url": "https://example.com/"}


def test_probe_prints_result(config, runner):
    body = {"url": "https://example.com/", "status_code": 200, "error": None}
    with patch.object(cli.httpx, "request", return_value=_response(200, body)):
        result = runner.invoke(cli.app, ["probe", "https://example.com/"])
    assert result.exit_code == 0
    assert "status_code" in result.output
    assert "200" in result.output


def test_save_envia_post_a_targets(config, runner):
    body = {"url": "https://example.com/", "enabled": True}
    with patch.object(cli.httpx, "request", return_value=_response(201, body)) as pedido:
        result = runner.invoke(cli.app, ["save", "https://example.com/"])
    assert result.exit_code == 0
    assert pedido.call_args.args[1] == "https://api.example.com/dev/targets"
    assert "Saved target" in result.output


def test_list_shows_urls(config, runner):
    body = {"targets": [{"url": "https://a.com/", "created_at": "2026-01-01", "enabled": True}]}
    with patch.object(cli.httpx, "request", return_value=_response(200, body)):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0
    assert "https://a.com/" in result.output
    assert "yes" in result.output


def test_list_without_targets_exits_ok(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(200, {"targets": []})):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0


def test_results_envia_la_url_como_query(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(200, {"results": []})) as pedido:
        runner.invoke(cli.app, ["results", "https://example.com/"])
    assert pedido.call_args.args[1] == "https://api.example.com/dev/results"
    assert pedido.call_args.kwargs["params"] == {"url": "https://example.com/"}


def test_results_shows_error_of_each_check(config, runner):
    body = {"results": [{"timestamp": "2026-01-01T00:00:00", "status_code": 503, "elapsed_ms": 9, "error": "Timeout"}]}
    with patch.object(cli.httpx, "request", return_value=_response(200, body)):
        result = runner.invoke(cli.app, ["results", "https://example.com/"])
    assert "Timeout" in result.output


def test_results_shows_dash_without_error(config, runner):
    body = {"results": [{"timestamp": "2026-01-01T00:00:00", "status_code": 200, "elapsed_ms": 5, "error": None}]}
    with patch.object(cli.httpx, "request", return_value=_response(200, body)):
        result = runner.invoke(cli.app, ["results", "https://example.com/"])
    assert "-" in result.output


def test_api_key_viaja_en_el_header(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(200, {"targets": []})) as pedido:
        runner.invoke(cli.app, ["list"])
    assert pedido.call_args.kwargs["headers"] == {"x-api-key": "test-key"}


def test_no_duplica_la_barra_final(config, runner):
    with patch.object(cli, "API_URL", "https://api.example.com/dev/"), \
         patch.object(cli.httpx, "request", return_value=_response(200, {"targets": []})) as pedido:
        runner.invoke(cli.app, ["list"])
    assert pedido.call_args.args[1] == "https://api.example.com/dev/targets"


def test_error_400_shows_reason_and_exits_1(config, runner):
    body = {"error": "Destination not allowed (private or internal IP)", "rejected": True}
    with patch.object(cli.httpx, "request", return_value=_response(400, body)):
        result = runner.invoke(cli.app, ["probe", "http://10.0.0.1"])
    assert result.exit_code == 1
    assert "Destination not allowed" in result.output


def test_error_409_de_url_repetida(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(409, {"error": "URL is already saved"})):
        result = runner.invoke(cli.app, ["save", "https://example.com/"])
    assert result.exit_code == 1
    assert "already saved" in result.output


def test_api_gateway_403_shows_forbidden(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(403, {"message": "Forbidden"})):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "Forbidden" in result.output


def test_502_from_checker_shows_message(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(502, {"error": "Could not reach the checker"})):
        result = runner.invoke(cli.app, ["probe", "https://example.com/"])
    assert result.exit_code == 1
    assert "Could not reach the checker" in result.output


def test_error_without_error_field_uses_generic_message(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(500, {})):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "unexpected response" in result.output


def test_error_with_non_object_body_uses_generic_message(config, runner):
    with patch.object(cli.httpx, "request", return_value=_response(500, ["x"])):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "unexpected response" in result.output


def test_connection_failure_shows_message(config, runner):
    with patch.object(cli.httpx, "request", side_effect=httpx.ConnectError("no network")):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "Could not reach the API" in result.output


def test_probe_without_url_exits_2(config, runner):
    result = runner.invoke(cli.app, ["probe"])
    assert result.exit_code == 2


def test_unknown_command_exits_2(config, runner):
    result = runner.invoke(cli.app, ["borrar"])
    assert result.exit_code == 2


def test_list_tolerates_targets_without_date(config, runner):
    body = {"targets": [{"url": "https://a.com/", "enabled": True}]}
    with patch.object(cli.httpx, "request", return_value=_response(200, body)):
        result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0
    assert "https://a.com/" in result.output