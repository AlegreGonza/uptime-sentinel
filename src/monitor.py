import json
import logging
import os
from collections.abc import Callable, Iterator
from itertools import takewhile

import boto3
from boto3.dynamodb.conditions import Attr, Key

from riesgo import risk_index

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKER_FUNCTION = os.environ["CHECKER_FUNCTION"]
TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
ALERTS_TOPIC = os.environ["ALERTS_TOPIC"]
REQUIRED_FIELDS = ("url", "timestamp", "status_code", "error", "elapsed_ms")
UMBRAL_CAIDA = 3
CANTIDAD_HISTORIAL = UMBRAL_CAIDA + 1
UMBRAL_RIESGO = 50
CANTIDAD_HISTORIAL_RIESGO = 60


def _paginar(consulta: Callable, **kwargs) -> Iterator[dict]:
    """Recorre todas las páginas de scan() o query() de DynamoDB."""
    while True:
        pagina = consulta(**kwargs)
        yield from pagina.get("Items", [])
        if "LastEvaluatedKey" not in pagina:
            return
        kwargs["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]


def _ejecutar_paso(nombre: str, url: str, funcion: Callable, *args):
    """Ejecuta un paso del chequeo; si falla, lo registra y devuelve None."""
    try:
        return funcion(*args)
    except Exception:
        logger.exception("Falló %s para %s", nombre, url)
        return None


def _publicar(sns_client, asunto: str, mensaje: str) -> None:
    sns_client.publish(TopicArn=ALERTS_TOPIC, Subject=asunto, Message=mensaje)


def _objetivos_habilitados(tabla):
    for item in _paginar(tabla.scan, FilterExpression=Attr("enabled").eq(True)):
        url = item.get("url")
        if url:
            yield url
        else:
            logger.warning("Objetivo sin url: %s", item)


def _invocar_checker(lambda_client, url):
    respuesta = lambda_client.invoke(
        FunctionName=CHECKER_FUNCTION,
        Payload=json.dumps({"url": url}),
    )
    cuerpo = json.loads(respuesta["Payload"].read())
    if respuesta.get("FunctionError"):
        raise RuntimeError(f"El checker falló: {cuerpo}")
    return cuerpo


def _validar_registro(registro):
    faltan = [campo for campo in REQUIRED_FIELDS if campo not in registro]
    if faltan:
        raise ValueError(f"Respuesta del checker incompleta, faltan: {faltan}")


def _revisar_objetivo(lambda_client, tabla_resultados, url):
    resultado = _invocar_checker(lambda_client, url)
    if resultado.get("rechazado"):
        return "rechazado"
    _validar_registro(resultado)
    tabla_resultados.put_item(Item=resultado)
    return "revisado"


def _ultimos_chequeos(tabla_resultados, url, cantidad):
    """Devuelve los chequeos más recientes primero."""
    return list(
        _paginar(
            tabla_resultados.query,
            KeyConditionExpression=Key("url").eq(url),
            ScanIndexForward=False,
            Limit=cantidad,
            ConsistentRead=True,
        )
    )


def _fallo(registro):
    return registro.get("error") is not None or registro.get("status_code") != 200


def _tipo_alerta(fallos):
    if len(fallos) == CANTIDAD_HISTORIAL and not fallos[0] and all(fallos[1:]):
        return "RECUPERADO"
    consecutivos = len(list(takewhile(bool, fallos)))
    return {1: "PRE-ALERTA", UMBRAL_CAIDA: "CAÍDA"}.get(consecutivos)


def _mensaje_alerta(tipo, url):
    mensajes = {
        "PRE-ALERTA": f"{url} falló en el último chequeo. Si falla {UMBRAL_CAIDA} veces seguidas, se envía alerta de caída.",
        "CAÍDA": f"{url} falló {UMBRAL_CAIDA} chequeos seguidos.",
        "RECUPERADO": f"{url} volvió a responder correctamente.",
    }
    return mensajes[tipo]


def _evaluar_alertas(sns_client, tabla_resultados, url):
    chequeos = _ultimos_chequeos(tabla_resultados, url, CANTIDAD_HISTORIAL)
    tipo = _tipo_alerta([_fallo(c) for c in chequeos])
    if tipo:
        _publicar(sns_client, f"{tipo} uptime", _mensaje_alerta(tipo, url))


def _evaluar_riesgo(sns_client, tabla_resultados, url):
    historial = _ultimos_chequeos(tabla_resultados, url, CANTIDAD_HISTORIAL_RIESGO)
    actual = risk_index(historial)
    anterior = risk_index(historial[1:])
    cruzo_umbral = (
        actual is not None
        and anterior is not None
        and anterior["index"] < UMBRAL_RIESGO <= actual["index"]
    )
    if cruzo_umbral:
        _publicar(
            sns_client,
            "RIESGO uptime",
            f"{url} superó el índice de riesgo {UMBRAL_RIESGO}: ahora es {actual['index']}.",
        )


def handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    lambda_client = boto3.client("lambda")
    sns_client = boto3.client("sns")
    targets = dynamodb.Table(TARGETS_TABLE)
    results = dynamodb.Table(RESULTS_TABLE)
    checked = rejected = failed = 0
    for url in _objetivos_habilitados(targets):
        estado = _ejecutar_paso("la revisión", url, _revisar_objetivo, lambda_client, results, url)
        if estado is None:
            failed += 1
            continue
        if estado == "rechazado":
            rejected += 1
            continue
        checked += 1
        _ejecutar_paso("la evaluación de alertas", url, _evaluar_alertas, sns_client, results, url)
        _ejecutar_paso("la evaluación de riesgo", url, _evaluar_riesgo, sns_client, results, url)
    return {"checked": checked, "rejected": rejected, "failed": failed}