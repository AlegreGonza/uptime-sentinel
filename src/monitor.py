import json
import logging
import os
import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKER_FUNCTION = os.environ["CHECKER_FUNCTION"]
TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
ALERTS_TOPIC = os.environ["ALERTS_TOPIC"]
REQUIRED_FIELDS = ("url", "timestamp", "status_code", "error", "elapsed_ms")
UMBRAL_CAIDA = 3
CANTIDAD_HISTORIAL = UMBRAL_CAIDA + 1


def _objetivos_habilitados(tabla):
    kwargs = {"FilterExpression": Attr("enabled").eq(True)}
    while True:
        page = tabla.scan(**kwargs)
        for item in page.get("Items", []):
            url = item.get("url")
            if url:
                yield url
            else:
                logger.warning("Objetivo sin url: %s", item)
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


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


def _fallo(registro):
    return registro.get("error") is not None or registro.get("status_code") != 200


def _historial_reciente(tabla_resultados, url):
    respuesta = tabla_resultados.query(
        KeyConditionExpression=Key("url").eq(url),
        ScanIndexForward=False,
        Limit=CANTIDAD_HISTORIAL,
        ConsistentRead=True,
    )
    return [_fallo(item) for item in respuesta.get("Items", [])]


def _evaluar_alertas(sns_client, tabla_resultados, url):
    fallos = _historial_reciente(tabla_resultados, url)
    consecutivos = 0
    for fallo in fallos:
        if not fallo:
            break
        consecutivos += 1
    if consecutivos == 1:
        sns_client.publish(
            TopicArn=ALERTS_TOPIC,
            Subject="PRE-ALERTA uptime",
            Message=f"{url} falló en el último chequeo. Si falla {UMBRAL_CAIDA} veces seguidas, se envía alerta de caída.",
        )
    elif consecutivos == UMBRAL_CAIDA:
        sns_client.publish(
            TopicArn=ALERTS_TOPIC,
            Subject="CAÍDA uptime",
            Message=f"{url} falló {UMBRAL_CAIDA} chequeos seguidos.",
        )
    elif consecutivos == 0 and len(fallos) == CANTIDAD_HISTORIAL and all(fallos[1:]):
        sns_client.publish(
            TopicArn=ALERTS_TOPIC,
            Subject="RECUPERADO uptime",
            Message=f"{url} volvió a responder correctamente.",
        )


def handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    lambda_client = boto3.client("lambda")
    sns_client = boto3.client("sns")
    targets = dynamodb.Table(TARGETS_TABLE)
    results = dynamodb.Table(RESULTS_TABLE)
    checked = 0
    rejected = 0
    failed = 0
    for url in _objetivos_habilitados(targets):
        try:
            resultado = _invocar_checker(lambda_client, url)
            if resultado.get("rechazado"):
                rejected += 1
                continue
            _validar_registro(resultado)
            results.put_item(Item=resultado)
            checked += 1
        except Exception:
            logger.exception("Falló la revisión de %s", url)
            failed += 1
            continue
        try:
            _evaluar_alertas(sns_client, results, url)
        except Exception:
            logger.exception("Falló la evaluación de alertas para %s", url)
    return {"checked": checked, "rejected": rejected, "failed": failed}