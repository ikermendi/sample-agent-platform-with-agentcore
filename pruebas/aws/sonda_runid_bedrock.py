"""Sonda 7b del nivel 2: el run_id con el usuario dentro como RoleSessionName, hasta el registro de Bedrock.

La sonda 7 vio que el registro de invocaciones trae `identity.arn` con el RoleSessionName pero no el
SourceIdentity ni las etiquetas de sesión. Decisión: run_id = "{oid32hex}.{sufijo26}" (59 caracteres; ver
runid_coste.py) y RoleSessionName = run_id, para que la Lambda de coste saque el usuario del ARN. Comprueba:

  1. STS acepta ese RoleSessionName con SourceIdentity = oid (GUID con guiones) y etiquetas oid, chat y
     run_id, en un chat (sufijo ULID) y en una tarea (sufijo base32 de SHA-256); rechaza 65 caracteres.
  2. Converse y ConverseStream llegan al registro (solo metadatos) con identity.arn exacto.
  3. La función de parseo de la Lambda sobre los registros reales y sobre casos malos.
  4. La denegación por aws:SourceIdentity sigue funcionando con este nombre de sesión, y cuánto tarda.
  5. La latencia hasta FilterLogEvents.

Guarda y restaura la configuración del registro de invocaciones (o la borra si no había), también si
falla. Crea "sonda-n2-<sufijo>" (rol de sesión, rol del registro, grupo de logs) con la etiqueta
sonda=nivel2 y lo borra todo al acabar. Coste: céntimos (Haiku 4.5). Uso:

    SONDA_REGION=eu-north-1 python sonda_runid_bedrock.py
"""
import json
import os
import re
import statistics
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from runid_coste import parsear_arn, run_id, sufijo_tarea, ulid

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0")
ESPERA_MAX = int(os.environ.get("SONDA_ESPERA", "600"))
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
ROL_SESION = f"{NOMBRE}-sesion"
ROL_LOG = f"{NOMBRE}-registro"
GRUPO = f"sonda-n2-{SUF}-invocaciones"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
PERMITIDO = re.compile(r"^[\w+=,.@-]+$", re.ASCII)

OID = str(uuid.uuid4())
CHAT = f"chat-{uuid.uuid4().hex[:8]}"
RUN_CHAT = run_id(OID, ulid())
RUN_TAREA = run_id(OID, sufijo_tarea(f"tarea-{SUF}", "2026-11-01T08:00:00Z"))
MENSAJE = [{"role": "user", "content": [{"text": "Di hola."}]}]

ses = boto3.Session(region_name=REGION)
sts, iam, bedrock, logs = (ses.client(x) for x in ("sts", "iam", "bedrock", "logs"))
creado, original, resultados = {}, {}, []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def por_iam(e):
    return codigo(e) == "AccessDeniedException" and "is not authorized to perform" in str(e)


def reintenta(f, errores=("ValidationException", "AccessDeniedException", "AccessDenied"), plazo=90):
    t0 = time.time()
    while True:
        try:
            return f()
        except ClientError as e:
            if codigo(e) not in errores or time.time() - t0 > plazo:
                raise
            time.sleep(5)


def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    if ":assumed-role/" in mi_arn:
        mi_arn = f"arn:aws:iam::{cuenta}:role/" + mi_arn.split(":assumed-role/")[1].split("/")[0]
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=MODELO)
    perfil_arn, modelos = p["inferenceProfileArn"], [m["modelArn"] for m in p["models"]]

    original["config"] = bedrock.get_model_invocation_logging_configuration().get("loggingConfig")
    print("     registro de invocaciones actual:", json.dumps(original["config"]) if original["config"] else "ninguno",
          flush=True)

    logs.create_log_group(logGroupName=GRUPO, tags={"sonda": "nivel2"})
    creado["grupo"] = GRUPO
    logs.put_retention_policy(logGroupName=GRUPO, retentionInDays=1)
    iam.create_role(RoleName=ROL_LOG, Tags=TAGS, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": cuenta},
                          "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{REGION}:{cuenta}:*"}}}]}))
    creado["rol_log"] = ROL_LOG
    iam.put_role_policy(RoleName=ROL_LOG, PolicyName="escribir", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"arn:aws:logs:{REGION}:{cuenta}:log-group:{GRUPO}:log-stream:*"}]}))

    iam.create_role(RoleName=ROL_SESION, Tags=TAGS, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"AWS": mi_arn},
            "Action": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]}]}))
    creado["rol_sesion"] = ROL_SESION
    iam.put_role_policy(RoleName=ROL_SESION, PolicyName="modelos", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": [perfil_arn] + modelos}]}))
    print("esperando 15 s a que IAM propague los roles…", flush=True)
    time.sleep(15)

    cfg = {"cloudWatchConfig": {"logGroupName": GRUPO, "roleArn": f"arn:aws:iam::{cuenta}:role/{ROL_LOG}"},
           "textDataDeliveryEnabled": False, "imageDataDeliveryEnabled": False,
           "embeddingDataDeliveryEnabled": False, "videoDataDeliveryEnabled": False,
           "audioDataDeliveryEnabled": False}
    creado["registro"] = True
    reintenta(lambda: bedrock.put_model_invocation_logging_configuration(loggingConfig=cfg))
    print(f"     registro de invocaciones (solo metadatos) hacia {GRUPO}", flush=True)
    time.sleep(10)
    return cuenta


def asumir(cuenta, nombre, tags=True):
    kw = {"Tags": [{"Key": "oid", "Value": OID}, {"Key": "chat", "Value": CHAT},
                   {"Key": "run_id", "Value": nombre}]} if tags else {}
    return reintenta(lambda: sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{ROL_SESION}",
                                             RoleSessionName=nombre, SourceIdentity=OID, DurationSeconds=3600, **kw),
                     errores=("AccessDenied",), plazo=60)


def cliente(r):
    c = r["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=REGION).client("bedrock-runtime")


def parte1(cuenta):
    for nombre, rid in (("chat (ULID)", RUN_CHAT), ("tarea (SHA-256 base32)", RUN_TAREA)):
        suf = rid.split(".")[1]
        anota(f"Formato del run_id de {nombre}",
              len(rid) == 59 and "=" not in suf and bool(PERMITIDO.match(rid)) and suf == suf.upper(),
              f"{rid} ({len(rid)} caracteres; alfabeto del sufijo: {''.join(sorted(set(suf)))})")
    sesiones = {}
    for nombre, rid in (("chat", RUN_CHAT), ("tarea", RUN_TAREA)):
        try:
            r = asumir(cuenta, rid)
            esperado = f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{rid}"
            arn = r["AssumedRoleUser"]["Arn"]
            anota(f"AssumeRole del {nombre} con SourceIdentity y etiquetas oid, chat, run_id",
                  arn == esperado and r.get("SourceIdentity") == OID,
                  f"{arn}; SourceIdentity={r.get('SourceIdentity')}")
            sesiones[nombre] = (rid, arn, cliente(r))
        except ClientError as e:
            anota(f"AssumeRole del {nombre} con SourceIdentity y etiquetas", False, f"{codigo(e)}: {e}")
    for largo, debe in ((64, True), (65, False)):
        nombre = (RUN_CHAT + "." + "X" * 10)[:largo]
        try:
            asumir(cuenta, nombre, tags=False)
            anota(f"RoleSessionName de {largo} caracteres {'aceptado' if debe else 'rechazado'}", debe, "aceptado")
        except ClientError as e:
            anota(f"RoleSessionName de {largo} caracteres {'aceptado' if debe else 'rechazado'}", not debe,
                  f"{codigo(e)}: {str(e)[:140]}")
    return sesiones


def converse(rt):
    r = rt.converse(modelId=MODELO, inferenceConfig={"maxTokens": 5}, messages=MENSAJE)
    return r["ResponseMetadata"]["RequestId"]


def converse_stream(rt):
    r = rt.converse_stream(modelId=MODELO, inferenceConfig={"maxTokens": 5}, messages=MENSAJE)
    for _ in r["stream"]:
        pass
    return r["ResponseMetadata"]["RequestId"]


def leer(desde):
    eventos, token = [], None
    while True:
        r = logs.filter_log_events(logGroupName=GRUPO, startTime=desde, **({"nextToken": token} if token else {}))
        eventos += r["events"]
        token = r.get("nextToken")
        if not token:
            return eventos


def parte2_3_5(cuenta, sesiones):
    hechas = {}
    desde = int(time.time() * 1000) - 60_000
    for nombre, (rid, arn, rt) in sesiones.items():
        llamadas = [("Converse", converse), ("ConverseStream", converse_stream)] if nombre == "chat" else [("Converse", converse)]
        for op, f in llamadas:
            req = f(rt)
            hechas[req] = (f"{op} del {nombre}", rid, arn, int(time.time() * 1000))
            print(f"     {op} del {nombre}: {req}", flush=True)
    vistos, registros = {}, {}
    t0 = time.time()
    while time.time() - t0 < ESPERA_MAX and len(vistos) < len(hechas):
        ahora = int(time.time() * 1000)
        for ev in leer(desde):
            try:
                reg = json.loads(ev["message"])
            except ValueError:
                continue
            req = reg.get("requestId")
            if req in hechas and req not in vistos:
                vistos[req], registros[req] = ahora, reg
                print(f"     registro de {hechas[req][0]} visible a {(ahora - hechas[req][3]) / 1000:.1f} s", flush=True)
        time.sleep(3)
    anota("Llegan los registros (solo metadatos)", len(vistos) == len(hechas), f"{len(vistos)} de {len(hechas)}")

    for req, reg in registros.items():
        etiqueta, rid, esperado, _ = hechas[req]
        arn = (reg.get("identity") or {}).get("arn")
        anota(f"identity.arn exacto ({etiqueta}, operation={reg.get('operation')})", arn == esperado, arn)
        p = parsear_arn(arn, roles={ROL_SESION})
        anota(f"Parseo del registro real ({etiqueta})",
              p is not None and p["oid"] == OID and p["run_id"] == rid and p["rol"] == ROL_SESION
              and p["cuenta"] == cuenta, json.dumps(p))

    s = [(vistos[r] - hechas[r][3]) / 1000 for r in vistos]
    if s:
        anota("Latencia fin de la llamada → visible en FilterLogEvents", True,
              f"mín {min(s):.1f} s, mediana {statistics.median(s):.1f} s, máx {max(s):.1f} s ({len(s)} registros)")


def parte3_malos(cuenta):
    hex_ = uuid.UUID(OID).hex
    malos = {
        "otro rol": f"arn:aws:sts::{cuenta}:assumed-role/otro-rol/{RUN_CHAT}",
        "usuario IAM": f"arn:aws:iam::{cuenta}:user/alguien",
        "sesión con nombre libre": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/botocore-session-123",
        "oid con guiones": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{OID}.{RUN_CHAT[-26:]}",
        "oid en mayúsculas": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{hex_.upper()}.{RUN_CHAT[-26:]}",
        "sufijo en minúsculas": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{RUN_CHAT.lower()}",
        "sufijo de 25": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{RUN_CHAT[:-1]}",
        "sufijo con relleno =": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{RUN_CHAT[:-1]}=",
        "sin punto": f"arn:aws:sts::{cuenta}:assumed-role/{ROL_SESION}/{RUN_CHAT.replace('.', '-')}",
        "vacío": "",
        "None": None,
    }
    mal = {k: parsear_arn(v, roles={ROL_SESION}) for k, v in malos.items()}
    anota("Parseo descarta los casos malos", all(v is None for v in mal.values()),
          ", ".join(f"{k}={'None' if v is None else 'ACEPTADO'}" for k, v in mal.items()))


def parte4(sesiones):
    rt = sesiones["chat"][2]
    deny = {"Version": "2012-10-17", "Statement": [{
        "Sid": "SinPresupuesto", "Effect": "Deny", "Action": "bedrock:InvokeModel*", "Resource": "*",
        "Condition": {"StringEquals": {"aws:SourceIdentity": OID}}}]}
    t0 = time.time()
    iam.put_role_policy(RoleName=ROL_SESION, PolicyName="corte", PolicyDocument=json.dumps(deny))
    creado["corte"] = True
    pasan, corte, detalle = 0, None, ""
    while time.time() - t0 < 120:
        try:
            converse(rt)
            pasan += 1
        except ClientError as e:
            if por_iam(e):
                corte, detalle = time.time() - t0, str(e)[:200]
                break
            print("     error inesperado:", codigo(e), str(e)[:160], flush=True)
            break
        time.sleep(2)
    anota("Deny por aws:SourceIdentity con este RoleSessionName", corte is not None,
          f"AccessDenied a {corte:.1f} s tras PutRolePolicy ({pasan} llamadas pasaron antes): {detalle}"
          if corte else f"no se cortó en 2 minutos ({pasan} llamadas pasaron)")
    iam.delete_role_policy(RoleName=ROL_SESION, PolicyName="corte")
    creado.pop("corte")


def borrar():
    print("restaurando el registro de invocaciones y borrando…", flush=True)
    if "config" in original:
        try:
            if original["config"]:
                bedrock.put_model_invocation_logging_configuration(loggingConfig=original["config"])
            else:
                bedrock.delete_model_invocation_logging_configuration()
            final = bedrock.get_model_invocation_logging_configuration().get("loggingConfig")
            anota("Registro de invocaciones restaurado", final == original["config"],
                  json.dumps(final) if final else "ninguno, como estaba")
        except ClientError as e:
            print("   AVISO al restaurar el registro de invocaciones:", codigo(e), e, flush=True)
    if "grupo" in creado:
        try:
            logs.delete_log_group(logGroupName=GRUPO)
        except ClientError as e:
            print("   aviso al borrar el grupo:", codigo(e), flush=True)
    for clave, rol in (("rol_sesion", ROL_SESION), ("rol_log", ROL_LOG)):
        if clave in creado:
            try:
                for pol in iam.list_role_policies(RoleName=rol)["PolicyNames"]:
                    iam.delete_role_policy(RoleName=rol, PolicyName=pol)
                iam.delete_role(RoleName=rol)
            except ClientError as e:
                print(f"   aviso al borrar {rol}:", codigo(e), flush=True)


if __name__ == "__main__":
    try:
        cuenta = crear()
        sesiones = parte1(cuenta)
        parte3_malos(cuenta)
        if sesiones:
            parte2_3_5(cuenta, sesiones)
        if "chat" in sesiones:
            parte4(sesiones)
    finally:
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
