"""Sonda 3 del nivel 2: inferencia directa a Claude por perfil, con las credenciales de la sesión.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre la llamada al modelo
(secciones "Inferencia directa" en Por verificar y "Credenciales del kernel"):

  1. Los perfiles de inferencia de Claude disponibles en la cuenta (ListInferenceProfiles).
  2. Con una política por ARN de perfil y de sus `foundation-model`, la sesión llama a ese modelo
     en streaming (ConverseStream) y recibe el uso completo, incluida la caché de prompt.
  3. La misma sesión no puede llamar a otro modelo (AccessDenied).
  4. Cuánto tarda en aplicarse una denegación de IAM por `aws:SourceIdentity` sobre una sesión ya
     emitida (IAM no da plazo): se añade la denegación y se llama cada 2 s hasta el primer AccessDenied.

No mide el VPC endpoint de `bedrock-runtime` (eso va en la sonda de AgentCore, dentro de la VPC).
Usa el Claude más barato del catálogo; las llamadas de la parte 4 piden 1 token. Coste: céntimos.
Crea un rol "sonda-n2-<sufijo>" con la etiqueta sonda=nivel2 y lo borra al acabar. Uso:

    SONDA_REGION=eu-north-1 python sonda_bedrock.py
    SONDA_MODELO=eu.anthropic.claude-haiku-5-5 SONDA_OTRO=eu.anthropic.claude-sonnet-5-5 python sonda_bedrock.py
"""
import json
import os
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-haiku-5-5")
OTRO = os.environ.get("SONDA_OTRO", f"{PREFIJO}.anthropic.claude-sonnet-5-5")
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]

ses = boto3.Session(region_name=REGION)
sts, iam, bedrock = (ses.client(x) for x in ("sts", "iam", "bedrock"))
creado = {}
resultados = []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def por_iam(e):
    """AccessDenied de IAM, no el de falta de acceso al modelo (formulario o acuerdo pendiente)."""
    return codigo(e) == "AccessDeniedException" and "is not authorized to perform" in str(e)


def arns_del_perfil(perfil):
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=perfil)
    return p["inferenceProfileArn"], [m["modelArn"] for m in p["models"]]


def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    if ":assumed-role/" in mi_arn:
        mi_arn = f"arn:aws:iam::{cuenta}:role/" + mi_arn.split(":assumed-role/")[1].split("/")[0]

    perfiles = [p["inferenceProfileId"] for p in bedrock.list_inference_profiles()["inferenceProfileSummaries"]
                if ".anthropic." in p["inferenceProfileId"]]
    print(f"     perfiles de Claude en {REGION}: {len(perfiles)} ({', '.join(sorted(p for p in perfiles if p.startswith(PREFIJO + '.')))})")
    anota("ListInferenceProfiles: el modelo y el de contraste existen", MODELO in perfiles and OTRO in perfiles,
          f"{MODELO}, {OTRO}")

    perfil_arn, modelos = arns_del_perfil(MODELO)
    print(f"     {MODELO}: {len(modelos)} foundation-model en " + ", ".join(sorted({m.split(':')[3] for m in modelos})))
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": mi_arn},
        "Action": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]}]}
    iam.create_role(RoleName=NOMBRE, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol"] = NOMBRE
    politica = {"Version": "2012-10-17", "Statement": [{
        "Sid": "ModelosDeLaVersion", "Effect": "Allow",
        "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        "Resource": [perfil_arn] + modelos}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="modelos", PolicyDocument=json.dumps(politica))
    print("esperando 15 s a que IAM propague el rol…", flush=True)
    time.sleep(15)
    return cuenta


def sesion(cuenta, oid):
    r = sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{NOMBRE}", RoleSessionName="run-" + SUF,
                        SourceIdentity=oid, DurationSeconds=3600)
    c = r["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=REGION).client("bedrock-runtime")


SISTEMA = ("Eres un asistente de pruebas. Responde siempre con una sola palabra. "
           + " ".join(f"Regla {i}: no añadas explicaciones, ni saludos, ni puntuación innecesaria." for i in range(600)))


def converse_stream(rt, modelo, cache=True, max_tokens=20):
    system = [{"text": SISTEMA}] + ([{"cachePoint": {"type": "default"}}] if cache else [])
    r = rt.converse_stream(modelId=modelo, system=system, inferenceConfig={"maxTokens": max_tokens},
                           messages=[{"role": "user", "content": [{"text": "Di hola."}]}])
    texto, uso, trozos, t0, primero = "", {}, 0, time.time(), None
    for ev in r["stream"]:
        if "contentBlockDelta" in ev:
            texto += ev["contentBlockDelta"]["delta"].get("text", "")
            trozos += 1
            primero = primero or time.time() - t0
        if "metadata" in ev:
            uso = ev["metadata"].get("usage", {})
    return texto.strip(), uso, trozos, primero


def probar(cuenta):
    oid = str(uuid.uuid4())
    rt = sesion(cuenta, oid)

    try:
        texto, uso1, trozos, primero = converse_stream(rt, MODELO)
        anota("ConverseStream con la sesión al modelo permitido", bool(texto),
              f"«{texto}», {trozos} trozos, primer token a {primero:.2f} s")
        texto, uso2, _, _ = converse_stream(rt, MODELO)
        print("     uso 1:", uso1)
        print("     uso 2:", uso2)
        campos = {"inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens", "cacheWriteInputTokens"}
        anota("El uso del stream trae entrada, salida y caché", campos <= set(uso1) | set(uso2),
              ", ".join(sorted(set(uso1) | set(uso2))))
        anota("La segunda llamada lee de la caché",
              uso2.get("cacheReadInputTokens", 0) > 0,
              f"escritura {uso1.get('cacheWriteInputTokens')} → lectura {uso2.get('cacheReadInputTokens')}")
    except ClientError as e:
        anota("ConverseStream con la sesión al modelo permitido", False, codigo(e) + ": " + str(e)[:200])
        if not por_iam(e):
            print("     la cuenta no tiene acceso al modelo: el resto de la sonda no se puede medir", flush=True)
            return

    try:
        converse_stream(rt, OTRO, cache=False, max_tokens=1)
        anota("Otro modelo con la misma sesión: denegado", False, "se permitió")
    except ClientError as e:
        anota("Otro modelo con la misma sesión: denegado por IAM", por_iam(e), str(e)[:160])

    # 4. Denegación por SourceIdentity sobre la sesión ya emitida
    deny = {"Version": "2012-10-17", "Statement": [{
        "Sid": "SinPresupuesto", "Effect": "Deny", "Action": "bedrock:*", "Resource": "*",
        "Condition": {"StringEquals": {"aws:SourceIdentity": [oid]}}}]}
    t0 = time.time()
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="corte", PolicyDocument=json.dumps(deny))
    pasan, primer_corte, despues = 0, None, []
    while time.time() - t0 < 300:
        try:
            rt.converse(modelId=MODELO, inferenceConfig={"maxTokens": 1},
                        messages=[{"role": "user", "content": [{"text": "1"}]}])
            if primer_corte is None:
                pasan += 1
            else:
                despues.append(round(time.time() - t0, 1))
        except ClientError as e:
            if not por_iam(e):
                print("     error inesperado:", codigo(e), str(e)[:160], flush=True)
                break
            elif primer_corte is None:
                primer_corte = time.time() - t0
                print(f"     primer AccessDenied a {primer_corte:.1f} s tras PutRolePolicy ({pasan} llamadas pasaron antes)",
                      flush=True)
            if primer_corte and time.time() - t0 > primer_corte + 30:
                break
        time.sleep(2)
    anota("Denegación por SourceIdentity sobre una sesión ya emitida",
          primer_corte is not None and not despues,
          f"corte a {primer_corte:.0f} s, {pasan} llamadas antes, {len(despues)} pasaron después del primer corte"
          if primer_corte else "no se cortó en 5 minutos")
    otra = sesion(cuenta, str(uuid.uuid4()))
    try:
        otra.converse(modelId=MODELO, inferenceConfig={"maxTokens": 1},
                      messages=[{"role": "user", "content": [{"text": "1"}]}])
        anota("La denegación no afecta a otro usuario", True)
    except ClientError as e:
        anota("La denegación no afecta a otro usuario", False, codigo(e))


def borrar():
    print("borrando…", flush=True)
    for pol in ("modelos", "corte"):
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
        cuenta = crear()
        probar(cuenta)
    finally:
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
