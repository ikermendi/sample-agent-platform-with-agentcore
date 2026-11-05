"""Sonda 20 del nivel 2: cómo distingue el kernel el corte por presupuesto de cualquier otro AccessDenied.

El kernel no lee el gasto del mes: cuando la Lambda de coste mete el oid del usuario en `DENY#`, la
siguiente llamada a Bedrock recibe un AccessDeniedException y el kernel termina con `end` (`limit`)
en lugar de `error`. Para no confundirlo con otros AccessDenied, el diseño se apoya en que el único
deny explícito sobre Bedrock en el rol de sesión es esa lista, y en que un modelo fuera de la versión
es un deny implícito (la política de sesión no lo permite). Esta sonda comprueba que el mensaje de
Bedrock permite separar los dos casos (fila "Cost from invocation logging" de "To verify"):

  1. Sesión como la del diseño (run_id de nombre, oid de SourceIdentity, política de sesión con un solo
     modelo). Llamada al modelo permitido: bien.
  2. La misma sesión llama a otro modelo que el rol permite pero la sesión no: deny implícito. Se
     guarda el código y el mensaje completos.
  3. Se añade el oid a una política inline Deny por `aws:SourceIdentity` (como la Lambda de coste) y
     se llama al modelo permitido hasta el primer AccessDenied (máx. 120 s): deny explícito. Converse
     y ConverseStream, con su código y su mensaje.
  4. Lo mismo a través de Strands (`Agent` con `BedrockModel` y esa sesión, en streaming), que es como
     llama el kernel: qué excepción llega al kernel y si el ClientError original está en la cadena.
  5. Aplica a los tres casos el clasificador que usaría el kernel (`es_limite`: AccessDeniedException
     y "explicit deny" en el mensaje) y comprueba que solo el 3 y el 4 salen como límite.

Si el mensaje no dice "explicit deny", el resultado de esta sonda es el que hay que llevar al diseño:
o el kernel termina con `error` en el corte (el corte funciona igual), o necesita otra señal.
Reintentos de botocore apagados. Coste: céntimos (llamadas de 1 token de salida).
Crea un rol "sonda-n2-<sufijo>" con la etiqueta sonda=nivel2 y lo borra al acabar. Uso:

    SONDA_REGION=eu-north-1 SONDA_MODELO=eu.anthropic.claude-haiku-4-5-20251001-v1:0 \\
      SONDA_OTRO=eu.anthropic.claude-sonnet-4-6 python sonda_limite_bedrock.py
"""
import json
import os
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from runid_coste import run_id, ulid

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0")
OTRO = os.environ.get("SONDA_OTRO", f"{PREFIJO}.anthropic.claude-sonnet-4-6")
ESPERA_MAX = int(os.environ.get("SONDA_ESPERA_MAX", "120"))
ESPACIO = 6.5  # s entre llamadas a un mismo modelo: la cuota de Haiku 4.5 es de 10 por minuto
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
SIN_REINTENTOS = Config(retries={"total_max_attempts": 1, "mode": "standard"})

ses = boto3.Session(region_name=REGION)
sts, iam, bedrock = (ses.client(x) for x in ("sts", "iam", "bedrock"))
creado = {}
resultados = []
errores = {}  # caso -> (código, mensaje)
ultima = {}


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def mensaje(e):
    return e.response.get("Error", {}).get("Message", "") if isinstance(e, ClientError) else str(e)


def client_error_en_cadena(e):
    """El ClientError original, buscándolo por __cause__ y __context__ (Strands puede envolverlo)."""
    vistos = set()
    while e is not None and id(e) not in vistos:
        if isinstance(e, ClientError):
            return e
        vistos.add(id(e))
        e = e.__cause__ or e.__context__
    return None


def es_limite(e):
    """El clasificador del kernel: el corte por presupuesto es el único deny explícito sobre Bedrock."""
    ce = client_error_en_cadena(e)
    return ce is not None and codigo(ce) == "AccessDeniedException" and "explicit deny" in mensaje(ce).lower()


def espera_turno(modelo):
    falta = ultima.get(modelo, 0) + ESPACIO - time.time()
    if falta > 0:
        time.sleep(falta)
    ultima[modelo] = time.time()


def perfil(pid):
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=pid)
    return [p["inferenceProfileArn"]] + [m["modelArn"] for m in p["models"]]


def politica_sesion(recursos):
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "Modelos", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": recursos},
        {"Sid": "Resto", "Effect": "Allow",
         "NotAction": ["bedrock:InvokeModel*", "s3:GetObject", "s3:ListBucket"], "Resource": "*"}]},
        separators=(",", ":"))


def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    if ":assumed-role/" in mi_arn:
        mi_arn = f"arn:aws:iam::{cuenta}:role/" + mi_arn.split(":assumed-role/")[1].split("/")[0]
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": mi_arn},
        "Action": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]}]}
    iam.create_role(RoleName=NOMBRE, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol"] = NOMBRE
    rol = {"Version": "2012-10-17", "Statement": [{
        "Sid": "Claude", "Effect": "Allow",
        "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        "Resource": [f"arn:aws:bedrock:*:{cuenta}:inference-profile/*", "arn:aws:bedrock:*::foundation-model/*"]}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="rol", PolicyDocument=json.dumps(rol))
    print("esperando 15 s a que IAM propague el rol…", flush=True)
    time.sleep(15)
    return cuenta


def sesion(cuenta, oid):
    pol = politica_sesion(perfil(MODELO))
    for intento in range(6):
        try:
            r = sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{NOMBRE}", RoleSessionName=run_id(oid, ulid()),
                                SourceIdentity=oid, DurationSeconds=3600, Policy=pol)
            break
        except ClientError as e:
            if codigo(e) != "AccessDenied" or intento == 5:
                raise
            time.sleep(5)
    c = r["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=REGION)


def converse(rt, modelo):
    espera_turno(modelo)
    rt.converse(modelId=modelo, inferenceConfig={"maxTokens": 1},
                messages=[{"role": "user", "content": [{"text": "1"}]}])


def converse_stream(rt, modelo):
    espera_turno(modelo)
    r = rt.converse_stream(modelId=modelo, inferenceConfig={"maxTokens": 1},
                           messages=[{"role": "user", "content": [{"text": "1"}]}])
    for _ in r["stream"]:
        pass


def guarda(caso, e):
    ce = client_error_en_cadena(e)
    errores[caso] = (type(e).__name__, codigo(ce) if ce else None, mensaje(ce) if ce else str(e), es_limite(e))
    tipo, cod, msg, lim = errores[caso]
    print(f"     [{caso}] excepción {tipo} · código {cod} · es_limite={lim}\n       mensaje: {msg}", flush=True)


def partes(cuenta):
    oid = str(uuid.uuid4())
    s = sesion(cuenta, oid)
    rt = s.client("bedrock-runtime", config=SIN_REINTENTOS)

    # 1. El modelo permitido funciona
    try:
        converse(rt, MODELO)
        anota("Modelo permitido por la sesión: bien", True, MODELO)
    except ClientError as e:
        anota("Modelo permitido por la sesión: bien", False, f"{codigo(e)}: {mensaje(e)[:200]}")
        return

    # 2. Deny implícito: otro modelo que el rol deja pero la sesión no
    try:
        converse(rt, OTRO)
        anota("Otro modelo, fuera de la política de sesión: denegado", False, "se permitió")
    except ClientError as e:
        guarda("implícito", e)
        anota("Otro modelo, fuera de la política de sesión: denegado", codigo(e) == "AccessDeniedException", codigo(e))

    # 3. Deny explícito por SourceIdentity, como la Lambda de coste
    deny = {"Version": "2012-10-17", "Statement": [{
        "Sid": "SinPresupuesto", "Effect": "Deny", "Action": "bedrock:InvokeModel*", "Resource": "*",
        "Condition": {"StringEquals": {"aws:SourceIdentity": [oid]}}}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="corte", PolicyDocument=json.dumps(deny))
    t0, corte = time.time(), None
    while time.time() - t0 < ESPERA_MAX:
        try:
            converse(rt, MODELO)
        except ClientError as e:
            if codigo(e) == "AccessDeniedException":
                corte = time.time() - t0
                guarda("explícito (Converse)", e)
                break
            if codigo(e) != "ThrottlingException":
                print("     error inesperado:", codigo(e), mensaje(e)[:160], flush=True)
    anota("Deny por SourceIdentity: AccessDenied en la sesión ya emitida", corte is not None,
          f"a {corte:.1f} s" if corte is not None else f"sin corte en {ESPERA_MAX} s")
    if corte is None:
        return
    try:
        converse_stream(rt, MODELO)
        anota("ConverseStream con el deny explícito: denegado", False, "se permitió")
    except ClientError as e:
        guarda("explícito (ConverseStream)", e)
        anota("ConverseStream con el deny explícito: denegado", codigo(e) == "AccessDeniedException", codigo(e))

    # 4. A través de Strands, como el kernel
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel
    except ImportError:
        anota("Strands disponible para la parte 4", False, "instala strands-agents en este entorno")
    else:
        espera_turno(MODELO)
        modelo = BedrockModel(boto_session=s, model_id=MODELO, max_tokens=1, streaming=True,
                              boto_client_config=SIN_REINTENTOS)
        try:
            Agent(model=modelo, callback_handler=None)("1")
            anota("Strands con el deny explícito: excepción", False, "se permitió")
        except Exception as e:  # noqa: BLE001 - se registra el tipo que llega al kernel
            guarda("explícito (Strands)", e)
            anota("Strands con el deny explícito: el ClientError está en la cadena",
                  client_error_en_cadena(e) is not None, type(e).__name__)

    # 5. El clasificador del kernel
    for caso, (_, _, _, lim) in errores.items():
        espera = caso.startswith("explícito")
        anota(f"es_limite en el caso {caso}: {'límite' if espera else 'error'}", lim == espera,
              f"devuelve {lim}")


def borrar():
    print("borrando…", flush=True)
    for pol in ("rol", "corte"):
        try:
            iam.delete_role_policy(RoleName=NOMBRE, PolicyName=pol)
        except ClientError:
            pass
    if "rol" in creado:
        try:
            iam.delete_role(RoleName=NOMBRE)
        except ClientError as e:
            print("   aviso al borrar:", codigo(e))


if __name__ == "__main__":
    try:
        partes(crear())
    finally:
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print("\nMensajes completos, para el documento:")
    for caso, (tipo, cod, msg, lim) in errores.items():
        print(f"  {caso}: {tipo} / {cod}: {msg}")
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
