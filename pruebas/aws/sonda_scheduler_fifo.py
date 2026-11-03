"""Sonda 2 del nivel 2: EventBridge Scheduler hacia la cola SQS FIFO cifrada con KMS.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre el disparo de las
tareas programadas (sección "Scheduled tasks" de "Operations"):

  1. Un horario `at(...)` con zona horaria entrega en la cola FIFO cifrada con una clave de KMS propia,
     con el rol de ejecución (sqs:SendMessage + kms:GenerateDataKey y kms:Decrypt) y la política de
     la clave por defecto, y el mensaje llega con su MessageGroupId (el chat de la tarea).
  2. El cuerpo puede llevar un identificador distinto en cada disparo con los atributos de contexto
     de Scheduler (<aws.scheduler.execution-id>, <aws.scheduler.scheduled-time>), así que la
     deduplicación por contenido de la FIFO no se come disparos seguidos.
  3. Con un rol sin permisos de KMS, el mensaje no llega a la FIFO y acaba en la cola de mensajes
     fallidos del horario, con el error.

Crea todo con el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2 y lo borra al acabar,
aunque algo falle; la clave de KMS queda con el borrado programado a 7 días (el mínimo).
Coste: céntimos (la clave se cobra por horas hasta que se programa su borrado). Uso:

    SONDA_REGION=eu-north-1 python sonda_scheduler_fifo.py
    SONDA_REGION=eu-north-1 python sonda_scheduler_fifo.py --conservar
"""
import datetime as dt
import json
import os
import sys
import time
import uuid
import zoneinfo

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
ZONA = "Europe/Madrid"

ses = boto3.Session(region_name=REGION)
sts, iam, kms, sqs, sch = (ses.client(x) for x in ("sts", "iam", "kms", "sqs", "scheduler"))
creado = {}
resultados = []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def rol(nombre, cuenta, acciones_kms):
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": cuenta}}}]}
    iam.create_role(RoleName=nombre, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado.setdefault("roles", []).append(nombre)
    sentencias = [{"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": [creado["fifo_arn"], creado["dlq_arn"]]}]
    if acciones_kms:
        sentencias.append({"Effect": "Allow", "Action": acciones_kms, "Resource": creado["clave_arn"]})
    iam.put_role_policy(RoleName=nombre, PolicyName="envio",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": sentencias}))
    return f"arn:aws:iam::{cuenta}:role/{nombre}"


def crear():
    cuenta = sts.get_caller_identity()["Account"]
    print("cuenta", cuenta, flush=True)
    clave = kms.create_key(Description=NOMBRE, Tags=[{"TagKey": "sonda", "TagValue": "nivel2"}])["KeyMetadata"]
    creado["clave"], creado["clave_arn"] = clave["KeyId"], clave["Arn"]

    fifo = sqs.create_queue(QueueName=f"{NOMBRE}.fifo", tags={"sonda": "nivel2"}, Attributes={
        "FifoQueue": "true", "ContentBasedDeduplication": "true", "KmsMasterKeyId": clave["KeyId"]})["QueueUrl"]
    creado["fifo"] = fifo
    creado["fifo_arn"] = sqs.get_queue_attributes(QueueUrl=fifo, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    dlq = sqs.create_queue(QueueName=f"{NOMBRE}-dlq", tags={"sonda": "nivel2"})["QueueUrl"]
    creado["dlq"] = dlq
    creado["dlq_arn"] = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    bueno = rol(f"{NOMBRE}-bueno", cuenta, ["kms:GenerateDataKey", "kms:Decrypt"])
    sin_kms = rol(f"{NOMBRE}-sinkms", cuenta, None)
    print("esperando 15 s a que IAM propague los roles…", flush=True)
    time.sleep(15)
    return bueno, sin_kms


def horario(nombre, expresion, rol_arn, chat, etiqueta):
    cuerpo = json.dumps({"tipo": "tarea", "tarea": etiqueta, "chat": chat,
                         "run_id": "<aws.scheduler.execution-id>", "programada": "<aws.scheduler.scheduled-time>"})
    sch.create_schedule(Name=nombre, ScheduleExpression=expresion, ScheduleExpressionTimezone=ZONA,
                        FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="NONE",
                        Target={"Arn": creado["fifo_arn"], "RoleArn": rol_arn, "Input": cuerpo,
                                "SqsParameters": {"MessageGroupId": chat},
                                "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60},
                                "DeadLetterConfig": {"Arn": creado["dlq_arn"]}})
    creado.setdefault("horarios", []).append(nombre)


def recoger(url, segundos, atributos=True):
    """Lee la cola durante unos segundos y devuelve los mensajes (borrándolos)."""
    fin, vistos = time.time() + segundos, []
    while time.time() < fin:
        r = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=10,
                                AttributeNames=["All"], MessageAttributeNames=["All"])
        for m in r.get("Messages", []):
            vistos.append((time.time(), m))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return vistos


def probar(bueno, sin_kms):
    chat_a, chat_c, chat_b = (str(uuid.uuid4()) for _ in range(3))
    # Scheduler admite at() con precisión de minuto: el siguiente minuto entero con al menos 70 s de margen.
    ahora = dt.datetime.now(dt.timezone.utc).astimezone(zoneinfo.ZoneInfo(ZONA))
    cuando = (ahora + dt.timedelta(seconds=130)).replace(second=0, microsecond=0)
    horario(f"{NOMBRE}-at", f"at({cuando.strftime('%Y-%m-%dT%H:%M:%S')})", bueno, chat_a, "at")
    horario(f"{NOMBRE}-rate", "rate(1 minute)", bueno, chat_c, "rate")
    horario(f"{NOMBRE}-sinkms", f"at({cuando.strftime('%Y-%m-%dT%H:%M:%S')})", sin_kms, chat_b, "sinkms")
    print(f"horarios creados; el at() salta a las {cuando:%H:%M} ({ZONA}). Leyendo la cola 5 minutos…", flush=True)

    vistos = recoger(creado["fifo"], 300)
    sch.delete_schedule(Name=f"{NOMBRE}-rate")
    creado["horarios"].remove(f"{NOMBRE}-rate")
    cuerpos = [(t, json.loads(m["Body"]), m["Attributes"]) for t, m in vistos]
    for t, c, a in cuerpos:
        print(f"     {dt.datetime.fromtimestamp(t):%H:%M:%S} tarea={c['tarea']} grupo={a.get('MessageGroupId')[:8]}… "
              f"run_id={c['run_id'][:13]}… programada={c['programada']}", flush=True)

    de_a = [x for x in cuerpos if x[1]["tarea"] == "at"]
    anota("at() con zona horaria entrega en la FIFO cifrada", len(de_a) == 1, f"{len(de_a)} mensaje(s)")
    if de_a:
        t, c, a = de_a[0]
        anota("El mensaje llega con MessageGroupId = chat de la tarea", a.get("MessageGroupId") == chat_a)
        anota("Los atributos de contexto se sustituyen en el cuerpo",
              "<aws.scheduler" not in json.dumps(c) and len(c["run_id"]) >= 32, f"run_id={c['run_id']}")
        retraso = t - cuando.timestamp()
        anota("Llega en el minuto programado", 0 <= retraso < 60, f"{retraso:.0f} s tras la hora")
    de_c = [x for x in cuerpos if x[1]["tarea"] == "rate"]
    ids = {c["run_id"] for _, c, _ in de_c}
    anota("rate(1 minute): cada disparo llega, sin que la deduplicación por contenido se coma ninguno",
          len(de_c) >= 3 and len(ids) == len(de_c), f"{len(de_c)} mensajes, {len(ids)} run_id distintos")
    anota("Rol sin KMS: no llega nada a la FIFO", not [x for x in cuerpos if x[1]["tarea"] == "sinkms"])

    fallidos = recoger(creado["dlq"], 180)  # Scheduler tarda en dejarlo en la cola de fallidos
    for _, m in fallidos:
        print("     en la cola de fallidos:", m["Body"][:300],
              {k: (v.get("StringValue") or "")[:120] for k, v in m.get("MessageAttributes", {}).items()}, flush=True)
    del_b = [m for _, m in fallidos if "sinkms" in m["Body"] or "sinkms" in json.dumps(m.get("MessageAttributes", {}))]
    if del_b:
        attrs = {k: v.get("StringValue") for k, v in del_b[0].get("MessageAttributes", {}).items()}
        print("     atributos del fallido:", {k: (v or "")[:160] for k, v in attrs.items()}, flush=True)
        texto = json.dumps(attrs)
        anota("Rol sin KMS: el disparo acaba en la cola de fallidos con el error",
              "KMS" in texto or "kms" in texto, attrs.get("ERROR_CODE", ""))
    else:
        anota("Rol sin KMS: el disparo acaba en la cola de fallidos con el error", False, "no llegó nada a la DLQ")


def intenta(desc, fn):
    try:
        fn()
    except ClientError as e:
        print(f"   aviso al borrar {desc}:", e.response["Error"]["Code"], flush=True)


def borrar():
    print("borrando…", flush=True)
    for h in creado.get("horarios", []):
        intenta(h, lambda h=h: sch.delete_schedule(Name=h))
    for q in ("fifo", "dlq"):
        if q in creado:
            intenta(q, lambda q=q: sqs.delete_queue(QueueUrl=creado[q]))
    for r in creado.get("roles", []):
        intenta(r, lambda r=r: iam.delete_role_policy(RoleName=r, PolicyName="envio"))
        intenta(r, lambda r=r: iam.delete_role(RoleName=r))
    if "clave" in creado:
        intenta("clave", lambda: kms.schedule_key_deletion(KeyId=creado["clave"], PendingWindowInDays=7))


if __name__ == "__main__":
    conservar = "--conservar" in sys.argv
    try:
        roles = crear()
        probar(*roles)
    finally:
        if not conservar:
            borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
