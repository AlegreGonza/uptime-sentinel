import os

import httpx
import typer
from rich.console import Console
from rich.table import Table

VERSION = "1.0.0"
AYUDA_URL = "Full URL, including https://"
API_URL = os.environ.get("UPTIME_API_URL", "")
API_KEY = os.environ.get("UPTIME_API_KEY", "")

app = typer.Typer(help="Query the uptime-sentinel availability monitor.", no_args_is_help=True)
console = Console()
errors = Console(stderr=True)


def _show_version(value: bool):
    if value:
        typer.echo(f"uptime {VERSION}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(False, "--version", callback=_show_version, is_eager=True, help="Show the version"),
):
    pass


def _request(method, route, body=None, params=None):
    if not API_URL or not API_KEY:
        errors.print("[red]UPTIME_API_URL and UPTIME_API_KEY are missing[/]")
        raise typer.Exit(2)
    try:
        response = httpx.request(
            method,
            API_URL.rstrip("/") + route,
            json=body,
            params=params,
            headers={"x-api-key": API_KEY},
            timeout=30,
        )
    except httpx.HTTPError as error:
        errors.print(f"[red]Could not reach the API:[/] {error}")
        raise typer.Exit(1)
    try:
        body = response.json()
    except ValueError:
        errors.print(f"[red]The API returned something that is not JSON (status {response.status_code})[/]")
        raise typer.Exit(1)
    return response.status_code, body


def _data(status, body):
    if status >= 400:
        message = "unexpected response"
        if isinstance(body, dict):
            message = body.get("error") or body.get("message", message)
        errors.print(f"[red]Error {status}:[/] {message}")
        raise typer.Exit(1)
    return body


def _print_record(title, record):
    table = Table(title=title, show_header=False)
    for key, value in record.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command(help="Check a URL without saving it")
def probe(url: str = typer.Argument(..., help=AYUDA_URL)):
    data = _data(*_request("POST", "/probe", {"url": url}))
    _print_record("Result", data)


@app.command(help="Save a URL to monitor it every 5 minutes")
def save(
    url: str = typer.Argument(..., help=AYUDA_URL),
    expected_status: int = typer.Option(
        None, "--expected-status", help="Status code that counts as OK (default: any 2xx or 3xx)"
    ),
):
    body = {"url": url}
    if expected_status is not None:
        body["expected_status"] = expected_status
    data = _data(*_request("POST", "/targets", body))
    _print_record("Saved target", data)


@app.command("list", help="Show the monitored URLs")
def list_targets():
    data = _data(*_request("GET", "/targets"))
    table = Table("URL", "Created", "Enabled")
    for target in data.get("targets", []):
        table.add_row(target.get("url", "-"), target.get("created_at", "-"), "yes" if target.get("enabled") else "no")
    console.print(table)


@app.command(help="Show the history of a URL")
def results(url: str = typer.Argument(..., help=AYUDA_URL)):
    data = _data(*_request("GET", "/results", params={"url": url}))
    table = Table("Date", "Status", "ms", "Error")
    for result in data.get("results", []):
        table.add_row(
            str(result.get("timestamp", "-")),
            str(result.get("status_code", "-")),
            str(result.get("elapsed_ms", "-")),
            result.get("error") or "-",
        )
    console.print(table)


if __name__ == "__main__":
    app()