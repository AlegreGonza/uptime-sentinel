import errno
import http.client
import ipaddress
import json
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

TIMEOUT = 10
FETCH_TIMEOUT = 15
DNS_TIMEOUT = 5
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


def is_public_ip(text):
    if "%" in text:
        return False
    ip = ipaddress.ip_address(text)
    return all(_is_public_unicast(candidate) for candidate in [ip, *_embedded_ipv4(ip)])


def _resolve_all(host):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        futuro = pool.submit(socket.getaddrinfo, host, PORT, 0, socket.SOCK_STREAM)
        return futuro.result(timeout=DNS_TIMEOUT)
    except FuturesTimeout:
        raise ValueError("Could not resolve the host")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _resolve_public(host):
    ips = {info[4][0] for info in _resolve_all(host)}
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise ValueError("Destination not allowed (private or internal IP)")
    return min(ips)


def _port(parts):
    try:
        return parts.port or PORT
    except ValueError:
        raise ValueError("Invalid port")


URL_RULES = (
    (lambda parts: parts.scheme == "https", "Only HTTPS is allowed"),
    (lambda parts: bool(parts.hostname), "Invalid host"),
    (lambda parts: not (parts.username or parts.password), "The URL cannot include credentials"),
    (lambda parts: _port(parts) == PORT, "Port not allowed"),
)


def validate(value, rules):
    for rule, message in rules:
        if not rule(value):
            raise ValueError(message)


def validate_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        raise ValueError("Invalid URL")
    try:
        parts = urlsplit(url)
        _ = parts.port  # also validates the port
    except ValueError:
        raise ValueError("Invalid URL") from None
    validate(parts, URL_RULES)
    host = parts.hostname.rstrip(".")
    try:
        ip = _resolve_public(host)
    except (OSError, UnicodeError):
        raise ValueError("Could not resolve the host")
    path = quote((parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                 safe="/?=&%:@!$'()*+,;~-._")
    return host, path, ip


def _ms(start):
    return int((time.perf_counter() - start) * 1000)


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, port=PORT, timeout=TIMEOUT, context=ssl.create_default_context())
        self._ip = ip
        self.timings = {}

    def connect(self):
        start = time.perf_counter()
        sock = socket.create_connection((self._ip, PORT), TIMEOUT)
        self.timings["connect_ms"] = _ms(start)
        start = time.perf_counter()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        self.timings["tls_ms"] = _ms(start)


def _error_message(e):
    """Maps a connection error to a short, fixed message without internal details."""
    if isinstance(e, TimeoutError):
        return "The site did not respond in time"
    if isinstance(e, ssl.SSLError):
        return "The HTTPS certificate is not valid"
    if isinstance(e, ConnectionRefusedError):
        return "The site refused the connection (nothing is listening on port 443)"
    if isinstance(e, OSError) and e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "The site is unreachable from the checker"
    if isinstance(e, OSError):
        return "Could not connect to the site"
    return "The site sent an invalid response"


def _fetch_once(conn, host, path):
    try:
        conn.connect()
        start = time.perf_counter()
        conn.request("GET", path, headers={"Host": host, "User-Agent": "uptime-sentinel/1.0"})
        status = conn.getresponse().status
        conn.timings["response_ms"] = _ms(start)
        return status, None, conn.timings
    except (OSError, http.client.HTTPException) as e:
        return 0, _error_message(e), conn.timings
    finally:
        conn.close()


def _abort(conn):
    """Unblocks a request stuck in recv() by shutting down its socket.

    Python cannot stop a thread from outside, but shutting down the socket makes the
    blocked recv() return at once, so the worker thread ends instead of waiting for
    the socket timeout.
    """
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    conn.close()


def _fetch(host, path, ip):
    """Runs the request with a total deadline; on timeout the socket is aborted."""
    conn = _PinnedConnection(host, ip)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_fetch_once, conn, host, path)
        return future.result(timeout=FETCH_TIMEOUT)
    except FuturesTimeout:
        _abort(conn)
        return 0, "The site did not respond in time", conn.timings
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _extract_url(event):
    body = event.get("body", event) if isinstance(event, dict) else event
    if isinstance(body, str):
        try:
            body = json.loads(body or "{}")
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON body") from None
    return body.get("url") if isinstance(body, dict) else None


def check_url(url):
    dns_start = time.perf_counter()
    host, path, ip = validate_url(url)
    dns_ms = _ms(dns_start)
    start = time.time()
    status_code, error, timings = _fetch(host, path, ip)
    return {
        "url": f"https://{host}{path}",
        "timestamp": datetime.now(UTC).isoformat(),
        "status_code": status_code,
        "error": error,
        "elapsed_ms": int((time.time() - start) * 1000),
        "dns_ms": dns_ms,
        "connect_ms": timings.get("connect_ms", 0),
        "tls_ms": timings.get("tls_ms", 0),
        "response_ms": timings.get("response_ms", 0),
    }


def handler(event, context):
    try:
        return check_url(_extract_url(event))
    except ValueError as e:
        return {"status_code": 400, "error": str(e), "rejected": True}