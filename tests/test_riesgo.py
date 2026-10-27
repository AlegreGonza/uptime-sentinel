from decimal import Decimal

import pytest

import riesgo
from riesgo import MIN_BASELINE, RECENT_WINDOW, _checks_for, _urls, risk_index

MIN_CHECKS = MIN_BASELINE + RECENT_WINDOW


def make_checks(statuses):
    return [
        {
            "timestamp": f"2026-10-27T00:{i:02d}:00Z",
            "status_code": status,
            "error": None if status == 200 else "fallo",
        }
        for i, status in enumerate(statuses)
    ]


class PagedTable:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _next(self, kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]

    def scan(self, **kwargs):
        return self._next(kwargs)

    def query(self, **kwargs):
        return self._next(kwargs)


@pytest.mark.parametrize("length", [0, 1, MIN_CHECKS - 1])
def test_below_minimum_history_returns_none(length):
    assert risk_index(make_checks([200] * length)) is None


def test_exact_minimum_history_returns_an_index():
    assert risk_index(make_checks([200] * MIN_CHECKS)) is not None


def test_stable_site_has_zero_index():
    assert risk_index(make_checks([200] * MIN_CHECKS))["index"] == 0


def test_sudden_failures_give_maximum_index():
    statuses = [200] * MIN_BASELINE + [500] * RECENT_WINDOW
    assert risk_index(make_checks(statuses))["index"] == 100


def test_partial_failures_give_proportional_index():
    statuses = [200] * MIN_BASELINE + [500, 500, 200, 200]
    assert risk_index(make_checks(statuses))["index"] == 50


def test_error_counts_as_failure_even_with_status_200():
    checks = make_checks([200] * MIN_CHECKS)
    for check in checks[-RECENT_WINDOW:]:
        check["error"] = "timeout"
    assert risk_index(checks)["index"] == 100


def test_missing_status_code_counts_as_failure():
    checks = make_checks([200] * MIN_BASELINE)
    checks += [{"timestamp": f"2026-10-27T01:{i:02d}:00Z", "status_code": None, "error": None} for i in range(RECENT_WINDOW)]
    assert risk_index(checks)["index"] == 100


def test_decimal_status_codes_from_dynamodb_are_compared_correctly():
    statuses = [Decimal(200)] * MIN_BASELINE + [Decimal(500)] * RECENT_WINDOW
    assert risk_index(make_checks(statuses))["index"] == 100


def test_recovering_site_is_clamped_to_zero():
    statuses = [500] * MIN_BASELINE + [200] * RECENT_WINDOW
    result = risk_index(make_checks(statuses))
    assert result["index"] == 0
    assert result["baseline_rate"] == 1.0
    assert result["recent_rate"] == 0.0


def test_baseline_failures_are_subtracted_from_the_index():
    statuses = [500, 500] + [200] * (MIN_BASELINE - 2) + [500] * RECENT_WINDOW
    result = risk_index(make_checks(statuses))
    assert result["baseline_rate"] == 0.1
    assert result["recent_rate"] == 1.0
    assert result["index"] == 90


def test_checks_are_sorted_by_timestamp_before_splitting():
    checks = make_checks([200] * MIN_BASELINE + [500] * RECENT_WINDOW)
    assert risk_index(list(reversed(checks)))["index"] == 100


def test_scan_follows_all_pages_for_targets():
    table = PagedTable(
        [
            {"Items": [{"url": "https://a.example/"}], "LastEvaluatedKey": {"url": "https://a.example/"}},
            {"Items": [{"url": "https://b.example/"}]},
        ]
    )
    assert _urls(table) == ["https://a.example/", "https://b.example/"]
    assert table.calls[1]["ExclusiveStartKey"] == {"url": "https://a.example/"}


def test_query_follows_all_pages_and_reads_oldest_first():
    table = PagedTable(
        [
            {"Items": make_checks([200])[:1], "LastEvaluatedKey": {"url": "x", "timestamp": "t"}},
            {"Items": make_checks([500])},
        ]
    )
    items = _checks_for(table, "https://a.example/")
    assert len(items) == 2
    assert table.calls[0]["ScanIndexForward"] is True
    assert table.calls[1]["ExclusiveStartKey"] == {"url": "x", "timestamp": "t"}


def test_main_reports_each_target(monkeypatch, capsys):
    targets = PagedTable([{"Items": [{"url": "https://a.example/"}, {"url": "https://b.example/"}]}])
    checks = PagedTable(
        [
            {"Items": make_checks([200] * MIN_BASELINE + [500] * RECENT_WINDOW)},
            {"Items": make_checks([200] * 3)},
        ]
    )
    tables = {"targets": targets, "checks": checks}

    class FakeDynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setattr(riesgo.boto3, "resource", lambda service: FakeDynamo())
    monkeypatch.setenv("TARGETS_TABLE", "targets")
    monkeypatch.setenv("RESULTS_TABLE", "checks")

    riesgo.main()

    out = capsys.readouterr().out
    assert "https://a.example/: riesgo 100" in out
    assert "https://b.example/: sin datos suficientes" in out