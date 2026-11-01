"""Sonda 12 del nivel 2: EventBridge Scheduler → cola SQS estándar cifrada → consumidor de tareas de `api`.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre el consumidor de la cola de
tareas (sección "Tareas programadas" de Operación, "Lanzar" y "Sin ejecuciones duplicadas"):

  1. El cuerpo con <aws.scheduler.scheduled-time> trae la hora programada, no la de entrega; dos disparos
     de un rate(1 minute) dan run_id distintos. Intenta provocar reintentos de Scheduler con cinco fallos
     que se arreglan a los 75 s (rol sin KMS, clave desactivada, cola que aún no existe, rol borrado tras
     crear el horario, confianza rota): el 1-11-2026 Scheduler no reintentó ninguno (los tres primeros, a la
     cola de fallidos con RETRY_ATTEMPTS 0; los dos de rol, perdidos, porque la cola de fallidos se escribe
     con el mismo rol: solo lo cuenta la métrica InvocationsFailedToBeSentToDeadLetterCount).
  2. Una entrega repetida (el mismo cuerpo dos veces, y un mensaje cuya ejecución ya se creó y no se
     borró) no crea una segunda ejecución: la transacción falla por "ya existe" y el consumidor lo borra.
  3. Chat ocupado: ChangeMessageVisibility de 60 a 120 s y, al liberar el chat, se lanza; un disparo con
     más de 30 minutos de retraso se salta y queda anotado en la tarea.
  4. Parada limpia: SIGTERM a mitad de un lote; lo no borrado reaparece y se procesa una vez. Con SIGKILL
     a mitad de un lanzamiento, la reentrega ve "ya existe" y la ejecución queda en pendiente (la vigilante).
  5. Ráfaga: 50 horarios at() a la misma hora con 2 consumidores a 5 arranques/s; y 50 mensajes de golpe.
  6. Un mensaje que el consumidor siempre falla acaba en la DLQ tras 8 entregas.

"Lanzar" es aquí escribir la ejecución (RUN#{run_id} + ejecucion_activa del chat, en una transacción) y
esperar unos segundos; no hace falta AgentCore. El consumidor es este mismo fichero con el subcomando
`consumidor`, que la sonda arranca como subproceso (para poder mandarle SIGTERM).

Crea todo con el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2 y lo borra al acabar, aunque algo
falle; las claves de KMS quedan con el borrado programado a 7 días. Coste: céntimos. Unos 20 minutos. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_tareas_cola.py
    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_tareas_cola.py --solo-1   # solo Scheduler

Los registros de los consumidores van al directorio temporal del sistema (o a SONDA_LOGS).
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import queue
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
UTC = dt.timezone.utc
RETRASO_MAX = 30 * 60  # un disparo con más retraso se salta


def ahora_iso(delta=0):
    return (dt.datetime.now(UTC) + dt.timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sufijo_tarea(tarea, hora):
    """Igual que runid_coste.py: 26 primeros de base32 (RFC 4648) de SHA-256(tarea|hora)."""
    return base64.b32encode(hashlib.sha256(f"{tarea}|{hora}".encode()).digest()).decode()[:26]


def run_id(oid, tarea, hora):
    return f"{uuid.UUID(oid).hex}.{sufijo_tarea(tarea, hora)}"


def hora(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ----------------------------------------------------------------------------------------------------
# Consumidor (lo que hará el de `api`)
# ----------------------------------------------------------------------------------------------------

class Limitador:
    """Arranques por segundo de la réplica: cada arranque reserva el siguiente hueco de 1/tasa s."""

    def __init__(self, tasa):
        self.paso, self.siguiente, self.cerrojo = 1.0 / tasa, 0.0, threading.Lock()

    def espera(self):
        with self.cerrojo:
            t = max(time.time(), self.siguiente)
            self.siguiente = t + self.paso
        time.sleep(max(0.0, t - time.time()))


def consumidor(a):
    ses = boto3.Session(region_name=REGION)
    cfg = Config(max_pool_connections=32)
    sqs, ddb = ses.client("sqs", config=cfg), ses.client("dynamodb", config=cfg)
    parar = threading.Event()
    pendientes = queue.Queue()  # recibidos y aún no empezados
    limitador = Limitador(a.tasa)
    salida = open(a.log, "a", buffering=1)
    cerrojo = threading.Lock()
    espera_min, espera_max = (int(x) for x in a.espera_ocupado.split(","))

    def log(ev, **kw):
        with cerrojo:
            salida.write(json.dumps({"t": time.time(), "c": a.nombre, "ev": ev, **kw}) + "\n")

    def visibilidad(m, segundos):
        sqs.change_message_visibility(QueueUrl=a.cola, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=segundos)

    def borrar(m):
        sqs.delete_message(QueueUrl=a.cola, ReceiptHandle=m["ReceiptHandle"])

    def procesar(m):
        rc = int(m["Attributes"]["ApproximateReceiveCount"])
        cuerpo = json.loads(m["Body"])
        tarea, programada = cuerpo["tarea"], cuerpo["programada"]
        retraso = time.time() - hora(programada)
        if retraso > RETRASO_MAX:
            ddb.update_item(TableName=a.tabla, Key={"pk": {"S": f"TAREA#{tarea}"}},
                            UpdateExpression="ADD saltados :uno SET ultimo_saltado = :h",
                            ExpressionAttributeValues={":uno": {"N": "1"}, ":h": {"S": programada}})
            borrar(m)
            log("saltado", tarea=tarea, programada=programada, retraso=round(retraso), rc=rc)
            return
        t = ddb.get_item(TableName=a.tabla, Key={"pk": {"S": f"TAREA#{tarea}"}}, ConsistentRead=True).get("Item")
        if not t:
            borrar(m)
            log("sin_tarea", tarea=tarea, rc=rc)
            return
        oid, chat = t["oid"]["S"], t["chat"]["S"]
        rid = run_id(oid, tarea, programada)
        if t.get("falla", {}).get("BOOL"):
            # Un fallo inesperado: no se borra; en la sonda vuelve en `vis_fallo` s (en `api`, tras el visibility timeout).
            visibilidad(m, a.vis_fallo)
            log("fallo", tarea=tarea, run_id=rid, rc=rc)
            return
        limitador.espera()
        try:
            ddb.transact_write_items(TransactItems=[
                {"Put": {"TableName": a.tabla, "ConditionExpression": "attribute_not_exists(pk)",
                         "Item": {"pk": {"S": f"RUN#{rid}"}, "estado": {"S": "pendiente"}, "chat": {"S": chat},
                                  "tarea": {"S": tarea}, "programada": {"S": programada}, "por": {"S": a.nombre}}}},
                {"Update": {"TableName": a.tabla, "Key": {"pk": {"S": f"CHAT#{chat}"}},
                            "ConditionExpression": "attribute_not_exists(ejecucion_activa)",
                            "UpdateExpression": "SET ejecucion_activa = :r", "ExpressionAttributeValues": {":r": {"S": rid}}}},
            ])
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            motivos = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            if motivos and motivos[0] == "ConditionalCheckFailed":
                borrar(m)  # la ejecución ya existe: entrega repetida del mismo disparo
                log("duplicado", tarea=tarea, run_id=rid, rc=rc, motivos=motivos)
            elif len(motivos) > 1 and motivos[1] == "ConditionalCheckFailed":
                espera = random.randint(espera_min, espera_max)
                visibilidad(m, espera)
                log("ocupado", tarea=tarea, run_id=rid, rc=rc, espera=espera, motivos=motivos)
            else:
                raise
            return
        log("lanzando", tarea=tarea, run_id=rid, rc=rc, programada=programada,
            enviado=int(m["Attributes"]["SentTimestamp"]) / 1000)
        time.sleep(a.trabajo)  # OBO, STS, token de ejecución, InvokeAgentRuntime…
        ddb.update_item(TableName=a.tabla, Key={"pk": {"S": f"RUN#{rid}"}},
                        ConditionExpression="estado = :p", UpdateExpression="SET estado = :e",
                        ExpressionAttributeValues={":p": {"S": "pendiente"}, ":e": {"S": "en_marcha"}})
        borrar(m)
        log("lanzada", tarea=tarea, run_id=rid, rc=rc)

    def trabajador():
        while not parar.is_set():
            try:
                m = pendientes.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                procesar(m)
            except Exception as e:  # noqa: BLE001 - el mensaje reaparece tras el visibility timeout
                log("error", detalle=repr(e)[:300])

    def al_parar(*_):
        log("sigterm")
        parar.set()

    signal.signal(signal.SIGTERM, al_parar)
    hilos = [threading.Thread(target=trabajador, daemon=True) for _ in range(a.paralelo)]
    for h in hilos:
        h.start()
    log("arranca", paralelo=a.paralelo, tasa=a.tasa)
    while not parar.is_set():
        if pendientes.qsize() >= a.paralelo:  # no acumular más de un lote por delante
            time.sleep(0.05)
            continue
        r = sqs.receive_message(QueueUrl=a.cola, MaxNumberOfMessages=10, WaitTimeSeconds=20,
                                AttributeNames=["ApproximateReceiveCount", "SentTimestamp"])
        ms = r.get("Messages", [])
        if ms:
            log("recibe", n=len(ms))
        for m in ms:
            pendientes.put(m)
    # Parada: deja de leer, termina lo que tiene en curso y devuelve a la cola lo que no empezó.
    for h in hilos:
        h.join(timeout=a.gracia)
    devueltos = []
    while True:
        try:
            devueltos.append(pendientes.get_nowait())
        except queue.Empty:
            break
    for i in range(0, len(devueltos), 10):
        sqs.change_message_visibility_batch(QueueUrl=a.cola, Entries=[
            {"Id": str(j), "ReceiptHandle": m["ReceiptHandle"], "VisibilityTimeout": 0}
            for j, m in enumerate(devueltos[i:i + 10])])
    log("sale", devueltos=len(devueltos), tareas=[json.loads(m["Body"])["tarea"] for m in devueltos])


# ----------------------------------------------------------------------------------------------------
# Sonda
# ----------------------------------------------------------------------------------------------------

class Sonda:
    def __init__(self, dir_logs):
        self.suf = uuid.uuid4().hex[:8]
        self.nombre = f"sonda-n2-{self.suf}"
        ses = boto3.Session(region_name=REGION)
        self.sts, self.iam, self.kms, self.sqs, self.sch, self.ddb = (
            ses.client(x) for x in ("sts", "iam", "kms", "sqs", "scheduler", "dynamodb"))
        self.creado = {"horarios": [], "colas": {}, "roles": [], "claves": [], "procesos": []}
        self.resultados = []
        self.dir_logs = dir_logs
        self.cerrojo = threading.Lock()

    # -- utilidades --------------------------------------------------------------------------------
    def anota(self, nombre, ok, detalle=""):
        with self.cerrojo:
            self.resultados.append((nombre, ok, detalle))
            print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)

    def dice(self, *x):
        with self.cerrojo:
            print(f"[{dt.datetime.now():%H:%M:%S}]", *x, flush=True)

    def clave(self, etiqueta):
        k = self.kms.create_key(Description=f"{self.nombre}-{etiqueta}",
                                Tags=[{"TagKey": "sonda", "TagValue": "nivel2"}])["KeyMetadata"]
        self.creado["claves"].append(k["KeyId"])
        return k

    def cola(self, etiqueta, crear=True, **attrs):
        nombre = f"{self.nombre}-{etiqueta}"
        arn = f"arn:aws:sqs:{REGION}:{self.cuenta}:{nombre}"
        if not crear:
            return None, arn
        url = self.sqs.create_queue(QueueName=nombre, tags={"sonda": "nivel2"},
                                    Attributes={k: str(v) for k, v in attrs.items()})["QueueUrl"]
        self.creado["colas"][etiqueta] = url
        return url, arn

    def confianza(self, servicio="scheduler.amazonaws.com"):
        return json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": servicio}, "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": self.cuenta}}}]})

    def rol(self, etiqueta, claves, servicio="scheduler.amazonaws.com"):
        nombre = f"{self.nombre}-{etiqueta}"
        self.iam.create_role(RoleName=nombre, AssumeRolePolicyDocument=self.confianza(servicio), Tags=TAGS)
        self.creado["roles"].append(nombre)
        self.politica(nombre, claves)
        return nombre, f"arn:aws:iam::{self.cuenta}:role/{nombre}"

    def politica(self, rol, claves):
        sent = [{"Effect": "Allow", "Action": "sqs:SendMessage",
                 "Resource": f"arn:aws:sqs:{REGION}:{self.cuenta}:{self.nombre}-*"}]
        if claves:
            sent.append({"Effect": "Allow", "Action": ["kms:GenerateDataKey", "kms:Decrypt"], "Resource": claves})
        self.iam.put_role_policy(RoleName=rol, PolicyName="envio",
                                 PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": sent}))

    def horario(self, nombre, expresion, rol_arn, cola_arn, tarea, reintentos=0, edad=60, grupo=None):
        cuerpo = json.dumps({"tipo": "tarea", "tarea": tarea, "programada": "<aws.scheduler.scheduled-time>",
                             "intento": "<aws.scheduler.attempt-number>", "disparo": "<aws.scheduler.execution-id>"})
        kw = {"GroupName": grupo} if grupo else {}
        self.sch.create_schedule(
            Name=nombre, ScheduleExpression=expresion, ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="NONE", **kw,
            Target={"Arn": cola_arn, "RoleArn": rol_arn, "Input": cuerpo,
                    "RetryPolicy": {"MaximumRetryAttempts": reintentos, "MaximumEventAgeInSeconds": edad},
                    "DeadLetterConfig": {"Arn": self.arn_dlq_horarios}})
        if not grupo:
            self.creado["horarios"].append(nombre)

    def tarea(self, etiqueta, chat=None, falla=False):
        t = f"{etiqueta}-{uuid.uuid4().hex[:6]}"
        item = {"pk": {"S": f"TAREA#{t}"}, "oid": {"S": str(uuid.uuid4())}, "chat": {"S": chat or str(uuid.uuid4())}}
        if falla:
            item["falla"] = {"BOOL": True}
        self.ddb.put_item(TableName=self.tabla, Item=item)
        return t, item["oid"]["S"], item["chat"]["S"]

    def envia(self, tarea, programada):
        self.sqs.send_message(QueueUrl=self.q, MessageBody=json.dumps(
            {"tipo": "tarea", "tarea": tarea, "programada": programada, "intento": "1", "disparo": "manual"}))

    def lanza_consumidor(self, nombre, **kw):
        opciones = {"trabajo": 1, "tasa": 5, "paralelo": 5, "espera_ocupado": "60,120", "vis_fallo": 2, "gracia": 30}
        opciones.update(kw)
        log = os.path.join(self.dir_logs, f"{self.suf}-{nombre}.jsonl")
        cmd = [sys.executable, os.path.abspath(__file__), "consumidor", "--cola", self.q, "--tabla", self.tabla,
               "--nombre", nombre, "--log", log] + sum(
            ([f"--{k.replace('_', '-')}", str(v)] for k, v in opciones.items()), [])
        p = subprocess.Popen(cmd, env={**os.environ, "SONDA_REGION": REGION})
        self.creado["procesos"].append(p)
        return p, log

    @staticmethod
    def eventos(*logs):
        out = []
        for log in logs:
            if os.path.exists(log):
                with open(log) as f:
                    out += [json.loads(x) for x in f if x.strip()]
        return sorted(out, key=lambda e: e["t"])

    def espera(self, cond, maximo, cada=1.0):
        fin = time.time() + maximo
        while time.time() < fin:
            if cond():
                return True
            time.sleep(cada)
        return cond()

    def cuenta_cola(self, url):
        a = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return int(a["ApproximateNumberOfMessages"]), int(a["ApproximateNumberOfMessagesNotVisible"])

    def run(self, rid):
        return self.ddb.get_item(TableName=self.tabla, Key={"pk": {"S": f"RUN#{rid}"}}, ConsistentRead=True).get("Item")

    # -- infraestructura ----------------------------------------------------------------------------
    def crear(self):
        self.cuenta = self.sts.get_caller_identity()["Account"]
        self.dice("cuenta", self.cuenta, "región", REGION, "prefijo", self.nombre)
        k1, k2, k3 = self.clave("principal"), self.clave("reintento-rol"), self.clave("reintento-desactivada")
        self.k1, self.k2, self.k3 = k1, k2, k3
        self.kms.disable_key(KeyId=k3["KeyId"])
        self.tabla = self.nombre
        self.ddb.create_table(TableName=self.tabla, BillingMode="PAY_PER_REQUEST", Tags=TAGS,
                              AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                              KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        self.creado["tabla"] = self.tabla
        self.dlq, dlq_arn = self.cola("dlq", KmsMasterKeyId=k1["KeyId"], MessageRetentionPeriod=1209600)
        self.q, self.q_arn = self.cola("tareas", KmsMasterKeyId=k1["KeyId"], VisibilityTimeout=120,
                                       ReceiveMessageWaitTimeSeconds=20, MessageRetentionPeriod=345600,
                                       RedrivePolicy=json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 8}))
        self.dlq_horarios, self.arn_dlq_horarios = self.cola("dlq-horarios")
        self.q_rate, self.q_rate_arn = self.cola("rate", KmsMasterKeyId=k1["KeyId"])
        self.q_a, self.q_a_arn = self.cola("reintento-rol", KmsMasterKeyId=k2["KeyId"])
        self.q_b, self.q_b_arn = self.cola("reintento-clave", KmsMasterKeyId=k3["KeyId"])
        self.q_c, self.q_c_arn = self.cola("reintento-cola", crear=False)
        self.q_d, self.q_d_arn = self.cola("reintento-sinrol")
        self.q_e, self.q_e_arn = self.cola("reintento-confianza")
        self.rol_tardio, self.rol_tardio_arn = self.rol("tardio", None)
        self.rol_conf, self.rol_conf_arn = self.rol("confianza", None)
        self.rol_bueno, self.rol_bueno_arn = self.rol("bueno", [k1["Arn"], k3["Arn"]])
        self.rol_sinkms, self.rol_sinkms_arn = self.rol("sinkms", None)
        self.grupo = self.nombre
        self.sch.create_schedule_group(Name=self.grupo, Tags=TAGS)
        self.creado["grupo"] = self.grupo
        self.ddb.get_waiter("table_exists").wait(TableName=self.tabla)
        self.dice("esperando 15 s a que IAM propague los roles…")
        time.sleep(15)

    # -- 1. hora programada y reintentos de Scheduler ------------------------------------------------
    def prueba_1(self):
        try:
            self._prueba_1()
        except Exception as e:  # noqa: BLE001
            self.anota("Prueba 1 sin errores", False, repr(e)[:300])

    def _prueba_1(self):
        ahora = dt.datetime.now(UTC)
        t1 = (ahora + dt.timedelta(seconds=130)).replace(second=0, microsecond=0)
        at = f"at({t1:%Y-%m-%dT%H:%M:%S})"
        tareas = {}
        for etiqueta, rol, arn in (("rol", self.rol_sinkms_arn, self.q_a_arn), ("clave", self.rol_bueno_arn, self.q_b_arn),
                                   ("cola", self.rol_bueno_arn, self.q_c_arn), ("sinrol", self.rol_tardio_arn, self.q_d_arn),
                                   ("confianza", self.rol_conf_arn, self.q_e_arn)):
            t = f"reintento-{etiqueta}-{uuid.uuid4().hex[:6]}"
            tareas[etiqueta] = t
            self.horario(f"{self.nombre}-r-{etiqueta}", at, rol, arn, t, reintentos=20, edad=900)
        # Scheduler valida el rol al crear el horario: se rompe después (confianza de otro servicio; rol borrado).
        self.iam.update_assume_role_policy(RoleName=self.rol_conf, PolicyDocument=self.confianza("lambda.amazonaws.com"))
        self.iam.delete_role_policy(RoleName=self.rol_tardio, PolicyName="envio")
        self.iam.delete_role(RoleName=self.rol_tardio)
        self.creado["roles"].remove(self.rol_tardio)
        t_rate = f"rate-{uuid.uuid4().hex[:6]}"
        self.horario(f"{self.nombre}-rate", "rate(1 minute)", self.rol_bueno_arn, self.q_rate_arn, t_rate)
        self.dice(f"prueba 1: {len(tareas)} horarios {at} UTC con fallo temporal y un rate(1 minute)")

        recibidos = {}  # etiqueta -> (t_llegada, cuerpo, attrs)
        rate, en_dlq, arreglado = [], [], False
        fin = t1.timestamp() + 13 * 60
        colas = {"rol": self.q_a, "clave": self.q_b, "sinrol": self.q_d, "confianza": self.q_e, "rate": self.q_rate,
                 "dlq-horarios": self.dlq_horarios}
        while time.time() < fin:
            if not arreglado and time.time() > t1.timestamp() + 75:
                self.politica(self.rol_sinkms, [self.k2["Arn"]])
                self.kms.enable_key(KeyId=self.k3["KeyId"])
                self.q_c, _ = self.cola("reintento-cola")
                colas["cola"] = self.q_c
                self.rol("tardio", None)
                self.iam.update_assume_role_policy(RoleName=self.rol_conf, PolicyDocument=self.confianza())
                arreglado = time.time()
                self.dice("prueba 1: arreglado (KMS al rol, clave activada, cola creada, rol creado, confianza)",
                          f"{arreglado - t1.timestamp():.0f} s tras la hora")
            for etiqueta, url in list(colas.items()):
                r = self.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                             AttributeNames=["All"], MessageAttributeNames=["All"])
                for m in r.get("Messages", []):
                    self.sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
                    llegada = int(m["Attributes"]["SentTimestamp"]) / 1000
                    if etiqueta == "dlq-horarios":
                        attrs = {k: (v.get("StringValue") or "")[:200] for k, v in m.get("MessageAttributes", {}).items()}
                        en_dlq.append((llegada, m["Body"], attrs))
                        self.dice("prueba 1: en la cola de fallidos del horario:", attrs.get("ERROR_CODE"), m["Body"][:160])
                        continue
                    c = json.loads(m["Body"])
                    if etiqueta == "rate":
                        rate.append((llegada, c))
                        if len(rate) == 3:
                            self.sch.delete_schedule(Name=f"{self.nombre}-rate")
                            self.creado["horarios"].remove(f"{self.nombre}-rate")
                    else:
                        recibidos[etiqueta] = (llegada, c)
                        self.dice(f"prueba 1: {etiqueta} entregado {llegada - t1.timestamp():.0f} s tras la hora,"
                                  f" intento={c['intento']} programada={c['programada']} disparo={c['disparo'][:12]}…")
            if len(recibidos) + len(en_dlq) >= len(tareas) and len(rate) >= 3:
                break
            time.sleep(2)

        esperado = t1.strftime("%Y-%m-%dT%H:%M:%SZ")
        reintentados = 0
        for etiqueta, t in tareas.items():
            oid = str(uuid.uuid5(uuid.NAMESPACE_URL, t))
            if etiqueta in recibidos:
                reintentados += 1
                llegada, c = recibidos[etiqueta]
                self.anota(f"Fallo temporal ({etiqueta}): Scheduler reintenta y llega con intento > 1 y la hora programada original",
                           int(c["intento"]) > 1 and c["programada"] == esperado
                           and run_id(oid, t, c["programada"]) == run_id(oid, t, esperado),
                           f"intento {c['intento']}, programada {c['programada']} (esperada {esperado}), "
                           f"entregado {llegada - t1.timestamp():.0f} s tras la hora")
                continue
            d = [x for x in en_dlq if t in x[1]]
            if not d:
                self.anota(f"Fallo ({etiqueta}): llega a la cola o a la de fallidos", False, "no llegó a ninguna")
                continue
            llegada, cuerpo, attrs = d[0]
            try:
                c = json.loads(json.loads(cuerpo)["MessageBody"])
            except (ValueError, KeyError, TypeError):
                c = {}
            self.anota(f"Fallo ({etiqueta}) permanente para Scheduler: a la cola de fallidos sin reintentar, con la hora nominal",
                       c.get("programada") == esperado and c.get("intento") == "1",
                       f"{attrs.get('ERROR_CODE')}, RETRY_ATTEMPTS {attrs.get('RETRY_ATTEMPTS')}, "
                       f"{llegada - t1.timestamp():.0f} s tras la hora, programada {c.get('programada')}")
        self.anota("Algún fallo temporal provoca reintentos de Scheduler (para ver la hora en un reintento)",
                   reintentados > 0, f"{reintentados} de {len(tareas)}")
        for llegada, c in rate:
            self.dice(f"prueba 1: rate programada={c['programada']} entregado +{llegada - hora(c['programada']):.0f} s "
                      f"intento={c['intento']}")
        progs = [c["programada"] for _, c in rate]
        rids = {run_id("00000000-0000-0000-0000-000000000001", t_rate, p) for p in progs}
        self.anota("rate(1 minute): cada disparo trae su propia hora programada (cada 60 s exactos) y da un run_id distinto",
                   len(rate) >= 2 and len(rids) == len(rate)
                   and all(hora(b) - hora(a) == 60 for a, b in zip(progs, progs[1:])),
                   f"{len(rate)} disparos: {', '.join(progs)}")
        retrasos = [llegada - hora(c["programada"]) for llegada, c in rate]
        self.anota("La hora programada es la nominal, no la de entrega",
                   bool(retrasos) and all(r > 0 for r in retrasos),
                   "entregados " + ", ".join(f"{r:.0f}" for r in retrasos) + " s después")
        self.p1 = {"recibidos": recibidos, "dlq": en_dlq, "rate": rate, "t1": t1}

    # -- 2, 3 y 6 con un consumidor --------------------------------------------------------------------
    def prueba_2_3_6(self):
        # 2b. otro consumidor creó la ejecución y murió antes de borrar el mensaje: reaparece
        t_muere, oid_muere, chat_muere = self.tarea("muere")
        prog_m = ahora_iso()
        self.envia(t_muere, prog_m)
        m = self.sqs.receive_message(QueueUrl=self.q, MaxNumberOfMessages=1, WaitTimeSeconds=5)["Messages"][0]
        rid_m = run_id(oid_muere, t_muere, prog_m)
        self.ddb.transact_write_items(TransactItems=[
            {"Put": {"TableName": self.tabla, "Item": {"pk": {"S": f"RUN#{rid_m}"}, "estado": {"S": "en_marcha"}}}},
            {"Update": {"TableName": self.tabla, "Key": {"pk": {"S": f"CHAT#{chat_muere}"}},
                        "UpdateExpression": "SET ejecucion_activa = :r", "ExpressionAttributeValues": {":r": {"S": rid_m}}}}])
        self.sqs.change_message_visibility(QueueUrl=self.q, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=5)
        # 2a. el mismo cuerpo dos veces
        t_dup, oid_dup, _ = self.tarea("dup")
        prog = ahora_iso()
        self.envia(t_dup, prog)
        self.envia(t_dup, prog)
        # 3a. chat ocupado
        t_ocu, oid_ocu, chat_ocu = self.tarea("ocupado")
        self.ddb.put_item(TableName=self.tabla, Item={"pk": {"S": f"CHAT#{chat_ocu}"}, "ejecucion_activa": {"S": "otra"}})
        self.envia(t_ocu, ahora_iso())
        # 3b. disparo con 40 min de retraso
        t_viejo, _, _ = self.tarea("viejo")
        self.envia(t_viejo, ahora_iso(-40 * 60))
        # 6. siempre falla
        t_falla, _, _ = self.tarea("falla", falla=True)
        self.envia(t_falla, ahora_iso())
        self.dice("pruebas 2, 3 y 6: mensajes enviados; arranca el consumidor c1")
        t0 = time.time()
        p, log = self.lanza_consumidor("c1")

        def ev(tipo, tarea):
            return [e for e in self.eventos(log) if e["ev"] == tipo and e.get("tarea") == tarea]

        # 2
        self.espera(lambda: len(ev("lanzada", t_dup)) + len(ev("duplicado", t_dup)) >= 2 and ev("duplicado", t_muere), 60)
        rid_dup = run_id(oid_dup, t_dup, prog)
        self.anota("Mismo cuerpo dos veces: una ejecución, la otra entrega falla por 'ya existe' y se borra",
                   len(ev("lanzada", t_dup)) == 1 and len(ev("duplicado", t_dup)) == 1 and self.run(rid_dup) is not None,
                   f"lanzada {len(ev('lanzada', t_dup))}, duplicado {len(ev('duplicado', t_dup))} "
                   f"{ev('duplicado', t_dup)[0]['motivos'] if ev('duplicado', t_dup) else ''}")
        d = ev("duplicado", t_muere)
        self.anota("Mensaje no borrado que reaparece con su ejecución ya creada: 'ya existe' y se borra",
                   len(d) == 1 and not ev("lanzada", t_muere) and d[0]["rc"] == 2,
                   f"rc={d[0]['rc'] if d else '-'} motivos={d[0]['motivos'] if d else '-'}")
        # 3b
        self.espera(lambda: ev("saltado", t_viejo), 30)
        s = ev("saltado", t_viejo)
        item = self.ddb.get_item(TableName=self.tabla, Key={"pk": {"S": f"TAREA#{t_viejo}"}}).get("Item", {})
        self.anota("Disparo con más de 30 min de retraso: se salta, se anota en la tarea y se borra",
                   len(s) == 1 and item.get("saltados", {}).get("N") == "1",
                   f"retraso {s[0]['retraso']} s" if s else "")
        # 3a
        self.espera(lambda: ev("ocupado", t_ocu), 40)
        o = ev("ocupado", t_ocu)
        if o:
            self.dice(f"prueba 3: chat ocupado, ChangeMessageVisibility {o[0]['espera']} s (rc={o[0]['rc']});"
                      " libero el chat en 20 s")
            time.sleep(20)
            self.ddb.update_item(TableName=self.tabla, Key={"pk": {"S": f"CHAT#{chat_ocu}"}},
                                 UpdateExpression="REMOVE ejecucion_activa")
            t_libera = time.time()
        # 6 (mientras tanto)
        self.espera(lambda: ev("lanzada", t_ocu), 150)
        la = ev("lanzada", t_ocu)
        self.anota("Chat ocupado: ChangeMessageVisibility de 60 a 120 s, vuelve y se lanza al liberar el chat",
                   bool(o) and bool(la) and 60 <= o[0]["espera"] <= 120 and len(la) == 1
                   and abs((la[0]["t"] - o[0]["t"]) - o[0]["espera"]) < 25,
                   (f"esperó {o[0]['espera']} s; volvió {la[0]['t'] - o[0]['t']:.0f} s después, con rc={la[0]['rc']}, "
                    f"{la[0]['t'] - t_libera:.0f} s tras liberar") if o and la else "")

        def en_dlq():
            r = self.sqs.receive_message(QueueUrl=self.dlq, MaxNumberOfMessages=10, WaitTimeSeconds=2,
                                         AttributeNames=["All"])
            return r.get("Messages", [])
        dlq = []
        self.espera(lambda: dlq.extend(en_dlq()) or any(t_falla in x["Body"] for x in dlq), 120)
        f = ev("fallo", t_falla)
        x = [m for m in dlq if t_falla in m["Body"]]
        for m in dlq:
            self.sqs.delete_message(QueueUrl=self.dlq, ReceiptHandle=m["ReceiptHandle"])
        self.anota("Mensaje que siempre falla: 8 entregas y a la DLQ con el cuerpo intacto",
                   len(f) == 8 and len(x) == 1 and json.loads(x[0]["Body"])["tarea"] == t_falla,
                   f"{len(f)} entregas (rc {[e['rc'] for e in f]}) en {f[-1]['t'] - f[0]['t']:.0f} s; "
                   f"en la DLQ: {({k: x[0]['Attributes'].get(k) for k in ('ApproximateReceiveCount', 'DeadLetterQueueSourceArn')} if x else 'nada')}"
                   if f else "sin entregas")
        self.p236 = {"duracion": time.time() - t0}
        self.para(p, log, "c1")

    def para(self, p, log, nombre):
        t = time.time()
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=60)
        s = [e for e in self.eventos(log) if e["ev"] == "sale"]
        self.dice(f"{nombre}: SIGTERM → sale en {time.time() - t:.1f} s, devueltos {s[-1]['devueltos'] if s else '?'}")
        return time.time() - t, s[-1] if s else None

    # -- 4. parada limpia -------------------------------------------------------------------------------
    def prueba_4(self):
        tareas = [self.tarea("parada")[0] for _ in range(10)]
        prog = ahora_iso()
        self.sqs.send_message_batch(QueueUrl=self.q, Entries=[
            {"Id": str(i), "MessageBody": json.dumps({"tarea": t, "programada": prog, "intento": "1", "disparo": "m"})}
            for i, t in enumerate(tareas)])
        p, log = self.lanza_consumidor("c4", trabajo=6)
        self.espera(lambda: len([e for e in self.eventos(log) if e["ev"] == "lanzando"]) >= 3, 40, 0.2)
        en_curso = {e["tarea"] for e in self.eventos(log) if e["ev"] == "lanzando"}
        tarda, sale = self.para(p, log, "c4")
        evs = self.eventos(log)
        lanzadas = {e["tarea"] for e in evs if e["ev"] == "lanzada"}
        visibles, ocultos = self.cuenta_cola(self.q)
        empezadas = {e["tarea"] for e in evs if e["ev"] == "lanzando"}
        self.anota("SIGTERM a mitad de un lote: termina lo que tiene en curso y devuelve lo no empezado",
                   en_curso <= lanzadas and empezadas == lanzadas and sale is not None and sale["devueltos"] > 0,
                   f"en curso al parar {len(en_curso)}, lanzadas {len(lanzadas)}, devueltas {sale['devueltos'] if sale else '?'}, "
                   f"sale en {tarda:.1f} s; cola: {visibles} visibles, {ocultos} en vuelo")
        p2, log2 = self.lanza_consumidor("c4b", trabajo=1)
        self.espera(lambda: len({e["tarea"] for e in self.eventos(log, log2) if e["ev"] == "lanzada"}) >= 10, 60)
        self.para(p2, log2, "c4b")
        evs = self.eventos(log, log2)
        cuenta = {t: sum(1 for e in evs if e["ev"] == "lanzando" and e["tarea"] == t) for t in tareas}
        runs = sum(1 for t in tareas if any(e for e in evs if e["ev"] == "lanzada" and e["tarea"] == t))
        self.anota("Al relanzar, lo devuelto se procesa una sola vez (10 ejecuciones, ninguna doble)",
                   all(v == 1 for v in cuenta.values()) and runs == 10,
                   f"lanzamientos por tarea {sorted(cuenta.values())}; duplicados "
                   f"{sum(1 for e in evs if e['ev'] == 'duplicado')}")
        # 4b. SIGKILL a mitad de un lanzamiento
        t, oid, _ = self.tarea("kill")
        prog = ahora_iso()
        self.envia(t, prog)
        p3, log3 = self.lanza_consumidor("c4c", trabajo=30)
        self.espera(lambda: any(e["ev"] == "lanzando" for e in self.eventos(log3)), 40, 0.2)
        p3.kill()
        p3.wait()
        t_kill = time.time()
        self.dice("prueba 4b: SIGKILL a mitad de un lanzamiento; espero a que reaparezca (visibility 120 s)")
        p4, log4 = self.lanza_consumidor("c4d", trabajo=1)
        self.espera(lambda: any(e["tarea"] == t for e in self.eventos(log4) if e["ev"] in ("duplicado", "lanzada")), 170)
        self.para(p4, log4, "c4d")
        d = [e for e in self.eventos(log4) if e.get("tarea") == t]
        r = self.run(run_id(oid, t, prog))
        self.anota("SIGKILL a mitad: reaparece tras el visibility timeout, 'ya existe', se borra y la ejecución queda en pendiente",
                   bool(d) and d[0]["ev"] == "duplicado" and r and r["estado"]["S"] == "pendiente",
                   f"reapareció {d[0]['t'] - t_kill:.0f} s después (rc={d[0]['rc']}), estado {r['estado']['S'] if r else '-'}"
                   if d else "no reapareció")

    # -- 5. ráfaga -------------------------------------------------------------------------------------
    def prueba_5(self):
        t5 = (dt.datetime.now(UTC) + dt.timedelta(seconds=150)).replace(second=0, microsecond=0)
        tareas = [self.tarea("rafaga")[0] for _ in range(50)]
        for i, t in enumerate(tareas):
            self.horario(f"r{i:02d}", f"at({t5:%Y-%m-%dT%H:%M:%S})", self.rol_bueno_arn, self.q_arn, t, grupo=self.grupo)
        self.dice(f"prueba 5: 50 horarios at() a las {t5:%H:%M:%S} UTC; dos consumidores")
        pa, la = self.lanza_consumidor("c5a")
        pb, lb = self.lanza_consumidor("c5b")
        ok = self.espera(lambda: len({e["tarea"] for e in self.eventos(la, lb) if e["ev"] == "lanzada"}) >= 50,
                         t5.timestamp() + 240 - time.time())
        evs = [e for e in self.eventos(la, lb) if e.get("tarea") in tareas]
        lz = [e for e in evs if e["ev"] == "lanzando"]
        envios = sorted(e["enviado"] - t5.timestamp() for e in lz)
        fin = max((e["t"] for e in evs if e["ev"] == "lanzada"), default=0)
        por_seg = {}
        for e in lz:
            por_seg[int(e["t"])] = por_seg.get(int(e["t"]), 0) + 1
        dlq = self.cuenta_cola(self.dlq)
        self.anota("Ráfaga de 50 at(): se vacía, una ejecución por disparo, ≤ 10 arranques/s y DLQ vacía",
                   ok and len(lz) == 50 and len({e["run_id"] for e in lz}) == 50 and max(por_seg.values()) <= 10
                   and dlq == (0, 0),
                   f"Scheduler entregó entre +{envios[0]:.0f} y +{envios[-1]:.0f} s; última lanzada "
                   f"{fin - t5.timestamp() - envios[-1]:.1f} s tras la última entrega (+{fin - t5.timestamp():.0f} s); "
                   f"máx {max(por_seg.values())}/s; c5a {sum(1 for e in lz if e['c'] == 'c5a')}, "
                   f"c5b {sum(1 for e in lz if e['c'] == 'c5b')}; DLQ {dlq}" if lz else "nada")
        # 5b. 50 mensajes de golpe (el consumidor es el cuello de botella)
        tareas = [self.tarea("golpe")[0] for _ in range(50)]
        prog = ahora_iso()
        t0 = time.time()
        for i in range(0, 50, 10):
            self.sqs.send_message_batch(QueueUrl=self.q, Entries=[
                {"Id": str(j), "MessageBody": json.dumps({"tarea": t, "programada": prog, "intento": "1", "disparo": "m"})}
                for j, t in enumerate(tareas[i:i + 10])])
        ok = self.espera(lambda: len({e["tarea"] for e in self.eventos(la, lb)
                                      if e["ev"] == "lanzada" and e["tarea"] in tareas}) >= 50, 120, 0.25)
        evs = [e for e in self.eventos(la, lb) if e.get("tarea") in tareas]
        lz = [e for e in evs if e["ev"] == "lanzando"]
        fin = max((e["t"] for e in evs if e["ev"] == "lanzada"), default=t0)
        por_seg = {}
        for e in lz:
            por_seg[int(e["t"])] = por_seg.get(int(e["t"]), 0) + 1
        self.anota("50 mensajes de golpe con 2 consumidores a 5/s: se vacía en unos 5-10 s",
                   ok and len(lz) == 50 and (fin - t0) < 20,
                   f"primera {min(e['t'] for e in lz) - t0:.1f} s, última lanzada {fin - t0:.1f} s; "
                   f"máx {max(por_seg.values())} arranques/s" if lz else "nada")
        self.para(pa, la, "c5a")
        self.para(pb, lb, "c5b")
        self.anota("DLQ vacía al acabar (sin contar el mensaje de la prueba 6, ya retirado)",
                   self.cuenta_cola(self.dlq) == (0, 0), str(self.cuenta_cola(self.dlq)))

    # -- limpieza ----------------------------------------------------------------------------------------
    def intenta(self, desc, fn):
        try:
            fn()
        except ClientError as e:
            print(f"   aviso al borrar {desc}:", e.response["Error"]["Code"], flush=True)

    def borrar(self):
        print("borrando…", flush=True)
        for p in self.creado["procesos"]:
            if p.poll() is None:
                p.kill()
        for h in self.creado["horarios"]:
            self.intenta(h, lambda h=h: self.sch.delete_schedule(Name=h))
        if "grupo" in self.creado:
            self.intenta("grupo", lambda: self.sch.delete_schedule_group(Name=self.creado["grupo"]))
        for etiqueta, url in self.creado["colas"].items():
            self.intenta(etiqueta, lambda url=url: self.sqs.delete_queue(QueueUrl=url))
        if "tabla" in self.creado:
            self.intenta("tabla", lambda: self.ddb.delete_table(TableName=self.creado["tabla"]))
        for r in self.creado["roles"]:
            self.intenta(r, lambda r=r: self.iam.delete_role_policy(RoleName=r, PolicyName="envio"))
            self.intenta(r, lambda r=r: self.iam.delete_role(RoleName=r))
        for k in self.creado["claves"]:
            self.intenta(k, lambda k=k: self.kms.schedule_key_deletion(KeyId=k, PendingWindowInDays=7))


def main():
    dir_logs = os.environ.get("SONDA_LOGS") or os.path.join(tempfile.gettempdir(), "sonda-tareas-cola")
    os.makedirs(dir_logs, exist_ok=True)
    s = Sonda(dir_logs)
    try:
        s.crear()
        h = threading.Thread(target=s.prueba_1)
        h.start()
        if "--solo-1" not in sys.argv:
            s.prueba_2_3_6()
            s.prueba_4()
        h.join()
        if "--solo-1" not in sys.argv:
            s.prueba_5()
    except Exception as e:  # noqa: BLE001
        s.anota("La sonda termina sin errores", False, repr(e)[:400])
        raise
    finally:
        if "--conservar" not in sys.argv:
            s.borrar()
        fallos = [r for r in s.resultados if not r[1]]
        print(f"\n{len(s.resultados) - len(fallos)} de {len(s.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "consumidor":
        ap = argparse.ArgumentParser()
        ap.add_argument("modo")
        for nombre, tipo in (("cola", str), ("tabla", str), ("nombre", str), ("log", str), ("trabajo", float),
                             ("tasa", float), ("paralelo", int), ("espera-ocupado", str), ("vis-fallo", int),
                             ("gracia", float)):
            ap.add_argument(f"--{nombre}", type=tipo, required=True)
        consumidor(ap.parse_args())
    else:
        main()
