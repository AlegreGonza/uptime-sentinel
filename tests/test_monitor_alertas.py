import os

os.environ.setdefault("CHECKER_FUNCTION", "uptime-checker-dev")
os.environ.setdefault("TARGETS_TABLE", "uptime-targets-dev")
os.environ.setdefault("RESULTS_TABLE", "uptime-checks-dev")

from unittest.mock import MagicMock

import monitor

URL = "https://a.com/"


class TablaFalsa:
    """Imita query() de DynamoDB: respeta Limit y devuelve LastEvaluatedKey."""

    def __init__(self, chequeos_mas_recientes_primero):
        self.items = chequeos_mas_recientes_primero

    def query(self, Limit=None, ExclusiveStartKey=None, **_):
        inicio = ExclusiveStartKey["pos"] if ExclusiveStartKey else 0
        fin = inicio + Limit if Limit else len(self.items)
        salida = {"Items": self.items[inicio:fin]}
        if fin < len(self.items):
            salida["LastEvaluatedKey"] = {"pos": fin}
        return salida


def _chequeo(ts, ok):
    return {
        "url": URL,
        "timestamp": ts,
        "status_code": 200 if ok else 0,
        "error": None if ok else "timeout",
        "elapsed_ms": 5,
    }


def test_ultimos_chequeos_devuelve_solo_la_cantidad_pedida():
    tabla = TablaFalsa([_chequeo(f"t{i:02d}", True) for i in range(10, 0, -1)])
    chequeos = monitor._ultimos_chequeos(tabla, URL, 4)
    assert len(chequeos) == 4
    assert chequeos[0]["timestamp"] == "t10"


def test_recuperado_se_dispara_con_historial_mayor_a_cuatro():
    # Más reciente OK, luego tres fallos: hubo caída y ya se recuperó.
    historial = [_chequeo("t10", True)] + [_chequeo(f"t{i:02d}", False) for i in range(9, 5, -1)]
    historial += [_chequeo(f"t{i:02d}", True) for i in range(5, 0, -1)]
    tabla = TablaFalsa(historial)
    sns = MagicMock()
    monitor._evaluar_alertas(sns, tabla, URL)
    sns.publish.assert_called_once()
    assert sns.publish.call_args.kwargs["Subject"] == "RECUPERADO uptime"


def test_caida_se_dispara_exactamente_al_tercer_fallo_seguido():
    # Tres fallos recientes, luego OK: la CAÍDA se envía una sola vez.
    historial = [_chequeo(f"t{i:02d}", False) for i in range(10, 7, -1)]
    historial += [_chequeo(f"t{i:02d}", True) for i in range(7, 0, -1)]
    sns = MagicMock()
    monitor._evaluar_alertas(sns, TablaFalsa(historial), URL)
    assert sns.publish.call_args.kwargs["Subject"] == "CAÍDA uptime"


def test_no_repite_caida_con_cuatro_fallos_seguidos():
    historial = [_chequeo(f"t{i:02d}", False) for i in range(10, 0, -1)]
    sns = MagicMock()
    monitor._evaluar_alertas(sns, TablaFalsa(historial), URL)
    sns.publish.assert_not_called()


def test_pre_alerta_con_un_solo_fallo_reciente():
    historial = [_chequeo("t10", False)] + [_chequeo(f"t{i:02d}", True) for i in range(9, 0, -1)]
    sns = MagicMock()
    monitor._evaluar_alertas(sns, TablaFalsa(historial), URL)
    assert sns.publish.call_args.kwargs["Subject"] == "PRE-ALERTA uptime"


def test_sin_alerta_si_todo_responde():
    historial = [_chequeo(f"t{i:02d}", True) for i in range(10, 0, -1)]
    sns = MagicMock()
    monitor._evaluar_alertas(sns, TablaFalsa(historial), URL)
    sns.publish.assert_not_called()
