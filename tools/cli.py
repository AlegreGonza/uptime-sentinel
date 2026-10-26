import os
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

VERSION = "1.0.0"
API_URL = os.environ.get("UPTIME_API_URL", "")
API_KEY = os.environ.get("UPTIME_API_KEY", "")

app = typer.Typer(help="Consulta el monitor de disponibilidad uptime-sentinel.", no_args_is_help=True)
consola = Console()
errores = Console(stderr=True)


def _mostrar_version(valor: bool):
    if valor:
        typer.echo(f"uptime {VERSION}")
        raise typer.Exit()


@app.callback()
def principal(
    version: Annotated[bool, typer.Option("--version", callback=_mostrar_version, is_eager=True, help="Muestra la versión")] = False,
):
    pass


def _pedir(metodo, ruta, cuerpo=None, params=None):
    if not API_URL or not API_KEY:
        errores.print("[red]Faltan las variables UPTIME_API_URL y UPTIME_API_KEY[/]")
        raise typer.Exit(2)
    try:
        respuesta = httpx.request(
            metodo,
            API_URL.rstrip("/") + ruta,
            json=cuerpo,
            params=params,
            headers={"x-api-key": API_KEY},
            timeout=30,
        )
    except httpx.HTTPError as error:
        errores.print(f"[red]No se pudo conectar con la API:[/] {error}")
        raise typer.Exit(1)
    return respuesta.status_code, respuesta.json()


def _datos(status, cuerpo):
    if status >= 400:
        mensaje = cuerpo.get("error") or cuerpo.get("message", "respuesta inesperada")
        errores.print(f"[red]Error {status}:[/] {mensaje}")
        raise typer.Exit(1)
    return cuerpo


@app.command(help="Revisa una URL sin guardarla")
def probar(url: Annotated[str, typer.Argument(help="URL completa, con https://")]):
    datos = _datos(*_pedir("POST", "/probar", {"url": url}))
    tabla = Table(title="Resultado", show_header=False)
    for clave, valor in datos.items():
        tabla.add_row(clave, str(valor))
    consola.print(tabla)


@app.command(help="Guarda una URL para monitorearla cada 5 minutos")
def guardar(url: Annotated[str, typer.Argument(help="URL completa, con https://")]):
    datos = _datos(*_pedir("POST", "/objetivos", {"url": url}))
    tabla = Table(title="Objetivo guardado", show_header=False)
    for clave, valor in datos.items():
        tabla.add_row(clave, str(valor))
    consola.print(tabla)


@app.command(help="Muestra las URLs monitoreadas")
def listar():
    datos = _datos(*_pedir("GET", "/objetivos"))
    tabla = Table("URL", "Creado", "Activo")
    for objetivo in datos["objetivos"]:
        tabla.add_row(objetivo["url"], objetivo["created_at"], "sí" if objetivo["enabled"] else "no")
    consola.print(tabla)


@app.command(help="Muestra el historial de una URL")
def resultados(url: Annotated[str, typer.Argument(help="URL completa, con https://")]):
    datos = _datos(*_pedir("GET", "/resultados", params={"url": url}))
    tabla = Table("Fecha", "Status", "ms", "Error")
    for resultado in datos["resultados"]:
        tabla.add_row(
            resultado["timestamp"],
            str(resultado["status_code"]),
            str(resultado["elapsed_ms"]),
            resultado.get("error") or "-",
        )
    consola.print(tabla)


if __name__ == "__main__":
    app()