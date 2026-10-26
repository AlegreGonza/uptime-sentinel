import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHECKER_FUNCTION = os.environ["CHECKER_FUNCTION"]
TARGETS_TABLE = os.environ["TARGETS_TABLE"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]
MAX_RESULTS = 100
HEADERS = {"Content-Type": "application/json"}

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
tabla_objetivos = dynamodb.Table(TARGETS_TABLE)
tabla_resultados = dynamodb.Table(RESULTS_TABLE)


class ErrorCliente(Exception):
    def __init__(self, status, cuerpo):
        super().__init__(str(cuerpo))
        self.status = status
        self.cuerpo = cuerpo


def _a_json(valor):
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    raise TypeError(f"Tipo no serializable: {type(valor)}")


def _respuesta(status, cuerpo):
    return {
        "statusCode": status,
        "headers": HEADERS,
        "body": json.dumps(cuerpo, ensure_ascii=False, default=_a_json),
    }


def _leer_url(event):
    try:
        url = json.loads(event.get("body") or "{}")["url"].strip()
        if not url:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ErrorCliente(400, {"error": "Falta la URL"})
    return url


def _verificar_respuesta(respuesta, cuerpo):
    if respuesta.get("FunctionError"):
        logger.error("El checker falló: %s", cuerpo)
        raise RuntimeError("El checker falló al revisar la URL")
    if cuerpo.get("rechazado"):
        raise ErrorCliente(400, cuerpo)
    return cuerpo


def _invocar_checker(url):
    try:
        respuesta = lambda_client.invoke(
            FunctionName=CHECKER_FUNCTION,
            Payload=json.dumps({"url": url}),
        )
        cuerpo = json.loads(respuesta["Payload"].read())
    except ClientError:
        logger.exception("No se pudo invocar al checker")
        raise RuntimeError("No se pudo consultar el checker")
    return _verificar_respuesta(respuesta, cuerpo)


def _probar(event):
    return _respuesta(200, _invocar_checker(_leer_url(event)))


def _guardar(event):
    resultado = _invocar_checker(_leer_url(event))
    objetivo = {
        "url": resultado["url"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "enabled": True,
    }
    try:
        tabla_objetivos.put_item(
            Item=objetivo,
            ConditionExpression="attribute_not_exists(#u)",
            ExpressionAttributeNames={"#u": "url"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise ErrorCliente(409, {"error": "La URL ya está guardada", "url": objetivo["url"]})
    return _respuesta(201, objetivo)


def _listar(event):
    objetivos = []
    kwargs = {}
    while True:
        pagina = tabla_objetivos.scan(**kwargs)
        objetivos.extend(pagina.get("Items", []))
        if "LastEvaluatedKey" not in pagina:
            break
        kwargs["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]
    return _respuesta(200, {"objetivos": objetivos})


def _resultados(event):
    url = ((event.get("queryStringParameters") or {}).get("url") or "").strip()
    if not url:
        raise ErrorCliente(400, {"error": "Falta el parámetro url"})
    respuesta = tabla_resultados.query(
        KeyConditionExpression=Key("url").eq(url),
        ScanIndexForward=False,
        Limit=MAX_RESULTS,
    )
    return _respuesta(200, {"resultados": respuesta.get("Items", [])})


RUTAS = {
    ("POST", "/probar"): _probar,
    ("POST", "/objetivos"): _guardar,
    ("GET", "/objetivos"): _listar,
    ("GET", "/resultados"): _resultados,
}


def handler(event, context):
    funcion = RUTAS.get((event.get("httpMethod"), event.get("path")))
    if funcion is None:
        return _respuesta(404, {"error": "Ruta no encontrada"})
    try:
        return funcion(event)
    except ErrorCliente as e:
        return _respuesta(e.status, e.cuerpo)
    except RuntimeError as e:
        return _respuesta(502, {"error": str(e)})
    except Exception:
        logger.exception("Error inesperado en %s %s", event.get("httpMethod"), event.get("path"))
        return _respuesta(500, {"error": "Error interno del servidor"})