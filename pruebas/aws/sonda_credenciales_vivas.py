"""Sonda 8 del nivel 2: el Agent del chat se monta una vez y cada mensaje trae credenciales STS nuevas.

El kernel (Strands 1.63) reutiliza el Agent y su BedrockModel entre los mensajes del mismo chat en la misma
microVM; lo único que cambia por mensaje son las credenciales. El boto3.Session del modelo toma las
credenciales de un objeto propio cuya copia vigente cambia el kernel; botocore lo lee al firmar cada
petición. Esta sonda comprueba con Bedrock de verdad (Claude Haiku 4.5):

  1. Con el rol A (RoleSessionName msg-1) el Agent contesta, en streaming (ConverseStream) y sin él
     (Converse), y también un subagente con su propio BedrockModel sobre el mismo session.
  2. Cambiar a B (msg-2, sin permiso de Bedrock) a mitad de un ConverseStream no corta esa respuesta:
     la petición ya iba firmada.
  3. La llamada siguiente, sin tocar el Agent, da AccessDenied (usa las credenciales nuevas).
  4. Con A' (otra AssumeRole, msg-3) vuelve a ir bien, con el mismo Agent.
  5. En cada paso, sts:GetCallerIdentity con un cliente del mismo session devuelve el ARN del mensaje.

Crea "sonda-n2-<sufijo>-a" y "-b" con la etiqueta sonda=nivel2 y los borra al acabar aunque falle.
Coste: céntimos (Haiku 4.5). Necesita strands-agents==1.63.0 en el entorno. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_credenciales_vivas.py
"""
import json
import os
import sys
import time
import uuid

import boto3
import botocore.session
from botocore.credentials import CredentialProvider, CredentialResolver, Credentials, ReadOnlyCredentials
from botocore.exceptions import ClientError
from strands import Agent
from strands.models.bedrock import BedrockModel

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0")
SUF = uuid.uuid4().hex[:8]
ROL_A, ROL_B = f"sonda-n2-{SUF}-a", f"sonda-n2-{SUF}-b"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]

ses_admin = boto3.Session(region_name=REGION)
sts, iam, bedrock = (ses_admin.client(x) for x in ("sts", "iam", "bedrock"))
creados = []
resultados = []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


# ---------------------------------------------------------------- lo que iría al kernel
class CredencialesDelMensaje(Credentials):
    """botocore llama a get_frozen_credentials() al firmar cada petición: cada una usa las vigentes."""
    method = "kernel"

    def __init__(self):
        self._v = None

    def actualizar(self, c):
        self._v = ReadOnlyCredentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"], None)

    def get_frozen_credentials(self):
        return self._v

    access_key = property(lambda s: s._v.access_key)
    secret_key = property(lambda s: s._v.secret_key)
    token = property(lambda s: s._v.token)
    account_id = property(lambda s: s._v.account_id)


def sesion_boto(credenciales, region):
    class Proveedor(CredentialProvider):
        METHOD = "kernel"

        def load(self):
            return credenciales
    bs = botocore.session.Session()
    bs.register_component("credential_provider", CredentialResolver([Proveedor()]))
    return boto3.Session(botocore_session=bs, region_name=region)


# ---------------------------------------------------------------- preparación
def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    if ":assumed-role/" in mi_arn:
        mi_arn = f"arn:aws:iam::{cuenta}:role/" + mi_arn.split(":assumed-role/")[1].split("/")[0]
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=MODELO)
    recursos = [p["inferenceProfileArn"]] + [m["modelArn"] for m in p["models"]]
    confianza = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": mi_arn}, "Action": "sts:AssumeRole"}]})
    for rol in (ROL_A, ROL_B):
        iam.create_role(RoleName=rol, AssumeRolePolicyDocument=confianza, Tags=TAGS)
        creados.append(rol)
    iam.put_role_policy(RoleName=ROL_A, PolicyName="modelo", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": recursos}]}))
    print("esperando 15 s a que IAM propague los roles…", flush=True)
    time.sleep(15)
    return cuenta


def asumir(cuenta, rol, nombre):
    t0 = time.time()
    while True:
        try:
            return sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{rol}", RoleSessionName=nombre,
                                   DurationSeconds=3600)["Credentials"]
        except ClientError as e:
            if codigo(e) != "AccessDenied" or time.time() - t0 > 90:
                raise
            time.sleep(5)


def preguntar(agente, texto):
    """Llama al agente; si falla, quita lo que haya dejado en el historial y devuelve el código de error."""
    n = len(agente.messages)
    try:
        return "ok", str(agente(texto)).strip()
    except Exception as e:
        quedan = len(agente.messages) - n
        del agente.messages[n:]
        causa = e
        while causa is not None and not isinstance(causa, ClientError):
            causa = causa.__cause__ or causa.__context__
        return codigo(causa) if causa is not None else type(e).__name__, f"{quedan} mensajes sueltos en el historial"


def probar(cuenta):
    cred = CredencialesDelMensaje()
    cred.actualizar(asumir(cuenta, ROL_A, "msg-1"))
    ses = sesion_boto(cred, REGION)
    quien = ses.client("sts")

    cambiar_a = {}

    def al_recibir(**kw):  # cambia las credenciales a mitad de la respuesta en streaming
        if "data" in kw:
            cambiar_a.setdefault("trozos", 0)
            cambiar_a["trozos"] += 1
            if cambiar_a.get("credenciales") and cambiar_a["trozos"] == 1:
                cred.actualizar(cambiar_a.pop("credenciales"))
                cambiar_a["cambiado"] = True

    corto = dict(max_tokens=60)
    stream = Agent(model=BedrockModel(model_id=MODELO, boto_session=ses, **corto), callback_handler=al_recibir,
                   system_prompt="Contesta en una línea.")
    sin_stream = Agent(model=BedrockModel(model_id=MODELO, boto_session=ses, streaming=False, **corto),
                       callback_handler=None, system_prompt="Contesta en una línea.")
    sub = Agent(model=BedrockModel(model_id=MODELO, boto_session=ses, **corto), callback_handler=None,
                name="revisor", system_prompt="Contesta en una línea.")
    ids = [id(stream), id(stream.model), id(sin_stream.model), id(sub.model)]

    def paso(etiqueta, sesion_esperada, esperado):
        arn = quien.get_caller_identity()["Arn"]
        anota(f"{etiqueta}: GetCallerIdentity del mismo session", arn.endswith(f"/{sesion_esperada}"), arn)
        for nombre, ag in (("ConverseStream", stream), ("Converse", sin_stream), ("subagente", sub)):
            r, det = preguntar(ag, "Di hola")
            ok = (r == "ok") if esperado == "ok" else (r == esperado)
            anota(f"{etiqueta}: {nombre}", ok, f"{r} · {det[:80]}")

    # 1. Rol A
    paso("A msg-1", "msg-1", "ok")

    # 2. Cambio a B a mitad de un ConverseStream
    cambiar_a["credenciales"] = asumir(cuenta, ROL_B, "msg-2")
    cambiar_a["trozos"] = 0
    r, det = preguntar(stream, "Cuenta del 1 al 15 separando con comas.")
    anota("cambio a B a mitad de un ConverseStream: la respuesta en curso termina bien",
          r == "ok" and cambiar_a.get("cambiado") and cambiar_a["trozos"] > 1,
          f"{r} · {cambiar_a['trozos']} trozos · {det[:60]}")

    # 3. Rol B, sin tocar el Agent
    paso("B msg-2", "msg-2", "AccessDeniedException")

    # 4. Rol A' (otra AssumeRole)
    cred.actualizar(asumir(cuenta, ROL_A, "msg-3"))
    paso("A' msg-3", "msg-3", "ok")

    anota("los mismos objetos Agent y BedrockModel en todos los pasos",
          ids == [id(stream), id(stream.model), id(sin_stream.model), id(sub.model)])
    r, det = preguntar(stream, "¿Qué números contaste antes? Solo el último.")
    anota("el Agent conserva la conversación entre mensajes", r == "ok" and "15" in det, det[:60])


def borrar():
    for rol in creados:
        try:
            for p in iam.list_role_policies(RoleName=rol)["PolicyNames"]:
                iam.delete_role_policy(RoleName=rol, PolicyName=p)
            iam.delete_role(RoleName=rol)
            print("     borrado", rol, flush=True)
        except ClientError as e:
            print("     NO se pudo borrar", rol, codigo(e), flush=True)
    restos = [r["RoleName"] for pag in iam.get_paginator("list_roles").paginate()
              for r in pag["Roles"] if r["RoleName"].startswith(f"sonda-n2-{SUF}")]
    anota("sin restos en IAM", not restos, ", ".join(restos))


if __name__ == "__main__":
    print(f"región {REGION}, modelo {MODELO}, sufijo {SUF}", flush=True)
    try:
        probar(crear())
    except Exception as e:
        anota("la sonda terminó con una excepción", False, f"{type(e).__name__}: {e}")
    finally:
        borrar()
    malas = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(malas)} de {len(resultados)} bien", flush=True)
    sys.exit(1 if malas else 0)
