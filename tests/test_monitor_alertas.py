import os

os.environ.setdefault("CHECKER_FUNCTION", "uptime-checker-dev")
os.environ.setdefault("TARGETS_TABLE", "uptime-targets-dev")
os.environ.setdefault("RESULTS_TABLE", "uptime-checks-dev")

from unittest.mock import MagicMock

import monitor

URL = "https://a.com/"


class TablaFalsa:
    """Mimics DynamoDB query(): honors Limit and returns LastEvaluatedKey."""

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


def test_recent_checks_returns_only_the_requested_count():
    table = TablaFalsa([_chequeo(f"t{i:02d}", True) for i in range(10, 0, -1)])
    checks = monitor._recent_checks(table, URL, 4)
    assert len(checks) == 4
    assert checks[0]["timestamp"] == "t10"


def test_recovered_fires_with_history_longer_than_four():
    # Newest OK, then three failures: there was an outage and it recovered.
    history = [_chequeo("t10", True)] + [_chequeo(f"t{i:02d}", False) for i in range(9, 5, -1)]
    history += [_chequeo(f"t{i:02d}", True) for i in range(5, 0, -1)]
    table = TablaFalsa(history)
    sns = MagicMock()
    monitor._evaluate_alerts(sns, monitor._recent_checks(table, URL, monitor.RISK_HISTORY_SIZE), URL)
    sns.publish.assert_called_once()
    assert sns.publish.call_args.kwargs["Subject"] == "RECOVERED uptime"


def test_down_fires_exactly_on_the_third_consecutive_failure():
    # Three recent failures then OK: the DOWN alert is sent only once.
    history = [_chequeo(f"t{i:02d}", False) for i in range(10, 7, -1)]
    history += [_chequeo(f"t{i:02d}", True) for i in range(7, 0, -1)]
    sns = MagicMock()
    monitor._evaluate_alerts(sns, monitor._recent_checks(TablaFalsa(history), URL, monitor.RISK_HISTORY_SIZE), URL)
    assert sns.publish.call_args.kwargs["Subject"] == "DOWN uptime"


def test_does_not_repeat_down_with_four_failures():
    history = [_chequeo(f"t{i:02d}", False) for i in range(10, 0, -1)]
    sns = MagicMock()
    monitor._evaluate_alerts(sns, monitor._recent_checks(TablaFalsa(history), URL, monitor.RISK_HISTORY_SIZE), URL)
    sns.publish.assert_not_called()


def test_pre_alert_with_a_single_recent_failure():
    history = [_chequeo("t10", False)] + [_chequeo(f"t{i:02d}", True) for i in range(9, 0, -1)]
    sns = MagicMock()
    monitor._evaluate_alerts(sns, monitor._recent_checks(TablaFalsa(history), URL, monitor.RISK_HISTORY_SIZE), URL)
    assert sns.publish.call_args.kwargs["Subject"] == "PRE-ALERT uptime"


def test_no_alert_when_everything_responds():
    history = [_chequeo(f"t{i:02d}", True) for i in range(10, 0, -1)]
    sns = MagicMock()
    monitor._evaluate_alerts(sns, monitor._recent_checks(TablaFalsa(history), URL, monitor.RISK_HISTORY_SIZE), URL)
    sns.publish.assert_not_called()
