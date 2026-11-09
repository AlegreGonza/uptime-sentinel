import io
import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import api
import checker
import monitor
from risk import risk_index
from tools import cli


class FakeTable:
    def __init__(self, items=None):
        self.items = items or []

    def query(self, **kw):
        url = kw["KeyConditionExpression"].get_expression()["values"][1]
        return {"Items": [i for i in self.items if i["url"] == url]}


def _resolve(ip="93.184.216.34"):
    # Sin red: se mockea DNS y el request HTTP para que el test nunca toque internet.
    return _NoNetwork(patch.object(checker, "_resolve_public", return_value=ip))


class _NoNetwork:
    def __init__(self, dns_patch):
        self.dns_patch = dns_patch
        self.fetch_patch = patch.object(checker, "_fetch", return_value=(200, None, {}))

    def __enter__(self):
        self.dns_patch.__enter__()
        self.fetch_patch.__enter__()

    def __exit__(self, *exc):
        self.fetch_patch.__exit__(*exc)
        return self.dns_patch.__exit__(*exc)


# 1) Bug que introduje: el checker guarda el path codificado, la API no
def test_results_encuentra_url_no_ascii_guardada():
    with _resolve():
        saved = checker.handler({"url": "https://example.com/ñ"}, None)
    table = FakeTable([{"url": saved["url"], "timestamp": "t", "status_code": 200,
                        "error": None, "elapsed_ms": 1}])
    with patch.object(api, "checks_db", table):
        resp = api._results({"queryStringParameters": {"url": "https://example.com/ñ"}})
    assert json.loads(resp["body"])["results"], "la API no encuentra lo guardado"


# 2) Misma URL con y sin punto final guarda dos targets distintos
def test_host_con_punto_final_no_duplica_target():
    with _resolve():
        a = checker.handler({"url": "https://example.com/"}, None)
    with _resolve():
        b = checker.handler({"url": "https://example.com./"}, None)
    assert a["url"] == b["url"], f"duplicado: {a['url']} vs {b['url']}"


# 3) DNS que mezcla IP pública y privada debe rechazarse
def test_dns_mezclado_se_rechaza():
    fake = [(0, 0, 0, "", ("93.184.216.34", 443)), (0, 0, 0, "", ("10.0.0.5", 443))]
    with patch("checker.socket.getaddrinfo", return_value=fake), \
         pytest.raises(ValueError, match="not allowed"):
        checker.validate_url("https://example.com/")


# 4) Host que resuelve a loopback se rechaza (p. ej. forma decimal 2130706433)
def test_ip_decimal_privada_se_rechaza():
    fake = [(0, 0, 0, "", ("127.0.0.1", 443))]
    with patch("checker.socket.getaddrinfo", return_value=fake), pytest.raises(ValueError):
        checker.validate_url("https://2130706433/")


# 5) Índice de riesgo siempre entre 0 y 100
def test_indice_riesgo_acotado():
    checks = ([{"timestamp": f"a{i:03d}", "status_code": 200, "error": None} for i in range(40)]
              + [{"timestamp": f"b{i:03d}", "status_code": 500, "error": "x"} for i in range(4)])
    assert 0 <= risk_index(checks)["index"] <= 100


# 6) Cuatro fallos seguidos no repiten DOWN (diseño actual: solo al llegar a 3)
def test_cuatro_fallos_no_disparan_down_otra_vez():
    fails = [{"error": "x", "status_code": 0}] * 4
    assert monitor._alert_type([monitor.is_failure(c) for c in fails]) is None


# 7) CLI: respuesta sin la clave "targets" debe fallar limpio, no con traceback
def test_cli_list_respuesta_invalida_no_revienta():
    runner = CliRunner()
    with patch.object(cli, "_request", return_value=(200, {"otra": 1})), \
         patch.object(cli, "API_URL", "https://x"), patch.object(cli, "API_KEY", "k"):
        result = runner.invoke(cli.app, ["list"])
    assert result.exception is None or isinstance(result.exception, SystemExit)


# 8) CLI: un registro de results sin timestamp no debe reventar
def test_cli_results_registro_incompleto():
    runner = CliRunner()
    with patch.object(cli, "_request", return_value=(200, {"results": [{"status_code": 0}]})), \
            patch.object(cli, "API_URL", "https://x"), patch.object(cli, "API_KEY", "k"):
        result = runner.invoke(cli.app, ["results", "https://x/"])
    assert result.exception is None or isinstance(result.exception, SystemExit)


# 9) Cerrar el socket libera un recv bloqueado en otro hilo
def test_abort_libera_recv_bloqueado():
    import socket as _socket
    import threading
    import time as _time
    a, b = _socket.socketpair()
    conn = checker._PinnedConnection("example.com", "8.8.8.8")
    conn.sock = a
    done = threading.Event()

    def worker():
        try:
            a.recv(1)
        except OSError:
            pass
        done.set()

    threading.Thread(target=worker, daemon=True).start()
    _time.sleep(0.1)
    checker._abort(conn)
    assert done.wait(2), "el recv bloqueado no se liberó"
    b.close()


# 10) URL malformada devuelve mensaje fijo, sin texto de urlsplit
def test_url_malformada_devuelve_mensaje_fijo():
    with pytest.raises(ValueError) as info:
        checker.validate_url("https://[::1")
    assert str(info.value) == "Invalid URL"


# 11) expected_status: solo ese código es OK cuando está configurado
def test_expected_status_exacto():
    from risk import is_failure
    assert is_failure({"status_code": 404, "error": None}, expected_status=404) is False
    assert is_failure({"status_code": 200, "error": None}, expected_status=404) is True
    assert is_failure({"status_code": 301, "error": None}, expected_status=301) is False
    assert is_failure({"status_code": 0, "error": "x"}, expected_status=0) is True


# 12) La API rechaza expected_status inválido y guarda el válido
def test_api_valida_expected_status(monkeypatch):
    def event(**extra):
        return {"httpMethod": "POST", "path": "/targets",
                "body": json.dumps({"url": "https://example.com/", **extra})}
    for bad in (99, 600, "200", True):
        assert api.handler(event(expected_status=bad), None)["statusCode"] == 400, bad
    monkeypatch.setattr(api, "_invoke_checker", lambda url: {"url": "https://example.com/"})
    monkeypatch.setattr(api, "targets_db", MagicMock())
    resp = api.handler(event(expected_status=404), None)
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["expected_status"] == 404


# 13) Las invocaciones al checker corren en paralelo
def test_invocaciones_en_paralelo():
    import threading
    import time as _time
    from unittest.mock import MagicMock
    inflight = {"now": 0, "max": 0}
    lock = threading.Lock()

    def invoke(FunctionName, Payload):
        with lock:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        _time.sleep(0.2)
        with lock:
            inflight["now"] -= 1
        url = json.loads(Payload)["url"]
        body = {"url": url, "timestamp": "t", "status_code": 200, "error": None, "elapsed_ms": 1}
        return {"Payload": io.BytesIO(json.dumps(body).encode())}

    targets = MagicMock()
    targets.scan.return_value = {"Items": [{"url": f"https://s{i}.example/"} for i in range(4)]}
    lam = MagicMock()
    lam.invoke.side_effect = invoke
    checks = MagicMock()
    with patch.object(monitor, "TARGETS_DB", targets), patch.object(monitor, "CHECKS_DB", checks), \
         patch.object(monitor, "LAMBDA", lam), patch.object(monitor, "SNS", MagicMock()):
        summary = monitor.handler({}, None)
    assert summary == {"checked": 4, "rejected": 0, "failed": 0}
    assert inflight["max"] > 1, "las invocaciones fueron secuenciales"


# 14) La conexión usa la IP validada y no vuelve a resolver el host
def test_conexion_usa_ip_validada_sin_nueva_resolucion():
    answer = [(0, 0, 0, "", ("93.184.216.34", 443))]
    with patch("checker.socket.getaddrinfo", return_value=answer) as resolve:
        _, _, ip = checker.validate_url("https://example.com/")
    with patch("checker.ssl.create_default_context"), \
         patch("checker.socket.create_connection") as create:
        conn = checker._PinnedConnection("example.com", ip)
        conn.connect()
    assert create.call_args.args[0] == ("93.184.216.34", 443)
    assert resolve.call_count == 1
