import errno
import http.client
import socket
import ssl
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

import checker
from checker import (
    URL_RULES,
    check_url,
    first_match,
    is_public_ip,
    validate,
    validate_url,
)


def _addrinfo(*ips):
    return [
        (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in ips
    ]


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1",
    "::7f00:1",
    "2002:7f00:1::",
    "64:ff9b::a9fe:a9fe",
    "2001:0:4136:e378:8000:63bf:3f57:fffe",
    "5f00::1",
    "192.88.99.1",
])
def test_rejects_non_public_ips(ip):
    assert is_public_ip(ip) is False


@pytest.mark.parametrize("ip", [
    "8.8.8.8",
    "2001:4860:4860::8888",
    "64:ff9b::808:808",
    "::ffff:8.8.8.8",
])
def test_accepts_public_ips(ip):
    assert is_public_ip(ip) is True


@pytest.mark.parametrize("url", [
    "http://8.8.8.8",
    "https://user:pass@8.8.8.8",
    "https://8.8.8.8:8443",
    "https:///path",
    "https://127.0.0.1",
    "https://[::1]",
    "https://[::ffff:127.0.0.1]",
])
def test_validate_url_rejects(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(("url", "message"), [
    ("http://8.8.8.8", "Only HTTPS is allowed"),
    ("https:///path", "Invalid host"),
    ("https://user:pass@8.8.8.8", "The URL cannot include credentials"),
    ("https://8.8.8.8:8443", "Port not allowed"),
])
def test_each_url_rule_is_enforced(url, message):
    with pytest.raises(ValueError, match=message):
        validate(urlsplit(url), URL_RULES)


def test_validate_url_returns_host_path_and_pinned_ip():
    host, path, ip = validate_url("https://8.8.8.8/status?x=1")
    assert (host, path, ip) == ("8.8.8.8", "/status?x=1", "8.8.8.8")


def test_first_match_uses_first_true_condition():
    table = (
        (lambda x: x > 10, lambda x: "big"),
        (lambda x: x > 0, lambda x: "small"),
    )
    assert first_match(20, table, "none") == "big"
    assert first_match(5, table, "none") == "small"
    assert first_match(-1, table, "none") == "none"


def test_handler_rejects_invalid_url():
    result = checker.handler({"url": "https://127.0.0.1"}, None)
    assert result == {
        "status_code": 400,
        "error": "Destination not allowed (private or internal IP)",
        "rejected": True,
    }


def test_handler_rejects_malformed_api_gateway_body():
    result = checker.handler({"body": "{bad json"}, None)
    assert result["status_code"] == 400
    assert result["rejected"] is True


def test_check_url_returns_record_for_valid_url():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")), \
         patch("checker._fetch", return_value=(200, None, {})):
        record = check_url("https://example.com/")
    assert record["url"] == "https://example.com/"
    assert record["status_code"] == 200
    assert record["error"] is None
    assert set(record) == {"url", "timestamp", "status_code", "error", "elapsed_ms", "dns_ms", "connect_ms", "tls_ms", "response_ms"}


def test_handler_accepts_api_gateway_body():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")), \
         patch("checker._fetch", return_value=(200, None, {})):
        result = checker.handler({"body": '{"url": "https://example.com/"}'}, None)
    assert result["status_code"] == 200
    assert "rejected" not in result


def test_resolve_public_accepts_host_with_only_public_addresses():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "1.1.1.1")):
        assert checker._resolve_public("example.com") == "1.1.1.1"


def test_resolve_public_rejects_host_if_any_address_is_private():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "10.0.0.5")), pytest.raises(ValueError, match="Destination not allowed"):
        checker._resolve_public("example.com")


def test_resolve_public_rejects_internal_ipv6_next_to_public_ipv4():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "fd00:ec2::254")), pytest.raises(ValueError):
        checker._resolve_public("example.com")


def test_validate_url_pins_the_resolved_ip():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
        host, path, ip = validate_url("https://example.com/status?x=1")
    assert (host, path, ip) == ("example.com", "/status?x=1", "8.8.8.8")


def test_validate_url_rejects_host_resolving_to_metadata_endpoint():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")), pytest.raises(ValueError):
        validate_url("https://evil.example.com/")


def test_validate_url_reports_unresolvable_host():
    with patch("checker.socket.getaddrinfo", side_effect=socket.gaierror("nope")), pytest.raises(ValueError, match="Could not resolve"):
        validate_url("https://no-existe.example/")


def test_connection_goes_to_pinned_ip_not_to_hostname():
    with patch("checker.socket.create_connection") as create, \
         patch("checker.ssl.create_default_context") as context:
        connection = checker._PinnedConnection("example.com", "8.8.8.8")
        connection.connect()

    create.assert_called_once_with(("8.8.8.8", 443), checker.TIMEOUT)
    _, kwargs = context.return_value.wrap_socket.call_args
    assert kwargs["server_hostname"] == "example.com"


def test_resolution_timeout_is_reported_as_unresolvable():
    def slow(*args, **kwargs):
        time.sleep(0.5)
        return _addrinfo("8.8.8.8")

    with patch.object(checker, "DNS_TIMEOUT", 0.05), patch("checker.socket.getaddrinfo", side_effect=slow), \
            pytest.raises(ValueError, match="Could not resolve"):
        validate_url("https://example.com/")


def test_timeout_error_message_is_clear():
    assert checker._error_message(TimeoutError("timed out")) == "The site did not respond in time"


def test_refused_connection_message_is_clear():
    assert checker._error_message(ConnectionRefusedError("refused")) == "The site refused the connection (nothing is listening on port 443)"


def test_certificate_error_message_is_clear():
    assert checker._error_message(ssl.SSLError("bad cert")) == "The HTTPS certificate is not valid"


def test_invalid_response_message_is_clear():
    assert checker._error_message(http.client.BadStatusLine("x")) == "The site sent an invalid response"


def test_unreachable_host_message_is_clear():
    error = OSError(errno.EHOSTUNREACH, "No route to host")
    assert checker._error_message(error) == "The site is unreachable from the checker"


def test_network_unreachable_message_is_clear():
    error = OSError(errno.ENETUNREACH, "Network is unreachable")
    assert checker._error_message(error) == "The site is unreachable from the checker"


def test_refused_connection_is_reported_with_its_own_message():
    with patch("checker.socket.create_connection", side_effect=ConnectionRefusedError("refused")):
        status, error, timings = checker._fetch("example.com", "/", "8.8.8.8")
    assert status == 0
    assert error == "The site refused the connection (nothing is listening on port 443)"
    assert "connect_ms" not in timings


def test_connection_records_connect_and_tls_timings():
    with patch("checker.socket.create_connection") as create, \
         patch("checker.ssl.create_default_context"):
        connection = checker._PinnedConnection("example.com", "8.8.8.8")
        connection.connect()

    create.assert_called_once()
    assert isinstance(connection.timings["connect_ms"], int)
    assert isinstance(connection.timings["tls_ms"], int)
    assert connection.timings["connect_ms"] >= 0
    assert connection.timings["tls_ms"] >= 0


def test_check_url_returns_each_stage_timing():
    timings = {"connect_ms": 3, "tls_ms": 5, "response_ms": 7}
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")), \
         patch("checker._fetch", return_value=(200, None, timings)):
        record = checker.check_url("https://example.com/")
    assert record["connect_ms"] == 3
    assert record["tls_ms"] == 5
    assert record["response_ms"] == 7
    assert isinstance(record["dns_ms"], int)
    assert record["dns_ms"] >= 0


def test_check_url_defaults_missing_stage_timings_to_zero():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")), \
         patch("checker._fetch", return_value=(0, "The site did not respond in time", {})):
        record = checker.check_url("https://example.com/")
    assert record["connect_ms"] == 0
    assert record["tls_ms"] == 0
    assert record["response_ms"] == 0
    assert record["error"] == "The site did not respond in time"


def test_invalid_json_body_gets_a_fixed_message():
    result = checker.handler({"body": "not json{"}, None)
    assert result == {"status_code": 400, "error": "Invalid JSON body", "rejected": True}


def test_ip_with_zone_id_is_not_public():
    assert checker.is_public_ip("8.8.8.8%eth0") is False
