from unittest.mock import patch

import checker
import monitor
from risk import RISK_WINDOW, risk_index


def test_fetch_respeta_deadline_total():
    def slow(*args):
        import time
        time.sleep(2)
        return 200, None, {}
    with patch.object(checker, "FETCH_TIMEOUT", 0.1), \
            patch.object(checker, "_fetch_once", side_effect=slow):
        status, error, _ = checker._fetch("example.com", "/", "8.8.8.8")
    assert status == 0
    assert error == "The site did not respond in time"


def test_ruta_con_caracteres_no_ascii_queda_codificada():
    with patch.object(checker, "_resolve_public", return_value="93.184.216.34"):
        _, path, _ = checker.validate_url("https://example.com/ñ?q=á")
    path.encode("ascii")  # no debe lanzar
    assert path.startswith("/%C3%B1")


def test_indice_anterior_usa_ventana_del_mismo_tamano():
    # 61 checks: el índice actual usa los 60 más recientes y el anterior los 60 previos.
    history = [
        {"timestamp": f"{i:03d}", "status_code": 200, "error": None}
        for i in range(RISK_WINDOW + 1)
    ]
    current = risk_index(history[:RISK_WINDOW])
    previous = risk_index(history[1:RISK_WINDOW + 1])
    assert current is not None and previous is not None
    assert monitor.RISK_HISTORY_SIZE == RISK_WINDOW + 1


def test_redireccion_no_cuenta_como_caida():
    from risk import is_failure
    assert is_failure({"status_code": 301, "error": None}) is False
    assert is_failure({"status_code": 404, "error": None}) is True
