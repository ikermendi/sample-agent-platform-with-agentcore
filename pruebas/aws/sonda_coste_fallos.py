"""Sonda 7c del nivel 2: el registro de invocaciones de Bedrock con llamadas que fallan, streams cortados y carga.

Las sondas 7 y 7b vieron que el registro (solo metadatos) trae identidad y tokens iguales al `usage` en las
llamadas que terminan bien. Esta mira los bordes que importan a la Lambda de coste:

  1. Llamadas que fallan: Converse con AccessDenied (sesión con una política de sesión sin Bedrock), con
     ValidationException (maxTokens por encima del máximo del modelo; mensaje con content vacío) y con
     un modelo que no existe. ¿Llega registro? ¿Con qué campos (errorCode, tokens)?
  2. ConverseStream cortado por el cliente: pide contar hasta 500 y cierra el stream tras unos pocos trozos
     (y otro tras ~2 s). ¿Llega registro? ¿Con los tokens generados hasta el corte, los que habría
     generado (se compara con una llamada igual sin cortar) o ninguno? Y otro cortado por maxTokens.
  3. Throttling: ráfaga en paralelo con maxTokens=1 (la cuota de Haiku 4.5 entre regiones es de pocas
     llamadas por minuto). ¿Dejan registro las ThrottlingException?
  4. inferenceRegion en ~20 llamadas seguidas con el perfil eu.
  5. Latencia hasta FilterLogEvents con esas ~20 llamadas.

RoleSessionName = run_id con el formato de runid_coste.py y parseo de identity.arn con su parsear_arn.
Los reintentos de botocore están apagados para que cada intento sea una petición con su requestId.
Guarda y restaura la configuración del registro de invocaciones (o la borra si no había), también si
falla. Crea "sonda-n2-<sufijo>" (rol de sesión, rol del registro, grupo de logs) con la etiqueta
sonda=nivel2 y lo borra todo al acabar. Coste: céntimos (Haiku 4.5). Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 python sonda_coste_fallos.py
"""
import json
import os
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from runid_coste import parsear_arn, run_id, ulid

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0")
NO_EXISTE = f"{PREFIJO}.anthropic.claude-no-existe-20990101-v1:0"
ESPERA_MAX = int(os.environ.get("SONDA_ESPERA", "600"))
N_SEGUIDAS = int(os.environ.get("SONDA_SEGUIDAS", "20"))
N_RAFAGA = int(os.environ.get("SONDA_RAFAGA", "30"))
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
ROL_SESION = f"{NOMBRE}-sesion"
ROL_LOG = f"{NOMBRE}-registro"
GRUPO = f"sonda-n2-{SUF}-invocaciones"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]

OID = str(uuid.uuid4())
RUN_OK = run_id(OID, ulid())
RUN_DENEGADO = run_id(OID, ulid())
MENSAJE = [{"role": "user", "content": [{"text": "Di hola."}]}]
CONTAR = [{"role": "user", "content": [{"text": "Cuenta del 1 al 500 en cifras, separados por comas, sin nada más."}]}]
SIN_REINTENTOS = Config(retries={"total_max_attempts": 1, "mode": "standard"}, parameter_validation=False,
                        max_pool_connections=64, read_timeout=120)

ses = boto3.Session(region_name=REGION)
sts, iam, bedrock, logs = (ses.client(x) for x in ("sts", "iam", "bedrock", "logs"))
creado, original, resultados = {}, {}, []
hechas = {}  # requestId → {etiqueta, run_id, fin (ms), uso, error, recibido}
cerrojo = threading.Lock()


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def reintenta(f, errores=("ValidationException", "AccessDeniedException", "AccessDenied"), plazo=90):
    t0 = time.time()
    while True:
        try:
            return f()
        except ClientError as e:
            if codigo(e) not in errores or time.time() - t0 > plazo:
                raise
            time.sleep(5)


def apunta(rid, etiqueta, run, **kw):
    with cerrojo:
        hechas[rid] = {"etiqueta": etiqueta, "run_id": run, "fin": int(time.time() * 1000), **kw}


def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    if ":assumed-role/" in mi_arn:
        mi_arn = f"arn:aws:iam::{cuenta}:role/" + mi_arn.split(":assumed-role/")[1].split("/")[0]
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=MODELO)
    regiones = sorted({m["modelArn"].split(":")[3] for m in p["models"]})
    print(f"     {MODELO}: {p['status']}, regiones de destino: {', '.join(regiones)}", flush=True)

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

    # Resource "*" para que el modelo inexistente llegue a Bedrock y no lo pare IAM antes.
    iam.create_role(RoleName=ROL_SESION, Tags=TAGS, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"AWS": mi_arn},
            "Action": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]}]}))
    creado["rol_sesion"] = ROL_SESION
    iam.put_role_policy(RoleName=ROL_SESION, PolicyName="modelos", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": "*"}]}))
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


def cliente(cuenta, nombre, politica=None):
    kw = {"Policy": json.dumps(politica)} if politica else {}
    r = reintenta(lambda: sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{ROL_SESION}", RoleSessionName=nombre,
                                          SourceIdentity=OID, DurationSeconds=3600, **kw),
                  errores=("AccessDenied",), plazo=60)
    c = r["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=REGION).client("bedrock-runtime",
                                                                                        config=SIN_REINTENTOS)


def intenta(etiqueta, run, f):
    """Ejecuta f() → (requestId, uso, extra). Si falla, apunta el requestId del error."""
    try:
        rid, uso, extra = f()
        apunta(rid, etiqueta, run, uso=uso, error=None, **extra)
        print(f"     {etiqueta}: {rid} uso={uso} {extra or ''}", flush=True)
        return True, None
    except ClientError as e:
        rid = e.response.get("ResponseMetadata", {}).get("RequestId")
        apunta(rid, etiqueta, run, uso={}, error=codigo(e))
        print(f"     {etiqueta}: {rid} {codigo(e)}: {str(e)[:150]}", flush=True)
        return False, codigo(e)


def converse(rt, modelo=MODELO, max_tokens=5, mensajes=MENSAJE):
    r = rt.converse(modelId=modelo, inferenceConfig={"maxTokens": max_tokens}, messages=mensajes)
    return r["ResponseMetadata"]["RequestId"], r["usage"], {"stop": r["stopReason"]}


def stream(rt, max_tokens=4000, corta_trozos=None, corta_segundos=None):
    """ConverseStream de la cuenta hasta 500; puede cerrar la conexión tras N trozos o S segundos."""
    r = rt.converse_stream(modelId=MODELO, inferenceConfig={"maxTokens": max_tokens}, messages=CONTAR)
    rid, t0 = r["ResponseMetadata"]["RequestId"], time.time()
    uso, stop, trozos, texto = {}, None, 0, ""
    es = r["stream"]
    for ev in es:
        if "contentBlockDelta" in ev:
            trozos += 1
            texto += ev["contentBlockDelta"]["delta"].get("text", "")
            if (corta_trozos and trozos >= corta_trozos) or (corta_segundos and time.time() - t0 >= corta_segundos):
                es.close()  # cierra la respuesta HTTP subyacente
                break
        elif "messageStop" in ev:
            stop = ev["messageStop"].get("stopReason")
        elif "metadata" in ev:
            uso = ev["metadata"].get("usage", {})
    numeros = [x for x in texto.replace(" ", "").split(",") if x.isdigit()]
    return rid, uso, {"stop": stop or "cortado por el cliente", "trozos": trozos, "caracteres": len(texto),
                      "ultimo_numero": numeros[-1] if numeros else None, "segundos": round(time.time() - t0, 1)}


def parte1_2(cuenta, rt):
    # 1. Fallos
    rt_denegado = cliente(cuenta, RUN_DENEGADO, politica={"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"}]})
    intenta("fallo: AccessDenied (política de sesión sin Bedrock)", RUN_DENEGADO, lambda: converse(rt_denegado))
    intenta("fallo: ValidationException (maxTokens 1.000.000)", RUN_OK, lambda: converse(rt, max_tokens=1_000_000))
    # (Un primer mensaje de "assistant" no falla: Bedrock lo toma como prefill.)
    intenta("fallo: ValidationException (mensaje con content vacío)", RUN_OK, lambda: converse(
        rt, mensajes=[{"role": "user", "content": []}]))
    intenta("fallo: modelo que no existe", RUN_OK, lambda: converse(rt, modelo=NO_EXISTE))
    # 2. Streams (con pausas: la cuota de Haiku 4.5 entre regiones es de pocas llamadas por minuto)
    for etiqueta, kw in (("stream completo (referencia)", {}),
                         ("stream cortado tras 3 trozos", {"corta_trozos": 3}),
                         ("stream cortado tras 2 s", {"corta_segundos": 2}),
                         ("stream con maxTokens=50", {"max_tokens": 50})):
        time.sleep(7)
        intenta(etiqueta, RUN_OK, lambda kw=kw: stream(rt, **kw))


def parte4(rt):
    """N llamadas seguidas; si hay throttling, espera y repite (el intento rechazado queda apuntado)."""
    hechas_ok, throttles = 0, 0
    while hechas_ok < N_SEGUIDAS:
        ok, err = intenta(f"seguida {hechas_ok + 1}", RUN_OK, lambda: converse(rt, max_tokens=1))
        if ok:
            hechas_ok += 1
            time.sleep(1)
        elif err == "ThrottlingException":
            throttles += 1
            time.sleep(10)
        else:
            break
    print(f"     {hechas_ok} seguidas hechas, {throttles} ThrottlingException por el camino", flush=True)


def parte3(rt):
    def una(i):
        return intenta(f"ráfaga {i + 1}", RUN_OK, lambda: converse(rt, max_tokens=1))
    with ThreadPoolExecutor(max_workers=N_RAFAGA) as ex:
        res = list(ex.map(una, range(N_RAFAGA)))
    print("     ráfaga:", dict(Counter("ok" if ok else err for ok, err in res)), flush=True)


def leer(desde):
    eventos, token = [], None
    while True:
        r = logs.filter_log_events(logGroupName=GRUPO, startTime=desde, **({"nextToken": token} if token else {}))
        eventos += r["events"]
        token = r.get("nextToken")
        if not token:
            return eventos


vistos, registros, extra = {}, {}, {}
parar = threading.Event()


def sondeo(desde):
    """Hilo que lee el grupo cada 3 s desde el principio y apunta cuándo se ve cada registro por primera vez."""
    while not parar.is_set():
        try:
            eventos = leer(desde)
        except ClientError as e:
            print("     aviso en el sondeo:", codigo(e), flush=True)
            eventos = []
        ahora = int(time.time() * 1000)
        for ev in eventos:
            try:
                reg = json.loads(ev["message"])
            except ValueError:
                continue
            rid = reg.get("requestId")
            with cerrojo:
                if rid not in vistos and rid not in extra:
                    vistos[rid], registros[rid] = ahora, reg
        parar.wait(3)


def esperar():
    """Espera hasta ver todas las llamadas buenas y 120 s más (para los fallos), o agotar el plazo."""
    t0, todas_ok = time.time(), None
    buenas = {r for r, h in hechas.items() if h["error"] is None}
    while time.time() - t0 < ESPERA_MAX:
        if buenas <= set(vistos) and todas_ok is None:
            todas_ok = time.time()
            print(f"     vistas todas las buenas ({len(buenas)}); 120 s más por si llegan los fallos…", flush=True)
        if todas_ok and (time.time() - todas_ok > 120 or set(hechas) <= set(vistos)):
            break
        time.sleep(5)
    parar.set()
    for rid in [r for r in registros if r not in hechas]:
        extra[rid] = registros.pop(rid)
        vistos.pop(rid)
    return vistos, registros, extra


def sin_texto(reg):
    r = json.loads(json.dumps(reg))
    for parte in ("input", "output"):
        for k in list(r.get(parte, {})):
            if "Body" in k:
                r[parte][k] = "<omitido>"
    return r


def tokens(reg):
    i, o = reg.get("input", {}), reg.get("output", {})
    return {"in": i.get("inputTokenCount"), "out": o.get("outputTokenCount"),
            "cacheR": i.get("cacheReadInputTokenCount"), "cacheW": i.get("cacheWriteInputTokenCount")}


def analizar(vistos, registros, extra):
    print(f"\n     {len(hechas)} peticiones, {len(registros)} con registro, {len(extra)} registros ajenos a la sonda\n")
    for reg in list(registros.values()) + list(extra.values()):
        if "errorCode" in json.dumps(reg) or reg.get("errorCode"):
            print("     registro de ejemplo de un fallo (sin cuerpos):")
            print(json.dumps(sin_texto(reg), indent=2, ensure_ascii=False))
            break

    # 1. Fallos
    for rid, h in hechas.items():
        if not h["etiqueta"].startswith("fallo"):
            continue
        reg = registros.get(rid)
        anota(f"Registro de {h['etiqueta']}", True,
              f"{h['error']}; " + (f"llega: claves {sorted(reg)}, errorCode={reg.get('errorCode')}, "
                                   f"tokens={tokens(reg)}, arn={reg.get('identity', {}).get('arn')}" if reg
                                   else "no llega registro"))

    # 2. Streams
    ejemplo = None
    for rid, h in hechas.items():
        if not h["etiqueta"].startswith("stream"):
            continue
        reg = registros.get(rid)
        if reg and "cortado tras" in h["etiqueta"] and ejemplo is None:
            ejemplo = reg
        anota(f"Registro de {h['etiqueta']}", reg is not None,
              f"API: uso={h.get('uso')}, stop={h.get('stop')}, trozos={h.get('trozos')}, caracteres={h.get('caracteres')}, "
              f"último número={h.get('ultimo_numero')}, {h.get('segundos')} s; registro: "
              + (f"tokens={tokens(reg)}, errorCode={reg.get('errorCode')}, operation={reg.get('operation')}"
                 if reg else "no llega"))
    if ejemplo:
        print("\n     registro de ejemplo de un stream cortado (sin cuerpos):")
        print(json.dumps(sin_texto(ejemplo), indent=2, ensure_ascii=False))

    # 3. Throttling
    thr = {r: h for r, h in hechas.items() if h["error"] == "ThrottlingException"}
    con = [r for r in thr if r in registros]
    anota("Registro de las ThrottlingException", True,
          f"{len(con)} de {len(thr)} con registro" + (f"; ejemplo: errorCode={registros[con[0]].get('errorCode')}, "
                                                      f"tokens={tokens(registros[con[0]])}" if con else ""))

    # Parseo de identity.arn en todos los registros de la sonda
    malos = [r for r, reg in registros.items()
             if (parsear_arn((reg.get("identity") or {}).get("arn"), roles={ROL_SESION}) or {}).get("run_id")
             != hechas[r]["run_id"] or parsear_arn(reg["identity"]["arn"])["oid"] != OID]
    anota("parsear_arn saca oid y run_id de todos los registros", not malos and bool(registros),
          f"{len(registros) - len(malos)} de {len(registros)}")

    # 4. inferenceRegion
    regs = Counter(reg.get("inferenceRegion") for reg in registros.values() if reg.get("inferenceRegion"))
    sin = Counter(hechas[r]["etiqueta"].split(":")[0].split(" ")[0] for r, reg in registros.items()
                  if not reg.get("inferenceRegion"))
    anota("Distribución de inferenceRegion (todos los registros)", True,
          ", ".join(f"{k}: {v}" for k, v in regs.most_common()) + (f"; sin inferenceRegion: {dict(sin)}" if sin else ""))
    seg = Counter(registros[r].get("inferenceRegion") for r, h in hechas.items()
                  if h["etiqueta"].startswith("seguida") and r in registros and h["error"] is None)
    anota(f"inferenceRegion en las llamadas seguidas", bool(seg), ", ".join(f"{k}: {v}" for k, v in seg.most_common()))

    # 5. Latencias
    for nombre, filtro in (("seguidas", "seguida"), ("todas", "")):
        s = [(vistos[r] - h["fin"]) / 1000 for r, h in hechas.items() if r in vistos and h["etiqueta"].startswith(filtro)]
        if s:
            anota(f"Latencia fin de la llamada → visible en FilterLogEvents ({nombre})", True,
                  f"mín {min(s):.1f} s, mediana {statistics.median(s):.1f} s, máx {max(s):.1f} s ({len(s)} registros)")


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
        rt = cliente(cuenta, RUN_OK)
        desde = int(time.time() * 1000) - 60_000
        threading.Thread(target=sondeo, args=(desde,), daemon=True).start()
        parte1_2(cuenta, rt)
        print("     pausa de 60 s para vaciar la cuota antes de las llamadas seguidas…", flush=True)
        time.sleep(60)
        parte4(rt)
        parte3(rt)
        esperar()
        analizar(vistos, registros, extra)
    finally:
        parar.set()
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
