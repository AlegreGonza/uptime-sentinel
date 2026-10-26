import json
import logging
import os
import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKER_FUNCTION = os.environ["CHECKER_FUNCTION"]
TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
REQUIRED_FIELDS = ("url", "timestamp", "status_code", "error", "elapsed_ms")


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


def handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    lambda_client = boto3.client("lambda")
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
    return {"checked": checked, "rejected": rejected, "failed": failed}