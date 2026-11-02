"""Sonda 19: el stash del contexto desde la microVM, por el gateway de S3 (lo que dejó abierto la sonda 18).

La sonda 18 midió el stash y el snapshot en S3 desde fuera de AWS (unos 200 ms por PUT y 110 ms por GET, con 180
GET seguidos al restaurar un chat de 50 turnos). Esta repite la misma carga en los dos sitios, con el mismo
código y un modelo falso (el número de operaciones depende de los mensajes, no del modelo):

  - desde aquí (fuera de AWS), como en la sonda 18;
  - dentro de un runtime de AgentCore (Runtime V2) en una VPC sin salida, con el gateway de S3 y los endpoints de
    ECR y Logs, y las credenciales de una sesión limitada a `espacios/*/conversacion/*` en la carga.

Cargas: (1) 50 PUT y 50 GET de 2 KB, la latencia de base; (2) 10 turnos con una herramienta que devuelve ~3.000
tokens (modo automático, stash y snapshot en S3): PUT y tiempo por turno; (3) un chat de 50 turnos (la mitad con
resultados grandes) guardado por primera vez; (4) ese chat montado en un `Agent` nuevo: tiempo de montar, GET
del stash en la primera llamada y su tiempo.

Todo lleva el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2; se borra al acabar, aunque falle o la
paren con SIGTERM. Necesita Docker (arm64) y boto3 >= 1.43 (.venv312). Pocos dólares; unos 25 minutos. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_contexto_vpc.py
    ... sonda_contexto_vpc.py --solo-fuera   # solo la medida desde aquí, sin VPC ni runtime
"""
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
import base64
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import ClientError

SOLO_FUERA = "--solo-fuera" in sys.argv
AQUI = Path(__file__).parent

DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.12-slim
RUN pip install --no-cache-dir strands-agents==1.63.0 bedrock-agentcore==1.25.2 boto3
COPY carga.py fake.py main.py /app/
WORKDIR /app
EXPOSE 8080
CMD ["python", "/app/main.py"]
"""

# La carga: la misma dentro de la microVM (main.py) y desde aquí (importada).
CARGA = r'''
import json, random, statistics, time, uuid
import boto3
from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.s3_storage import S3Storage
from fake import FakeModel, texto, herramienta

PRODUCTOS = ["climatización", "iluminación", "cableado", "domótica", "seguridad", "energía solar"]


def tabla(zona, cod, n=220):
    rnd = random.Random(zona)
    filas = [(f"P-{cod}-{i:04d}", f"2026-{rnd.randint(7, 10):02d}-{rnd.randint(1, 28):02d}", f"CLI-{rnd.randint(1000, 9999)}",
              rnd.choice(PRODUCTOS), rnd.randint(1200, 98000)) for i in range(1, n + 1)]
    return (f"Ventas de la {zona}: {n} pedidos, total {sum(f[4] for f in filas)} EUR.\n"
            "pedido | fecha | cliente | producto | importe EUR\n" + "\n".join(" | ".join(map(str, f)) for f in filas))


@tool
def consultar_ventas(region: str) -> str:
    """Devuelve los pedidos de una zona (zona01 a zona50) con su total.

    Args:
        region: la zona.
    """
    return tabla(region, "Z" + region[-2:])


class Contador:
    def __init__(self, ses):
        self.ops = []
        ses.events.register("before-call.s3", self._a)
        ses.events.register("after-call.s3", self._d)

    def _a(self, context, **kw):
        context["_t0"] = time.monotonic()

    def _d(self, context, model, http_response, **kw):
        self.ops.append((model.name, (time.monotonic() - context["_t0"]) * 1000, http_response.status_code))

    def desde(self, i):
        r = {}
        for op, ms, st in self.ops[i:]:
            d = r.setdefault(op, {"n": 0, "ms": 0.0, "estados": set()})
            d["n"] += 1
            d["ms"] += ms
            d["estados"].add(st)
        return {op: {"n": d["n"], "ms": round(d["ms"]), "media_ms": round(d["ms"] / d["n"], 1),
                     "estados": sorted(d["estados"])} for op, d in r.items()}


def sesion(cred, region):
    return boto3.Session(aws_access_key_id=cred["AccessKeyId"], aws_secret_access_key=cred["SecretAccessKey"],
                         aws_session_token=cred["SessionToken"], region_name=region)


def agente(ses, bucket, prefijo, chat, guion):
    almac = S3Storage(bucket, prefix=f"{prefijo}/{chat}/conversacion", boto_session=ses)
    return Agent(model=FakeModel(guion), agent_id="ventas", system_prompt="Eres el analista de ventas.",
                 tools=[consultar_ventas], callback_handler=None, context_manager="auto", storage=almac,
                 session_manager=SnapshotSessionManager(chat, storage=almac, save_latest_on="message"))


def sinteticos(n=50):
    msgs = []
    for t in range(1, n + 1):
        zona, tid = f"zona{t:02d}", f"tooluse_{t:02d}{uuid.uuid4().hex[:12]}"
        res = tabla(zona, f"Z{t:02d}") if t % 2 else tabla(zona, f"Z{t:02d}").split("\n")[0]
        msgs += [{"role": "user", "content": [{"text": f"Turno {t}: dame las ventas de la {zona}."}]},
                 {"role": "assistant", "content": [{"text": "Consulto las ventas."}, {"toolUse": {
                     "toolUseId": tid, "name": "consultar_ventas", "input": {"region": zona}}}]},
                 {"role": "user", "content": [{"toolResult": {"toolUseId": tid, "status": "success",
                                                              "content": [{"text": res}]}}]},
                 {"role": "assistant", "content": [{"text": f"La {zona} vendió lo que indica la cabecera."}]}]
    return msgs


def medir(cred, region, bucket, prefijo):
    """Las cuatro cargas; devuelve un dict con tiempos y operaciones."""
    R = {}
    ses = sesion(cred, region)
    c = Contador(ses)
    s3 = ses.client("s3")
    # 1. Latencia de base
    i = len(c.ops)
    cuerpo = b"x" * 2048
    for k in range(50):
        s3.put_object(Bucket=bucket, Key=f"{prefijo}/base/conversacion/o{k}", Body=cuerpo)
    for k in range(50):
        s3.get_object(Bucket=bucket, Key=f"{prefijo}/base/conversacion/o{k}")["Body"].read()
    put = sorted(ms for op, ms, _ in c.ops[i:] if op == "PutObject")
    get = sorted(ms for op, ms, _ in c.ops[i:] if op == "GetObject")
    R["base"] = {"put_p50": round(statistics.median(put), 1), "put_p95": round(put[47], 1),
                 "get_p50": round(statistics.median(get), 1), "get_p95": round(get[47], 1)}
    # 2. Diez turnos con un resultado grande cada uno
    chat = "t-" + uuid.uuid4().hex[:8]
    guion = []
    for t in range(1, 11):
        guion += [herramienta("consultar_ventas", {"region": f"zona{t:02d}"}), texto(f"Hecho {t}.")]
    a = agente(ses, bucket, prefijo, chat, guion)
    turnos = []
    for t in range(1, 11):
        i, t0 = len(c.ops), time.monotonic()
        a(f"Turno {t}: dame las ventas de la zona{t:02d}.")
        turnos.append({"s": round(time.monotonic() - t0, 3), "s3": c.desde(i)})
    R["turnos"] = turnos
    # 3. Chat de 50 turnos guardado por primera vez
    chat = "c50-" + uuid.uuid4().hex[:8]
    a = agente(ses, bucket, prefijo, chat, [texto("De nada.")])
    a.messages.extend(sinteticos())
    i, t0 = len(c.ops), time.monotonic()
    a("Gracias.")
    R["crear50"] = {"s": round(time.monotonic() - t0, 2), "s3": c.desde(i)}
    # 4. Montarlo en un Agent nuevo
    ses2 = sesion(cred, region)
    c2 = Contador(ses2)
    t0 = time.monotonic()
    a2 = agente(ses2, bucket, prefijo, chat, [texto("Aquí estoy.")])
    t_montar = time.monotonic() - t0
    i, t1 = len(c2.ops), time.monotonic()
    a2("¿Sigues ahí?")
    R["restaurar50"] = {"montar_s": round(t_montar, 3), "montar_s3": c2.desde(0), "mensajes": len(a2.messages),
                        "primer_turno_s": round(time.monotonic() - t1, 2), "primer_turno_s3": c2.desde(i)}
    return R
'''

MAIN = r'''
import json, os, time
T0 = time.time()
import carga
from bedrock_agentcore.runtime import BedrockAgentCoreApp
app = BedrockAgentCoreApp()
print(f"KERNEL_LISTO {time.time() - T0:.2f}s", flush=True)


@app.entrypoint
def invoke(p):
    if p.get("accion") == "medir":
        t0 = time.time()
        r = carga.medir(p["cred"], p["region"], p["bucket"], p["prefijo"])
        r["total_s"] = round(time.time() - t0, 1)
        return r
    return {"ok": True}


if __name__ == "__main__":
    app.run()
'''

# ---------------------------------------------------------------- orquestación (aquí)
import sonda_agentcore as base  # noqa: E402
import sonda_runtime_imagen as ri  # noqa: E402

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = base.REGION, base.NOMBRE, base.NOMBRE_, base.TAGS, base.TAGS_DICT
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
ses, ctl, ec2, iam, s3, ecr = base.ses, base.ctl, base.ec2, base.iam, base.s3, ri.ecr
sts, logs = ses.client("sts"), ses.client("logs")
dp = ses.client("bedrock-agentcore", config=Config(retries={"max_attempts": 1}, read_timeout=600))
CIDR = "10.84.0.0/16"
ROL_SESION = NOMBRE + "-sesion"
DIR = Path(tempfile.mkdtemp())


def ficheros():
    (DIR / "carga.py").write_text(CARGA)
    (DIR / "main.py").write_text(MAIN)
    (DIR / "fake.py").write_text((AQUI.parent / "kernel" / "fake.py").read_text())
    (DIR / "Dockerfile").write_text(DOCKERFILE)


def preparar(cuenta):
    s3.create_bucket(Bucket=NOMBRE, **({} if REGION == "us-east-1" else
                     {"CreateBucketConfiguration": {"LocationConstraint": REGION}}))
    creado["bucket"] = True
    s3.put_bucket_tagging(Bucket=NOMBRE, Tagging={"TagSet": TAGS})
    yo = sts.get_caller_identity()["Arn"]
    if ":assumed-role/" in yo:
        yo = f"arn:aws:iam::{cuenta}:role/" + yo.split(":assumed-role/")[1].split("/")[0]
    iam.create_role(RoleName=ROL_SESION, Tags=TAGS, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": yo}, "Action": "sts:AssumeRole"}]}))
    creado["rol_sesion"] = True
    b = f"arn:aws:s3:::{NOMBRE}"
    iam.put_role_policy(RoleName=ROL_SESION, PolicyName="s3", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"{b}/espacios/*/conversacion/*"},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": b,
             "Condition": {"StringLike": {"s3:prefix": "espacios/*/conversacion/*"}}}]}))
    time.sleep(15)


def credenciales(cuenta):
    t0 = time.time()
    while True:
        try:
            return sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{ROL_SESION}", RoleSessionName="sonda-contexto",
                                   DurationSeconds=3600)["Credentials"]
        except ClientError as e:
            if codigo(e) != "AccessDenied" or time.time() - t0 > 120:
                raise
            time.sleep(5)


def endpoints(vpc, subredes):
    sg = ec2.create_security_group(GroupName=f"{NOMBRE}-ep", Description="endpoints", VpcId=vpc)["GroupId"]
    base.etiqueta([sg])
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": CIDR}]}])
    for servicio in ("ecr.api", "ecr.dkr", "logs"):
        ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc, ServiceName=f"com.amazonaws.{REGION}.{servicio}",
                                     SubnetIds=subredes, SecurityGroupIds=[sg], PrivateDnsEnabled=True)["VpcEndpoint"]
        base.etiqueta([ep["VpcEndpointId"]])
    print("esperando a los endpoints de interfaz…", flush=True)
    while not all(e["State"] == "available" for e in
                  ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["VpcEndpoints"]):
        time.sleep(15)


def imagen():
    repo = ecr.create_repository(repositoryName=NOMBRE, tags=TAGS)["repository"]["repositoryUri"]
    creado["repo"] = NOMBRE
    tok = ecr.get_authorization_token()["authorizationData"][0]
    usuario, clave = base64.b64decode(tok["authorizationToken"]).decode().split(":")
    subprocess.run(["docker", "login", "-u", usuario, "--password-stdin", tok["proxyEndpoint"]],
                   input=clave.encode(), check=True, capture_output=True)
    for args in (["build", "--platform", "linux/arm64", "-t", f"{repo}:v1", str(DIR)], ["push", f"{repo}:v1"]):
        p = subprocess.run(["docker", *args], capture_output=True)
        if p.returncode:
            raise RuntimeError(p.stderr.decode()[-2000:])
    print("imagen subida", flush=True)
    return f"{repo}:v1"


def runtime(uri, rol_arn, subredes, sg):
    r = ctl.create_agent_runtime(
        agentRuntimeName=NOMBRE_, roleArn=rol_arn, platformVersion="V2",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {"subnets": subredes, "securityGroups": [sg]}},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 120, "maxLifetime": 3600}, tags=TAGS_DICT)
    creado.setdefault("runtimes", []).append(r["agentRuntimeId"])
    print("runtime preparándose…", flush=True)
    base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"]), maximo=1800)
    print("READY", flush=True)
    return r["agentRuntimeId"], r["agentRuntimeArn"]


def resumen(nombre, R):
    b = R["base"]
    put_t = [t["s3"].get("PutObject", {}).get("n", 0) for t in R["turnos"]]
    s3_t = [sum(v["ms"] for v in t["s3"].values()) for t in R["turnos"]]
    c50, r50 = R["crear50"], R["restaurar50"]
    g = r50["primer_turno_s3"].get("GetObject", {})
    print(f"\n--- {nombre}", flush=True)
    print(f"     base: PUT P50 {b['put_p50']} ms (P95 {b['put_p95']}), GET P50 {b['get_p50']} ms (P95 {b['get_p95']})")
    print(f"     10 turnos: PUT por turno {put_t}; S3 por turno {s3_t} ms; turno {[t['s'] for t in R['turnos']]} s")
    print(f"     crear 50 turnos: {c50['s']} s · {c50['s3']}")
    print(f"     restaurar 50 turnos: montar {r50['montar_s']} s {r50['montar_s3']} · primer turno {r50['primer_turno_s']} s "
          f"· {r50['primer_turno_s3']}", flush=True)
    return {"put_p50": b["put_p50"], "get_p50": b["get_p50"], "s3_turno_ms": statistics.median(s3_t),
            "get_restaurar": g.get("n", 0), "restaurar_s": r50["primer_turno_s"], "montar_s": r50["montar_s"]}


def principal():
    cuenta = sts.get_caller_identity()["Account"]
    print("cuenta", cuenta, "·", REGION, "·", NOMBRE, flush=True)
    ficheros()
    sys.path.insert(0, str(DIR))
    import carga  # noqa: E402  (la misma que va en la imagen)
    preparar(cuenta)
    fuera = resumen("Desde fuera de AWS", carga.medir(credenciales(cuenta), REGION, NOMBRE, "espacios/fuera"))
    anota("1. Desde fuera: un GET por bloque al restaurar 50 turnos", fuera["get_restaurar"] > 150,
          f"{fuera['get_restaurar']} GET; primer turno {fuera['restaurar_s']} s; PUT P50 {fuera['put_p50']} ms")
    if SOLO_FUERA:
        return
    vpc, subredes, sg = base.vpc(CIDR, True, con_s3=True)
    endpoints(vpc, subredes)
    uri = imagen()
    rol_arn = ri.rol(cuenta)
    time.sleep(15)
    rid, arn = runtime(uri, rol_arn, subredes, sg)
    sesion = base.sesion_nueva()
    t0 = time.time()
    r = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sesion, contentType="application/json",
                                accept="application/json", payload=json.dumps({
                                    "accion": "medir", "cred": credenciales(cuenta), "region": REGION,
                                    "bucket": NOMBRE, "prefijo": "espacios/vpc"}, default=str).encode())
    R = json.loads(r["response"].read())
    print(f"invocación en {time.time() - t0:.1f} s", flush=True)
    if "base" not in R:
        anota("2. La medida dentro de la microVM", False, json.dumps(R)[:500])
        return
    dentro = resumen("Desde la microVM (gateway de S3)", R)
    intenta("parar sesión", lambda: dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=sesion))
    anota("2. Dentro: PUT y GET por el gateway de S3 más rápidos que desde fuera",
          dentro["put_p50"] < fuera["put_p50"] and dentro["get_p50"] < fuera["get_p50"],
          f"PUT P50 {dentro['put_p50']} frente a {fuera['put_p50']} ms; GET P50 {dentro['get_p50']} frente a "
          f"{fuera['get_p50']} ms")
    anota("3. Dentro: el stash añade menos de 0,5 s por turno", dentro["s3_turno_ms"] < 500,
          f"mediana {dentro['s3_turno_ms']:.0f} ms de S3 por turno (fuera {fuera['s3_turno_ms']:.0f} ms)")
    anota("4. Dentro: restaurar 50 turnos añade menos de 3 s a la primera llamada", dentro["restaurar_s"] < 3,
          f"{dentro['get_restaurar']} GET; primer turno {dentro['restaurar_s']} s (fuera {fuera['restaurar_s']} s); "
          f"montar {dentro['montar_s']} s")


def borrar():
    print("borrando…", flush=True)
    if creado.get("rol_sesion"):
        intenta("rol sesión", lambda: iam.delete_role_policy(RoleName=ROL_SESION, PolicyName="s3"))
        intenta("rol sesión", lambda: iam.delete_role(RoleName=ROL_SESION))
    if creado.get("bucket"):  # base.borrar solo borra la primera página de objetos
        for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=NOMBRE):
            objs = [{"Key": o["Key"]} for o in pg.get("Contents", [])]
            if objs:
                intenta("objetos", lambda objs=objs: s3.delete_objects(Bucket=NOMBRE, Delete={"Objects": objs}))
    ri.borrar()  # repo de ECR, runtime, bucket, rol del runtime y VPC
    for rid in creado.get("runtimes", []):
        for g in logs.describe_log_groups(logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{rid}").get("logGroups", []):
            intenta("logs", lambda g=g: logs.delete_log_group(logGroupName=g["logGroupName"]))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    try:
        principal()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        anota("la sonda terminó con una excepción", False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones bien")
    for n, _, d in fallos:
        print("  FALLA", n, d)
