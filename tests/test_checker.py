import socket
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

import checker
from checker import URL_RULES, comprobar, first_match, ip_es_publica, validar, validar_url


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
    assert ip_es_publica(ip) is False


@pytest.mark.parametrize("ip", [
    "8.8.8.8",
    "2001:4860:4860::8888",
    "64:ff9b::808:808",
    "::ffff:8.8.8.8",
])
def test_accepts_public_ips(ip):
    assert ip_es_publica(ip) is True


@pytest.mark.parametrize("url", [
    "http://8.8.8.8",
    "https://user:pass@8.8.8.8",
    "https://8.8.8.8:8443",
    "https:///path",
    "https://127.0.0.1",
    "https://[::1]",
    "https://[::ffff:127.0.0.1]",
])
def test_validar_url_rejects(url):
    with pytest.raises(ValueError):
        validar_url(url)


@pytest.mark.parametrize(("url", "message"), [
    ("http://8.8.8.8", "Solo se permite HTTPS"),
    ("https:///path", "Host inválido"),
    ("https://user:pass@8.8.8.8", "La URL no puede incluir credenciales"),
    ("https://8.8.8.8:8443", "Puerto no permitido"),
])
def test_each_url_rule_is_enforced(url, message):
    with pytest.raises(ValueError, match=message):
        validar(urlsplit(url), URL_RULES)


def test_validar_url_returns_host_path_and_pinned_ip():
    host, path, ip = validar_url("https://8.8.8.8/status?x=1")
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
        "error": "Destino no permitido (IP privada o interna)",
        "rechazado": True,
    }


def test_handler_rejects_malformed_api_gateway_body():
    result = checker.handler({"body": "{bad json"}, None)
    assert result["status_code"] == 400
    assert result["rechazado"] is True


def test_comprobar_returns_record_for_valid_url():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")), \
         patch("checker._consultar", return_value=(200, None)):
        record = comprobar("https://example.com/")
    assert record["url"] == "https://example.com/"
    assert record["status_code"] == 200
    assert record["error"] is None
    assert set(record) == {"url", "timestamp", "status_code", "error", "elapsed_ms"}


def test_handler_accepts_api_gateway_body():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")), \
         patch("checker._consultar", return_value=(200, None)):
        result = checker.handler({"body": '{"url": "https://example.com/"}'}, None)
    assert result["status_code"] == 200
    assert "rechazado" not in result


def test_resolve_public_accepts_host_with_only_public_addresses():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "1.1.1.1")):
        assert checker._resolve_public("example.com") == "1.1.1.1"


def test_resolve_public_rejects_host_if_any_address_is_private():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "10.0.0.5")):
        with pytest.raises(ValueError, match="Destino no permitido"):
            checker._resolve_public("example.com")


def test_resolve_public_rejects_internal_ipv6_next_to_public_ipv4():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8", "fd00:ec2::254")):
        with pytest.raises(ValueError):
            checker._resolve_public("example.com")


def test_validar_url_pins_the_resolved_ip():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
        host, path, ip = validar_url("https://example.com/status?x=1")
    assert (host, path, ip) == ("example.com", "/status?x=1", "8.8.8.8")


def test_validar_url_rejects_host_resolving_to_metadata_endpoint():
    with patch("checker.socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")):
        with pytest.raises(ValueError):
            validar_url("https://evil.example.com/")


def test_validar_url_reports_unresolvable_host():
    with patch("checker.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(ValueError, match="No se pudo resolver"):
            validar_url("https://no-existe.example/")


def test_connection_goes_to_pinned_ip_not_to_hostname():
    with patch("checker.socket.create_connection") as create, \
         patch("checker.ssl.create_default_context") as context:
        connection = checker._PinnedConnection("example.com", "8.8.8.8")
        connection.connect()

    create.assert_called_once_with(("8.8.8.8", 443), checker.TIMEOUT)
    _, kwargs = context.return_value.wrap_socket.call_args
    assert kwargs["server_hostname"] == "example.com"