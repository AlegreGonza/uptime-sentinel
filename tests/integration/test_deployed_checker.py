import json
import os

import boto3
import pytest
from botocore.exceptions import ClientError

FUNCTION = os.environ.get("CHECKER_FUNCTION", "uptime-checker-dev")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def invoke():
    client = boto3.client("lambda", region_name="us-east-1")
    try:
        client.get_function(FunctionName=FUNCTION)
    except ClientError:
        pytest.skip(f"function {FUNCTION} is not deployed")

    def _invoke(payload):
        response = client.invoke(
            FunctionName=FUNCTION,
            Payload=json.dumps(payload).encode(),
        )
        return json.loads(response["Payload"].read())

    return _invoke


UNSAFE_URLS = [
    "http://example.com/",
    "ftp://example.com/",
    "file:///etc/passwd",
    "gopher://127.0.0.1/",
    "//127.0.0.1/",
    "https:///path",
    "https://",
    "",
    "https://" + "a" * 2100 + ".example.com/",
    "https://user:pass@example.com/",
    "https://user@example.com/",
    "https://example.com:8443/",
    "https://example.com:80/",
    "https://127.0.0.1/",
    "https://127.0.0.1:443/",
    "https://127.1/",
    "https://0/",
    "https://0.0.0.0/",
    "https://0177.0.0.1/",
    "https://0x7f.0.0.1/",
    "https://2130706433/",
    "https://%31%32%37.0.0.1/",
    "https://example.com@127.0.0.1/",
    "https://example.com\\@127.0.0.1/",
    "https://127.0.0.1\\@example.com/",
    "https://10.0.0.1/",
    "https://172.16.0.1/",
    "https://192.168.1.1/",
    "https://100.64.0.1/",
    "https://169.254.169.254/",
    "https://169.254.169.254/latest/meta-data/",
    "https://192.0.0.1/",
    "https://192.0.2.1/",
    "https://198.18.0.1/",
    "https://198.51.100.1/",
    "https://203.0.113.1/",
    "https://224.0.0.1/",
    "https://240.0.0.1/",
    "https://255.255.255.255/",
    "https://[::1]/",
    "https://[::]/",
    "https://[fe80::1]/",
    "https://[fc00::1]/",
    "https://[fec0::1]/",
    "https://[ff02::1]/",
    "https://[2001:db8::1]/",
    "https://[100::1]/",
    "https://[::ffff:127.0.0.1]/",
    "https://[::ffff:169.254.169.254]/",
    "https://[::7f00:1]/",
    "https://[::a00:1]/",
    "https://[64:ff9b::7f00:1]/",
    "https://[64:ff9b::a9fe:a9fe]/",
    "https://[64:ff9b:1::7f00:1]/",
    "https://[2002:7f00:1::]/",
    "https://[2002:a9fe:a9fe::]/",
    "https://[2001:0:4136:e378:8000:63bf:3f57:fffe]/",
    "https://[5f00::1]/",
    "https://localhost/",
    "https://LOCALHOST/",
    "https://localhost./",
    "https://localtest.me/",
    "https://127.0.0.1.nip.io/",
    "https://127.0.0.1.sslip.io/",
    "https://169.254.169.254.nip.io/",
    "https://169.254.169.254.sslip.io/",
    "https://10.0.0.1.nip.io/",
    "https://no-existe-xyz.example/",
    "https://nonexistent-host.invalid/",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_rejects_unsafe_urls(invoke, url):
    assert invoke({"url": url})["status_code"] == 400


@pytest.mark.parametrize("payload", [
    {},
    {"url": None},
    {"url": 123},
    {"url": ["https://example.com/"]},
    {"otra_clave": "https://example.com/"},
])
def test_rejects_malformed_payloads(invoke, payload):
    assert invoke(payload)["status_code"] == 400


@pytest.mark.parametrize("url", [
    "https://www.google.com/",
    "https://example.com/",
    "https://www.wikipedia.org/",
    "https://example.com#@127.0.0.1/",
])
def test_accepts_and_checks_public_sites(invoke, url):
    result = invoke({"url": url})
    assert result["error"] is None
    assert 200 <= result["status_code"] < 400