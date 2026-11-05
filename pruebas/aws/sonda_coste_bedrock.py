"""Sonda 7 del nivel 2: el coste de Bedrock sacado del registro de invocaciones, sin fiarse del kernel.

La idea de diseño: el kernel llama a Bedrock (Converse/ConverseStream) con credenciales STS de una sesión
creada con AssumeRole (RoleSessionName = run_id, SourceIdentity = oid del usuario, etiquetas de sesión);
Bedrock escribe un registro por invocación con la identidad y los tokens; una Lambda suscrita a ese
registro suma el coste por usuario y por ejecución. Esta sonda comprueba:

  1. Con el registro de invocaciones solo de metadatos (texto, imagen, embeddings, vídeo y audio a
     false), si siguen llegando los registros con tokens e identidad. Si no llega nada, lo repite con
     texto activado y anota la diferencia.
  2. Qué trae cada registro: campos, `identity.arn` (¿con el RoleSessionName?), si aparecen el
     SourceIdentity o las etiquetas de sesión, `modelId` (perfil o foundation-model), `operation`,
     `requestId`, `region` y si hay región de inferencia, y si aparece `requestMetadata`.
  3. Si los tokens del registro coinciden con el `usage` de la respuesta: Converse, ConverseStream y dos
     ConverseStream con `cachePoint` (escritura y luego lectura de caché).
  4. La latencia desde la llamada hasta que el registro se ve con FilterLogEvents y hasta que llega a una
     Lambda por un subscription filter.

El registro de invocaciones es una configuración de toda la cuenta y región: la sonda guarda la que hay
y la restaura al acabar (o la borra si no había), también si falla. Crea "sonda-n2-<sufijo>" (rol de
sesión, rol del registro, grupo de logs, Lambda, su rol y su grupo de logs, subscription filter) con la
etiqueta sonda=nivel2 y lo borra todo al acabar. Coste: céntimos (Haiku 4.5). Uso:

    SONDA_REGION=eu-north-1 python sonda_coste_bedrock.py
    SONDA_SIN_LAMBDA=1 SONDA_REGION=eu-north-1 python sonda_coste_bedrock.py
"""
import io
import json
import os
import statistics
import sys
import time
import uuid
import zipfile

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0")
CON_LAMBDA = not os.environ.get("SONDA_SIN_LAMBDA")
ESPERA_MAX = int(os.environ.get("SONDA_ESPERA", "600"))
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
ROL_SESION = f"{NOMBRE}-sesion"
ROL_LOG = f"{NOMBRE}-registro"
ROL_LAMBDA = f"{NOMBRE}-lambda"
GRUPO = f"sonda-n2-{SUF}-invocaciones"
FUNCION = f"{NOMBRE}-llegada"
GRUPO_LAMBDA = f"/aws/lambda/{FUNCION}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]

RUN_ID = f"run-{uuid.uuid4().hex[:16]}"
OID = str(uuid.uuid4())
CHAT = f"chat-{uuid.uuid4().hex[:8]}"
META = {"run_id": RUN_ID, "sonda": "coste"}

ses = boto3.Session(region_name=REGION)
sts, iam, bedrock, logs, lam = (ses.client(x) for x in ("sts", "iam", "bedrock", "logs", "lambda"))
creado = {}
resultados = []
original = {}


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def reintenta(f, errores=("InvalidParameterValueException", "ValidationException", "AccessDeniedException"), plazo=90):
    t0 = time.time()
    while True:
        try:
            return f()
        except ClientError as e:
            if codigo(e) not in errores or time.time() - t0 > plazo:
                raise
            time.sleep(5)


CODIGO_LAMBDA = '''
import base64, gzip, json, time
def handler(evento, contexto):
    ahora = int(time.time() * 1000)
    datos = json.loads(gzip.decompress(base64.b64decode(evento["awslogs"]["data"])))
    for ev in datos["logEvents"]:
        try:
            rid = json.loads(ev["message"]).get("requestId")
        except Exception:
            rid = None
        print(json.dumps({"llegada": ahora, "requestId": rid, "timestamp": ev["timestamp"]}))
'''


def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    if ":assumed-role/" in mi_arn:
        mi_arn = f"arn:aws:iam::{cuenta}:role/" + mi_arn.split(":assumed-role/")[1].split("/")[0]

    # 1. Acceso al modelo
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=MODELO)
    perfil_arn, modelos = p["inferenceProfileArn"], [m["modelArn"] for m in p["models"]]
    print(f"     {MODELO}: {p['status']}, {len(modelos)} foundation-model en "
          + ", ".join(sorted({m.split(':')[3] for m in modelos})), flush=True)

    # 2. Registro de invocaciones actual (se guarda para restaurarlo)
    actual = bedrock.get_model_invocation_logging_configuration().get("loggingConfig")
    original["config"] = actual
    print("     registro de invocaciones actual:", json.dumps(actual) if actual else "ninguno", flush=True)

    # 3. Grupo de logs y rol con el que Bedrock escribe en él
    logs.create_log_group(logGroupName=GRUPO, tags={"sonda": "nivel2"})
    creado["grupo"] = GRUPO
    logs.put_retention_policy(logGroupName=GRUPO, retentionInDays=1)
    grupo_arn = f"arn:aws:logs:{REGION}:{cuenta}:log-group:{GRUPO}"
    confianza_log = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": cuenta},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{REGION}:{cuenta}:*"}}}]}
    iam.create_role(RoleName=ROL_LOG, AssumeRolePolicyDocument=json.dumps(confianza_log), Tags=TAGS)
    creado["rol_log"] = ROL_LOG
    iam.put_role_policy(RoleName=ROL_LOG, PolicyName="escribir", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"{grupo_arn}:log-stream:*"}]}))

    # 4. Rol de sesión (el que usaría el kernel)
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": mi_arn},
        "Action": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]}]}
    iam.create_role(RoleName=ROL_SESION, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol_sesion"] = ROL_SESION
    iam.put_role_policy(RoleName=ROL_SESION, PolicyName="modelos", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": [perfil_arn] + modelos}]}))

    # 5. Lambda suscrita al grupo (mide la llegada real a un consumidor)
    if CON_LAMBDA:
        iam.create_role(RoleName=ROL_LAMBDA, Tags=TAGS, AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))
        creado["rol_lambda"] = ROL_LAMBDA
        iam.put_role_policy(RoleName=ROL_LAMBDA, PolicyName="logs", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{cuenta}:log-group:{GRUPO_LAMBDA}:*"}]}))
        logs.create_log_group(logGroupName=GRUPO_LAMBDA, tags={"sonda": "nivel2"})
        creado["grupo_lambda"] = GRUPO_LAMBDA
        logs.put_retention_policy(logGroupName=GRUPO_LAMBDA, retentionInDays=1)

    print("esperando 15 s a que IAM propague los roles…", flush=True)
    time.sleep(15)

    if CON_LAMBDA:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("llegada.py", CODIGO_LAMBDA)
        f = reintenta(lambda: lam.create_function(
            FunctionName=FUNCION, Runtime="python3.12", Handler="llegada.handler", Timeout=30, MemorySize=128,
            Role=f"arn:aws:iam::{cuenta}:role/{ROL_LAMBDA}", Code={"ZipFile": buf.getvalue()},
            Tags={"sonda": "nivel2"}))
        creado["lambda"] = FUNCION
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCION)
        lam.add_permission(FunctionName=FUNCION, StatementId="logs", Action="lambda:InvokeFunction",
                           Principal="logs.amazonaws.com", SourceArn=f"{grupo_arn}:*", SourceAccount=cuenta)
        reintenta(lambda: logs.put_subscription_filter(
            logGroupName=GRUPO, filterName=NOMBRE, filterPattern="", destinationArn=f["FunctionArn"]))
        creado["filtro"] = NOMBRE

    return cuenta


def activar_registro(cuenta, texto):
    cfg = {"cloudWatchConfig": {"logGroupName": GRUPO, "roleArn": f"arn:aws:iam::{cuenta}:role/{ROL_LOG}"},
           "textDataDeliveryEnabled": texto, "imageDataDeliveryEnabled": False,
           "embeddingDataDeliveryEnabled": False, "videoDataDeliveryEnabled": False,
           "audioDataDeliveryEnabled": False}
    reintenta(lambda: bedrock.put_model_invocation_logging_configuration(loggingConfig=cfg))
    creado["registro"] = True
    print(f"     registro de invocaciones activado hacia {GRUPO} (texto={texto})", flush=True)
    time.sleep(10)


def sesion(cuenta):
    r = sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{ROL_SESION}", RoleSessionName=RUN_ID,
                        SourceIdentity=OID, DurationSeconds=3600,
                        Tags=[{"Key": "oid", "Value": OID}, {"Key": "chat", "Value": CHAT},
                              {"Key": "run_id", "Value": RUN_ID}])
    print("     sesión:", r["AssumedRoleUser"]["Arn"], flush=True)
    c = r["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=REGION).client("bedrock-runtime")


# Haiku 4.5 no cachea por debajo de 4.096 tokens: el prompt de sistema pasa de 8.000 de largo.
SISTEMA = (f"Prueba {SUF}. Eres un asistente de pruebas. Responde siempre con una sola palabra. "
           + " ".join(f"Regla {i}: no añadas explicaciones, ni saludos, ni puntuación innecesaria." for i in range(600)))
MENSAJE = [{"role": "user", "content": [{"text": "Di hola."}]}]


def converse(rt, meta=None):
    kw = {"requestMetadata": meta} if meta else {}
    r = rt.converse(modelId=MODELO, inferenceConfig={"maxTokens": 10}, messages=MENSAJE, **kw)
    return r["ResponseMetadata"]["RequestId"], r["usage"]


def converse_stream(rt, cache=False, meta=None):
    kw = {"requestMetadata": meta} if meta else {}
    system = [{"text": SISTEMA}, {"cachePoint": {"type": "default"}}] if cache else [{"text": "Responde con una palabra."}]
    r = rt.converse_stream(modelId=MODELO, system=system, inferenceConfig={"maxTokens": 10}, messages=MENSAJE, **kw)
    uso = {}
    for ev in r["stream"]:
        if "metadata" in ev:
            uso = ev["metadata"].get("usage", {})
    return r["ResponseMetadata"]["RequestId"], uso


def llamar(rt):
    """Devuelve {requestId: (etiqueta, usage, hora_fin_ms, con_meta)}."""
    hechas = {}

    def apunta(etiqueta, f, con_meta=False):
        rid, uso = f()
        hechas[rid] = (etiqueta, uso, int(time.time() * 1000), con_meta)
        print(f"     {etiqueta}: {rid} uso={uso}", flush=True)

    apunta("Converse con requestMetadata", lambda: converse(rt, META), True)
    apunta("ConverseStream", lambda: converse_stream(rt))
    apunta("ConverseStream caché 1 (escritura)", lambda: converse_stream(rt, cache=True, meta=META), True)
    apunta("ConverseStream caché 2 (lectura)", lambda: converse_stream(rt, cache=True))
    return hechas


def leer(grupo, desde):
    eventos, token = [], None
    while True:
        kw = {"nextToken": token} if token else {}
        try:
            r = logs.filter_log_events(logGroupName=grupo, startTime=desde, **kw)
        except ClientError as e:
            if codigo(e) == "ResourceNotFoundException":
                return eventos
            raise
        eventos += r["events"]
        token = r.get("nextToken")
        if not token:
            return eventos


def esperar(hechas, desde):
    """Sondea FilterLogEvents (y el grupo de la Lambda) hasta ver todos los requestId o agotar el plazo."""
    vistos, en_lambda, registros = {}, {}, {}
    t0 = time.time()
    while time.time() - t0 < ESPERA_MAX:
        ahora = int(time.time() * 1000)
        for ev in leer(GRUPO, desde):
            try:
                reg = json.loads(ev["message"])
            except ValueError:
                continue
            rid = reg.get("requestId")
            if rid in hechas and rid not in vistos:
                vistos[rid] = ahora
                registros[rid] = (reg, ev)
                print(f"     registro de {hechas[rid][0]} visible a {(ahora - hechas[rid][2]) / 1000:.1f} s", flush=True)
        if CON_LAMBDA:
            for ev in leer(GRUPO_LAMBDA, desde):
                try:
                    m = json.loads(ev["message"])
                except ValueError:
                    continue
                if isinstance(m, dict) and m.get("requestId") in hechas and m["requestId"] not in en_lambda:
                    en_lambda[m["requestId"]] = m["llegada"]
        if len(vistos) == len(hechas) and (not CON_LAMBDA or len(en_lambda) == len(hechas)):
            break
        time.sleep(3)
    return vistos, en_lambda, registros


def busca(d, ruta=""):
    """Todas las rutas de claves de un JSON anidado."""
    if isinstance(d, dict):
        for k, v in d.items():
            yield f"{ruta}.{k}" if ruta else k, v
            yield from busca(v, f"{ruta}.{k}" if ruta else k)
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield from busca(v, f"{ruta}[{i}]")


def valor(reg, nombre):
    for ruta, v in busca(reg):
        if ruta.split(".")[-1] == nombre and not isinstance(v, (dict, list)):
            return v
    return None


def sin_texto(reg):
    r = json.loads(json.dumps(reg))
    for parte in ("input", "output"):
        for k in list(r.get(parte, {})):
            if "Body" in k:
                r[parte][k] = "<omitido>"
    return r


def latencias(nombre, pares):
    if not pares:
        anota(nombre, False, "sin datos")
        return
    s = [x / 1000 for x in pares]
    anota(nombre, True, f"mín {min(s):.1f} s, mediana {statistics.median(s):.1f} s, máx {max(s):.1f} s ({len(s)} registros)")


def analizar(hechas, vistos, en_lambda, registros, texto):
    modo = "con texto" if texto else "solo metadatos"
    anota(f"Llegan los registros ({modo})", len(vistos) == len(hechas), f"{len(vistos)} de {len(hechas)}")
    if not registros:
        return False
    rid0 = next(iter(registros))
    print("\n     registro de ejemplo (sin cuerpos):")
    print(json.dumps(sin_texto(registros[rid0][0]), indent=2, ensure_ascii=False))
    print("     campos:", ", ".join(sorted({r for reg, _ in registros.values() for r, _ in busca(reg)})), "\n")

    for rid, (reg, ev) in registros.items():
        etiqueta, uso, _, con_meta = hechas[rid]
        pares = {"inputTokenCount": "inputTokens", "outputTokenCount": "outputTokens",
                 "cacheReadInputTokenCount": "cacheReadInputTokens", "cacheWriteInputTokenCount": "cacheWriteInputTokens"}
        dif = {k: (valor(reg, k), uso.get(v)) for k, v in pares.items()}
        iguales = all((a or 0) == (b or 0) for a, b in dif.values())
        anota(f"Tokens del registro = usage de la API ({etiqueta})", iguales,
              ", ".join(f"{k}={a}/{b}" for k, (a, b) in dif.items()))
        if con_meta:
            plano = json.dumps(reg)
            anota(f"requestMetadata aparece en el registro ({etiqueta})", RUN_ID in plano and "coste" in plano,
                  json.dumps(reg.get("requestMetadata")) if "requestMetadata" in reg else "no está")

    reg = registros[rid0][0]
    plano = json.dumps([r for r, _ in registros.values()])
    arn = (reg.get("identity") or {}).get("arn", "")
    anota("identity.arn lleva el RoleSessionName (run_id)", RUN_ID in arn, arn)
    anota("El SourceIdentity (oid) aparece en el registro", OID in plano, "buscado el oid en todo el JSON")
    anota("Las etiquetas de sesión aparecen en el registro", CHAT in plano, "buscado el valor de chat en todo el JSON")
    ops = sorted({(r.get("operation"), r.get("modelId")) for r, _ in registros.values()})
    anota("operation y modelId", True, "; ".join(f"{o} → {m}" for o, m in ops))
    regiones = {r for r2, _ in registros.values() for r, v in busca(r2) if "region" in r.lower() or "inference" in r.lower()}
    anota("Campos de región", True, ", ".join(f"{r}={valor(reg, r.split('.')[-1])}" for r in sorted(regiones)) or "ninguno")
    ingesta = [ev["ingestionTime"] - ev["timestamp"] for _, ev in registros.values()]
    latencias("Retraso timestamp → ingestionTime en CloudWatch", ingesta)
    latencias("Latencia fin de la llamada → visible en FilterLogEvents",
              [vistos[r] - hechas[r][2] for r in vistos])
    if CON_LAMBDA:
        latencias("Latencia fin de la llamada → llegada a la Lambda",
                  [en_lambda[r] - hechas[r][2] for r in en_lambda])
    return True


def probar(cuenta):
    activar_registro(cuenta, texto=False)
    rt = sesion(cuenta)
    desde = int(time.time() * 1000) - 60_000
    hechas = llamar(rt)
    vistos, en_lambda, registros = esperar(hechas, desde)
    if analizar(hechas, vistos, en_lambda, registros, texto=False):
        return
    print("\n     sin registros con solo metadatos: se repite con texto activado", flush=True)
    activar_registro(cuenta, texto=True)
    desde = int(time.time() * 1000) - 60_000
    hechas = llamar(rt)
    vistos, en_lambda, registros = esperar(hechas, desde)
    analizar(hechas, vistos, en_lambda, registros, texto=True)


def borrar():
    print("restaurando el registro de invocaciones y borrando…", flush=True)
    if "registro" in creado or "config" in original:
        try:
            if original.get("config"):
                bedrock.put_model_invocation_logging_configuration(loggingConfig=original["config"])
            else:
                bedrock.delete_model_invocation_logging_configuration()
            final = bedrock.get_model_invocation_logging_configuration().get("loggingConfig")
            anota("Registro de invocaciones restaurado", final == original.get("config"),
                  json.dumps(final) if final else "ninguno, como estaba")
        except ClientError as e:
            print("   AVISO al restaurar el registro de invocaciones:", codigo(e), e, flush=True)
    pasos = [
        ("filtro", lambda: logs.delete_subscription_filter(logGroupName=GRUPO, filterName=NOMBRE)),
        ("lambda", lambda: lam.delete_function(FunctionName=FUNCION)),
        ("grupo_lambda", lambda: logs.delete_log_group(logGroupName=GRUPO_LAMBDA)),
        ("grupo", lambda: logs.delete_log_group(logGroupName=GRUPO)),
    ]
    for clave, f in pasos:
        if clave in creado:
            try:
                f()
            except ClientError as e:
                print(f"   aviso al borrar {clave}:", codigo(e), flush=True)
    for clave, rol in (("rol_sesion", ROL_SESION), ("rol_log", ROL_LOG), ("rol_lambda", ROL_LAMBDA)):
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
        probar(cuenta)
    finally:
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
