import http.client
import ipaddress
import json
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

TIMEOUT = 10
MAX_URL_LEN = 2048
PORT = 443
NAT64 = ipaddress.ip_network("64:ff9b::/96")
TEREDO = ipaddress.ip_network("2001::/32")
NON_PUBLIC_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "fec0::/10",
    "192.88.99.0/24",
    "5f00::/16",
    "100:0:0:1::/64",
))


def first_match(value, table, default):
    for condition, action in table:
        if condition(value):
            return action(value)
    return default


def _teredo(ip):
    server = ipaddress.IPv4Address(ip.packed[4:8])
    client = ipaddress.IPv4Address(~int.from_bytes(ip.packed[-4:], "big") & 0xFFFFFFFF)
    return [server, client]


DECODERS = (
    (lambda ip: ip.ipv4_mapped is not None, lambda ip: [ip.ipv4_mapped]),
    (lambda ip: ip.sixtofour is not None, lambda ip: [ip.sixtofour]),
    (lambda ip: ip in NAT64, lambda ip: [ipaddress.IPv4Address(ip.packed[-4:])]),
    (lambda ip: ip in TEREDO, _teredo),
    (lambda ip: int(ip) < 2**32, lambda ip: [ipaddress.IPv4Address(int(ip))]),
)


def _embedded_ipv4(ip):
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    return first_match(ip, DECODERS, [])


def _is_public_unicast(ip):
    return (
        ip.is_global
        and not ip.is_multicast
        and not any(ip in net for net in NON_PUBLIC_NETWORKS)
    )


def ip_es_publica(text):
    ip = ipaddress.ip_address(text.split("%")[0])
    return all(_is_public_unicast(candidate) for candidate in [ip, *_embedded_ipv4(ip)])


def _resolve_public(host):
    ips = {info[4][0] for info in socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)}
    if not ips or not all(ip_es_publica(ip) for ip in ips):
        raise ValueError("Destino no permitido (IP privada o interna)")
    return sorted(ips)[0]


def _port(parts):
    try:
        return parts.port or PORT
    except ValueError:
        raise ValueError("Puerto inválido")


URL_RULES = (
    (lambda parts: parts.scheme == "https", "Solo se permite HTTPS"),
    (lambda parts: bool(parts.hostname), "Host inválido"),
    (lambda parts: not (parts.username or parts.password), "La URL no puede incluir credenciales"),
    (lambda parts: _port(parts) == PORT, "Puerto no permitido"),
)


def validar(value, rules):
    for rule, message in rules:
        if not rule(value):
            raise ValueError(message)


def validar_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        raise ValueError("URL inválida")
    parts = urlsplit(url)
    validar(parts, URL_RULES)
    try:
        ip = _resolve_public(parts.hostname)
    except (OSError, UnicodeError):
        raise ValueError("No se pudo resolver el host")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, path, ip


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, port=PORT, timeout=TIMEOUT, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, PORT), TIMEOUT)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _consultar(host, path, ip):
    conn = _PinnedConnection(host, ip)
    try:
        conn.request("GET", path, headers={"Host": host, "User-Agent": "uptime-sentinel/1.0"})
        return conn.getresponse().status, None
    except (OSError, http.client.HTTPException) as e:
        return 0, str(e)
    finally:
        conn.close()


def _extraer_url(event):
    body = event.get("body", event) if isinstance(event, dict) else event
    if isinstance(body, str):
        body = json.loads(body or "{}")
    return body.get("url") if isinstance(body, dict) else None


def comprobar(url):
    host, path, ip = validar_url(url)
    start = time.time()
    status_code, error = _consultar(host, path, ip)
    return {
        "url": f"https://{host}{path}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status_code": status_code,
        "error": error,
        "elapsed_ms": int((time.time() - start) * 1000),
    }


def handler(event, context):
    try:
        return comprobar(_extraer_url(event))
    except ValueError as e:
        return {"status_code": 400, "error": str(e), "rechazado": True}