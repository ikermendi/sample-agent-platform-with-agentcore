"""Sonda 6 del nivel 2: Runtime V2 con una imagen real desde ECR, en una VPC sin NAT y con DNS Firewall.

Completa la sonda 5 en lo que necesita una imagen de contenedor (sección "Runtime V2 (snapshot)"
y la parte de DNS de "Local code execution" en "To verify"):

  1. La imagen (Python 3.12 + Strands + boto3, arm64) llega por los endpoints de ECR (api y dkr) y el
     gateway de S3 de la VPC, sin NAT, y con DNS Firewall bloqueando todo lo que no sea *.amazonaws.com.
  2. Arranque en frío de sesiones nuevas con Strands cargado antes de la instantánea (P50/máx).
  3. DNS Firewall: desde la microVM un dominio externo no resuelve y los de AWS sí (incluidos sus CNAME).
  4. Una ejecución que sigue en segundo plano con /ping en HealthyBusy no se corta por inactividad (60 s)
     y dura hasta maxLifetime (3.300 s = 55 min): el kernel deja un latido en S3 cada 30 s.

Necesita Docker (arm64) y boto3 >= 1.43. Reutiliza las piezas de sonda_agentcore.py. Todo con el
prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2; se borra al acabar, aunque algo falle (las ENI
de AgentCore pueden tardar: si la VPC queda, `python sonda_agentcore.py --borrar-restos`).
Dura algo más de una hora. Coste: unos pocos dólares (endpoints de interfaz y la sesión larga). Uso:

    SONDA_REGION=eu-north-1 python sonda_runtime_imagen.py
    SONDA_REGION=eu-north-1 python sonda_runtime_imagen.py --sin-larga   # sin la ejecución de 55 min
"""
import base64
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

from botocore.exceptions import ClientError

import sonda_agentcore as base

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = base.REGION, base.NOMBRE, base.NOMBRE_, base.TAGS, base.TAGS_DICT
ses = base.ses
ecr, r53r = ses.client("ecr"), ses.client("route53resolver")
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
MAX_VIDA = 3300

DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.12-slim
RUN pip install --no-cache-dir strands-agents boto3
COPY main.py /app/main.py
WORKDIR /app
EXPOSE 8080
CMD ["python", "main.py"]
"""

KERNEL = r'''
import json, os, socket, threading, time, uuid
t0 = time.time()
import boto3, strands  # lo caro, antes de responder sano en /ping (y antes de la instantánea)
CARGA = round(time.time() - t0, 2)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARRANQUE = uuid.uuid4().hex
OCUPADO = {"n": 0}
BUCKET = os.environ["BUCKET"]

def resuelve(host):
    try:
        return socket.gethostbyname(host)
    except Exception as e:
        return "error: " + type(e).__name__

def red():
    reg = os.environ.get("AWS_REGION", "")
    hosts = {"externo": "example.com", "externo2": "pypi.org", "s3": f"s3.{reg}.amazonaws.com",
             "sts": f"sts.{reg}.amazonaws.com", "bedrock": f"bedrock-runtime.{reg}.amazonaws.com",
             "ecr_dkr": f"api.ecr.{reg}.amazonaws.com", "dynamodb": f"dynamodb.{reg}.amazonaws.com"}
    return {k: resuelve(v) for k, v in hosts.items()}

def larga(run_id, segundos):
    OCUPADO["n"] += 1
    s3 = boto3.client("s3")
    inicio = time.time()
    try:
        while time.time() - inicio < segundos:
            s3.put_object(Bucket=BUCKET, Key=f"latidos/{run_id}.json",
                          Body=json.dumps({"segundos": round(time.time() - inicio), "hora": time.time()}).encode())
            time.sleep(30)
    finally:
        OCUPADO["n"] -= 1

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def responde(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        self.responde({"status": "HealthyBusy" if OCUPADO["n"] else "Healthy", "time_of_last_update": int(time.time())})
    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        r = {"arranque": ARRANQUE, "carga_s": CARGA, "strands": getattr(strands, "__version__", "?")}
        if cuerpo.get("red"):
            r["red"] = red()
        if cuerpo.get("larga"):
            threading.Thread(target=larga, args=(cuerpo["run_id"], cuerpo["larga"]), daemon=True).start()
            r["larga"] = "en marcha"
        self.responde(r)

ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''


def endpoints(vpc, subredes):
    sg = ec2.create_security_group(GroupName=f"{NOMBRE}-ep", Description="endpoints", VpcId=vpc)["GroupId"]
    base.etiqueta([sg])
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "10.82.0.0/16"}]}])
    for servicio in ("ecr.api", "ecr.dkr", "logs"):
        ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc, ServiceName=f"com.amazonaws.{REGION}.{servicio}",
                                     SubnetIds=subredes, SecurityGroupIds=[sg], PrivateDnsEnabled=True)["VpcEndpoint"]
        base.etiqueta([ep["VpcEndpointId"]])
    print("esperando a los endpoints de interfaz…", flush=True)
    while True:
        eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["VpcEndpoints"]
        if all(e["State"] == "available" for e in eps):
            return
        time.sleep(15)


def dns_firewall(vpc):
    permitidos = r53r.create_firewall_domain_list(CreatorRequestId=NOMBRE + "p", Name=NOMBRE + "-aws", Tags=TAGS)["FirewallDomainList"]["Id"]
    creado["dl_aws"] = permitidos
    r53r.update_firewall_domains(FirewallDomainListId=permitidos, Operation="ADD", Domains=["amazonaws.com", "*.amazonaws.com"])
    todo = r53r.create_firewall_domain_list(CreatorRequestId=NOMBRE + "t", Name=NOMBRE + "-todo", Tags=TAGS)["FirewallDomainList"]["Id"]
    creado["dl_todo"] = todo
    r53r.update_firewall_domains(FirewallDomainListId=todo, Operation="ADD", Domains=["*"])
    grupo = r53r.create_firewall_rule_group(CreatorRequestId=NOMBRE + "g", Name=NOMBRE, Tags=TAGS)["FirewallRuleGroup"]["Id"]
    creado["rg"] = grupo
    r53r.create_firewall_rule(CreatorRequestId=NOMBRE + "r1", FirewallRuleGroupId=grupo, FirewallDomainListId=permitidos,
                              Priority=1, Action="ALLOW", Name="aws")
    r53r.create_firewall_rule(CreatorRequestId=NOMBRE + "r2", FirewallRuleGroupId=grupo, FirewallDomainListId=todo,
                              Priority=2, Action="BLOCK", BlockResponse="NXDOMAIN", Name="todo")
    asoc = r53r.associate_firewall_rule_group(CreatorRequestId=NOMBRE + "a", FirewallRuleGroupId=grupo, VpcId=vpc,
                                              Priority=101, Name=NOMBRE, Tags=TAGS)["FirewallRuleGroupAssociation"]["Id"]
    creado["asoc"] = asoc
    while r53r.get_firewall_rule_group_association(FirewallRuleGroupAssociationId=asoc)["FirewallRuleGroupAssociation"]["Status"] != "COMPLETE":
        time.sleep(10)
    print("DNS Firewall asociado: permite *.amazonaws.com y bloquea el resto (NXDOMAIN)", flush=True)


def imagen(cuenta):
    repo = ecr.create_repository(repositoryName=NOMBRE, tags=TAGS)["repository"]["repositoryUri"]
    creado["repo"] = NOMBRE
    tok = ecr.get_authorization_token()["authorizationData"][0]
    usuario, clave = base64.b64decode(tok["authorizationToken"]).decode().split(":")
    subprocess.run(["docker", "login", "-u", usuario, "--password-stdin", tok["proxyEndpoint"]],
                   input=clave.encode(), check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "Dockerfile"), "w").write(DOCKERFILE)
        open(os.path.join(d, "main.py"), "w").write(KERNEL)
        t0 = time.time()
        subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", f"{repo}:v1", d], check=True, capture_output=True)
        subprocess.run(["docker", "push", f"{repo}:v1"], check=True, capture_output=True)
    tam = ecr.describe_images(repositoryName=NOMBRE)["imageDetails"][0]["imageSizeInBytes"]
    print(f"imagen {repo}:v1 subida ({tam / 1e6:.0f} MB comprimida) en {time.time() - t0:.0f} s", flush=True)
    return f"{repo}:v1"


def rol(cuenta):
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": cuenta}}}]}
    iam.create_role(RoleName=NOMBRE, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol"] = NOMBRE
    politica = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{REGION}:{cuenta}:repository/{NOMBRE}"},
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": f"arn:aws:s3:::{NOMBRE}/latidos/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                       "logs:DescribeLogStreams", "logs:DescribeLogGroups"], "Resource": "*"}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="runtime", PolicyDocument=json.dumps(politica))
    return f"arn:aws:iam::{cuenta}:role/{NOMBRE}"


def probar(cuenta, con_larga):
    vpc, subredes, sg = base.vpc("10.82.0.0/16", True, con_s3=True)
    endpoints(vpc, subredes)
    dns_firewall(vpc)
    uri = imagen(cuenta)
    rol_arn = rol(cuenta)
    s3.create_bucket(Bucket=NOMBRE, CreateBucketConfiguration={"LocationConstraint": REGION}) if REGION != "us-east-1" \
        else s3.create_bucket(Bucket=NOMBRE)
    creado["bucket"] = NOMBRE
    time.sleep(15)

    t0 = time.time()
    try:
        r = ctl.create_agent_runtime(
            agentRuntimeName=NOMBRE_, roleArn=rol_arn, platformVersion="V2",
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
            networkConfiguration={"networkMode": "VPC", "networkModeConfig": {"subnets": subredes, "securityGroups": [sg]}},
            protocolConfiguration={"serverProtocol": "HTTP"}, environmentVariables={"BUCKET": NOMBRE},
            lifecycleConfiguration={"idleRuntimeSessionTimeout": 60, "maxLifetime": MAX_VIDA}, tags=TAGS_DICT)
    except ClientError as e:
        anota("Runtime V2 con imagen de ECR: se crea", False, codigo(e) + ": " + str(e)[:300])
        return
    rid, arn = r["agentRuntimeId"], r["agentRuntimeArn"]
    creado.setdefault("runtimes", []).append(rid)
    print("runtime", rid, "descargando la imagen y preparando la instantánea…", flush=True)
    try:
        _, info = base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=rid))
    except RuntimeError as e:
        anota("Imagen por los endpoints de ECR, sin NAT y con DNS Firewall: READY", False, str(e)[:400])
        return
    anota("Imagen por los endpoints de ECR, sin NAT y con DNS Firewall: READY", True,
          f"en {time.time() - t0:.0f} s, platformVersion={info.get('platformVersion')}")

    tiempos, resp = [], []
    for i in range(10):
        t, c = base.invoca(arn, base.sesion_nueva(), {"red": i == 0})
        tiempos.append(t)
        resp.append(c)
    o = sorted(tiempos)
    print(f"     arranques en frío (s): {', '.join(f'{t:.2f}' for t in tiempos)}", flush=True)
    anota("Arranque en frío con Strands cargado (10 sesiones)", True,
          f"P50 {statistics.median(o):.2f} s · P90 {o[8]:.2f} s · máx {o[-1]:.2f} s · Strands {resp[0]['strands']} "
          f"cargado en {resp[0]['carga_s']} s antes de la instantánea")
    anota("Instantánea: el mismo arranque en todas las sesiones", len({c["arranque"] for c in resp}) == 1)

    red = resp[0]["red"]
    print("     DNS desde la microVM:", red, flush=True)
    anota("DNS Firewall: los dominios externos no resuelven",
          all(red[k].startswith("error") for k in ("externo", "externo2")), f"example.com={red['externo']}, pypi.org={red['externo2']}")
    aws = {k: v for k, v in red.items() if not k.startswith("externo")}
    anota("DNS Firewall: los de AWS resuelven (con sus CNAME)", not [v for v in aws.values() if v.startswith("error")],
          ", ".join(f"{k}={v}" for k, v in aws.items()))

    if not con_larga:
        return
    run_id = base.uuid.uuid4().hex
    t0 = time.time()
    base.invoca(arn, base.sesion_nueva(), {"larga": 4000, "run_id": run_id})
    print(f"ejecución larga lanzada; vigilando su latido en S3 hasta que pare (maxLifetime={MAX_VIDA} s)…", flush=True)
    ultimo, visto = None, time.time()
    while time.time() - t0 < MAX_VIDA + 900:
        try:
            ultimo = json.loads(s3.get_object(Bucket=NOMBRE, Key=f"latidos/{run_id}.json")["Body"].read())
            if ultimo["segundos"] != getattr(probar, "_ult", None):
                probar._ult, visto = ultimo["segundos"], time.time()
        except ClientError:
            pass
        if ultimo and time.time() - visto > 180:
            break
        time.sleep(60)
    vida = ultimo["segundos"] if ultimo else 0
    print(f"     último latido a los {vida} s de empezar", flush=True)
    anota("Ocupado (HealthyBusy): no se corta por la inactividad de 60 s", vida > 300, f"{vida} s")
    anota("La ejecución dura hasta maxLifetime (55 min)", MAX_VIDA - 120 <= vida <= MAX_VIDA + 60, f"último latido a {vida} s")


def borrar():
    if "asoc" in creado:
        intenta("asociación dns firewall", lambda: r53r.disassociate_firewall_rule_group(FirewallRuleGroupAssociationId=creado["asoc"]))
        for _ in range(30):
            try:
                r53r.get_firewall_rule_group_association(FirewallRuleGroupAssociationId=creado["asoc"])
                time.sleep(10)
            except ClientError:
                break
    if "rg" in creado:
        for dl in ("dl_aws", "dl_todo"):
            if dl in creado:
                intenta("regla", lambda dl=dl: r53r.delete_firewall_rule(FirewallRuleGroupId=creado["rg"],
                                                                         FirewallDomainListId=creado[dl]))
        intenta("grupo de reglas", lambda: r53r.delete_firewall_rule_group(FirewallRuleGroupId=creado["rg"]))
    for dl in ("dl_aws", "dl_todo"):
        if dl in creado:
            intenta("lista de dominios", lambda dl=dl: r53r.delete_firewall_domain_list(FirewallDomainListId=creado[dl]))
    if "repo" in creado:
        intenta("repo ecr", lambda: ecr.delete_repository(repositoryName=NOMBRE, force=True))
    base.borrar()  # runtime, bucket, rol y VPC (con sus endpoints)


ec2, s3, iam, ctl = base.ec2, base.s3, base.iam, base.ctl

if __name__ == "__main__":
    try:
        cuenta = base.sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, flush=True)
        probar(cuenta, "--sin-larga" not in sys.argv)
    finally:
        borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
