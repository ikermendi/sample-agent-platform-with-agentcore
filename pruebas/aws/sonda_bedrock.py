"""Sonda 3 del nivel 2: inferencia directa a Claude por perfil, con las credenciales de la sesión.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre la llamada al modelo
(filas "Inferencia directa" y "Claude por perfil de inferencia" de Por verificar, y "Credenciales del
kernel"). Las sesiones son como las del diseño: RoleSessionName = run_id (`{oid}.{ULID}`, ver
runid_coste.py), SourceIdentity = oid y una política de sesión con los modelos de la versión (ARN del
perfil + `foundation-model` en cada región de destino) y un `NotAction` que deja pasar lo demás.

  1. Perfiles de Claude de la cuenta (ListInferenceProfiles): el modelo y el de contraste existen.
  2. Con la política de sesión del diseño, ConverseStream al modelo (uso con caché) y Converse; lo que
     no es Bedrock pasa por el `NotAction` (dynamodb:ListTables, que permite la política del rol).
  3. La misma sesión no puede llamar a otro modelo (AccessDenied de IAM), que el rol sí permite y la
     cuenta sí tiene habilitado (se comprueba con las credenciales de quien lanza la sonda).
  4. Variantes de la política mínima: sin una región de destino (una remota y la de origen), solo el
     perfil, solo los `foundation-model`; y el perfil `global.` con y sin el ARN sin región. Todas menos
     la completa se deniegan siempre (1-11-2026: también sin una región remota, 6 de 6).
  5. Cuánto tarda en aplicarse una denegación de IAM por `aws:SourceIdentity` sobre una sesión ya
     emitida (IAM no da plazo): 5 veces, cada una con un usuario y una sesión nuevos, añadiendo su oid
     a la lista de la política inline (como la Lambda de coste); se llama cada 2 s alternando dos modelos
     hasta el primer AccessDenied y 20 s más (para ver si alguna pasa después).

La cuota de Haiku 4.5 entre regiones es de 10 llamadas por minuto: las llamadas a un mismo modelo se
espacian (salvo en la parte 5, donde un ThrottlingException cuenta como "IAM aún lo deja pasar": el
estrangulamiento va después de la autorización). Reintentos de botocore apagados.
No mide el VPC endpoint de `bedrock-runtime` (eso va dentro de la VPC). Coste: céntimos.
Crea un rol "sonda-n2-<sufijo>" con la etiqueta sonda=nivel2 y lo borra al acabar. Uso:

    SONDA_REGION=eu-north-1 SONDA_MODELO=eu.anthropic.claude-haiku-4-5-20251001-v1:0 \\
      SONDA_OTRO=eu.anthropic.claude-sonnet-4-6 python sonda_bedrock.py
"""
import json
import os
import statistics
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
GLOBAL = "global." + MODELO.split(".", 1)[1]
REPETICIONES = int(os.environ.get("SONDA_REPETICIONES", "5"))
LLAMADAS_VARIANTE = int(os.environ.get("SONDA_LLAMADAS_VARIANTE", "6"))
ESPACIO = 6.5  # s entre llamadas a un mismo modelo: 10 por minuto
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
SIN_REINTENTOS = Config(retries={"total_max_attempts": 1, "mode": "standard"})

ses = boto3.Session(region_name=REGION)
sts, iam, bedrock = (ses.client(x) for x in ("sts", "iam", "bedrock"))
creado = {}
resultados = []
ultima = {}


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def por_iam(e):
    """AccessDenied de IAM, no el de falta de acceso al modelo ("is not available for this account")."""
    return codigo(e) == "AccessDeniedException" and "is not authorized to perform" in str(e)


def espera_turno(modelo):
    falta = ultima.get(modelo, 0) + ESPACIO - time.time()
    if falta > 0:
        time.sleep(falta)
    ultima[modelo] = time.time()


def perfil(pid):
    p = bedrock.get_inference_profile(inferenceProfileIdentifier=pid)
    return p["inferenceProfileArn"], [m["modelArn"] for m in p["models"]]


def politica_sesion(recursos):
    """La del diseño: los modelos de la versión y un NotAction que deja pasar lo que acota el rol."""
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

    perfiles = [p["inferenceProfileId"] for p in bedrock.list_inference_profiles(maxResults=1000)["inferenceProfileSummaries"]
                if ".anthropic." in p["inferenceProfileId"]]
    print(f"     perfiles de Claude en {REGION}: {len(perfiles)}")
    anota("ListInferenceProfiles: el modelo, el de contraste y el global existen",
          {MODELO, OTRO, GLOBAL} <= set(perfiles), f"{MODELO}, {OTRO}, {GLOBAL}")

    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": mi_arn},
        "Action": ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"]}]}
    iam.create_role(RoleName=NOMBRE, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol"] = NOMBRE
    # El rol deja todos los Claude (lo acota la sesión) y algo que no es Bedrock, para probar el NotAction.
    rol = {"Version": "2012-10-17", "Statement": [
        {"Sid": "Claude", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": [f"arn:aws:bedrock:*:{cuenta}:inference-profile/*", "arn:aws:bedrock:*::foundation-model/*"]},
        {"Sid": "NoBedrock", "Effect": "Allow", "Action": "dynamodb:ListTables", "Resource": "*"}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="rol", PolicyDocument=json.dumps(rol))
    print("esperando 15 s a que IAM propague el rol…", flush=True)
    time.sleep(15)
    return cuenta


def sesion(cuenta, oid, recursos):
    """AssumeRole como lo haría `api`: run_id de nombre, oid de SourceIdentity, política de sesión."""
    rid = run_id(oid, ulid())
    pol = politica_sesion(recursos)
    for intento in range(6):
        try:
            r = sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{NOMBRE}", RoleSessionName=rid,
                                SourceIdentity=oid, DurationSeconds=3600, Policy=pol)
            break
        except ClientError as e:
            if codigo(e) != "AccessDenied" or intento == 5:
                raise
            time.sleep(5)
    c = r["Credentials"]
    s = boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                      aws_session_token=c["SessionToken"], region_name=REGION)
    return s.client("bedrock-runtime", config=SIN_REINTENTOS), s.client("dynamodb"), len(pol)


SISTEMA = ("Eres un asistente de pruebas. Responde siempre con una sola palabra. "
           + " ".join(f"Regla {i}: no añadas explicaciones, ni saludos, ni puntuación innecesaria." for i in range(600)))


def converse_stream(rt, modelo, cache=True, max_tokens=20):
    espera_turno(modelo)
    system = [{"text": SISTEMA}] + ([{"cachePoint": {"type": "default"}}] if cache else [])
    t0 = time.time()
    r = rt.converse_stream(modelId=modelo, system=system, inferenceConfig={"maxTokens": max_tokens},
                           messages=[{"role": "user", "content": [{"text": "Di hola."}]}])
    texto, uso, trozos, primero = "", {}, 0, None
    for ev in r["stream"]:
        if "contentBlockDelta" in ev:
            texto += ev["contentBlockDelta"]["delta"].get("text", "")
            trozos += 1
            primero = primero or time.time() - t0
        if "metadata" in ev:
            uso = ev["metadata"].get("usage", {})
    return texto.strip(), uso, trozos, primero


def uno(rt, modelo, esperar=True):
    if esperar:
        espera_turno(modelo)
    rt.converse(modelId=modelo, inferenceConfig={"maxTokens": 1},
                messages=[{"role": "user", "content": [{"text": "1"}]}])


def recursos_de(pid):
    arn, modelos = perfil(pid)
    return arn, modelos


def parte2y3(cuenta):
    arn, modelos = recursos_de(MODELO)
    print(f"     {MODELO}: perfil {arn}; {len(modelos)} foundation-model en "
          + ", ".join(sorted(m.split(':')[3] for m in modelos)))
    rt, ddb, largo = sesion(cuenta, str(uuid.uuid4()), [arn] + modelos)
    print(f"     política de sesión: {largo} caracteres")
    try:
        texto, uso1, trozos, primero = converse_stream(rt, MODELO)
        anota("ConverseStream con la sesión al modelo permitido", bool(texto),
              f"«{texto}», {trozos} trozos, primer token a {primero:.2f} s")
        texto, uso2, _, _ = converse_stream(rt, MODELO)
        print("     uso 1:", uso1)
        print("     uso 2:", uso2)
        anota("La segunda llamada lee de la caché", uso2.get("cacheReadInputTokens", 0) > 0,
              f"escritura {uso1.get('cacheWriteInputTokens')} → lectura {uso2.get('cacheReadInputTokens')}")
        uno(rt, MODELO)
        anota("Converse con la sesión al modelo permitido", True)
    except ClientError as e:
        anota("Llamadas con la sesión al modelo permitido", False, codigo(e) + ": " + str(e)[:200])
        if not por_iam(e):
            print("     la cuenta no tiene acceso al modelo: el resto de la sonda no se puede medir", flush=True)
            return False
    try:
        ddb.list_tables(Limit=1)
        anota("Lo que no es Bedrock pasa por el NotAction (dynamodb:ListTables)", True)
    except ClientError as e:
        anota("Lo que no es Bedrock pasa por el NotAction (dynamodb:ListTables)", False, codigo(e))

    admin = ses.client("bedrock-runtime", config=SIN_REINTENTOS)
    try:
        uno(admin, OTRO)
        acceso = "con acceso en la cuenta (llamada de contraste con quien lanza la sonda: bien)"
    except ClientError as e:
        acceso = f"SIN acceso en la cuenta ({codigo(e)}: {str(e)[:80]})"
    try:
        uno(rt, OTRO)
        anota("Otro modelo con la misma sesión: denegado", False, "se permitió")
    except ClientError as e:
        anota("Otro modelo con la misma sesión: denegado por IAM", por_iam(e), f"{OTRO} {acceso}; " + str(e)[:150])
    return True


def variante(cuenta, nombre, recursos, modelo, espera_ok):
    """Llama LLAMADAS_VARIANTE veces con una sesión con esos recursos; cuenta bien y AccessDenied de IAM."""
    rt, _, _ = sesion(cuenta, str(uuid.uuid4()), recursos)
    bien, iam_no, otros = 0, 0, []
    for _ in range(LLAMADAS_VARIANTE):
        try:
            uno(rt, modelo)
            bien += 1
        except ClientError as e:
            if por_iam(e):
                iam_no += 1
                if not espera_ok and iam_no >= 2 and bien == 0:
                    break  # denegado siempre: no hace falta gastar más llamadas
            else:
                otros.append(codigo(e))
    total = bien + iam_no + len(otros)
    det = f"{bien} bien, {iam_no} AccessDenied de IAM, otros {otros or 'ninguno'} (de {total})"
    anota(nombre, (bien == total) if espera_ok else (bien == 0 and iam_no > 0), det)
    return bien, iam_no


def parte4(cuenta):
    arn, modelos = recursos_de(MODELO)
    remota = next(m for m in modelos if f":{REGION}:" not in m)
    origen = next(m for m in modelos if f":{REGION}:" in m)
    # La guía de AWS: se autoriza el foundation-model de cada región de destino candidata, no solo la elegida.
    variante(cuenta, f"Sin la región de destino {remota.split(':')[3]} (remota)",
             [arn] + [m for m in modelos if m != remota], MODELO, espera_ok=False)
    variante(cuenta, f"Sin la región de origen {REGION}",
             [arn] + [m for m in modelos if m != origen], MODELO, espera_ok=False)
    variante(cuenta, "Solo el ARN del perfil, sin foundation-model", [arn], MODELO, espera_ok=False)
    variante(cuenta, "Solo los foundation-model, sin el perfil", modelos, MODELO, espera_ok=False)
    garn, gmodelos = recursos_de(GLOBAL)
    print(f"     {GLOBAL}: perfil {garn}; foundation-model: {', '.join(gmodelos)}")
    variante(cuenta, "Perfil global. con sus foundation-model (con y sin región)", [garn] + gmodelos, GLOBAL,
             espera_ok=True)
    sin_region = [m for m in gmodelos if m.split(":")[3] == ""]
    if sin_region:
        variante(cuenta, "Perfil global. sin el foundation-model sin región",
                 [garn] + [m for m in gmodelos if m not in sin_region], GLOBAL, espera_ok=False)


def parte5(cuenta):
    """Denegación por SourceIdentity sobre sesiones ya emitidas: REPETICIONES medidas."""
    arn, modelos = recursos_de(MODELO)
    oarn, omodelos = recursos_de(OTRO)
    recursos = [arn, oarn] + modelos + omodelos
    cortados, medidas, pasadas, despues_total = [], [], [], 0
    for i in range(REPETICIONES):
        oid = str(uuid.uuid4())
        rt, _, _ = sesion(cuenta, oid, recursos)
        uno(rt, MODELO)  # la sesión funciona antes del corte
        time.sleep(ESPACIO)
        cortados.append(oid)
        deny = {"Version": "2012-10-17", "Statement": [{
            "Sid": "SinPresupuesto", "Effect": "Deny", "Action": "bedrock:InvokeModel*", "Resource": "*",
            "Condition": {"StringEquals": {"aws:SourceIdentity": cortados}}}]}
        iam.put_role_policy(RoleName=NOMBRE, PolicyName="corte", PolicyDocument=json.dumps(deny))
        t0 = time.time()
        pasan, estrangula, corte, despues, n, cuando = 0, 0, None, 0, 0, []
        while time.time() - t0 < 300:
            modelo = (MODELO, OTRO)[n % 2]
            n += 1
            try:
                uno(rt, modelo, esperar=False)
                if corte is None:
                    pasan += 1
                else:
                    despues += 1
                    cuando.append(f"{time.time() - t0:.1f} s")
            except ClientError as e:
                if codigo(e) == "ThrottlingException":
                    if corte is None:
                        estrangula += 1
                    else:
                        despues += 1
                        cuando.append(f"{time.time() - t0:.1f} s (estrangulada)")
                elif por_iam(e):
                    if corte is None:
                        corte = time.time() - t0
                else:
                    print("     error inesperado:", codigo(e), str(e)[:160], flush=True)
            if corte is not None and time.time() - t0 > corte + 20:
                break
            time.sleep(2)
        print(f"     medida {i + 1}: corte a {corte:.1f} s ({pasan} bien y {estrangula} estranguladas antes; "
              f"{despues} pasaron después{': ' + ', '.join(cuando) if cuando else ''})" if corte is not None else f"     medida {i + 1}: sin corte en 300 s",
              flush=True)
        if corte is not None:
            medidas.append(corte)
        pasadas.append(pasan + estrangula)
        despues_total += despues
        time.sleep(30)  # que se recupere la cuota antes de la siguiente
    if medidas:
        anota(f"Denegación por SourceIdentity: {len(medidas)} de {REPETICIONES} medidas",
              len(medidas) == REPETICIONES and despues_total == 0,
              f"mín {min(medidas):.1f} s, mediana {statistics.median(medidas):.1f} s, máx {max(medidas):.1f} s; "
              f"llamadas que pasaron antes del corte {pasadas}; después del primer corte {despues_total}")
    else:
        anota("Denegación por SourceIdentity", False, "no se cortó en ninguna medida")
    # Una sesión nueva de un usuario cortado nace denegada; la de otro usuario no se ve afectada.
    rt, _, _ = sesion(cuenta, cortados[0], recursos)
    try:
        uno(rt, OTRO)
        anota("Sesión nueva de un usuario ya cortado: denegada", False, "se permitió")
    except ClientError as e:
        anota("Sesión nueva de un usuario ya cortado: denegada", por_iam(e), codigo(e))
    rt, _, _ = sesion(cuenta, str(uuid.uuid4()), recursos)
    try:
        uno(rt, OTRO)
        anota("La denegación no afecta a otro usuario", True)
    except ClientError as e:
        anota("La denegación no afecta a otro usuario", False, codigo(e))


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
        cuenta = crear()
        if parte2y3(cuenta):
            parte4(cuenta)
            parte5(cuenta)
    finally:
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
