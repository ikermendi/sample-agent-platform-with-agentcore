"""Sonda 14 del nivel 2: trazas de punta a punta con OpenTelemetry y X-Ray (Transaction Search).

Comprueba contra AWS lo que la sección "Trazas de punta a punta" del documento de arquitectura deja en
"Por verificar":

  A. API Gateway REST (HTTP_PROXY por VPC Link, streaming, trazado activo en la etapa) delante de un
     servidor que devuelve las cabeceras que recibe: ¿llega `traceparent` tal cual?, ¿continúa la traza
     de `X-Amzn-Trace-Id` con `Sampled=1`?, ¿qué pasa si solo llega `traceparent`?, ¿aparece el segmento
     de API Gateway en la traza?
  B. Una Lambda en la VPC por defecto (sin salida a internet) que envía tramos OTLP firmados con SigV4 a
     `xray.{región}.amazonaws.com/v1/traces`: sin endpoint no sale; con el endpoint de interfaz de X-Ray
     (DNS privado) sí. Tramos con id de formato X-Ray en la misma traza que la petición de A, uno que
     llega una hora "tarde", y uno con id W3C aleatorio (sin la hora delante).
  C. Un runtime de AgentCore (V2, PUBLIC) con ADOT (`opentelemetry-instrument`) invocado con
     `X-Amzn-Trace-Id` y `traceparent`: ¿continúa el contenedor la traza?, ¿dónde quedan sus tramos
     (`aws/spans` o el grupo de logs del runtime)?, ¿llega un tramo creado después de responder?

Al final busca los tramos de cada traza con Logs Insights en `aws/spans` y en los grupos de los runtimes.

Activa Transaction Search en la cuenta y región (configuración de toda la cuenta: pasa la ingesta de
X-Ray a CloudWatch Logs) y deja el destino como estaba al acabar. Todo lo demás con el prefijo
"sonda-n2-<sufijo>" y la etiqueta sonda=nivel2, y se borra al acabar aunque algo falle. Necesita Docker
(arm64) y boto3 >= 1.43. Unos 30-40 minutos; céntimos. Uso:

    SONDA_REGION=eu-north-1 AWS_PROFILE=sonda .venv312/bin/python sonda_trazas.py
"""
import base64
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

import http.client
from botocore.config import Config
from botocore.exceptions import ClientError

import sonda_agentcore as base

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = base.REGION, base.NOMBRE, base.NOMBRE_, base.TAGS, base.TAGS_DICT
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
ses, ctl, iam, ec2, sts = base.ses, base.ctl, base.iam, base.ec2, base.sts
elb, apigw, ssm, lam, logs, xray, ecr = (ses.client(x) for x in
                                          ("elbv2", "apigateway", "ssm", "lambda", "logs", "xray", "ecr"))
dp = ses.client("bedrock-agentcore", config=Config(retries={"max_attempts": 2, "mode": "standard"}, read_timeout=120))
TRAZAS = {}  # nombre → trace_id (32 hex)


def id_xray():
    """Formato X-Ray: 8 hex de hora en segundos + 24 aleatorios."""
    return f"{int(time.time()):08x}{secrets.token_hex(12)}"


def cab_xray(trace_id, padre):
    return f"Root=1-{trace_id[:8]}-{trace_id[8:]};Parent={padre};Sampled=1"


def cab_w3c(trace_id, padre):
    return f"00-{trace_id}-{padre}-01"


# ---------------------------------------------------------------- 0. Transaction Search
def activar_transaction_search(cuenta):
    antes = xray.get_trace_segment_destination()
    creado["ts_antes"] = antes["Destination"]
    creado["spans_existia"] = bool(logs.describe_log_groups(logGroupNamePrefix="aws/spans").get("logGroups"))
    print(f"Transaction Search antes: destino {antes['Destination']} ({antes['Status']}); "
          f"aws/spans {'ya existía' if creado['spans_existia'] else 'no existía'}", flush=True)
    if antes["Destination"] == "CloudWatchLogs":
        return
    politica = {"Version": "2012-10-17", "Statement": [{
        "Sid": "TransactionSearchXRayAccess", "Effect": "Allow", "Principal": {"Service": "xray.amazonaws.com"},
        "Action": "logs:PutLogEvents",
        "Resource": [f"arn:aws:logs:{REGION}:{cuenta}:log-group:aws/spans:*",
                     f"arn:aws:logs:{REGION}:{cuenta}:log-group:/aws/application-signals/data:*"],
        "Condition": {"ArnLike": {"aws:SourceArn": f"arn:aws:xray:{REGION}:{cuenta}:*"},
                      "StringEquals": {"aws:SourceAccount": cuenta}}}]}
    logs.put_resource_policy(policyName=NOMBRE, policyDocument=json.dumps(politica))
    creado["politica_logs"] = NOMBRE
    xray.update_trace_segment_destination(Destination="CloudWatchLogs")
    creado["ts_cambiado"] = True
    t0 = time.time()
    while time.time() - t0 < 600:
        d = xray.get_trace_segment_destination()
        if d["Destination"] == "CloudWatchLogs" and d["Status"] == "ACTIVE":
            break
        time.sleep(15)
    anota("0. Transaction Search activado por API", d["Destination"] == "CloudWatchLogs" and d["Status"] == "ACTIVE",
          f"{d['Destination']} {d['Status']} en {time.time() - t0:.0f} s")


# ---------------------------------------------------------------- A. API Gateway
ECO = r'''
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def do_GET(self):
        b = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
ThreadingHTTPServer(("", 8080), H).serve_forever()
'''
USER_DATA = "#!/bin/bash\ncat > /opt/eco.py <<'PY'\n" + ECO + "\nPY\nnohup python3 /opt/eco.py >/var/log/eco.log 2>&1 &\n"


def vpc_defecto():
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    subredes = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]},
                                             {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    return vpc, [s["SubnetId"] for s in sorted(subredes, key=lambda s: s["AvailabilityZone"])]


def crear_api():
    vpc, subredes = vpc_defecto()
    sg = ec2.create_security_group(GroupName=NOMBRE + "-eco", Description="sonda trazas eco", VpcId=vpc["VpcId"],
                                   TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
    creado["sg_eco"] = sg
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": vpc["CidrBlock"]}]}])
    ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
    iid = ec2.run_instances(ImageId=ami, InstanceType="t4g.nano", MinCount=1, MaxCount=1, SubnetId=subredes[0],
                            SecurityGroupIds=[sg], UserData=USER_DATA, MetadataOptions={"HttpTokens": "required"},
                            TagSpecifications=[{"ResourceType": "instance",
                                                "Tags": TAGS + [{"Key": "Name", "Value": NOMBRE}]}])["Instances"][0]["InstanceId"]
    creado["instancia"] = iid
    tg = elb.create_target_group(Name=NOMBRE, Protocol="TCP", Port=8080, VpcId=vpc["VpcId"], TargetType="instance",
                                 HealthCheckProtocol="TCP", HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
                                 Tags=TAGS)["TargetGroups"][0]["TargetGroupArn"]
    creado["tg"] = tg
    nlb = elb.create_load_balancer(Name=NOMBRE, Type="network", Scheme="internal", Subnets=[subredes[0]],
                                   Tags=TAGS)["LoadBalancers"][0]
    creado["nlb"] = nlb["LoadBalancerArn"]
    elb.create_listener(LoadBalancerArn=nlb["LoadBalancerArn"], Protocol="TCP", Port=80,
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}])
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    elb.register_targets(TargetGroupArn=tg, Targets=[{"Id": iid, "Port": 8080}])
    elb.get_waiter("load_balancer_available").wait(LoadBalancerArns=[nlb["LoadBalancerArn"]])
    link = apigw.create_vpc_link(name=NOMBRE, targetArns=[nlb["LoadBalancerArn"]], tags=TAGS_DICT)["id"]
    creado["vpc_link"] = link
    elb.get_waiter("target_in_service").wait(TargetGroupArn=tg, Targets=[{"Id": iid, "Port": 8080}],
                                             WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    while True:
        e = apigw.get_vpc_link(vpcLinkId=link)
        if e["status"] == "AVAILABLE":
            break
        if e["status"] == "FAILED":
            raise RuntimeError("VPC Link FAILED: " + e.get("statusMessage", ""))
        time.sleep(15)
    api = apigw.create_rest_api(name=NOMBRE, endpointConfiguration={"types": ["REGIONAL"]}, tags=TAGS_DICT)["id"]
    creado["api"] = api
    raiz = apigw.get_resources(restApiId=api)["items"][0]["id"]
    proxy = apigw.create_resource(restApiId=api, parentId=raiz, pathPart="{proxy+}")["id"]
    apigw.put_method(restApiId=api, resourceId=proxy, httpMethod="GET", authorizationType="NONE",
                     requestParameters={"method.request.path.proxy": True})
    apigw.put_integration(restApiId=api, resourceId=proxy, httpMethod="GET", type="HTTP_PROXY",
                          integrationHttpMethod="GET", connectionType="VPC_LINK", connectionId=link,
                          uri=f"http://{nlb['DNSName']}/{{proxy}}",
                          requestParameters={"integration.request.path.proxy": "method.request.path.proxy"},
                          responseTransferMode="STREAM", timeoutInMillis=900000)
    apigw.create_deployment(restApiId=api, stageName="p")
    apigw.update_stage(restApiId=api, stageName="p",
                       patchOperations=[{"op": "replace", "path": "/tracingEnabled", "value": "true"}])
    time.sleep(10)
    creado["api_host"] = f"{api}.execute-api.{REGION}.amazonaws.com"
    print("API lista:", creado["api_host"], flush=True)


def pide(host, cabeceras):
    for intento in range(8):  # el despliegue recién hecho tarda en servir
        try:
            c = http.client.HTTPSConnection(host, timeout=30)
            c.request("GET", "/p/eco", headers=cabeceras)
            r = c.getresponse()
            cuerpo = r.read()
            if r.status == 200:
                return json.loads(cuerpo), {k.lower(): v for k, v in r.getheaders()}
            print(f"     intento {intento + 1}: HTTP {r.status} {cuerpo[:120]!r}", flush=True)
        except Exception as e:
            print(f"     intento {intento + 1}: {type(e).__name__} {str(e)[:120]}", flush=True)
        time.sleep(15)
    raise RuntimeError("la API no responde")


def probar_api():
    host = creado["api_host"]
    # a) las dos cabeceras, mismos ids
    t, p = id_xray(), secrets.token_hex(8)
    TRAZAS["A_api"] = t
    creado["padre_app"] = p
    eco, resp = pide(host, {"traceparent": cab_w3c(t, p), "X-Amzn-Trace-Id": cab_xray(t, p)})
    llega_tp = eco.get("traceparent", "")
    llega_x = eco.get("x-amzn-trace-id", "")
    print(f"     a) llega traceparent={llega_tp!r}\n        llega X-Amzn-Trace-Id={llega_x!r}\n"
          f"        respuesta X-Amzn-Trace-Id={resp.get('x-amzn-trace-id')!r}", flush=True)
    anota("A1. traceparent llega a la integración tal cual", llega_tp == cab_w3c(t, p), llega_tp)
    raiz = f"Root=1-{t[:8]}-{t[8:]}"
    padre_gw = next((x.split("=", 1)[1] for x in llega_x.split(";") if x.startswith("Parent=")), None)
    anota("A2. API Gateway continúa la traza de X-Amzn-Trace-Id (mismo Root, Sampled=1)",
          raiz in llega_x and "Sampled=1" in llega_x, llega_x)
    anota("A3. API Gateway pone su propio segmento como padre", bool(padre_gw) and padre_gw != p,
          f"Parent que llega = {padre_gw} (el de la app era {p})")
    creado["padre_gw"] = padre_gw
    # b) solo traceparent
    t2, p2 = id_xray(), secrets.token_hex(8)
    TRAZAS["A_solo_traceparent"] = t2
    eco2, _ = pide(host, {"traceparent": cab_w3c(t2, p2)})
    x2 = eco2.get("x-amzn-trace-id", "")
    print(f"     b) solo traceparent: llega traceparent={eco2.get('traceparent')!r}, X-Amzn-Trace-Id={x2!r}", flush=True)
    anota("A4. Solo con traceparent: llega igual a la integración (API Gateway no lo toca)",
          eco2.get("traceparent") == cab_w3c(t2, p2),
          f"y API Gateway abre su propia traza: {x2[:60]} (¿mismo id? {t2[8:] in x2})")


# ---------------------------------------------------------------- B. OTLP por el VPC endpoint de X-Ray
LAMBDA = r'''
import json, os, socket, urllib.request, urllib.error
import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

def handler(ev, ctx):
    region = os.environ["AWS_REGION"]
    host = f"xray.{region}.amazonaws.com"
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})
    except Exception as e:
        ips = ["ERR " + str(e)]
    body = json.dumps(ev["otlp"]).encode()
    req = AWSRequest(method="POST", url=f"https://{host}/v1/traces", data=body,
                     headers={"Content-Type": "application/json"})
    SigV4Auth(botocore.session.get_session().get_credentials(), "xray", region).add_auth(req)
    r = urllib.request.Request(req.url, data=body, headers=dict(req.headers), method="POST")
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            return {"ips": ips, "status": resp.status, "cuerpo": resp.read()[:300].decode("latin1")}
    except urllib.error.HTTPError as e:
        return {"ips": ips, "status": e.code, "cuerpo": e.read()[:300].decode("latin1")}
    except Exception as e:
        return {"ips": ips, "status": None, "error": type(e).__name__ + ": " + str(e)[:200]}
'''


def crear_lambda(cuenta):
    vpc, subredes = vpc_defecto()
    confianza = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                          "Action": "sts:AssumeRole"}]}
    iam.create_role(RoleName=NOMBRE + "-l", AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol_lambda"] = NOMBRE + "-l"
    iam.attach_role_policy(RoleName=NOMBRE + "-l",
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole")
    iam.put_role_policy(RoleName=NOMBRE + "-l", PolicyName="xray", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "xray:*", "Resource": "*"}]}))
    sg = ec2.create_security_group(GroupName=NOMBRE + "-l", Description="sonda trazas lambda", VpcId=vpc["VpcId"],
                                   TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
    creado["sg_lambda"] = sg
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("main.py", LAMBDA)
    time.sleep(12)  # el rol recién creado tarda en poder asumirse
    for intento in range(6):
        try:
            lam.create_function(FunctionName=NOMBRE, Runtime="python3.12", Role=f"arn:aws:iam::{cuenta}:role/{NOMBRE}-l",
                                Handler="main.handler", Code={"ZipFile": zbuf.getvalue()}, Timeout=15,
                                Architectures=["arm64"], VpcConfig={"SubnetIds": subredes[:2], "SecurityGroupIds": [sg]},
                                Tags=TAGS_DICT)
            break
        except ClientError as e:
            if codigo(e) != "InvalidParameterValueException" or intento == 5:
                raise
            time.sleep(10)
    creado["lambda"] = NOMBRE
    lam.get_waiter("function_active_v2").wait(FunctionName=NOMBRE)
    print("Lambda en la VPC lista", flush=True)


def crear_endpoint_xray():
    vpc, subredes = vpc_defecto()
    sg = ec2.create_security_group(GroupName=NOMBRE + "-ep", Description="sonda trazas endpoint", VpcId=vpc["VpcId"],
                                   TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
    creado["sg_ep"] = sg
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": vpc["CidrBlock"]}]}])
    ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc["VpcId"],
                                 ServiceName=f"com.amazonaws.{REGION}.xray", SubnetIds=subredes[:2],
                                 SecurityGroupIds=[sg], PrivateDnsEnabled=True,
                                 TagSpecifications=[{"ResourceType": "vpc-endpoint", "Tags": TAGS}])["VpcEndpoint"]
    creado["endpoint_xray"] = ep["VpcEndpointId"]
    while ec2.describe_vpc_endpoints(VpcEndpointIds=[ep["VpcEndpointId"]])["VpcEndpoints"][0]["State"] != "available":
        time.sleep(10)
    print("endpoint de X-Ray disponible; espero a que el DNS privado se aplique", flush=True)
    time.sleep(60)


def tramo(trace_id, span_id, nombre, ini, fin, padre=None, attrs=None):
    t = {"traceId": trace_id, "spanId": span_id, "name": nombre, "kind": 1,
         "startTimeUnixNano": str(int(ini * 1e9)), "endTimeUnixNano": str(int(fin * 1e9)),
         "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in (attrs or {}).items()]}
    if padre:
        t["parentSpanId"] = padre
    return t


def otlp(tramos, servicio="sonda-lambda"):
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": servicio}}]},
                               "scopeSpans": [{"scope": {"name": "sonda"}, "spans": tramos}]}]}


def invoca_lambda(carga):
    r = lam.invoke(FunctionName=NOMBRE, Payload=json.dumps(carga).encode())
    return json.loads(r["Payload"].read())


def probar_otlp_sin_endpoint():
    ahora = time.time()
    t = id_xray()
    r = invoca_lambda({"otlp": otlp([tramo(t, secrets.token_hex(8), "sonda-sin-endpoint", ahora - 1, ahora)])})
    print("     sin endpoint:", r, flush=True)
    anota("B1. Sin endpoint de X-Ray, la Lambda en la VPC no llega al endpoint OTLP", r.get("status") is None,
          r.get("error", f"status {r.get('status')}"))


def probar_otlp():
    ahora = time.time()
    t = TRAZAS.get("A_api") or id_xray()
    padre = creado.get("padre_gw") or creado.get("padre_app")
    TRAZAS.setdefault("A_api", t)
    r = invoca_lambda({"otlp": otlp([tramo(t, secrets.token_hex(8), "sonda-lambda-normal", ahora - 0.5, ahora, padre,
                                           {"run_id": "sonda"})])})
    print("     con endpoint:", r, flush=True)
    privadas = [ip for ip in r.get("ips", []) if ip.startswith(("10.", "172.", "192.168."))]
    anota("B2. Con el endpoint de interfaz, el endpoint OTLP de X-Ray responde (IP privada)",
          r.get("status") == 200 and bool(privadas), f"status {r.get('status')} · IPs {r.get('ips')} · {r.get('cuerpo', r.get('error'))}")
    # un tramo que llega una hora tarde, en la misma traza
    r2 = invoca_lambda({"otlp": otlp([tramo(t, secrets.token_hex(8), "sonda-lambda-tarde", ahora - 3600, ahora - 3599, padre)])})
    anota("B3. Un tramo con hora de hace 1 h se acepta", r2.get("status") == 200, f"status {r2.get('status')} {r2.get('cuerpo', '')[:120]}")
    # id W3C aleatorio, sin la hora delante (el primer byte a ff: no puede ser una hora razonable)
    tw = "ff" + secrets.token_hex(15)
    TRAZAS["B_w3c_aleatorio"] = tw
    r3 = invoca_lambda({"otlp": otlp([tramo(tw, secrets.token_hex(8), "sonda-lambda-w3c", ahora - 0.5, ahora)])})
    anota("B4. Un id W3C sin la hora delante: lo acepta el endpoint", r3.get("status") == 200,
          f"status {r3.get('status')} {r3.get('cuerpo', '')[:120]} (si se guarda, se ve al final)")


# ---------------------------------------------------------------- C. AgentCore con ADOT
DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.12-slim
RUN pip install --no-cache-dir bedrock-agentcore==1.25.2 'aws-opentelemetry-distro>=0.18' boto3
COPY main.py /app/
WORKDIR /app
EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "main.py"]
"""

KERNEL = r'''
import os, threading, time
from opentelemetry import trace, context as otctx
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
tr = trace.get_tracer("sonda-kernel")


def hx(n, w):
    return format(n, f"0{w}x")


@app.entrypoint
async def h(payload, context=None):
    if payload.get("flush"):
        try:
            ok = trace.get_tracer_provider().force_flush(10000)
        except Exception as e:
            ok = repr(e)
        return {"flush": ok}
    cur = trace.get_current_span()
    sc = cur.get_span_context()
    padre = getattr(cur, "parent", None)
    with tr.start_as_current_span("sonda-kernel-ejecucion") as s:
        s.set_attribute("run_id", payload.get("run_id", ""))
        ctx = otctx.get_current()
        propio = s.get_span_context()

        def tarde():
            time.sleep(15)
            tok = otctx.attach(ctx)
            with tr.start_as_current_span("sonda-kernel-tarde"):
                time.sleep(0.05)
            otctx.detach(tok)
        threading.Thread(target=tarde, daemon=True).start()
    return {"trace_actual": hx(sc.trace_id, 32) if sc.is_valid else None,
            "tramo_actual": hx(sc.span_id, 16) if sc.is_valid else None,
            "nombre_actual": getattr(cur, "name", None),
            "padre_actual": hx(padre.span_id, 16) if padre else None,
            "trace_propio": hx(propio.trace_id, 32),
            "proveedor": type(trace.get_tracer_provider()).__name__,
            "env": {k: v for k, v in sorted(os.environ.items())
                    if k.startswith(("OTEL", "AGENT_OBS", "AWS_GENAI", "UNIFIED", "DISABLE_ADOT"))}}

app.run()
'''


def imagen_kernel():
    repo = ecr.create_repository(repositoryName=NOMBRE, tags=TAGS)["repository"]["repositoryUri"]
    creado["repo"] = NOMBRE
    tok = ecr.get_authorization_token()["authorizationData"][0]
    usuario, clave = base64.b64decode(tok["authorizationToken"]).decode().split(":")
    subprocess.run(["docker", "login", "-u", usuario, "--password-stdin", tok["proxyEndpoint"]],
                   input=clave.encode(), check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "Dockerfile"), "w").write(DOCKERFILE)
        open(os.path.join(d, "main.py"), "w").write(KERNEL)
        for cmd in (["docker", "build", "--platform", "linux/arm64", "-t", f"{repo}:v1", d], ["docker", "push", f"{repo}:v1"]):
            p = subprocess.run(cmd, capture_output=True)
            if p.returncode:
                raise RuntimeError(p.stderr.decode()[-1500:])
    print("imagen del kernel subida", flush=True)
    return f"{repo}:v1"


def crear_runtime(cuenta):
    uri = imagen_kernel()
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": cuenta}}}]}
    iam.create_role(RoleName=NOMBRE + "-k", AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol_kernel"] = NOMBRE + "-k"
    iam.put_role_policy(RoleName=NOMBRE + "-k", PolicyName="runtime", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
             "Resource": f"arn:aws:ecr:{REGION}:{cuenta}:repository/{NOMBRE}"},
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                           "logs:DescribeLogStreams", "logs:DescribeLogGroups", "logs:PutResourcePolicy",
                                           "logs:DescribeResourcePolicies"], "Resource": "*"},
            {"Effect": "Allow", "Action": "xray:*", "Resource": "*"},
            {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
             "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}}]}))
    time.sleep(15)
    r = ctl.create_agent_runtime(
        agentRuntimeName=NOMBRE_, roleArn=f"arn:aws:iam::{cuenta}:role/{NOMBRE}-k", platformVersion="V2",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
        networkConfiguration={"networkMode": "PUBLIC"}, protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600}, tags=TAGS_DICT)
    creado.setdefault("runtimes", []).append(r["agentRuntimeId"])
    creado["runtime_arn"] = r["agentRuntimeArn"]
    base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"]))
    print("runtime READY", r["agentRuntimeId"], flush=True)


def probar_runtime():
    arn = creado["runtime_arn"]
    t, p = id_xray(), secrets.token_hex(8)
    TRAZAS["C_kernel"] = t
    sesion = base.sesion_nueva()
    creado["sesion"] = sesion
    r = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sesion, contentType="application/json",
                                accept="application/json", payload=json.dumps({"run_id": "sonda-c"}).encode(),
                                traceId=cab_xray(t, p), traceParent=cab_w3c(t, p), baggage="run_id=sonda-c")
    c = json.loads(r["response"].read())
    print("     kernel:", json.dumps(c, indent=1)[:1500], flush=True)
    anota("C1. El contenedor continúa la traza de la invocación (mismo trace_id)",
          c.get("trace_actual") == t or c.get("trace_propio") == t,
          f"actual {c.get('trace_actual')} ({c.get('nombre_actual')}), propio {c.get('trace_propio')}, esperado {t}")
    anota("C2. El tramo del contenedor cuelga del padre que mandamos o de uno de AgentCore",
          c.get("padre_actual") is not None, f"padre {c.get('padre_actual')} (el nuestro era {p})")
    creado["kernel_env"] = c.get("env")
    time.sleep(25)  # el tramo "tarde" se crea a los 15 s
    try:
        f = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sesion, contentType="application/json",
                                    accept="application/json", payload=json.dumps({"flush": True}).encode())
        print("     flush:", f["response"].read()[:200], flush=True)
    except ClientError as e:
        print("     flush falló:", codigo(e), flush=True)


# ---------------------------------------------------------------- Búsqueda final
def consulta(grupos, ids):
    lista = ", ".join(f"'{i}'" for i in ids)
    q = (f"fields @logStream, traceId, spanId, parentSpanId, name, resource.attributes.service.name as servicio "
         f"| filter traceId in [{lista}] | sort startTimeUnixNano asc | limit 200")
    qid = logs.start_query(logGroupNames=grupos, startTime=int(time.time() - 7200), endTime=int(time.time() + 300),
                           queryString=q)["queryId"]
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            return [{c["field"]: c["value"] for c in fila} for fila in r.get("results", [])]
        time.sleep(2)


def buscar():
    grupos = ["aws/spans"]
    for rid in creado.get("runtimes", []):
        grupos += [g["logGroupName"] for g in
                   logs.describe_log_groups(logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{rid}").get("logGroups", [])]
    grupos = [g for g in grupos if logs.describe_log_groups(logGroupNamePrefix=g).get("logGroups")]
    print("buscando tramos en", grupos, flush=True)
    filas = []
    for _ in range(16):  # hasta 8 minutos
        filas = consulta(grupos, list(TRAZAS.values()))
        nombres = {f.get("name") for f in filas}
        if {"sonda-lambda-normal", "sonda-kernel-tarde"} <= nombres and len(filas) >= 6:
            break
        time.sleep(30)
    print("     tramos encontrados:", flush=True)
    for f in filas:
        quien = next((k for k, v in TRAZAS.items() if v == f.get("traceId")), "?")
        print(f"       [{quien}] {f.get('name')} · servicio {f.get('servicio')} · padre {f.get('parentSpanId')} · "
              f"grupo/flujo {f.get('@logStream')}", flush=True)
    de = lambda clave: [f for f in filas if f.get("traceId") == TRAZAS.get(clave)]
    a = de("A_api")
    gw = [f for f in a if "sonda-lambda" not in (f.get("name") or "")]
    anota("A5. El segmento de API Gateway queda en la traza (aws/spans)", bool(gw),
          ", ".join(sorted({f.get('name') or '?' for f in gw})) or "ninguno")
    anota("B5. Los tramos de la Lambda (normal y una hora tarde) caen en la misma traza que API Gateway",
          {"sonda-lambda-normal", "sonda-lambda-tarde"} <= {f.get("name") for f in a},
          f"{len(a)} tramos en la traza A")
    anota("B6. El tramo con id W3C aleatorio se guarda", bool(de("B_w3c_aleatorio")), f"{len(de('B_w3c_aleatorio'))} tramos")
    k = de("C_kernel")
    flujos = sorted({f.get("@logStream") for f in k})
    anota("C3. Los tramos del contenedor y de AgentCore se encuentran por trace_id", bool(k),
          f"{len(k)} tramos: {', '.join(sorted({f.get('name') or '?' for f in k}))[:300]} · flujos {flujos}")
    anota("C4. El tramo creado 15 s después de responder llega a la misma traza",
          any(f.get("name") == "sonda-kernel-tarde" for f in k), "")
    print("     entorno OTEL del contenedor:", json.dumps(creado.get("kernel_env"), indent=1)[:2000], flush=True)


# ---------------------------------------------------------------- Borrado
def borrar():
    print("borrando…", flush=True)
    if "sesion" in creado:
        intenta("sesión", lambda: dp.stop_runtime_session(agentRuntimeArn=creado["runtime_arn"], runtimeSessionId=creado["sesion"]))
    for rid in creado.get("runtimes", []):
        intenta("runtime", lambda rid=rid: ctl.delete_agent_runtime(agentRuntimeId=rid))
    if "repo" in creado:
        intenta("ecr", lambda: ecr.delete_repository(repositoryName=creado["repo"], force=True))
    if "lambda" in creado:
        intenta("lambda", lambda: lam.delete_function(FunctionName=creado["lambda"]))
    if "api" in creado:
        intenta("api", lambda: apigw.delete_rest_api(restApiId=creado["api"]))
    if "vpc_link" in creado:
        intenta("vpc link", lambda: apigw.delete_vpc_link(vpcLinkId=creado["vpc_link"]))
        for _ in range(60):
            try:
                apigw.get_vpc_link(vpcLinkId=creado["vpc_link"])
                time.sleep(10)
            except ClientError:
                break
    if "nlb" in creado:
        intenta("nlb", lambda: elb.delete_load_balancer(LoadBalancerArn=creado["nlb"]))
        intenta("nlb", lambda: elb.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[creado["nlb"]]))
    if "instancia" in creado:
        intenta("instancia", lambda: ec2.terminate_instances(InstanceIds=[creado["instancia"]]))
    if "tg" in creado:
        for _ in range(20):
            try:
                elb.delete_target_group(TargetGroupArn=creado["tg"])
                break
            except ClientError as e:
                if codigo(e) != "ResourceInUse":
                    break
                time.sleep(15)
    if "endpoint_xray" in creado:
        intenta("endpoint", lambda: ec2.delete_vpc_endpoints(VpcEndpointIds=[creado["endpoint_xray"]]))
    if "instancia" in creado:
        intenta("instancia", lambda: ec2.get_waiter("instance_terminated").wait(InstanceIds=[creado["instancia"]]))
    for clave in ("sg_eco", "sg_lambda", "sg_ep"):
        if clave in creado:
            for _ in range(80):  # las interfaces de la Lambda y del NLB tardan en soltarse
                try:
                    ec2.delete_security_group(GroupId=creado[clave])
                    break
                except ClientError as e:
                    if codigo(e) != "DependencyViolation":
                        print("   aviso al borrar", clave, codigo(e))
                        break
                    time.sleep(15)
            else:
                print("   aviso: el grupo de seguridad", creado[clave], "sigue en uso; bórralo a mano")
    for clave in ("rol_lambda", "rol_kernel"):
        if clave in creado:
            rol = creado[clave]
            for p in iam.list_attached_role_policies(RoleName=rol).get("AttachedPolicies", []):
                intenta("política", lambda p=p: iam.detach_role_policy(RoleName=rol, PolicyArn=p["PolicyArn"]))
            for p in iam.list_role_policies(RoleName=rol).get("PolicyNames", []):
                intenta("política", lambda p=p: iam.delete_role_policy(RoleName=rol, PolicyName=p))
            intenta("rol", lambda: iam.delete_role(RoleName=rol))
    for prefijo in [f"/aws/lambda/{NOMBRE}"] + [f"/aws/bedrock-agentcore/runtimes/{r}" for r in creado.get("runtimes", [])]:
        for g in logs.describe_log_groups(logGroupNamePrefix=prefijo).get("logGroups", []):
            intenta("logs", lambda g=g: logs.delete_log_group(logGroupName=g["logGroupName"]))
    # Transaction Search como estaba
    if creado.get("ts_cambiado"):
        try:
            xray.update_trace_segment_destination(Destination=creado["ts_antes"])
            d = xray.get_trace_segment_destination()
            print(f"Transaction Search restaurado: destino {d['Destination']} ({d['Status']})", flush=True)
        except ClientError as e:
            print("   AVISO: no se pudo restaurar el destino de X-Ray:", codigo(e), str(e)[:200], flush=True)
    if "politica_logs" in creado:
        intenta("política de logs", lambda: logs.delete_resource_policy(policyName=creado["politica_logs"]))
    if creado.get("ts_cambiado") and not creado.get("spans_existia"):
        for g in ("aws/spans", "/aws/application-signals/data"):
            if logs.describe_log_groups(logGroupNamePrefix=g).get("logGroups"):
                intenta(g, lambda g=g: logs.delete_log_group(logGroupName=g))


if __name__ == "__main__":
    try:
        cuenta = sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, "·", NOMBRE, flush=True)
        activar_transaction_search(cuenta)
        errores = []

        def en_hilo(fn, *a):
            try:
                fn(*a)
            except Exception as e:
                errores.append(f"{fn.__name__}: {type(e).__name__}: {str(e)[:300]}")

        hilos = [threading.Thread(target=en_hilo, args=a) for a in ((crear_api,), (crear_runtime, cuenta), (crear_lambda, cuenta))]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        for e in errores:
            anota("montaje", False, e)
        for paso, cond in ((probar_api, "api_host"), (probar_otlp_sin_endpoint, "lambda"), (crear_endpoint_xray, "lambda"),
                           (probar_otlp, "lambda"), (probar_runtime, "runtime_arn")):
            if cond in creado:
                try:
                    paso()
                except Exception as e:
                    anota(paso.__name__, False, f"{type(e).__name__}: {str(e)[:300]}")
        buscar()
    finally:
        borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
