"""Sonda 4 del nivel 2: SSE por API Gateway REST con streaming, VPC Link y NLB.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre la entrada por la iDMZ
(secciones "Entry through the iDMZ" y "API Gateway REST streaming" en "Verified"):

  1. Una integración HTTP_PROXY por VPC Link con responseTransferMode=STREAM entrega los eventos
     según llegan (no espera al final de la respuesta).
  2. Sin datos, el stream se corta a los 5 minutos; con 4 minutos de silencio, aguanta.
  3. Con un latido cada 60 s (y cada 240 s), el stream dura hasta el tope de integración: 15 minutos.

Monta lo mínimo en la VPC por defecto: una instancia t4g.nano con un servidor SSE de juguete,
un NLB interno delante, un VPC Link y un API REST regional. Con --privatelink añade la cadena de
producción entre el VPC Link y el servidor: NLB frontal → endpoint de interfaz → servicio de endpoint
(PrivateLink) → NLB interno (en la misma VPC: el salto entre cuentas no cambia los tiempos). Todo con el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2; se borra al acabar,
aunque algo falle. Coste: céntimos (unos 30 minutos de NLB e instancia). Uso:

    SONDA_REGION=eu-north-1 python sonda_sse_idmz.py
    SONDA_REGION=eu-north-1 python sonda_sse_idmz.py --rapida     # solo la prueba de streaming
    SONDA_REGION=eu-north-1 python sonda_sse_idmz.py --privatelink  # con PrivateLink y segundo NLB
    SONDA_REGION=eu-north-1 python sonda_sse_idmz.py --conservar  # no borra
"""
import http.client
import os
import sys
import threading
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
TAGS_DICT = {"sonda": "nivel2"}

ses = boto3.Session(region_name=REGION)
ec2, elb, apigw, ssm = (ses.client(x) for x in ("ec2", "elbv2", "apigateway", "ssm"))
PRIVATELINK = "--privatelink" in sys.argv
creado = {}
resultados = []

SERVIDOR = r'''
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def trozo(self, datos):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(datos), datos))
        self.wfile.flush()
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not u.path.endswith("/sse"):
            b = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        latido = float(q.get("latido", ["0"])[0])
        dur = float(q.get("dur", ["60"])[0])
        cada = float(q.get("cada", ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        t0 = time.time()
        try:
            self.trozo(b"event: inicio\ndata: 0\n\n")
            prox_latido = t0 + latido if latido > 0 else None
            prox_evento = t0 + cada if cada > 0 else None
            while time.time() - t0 < dur:
                time.sleep(0.2)
                ahora = time.time()
                if prox_evento and ahora >= prox_evento:
                    self.trozo(b"event: delta\ndata: %.1f\n\n" % (ahora - t0))
                    prox_evento += cada
                if prox_latido and ahora >= prox_latido:
                    self.trozo(b": latido %d\n\n" % int(ahora - t0))
                    prox_latido += latido
            self.trozo(b"event: fin\ndata: %d\n\n" % int(time.time() - t0))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

ThreadingHTTPServer(("", 8080), H).serve_forever()
'''

USER_DATA = "#!/bin/bash\ncat > /opt/sse.py <<'PY'\n" + SERVIDOR + "\nPY\nnohup python3 /opt/sse.py >/var/log/sse.log 2>&1 &\n"


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def privatelink(vpc, subred, sg, nlb_interno):
    """Como en producción: NLB frontal → endpoint de interfaz → servicio de endpoint → NLB interno."""
    svc = ec2.create_vpc_endpoint_service_configuration(
        NetworkLoadBalancerArns=[nlb_interno], AcceptanceRequired=False,
        TagSpecifications=[{"ResourceType": "vpc-endpoint-service", "Tags": TAGS}])["ServiceConfiguration"]
    creado["svc"] = svc["ServiceId"]
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": vpc["CidrBlock"]}]}])
    ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc["VpcId"], ServiceName=svc["ServiceName"],
                                 SubnetIds=[subred], SecurityGroupIds=[sg], PrivateDnsEnabled=False,
                                 TagSpecifications=[{"ResourceType": "vpc-endpoint", "Tags": TAGS}])["VpcEndpoint"]
    creado["endpoint"] = ep["VpcEndpointId"]
    print("esperando al endpoint de PrivateLink…", flush=True)
    while True:
        ep = ec2.describe_vpc_endpoints(VpcEndpointIds=[creado["endpoint"]])["VpcEndpoints"][0]
        if ep["State"] == "available":
            break
        if ep["State"] in ("failed", "rejected"):
            raise RuntimeError("endpoint " + ep["State"])
        time.sleep(10)
    ip = ec2.describe_network_interfaces(NetworkInterfaceIds=ep["NetworkInterfaceIds"])["NetworkInterfaces"][0]["PrivateIpAddress"]
    tg = elb.create_target_group(Name=NOMBRE + "-f", Protocol="TCP", Port=80, VpcId=vpc["VpcId"], TargetType="ip",
                                 HealthCheckProtocol="TCP", HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
                                 Tags=TAGS)["TargetGroups"][0]["TargetGroupArn"]
    creado["tg_f"] = tg
    nlb = elb.create_load_balancer(Name=NOMBRE + "-f", Type="network", Scheme="internal", Subnets=[subred],
                                   Tags=TAGS)["LoadBalancers"][0]
    creado["nlb_f"] = nlb["LoadBalancerArn"]
    elb.create_listener(LoadBalancerArn=nlb["LoadBalancerArn"], Protocol="TCP", Port=80,
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}])
    elb.register_targets(TargetGroupArn=tg, Targets=[{"Id": ip, "Port": 80}])
    print("esperando al NLB frontal…", flush=True)
    elb.get_waiter("load_balancer_available").wait(LoadBalancerArns=[nlb["LoadBalancerArn"]])
    elb.get_waiter("target_in_service").wait(TargetGroupArn=tg, Targets=[{"Id": ip, "Port": 80}],
                                             WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print(f"cadena: NLB frontal → endpoint {creado['endpoint']} ({ip}) → servicio {creado['svc']} → NLB interno", flush=True)
    return nlb


def crear():
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    subredes = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]},
                                             {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    subred = sorted(subredes, key=lambda s: s["AvailabilityZone"])[0]["SubnetId"]

    sg = ec2.create_security_group(GroupName=NOMBRE, Description="sonda SSE", VpcId=vpc["VpcId"],
                                   TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
    creado["sg"] = sg
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": vpc["CidrBlock"]}]}])

    ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
    inst = ec2.run_instances(ImageId=ami, InstanceType="t4g.nano", MinCount=1, MaxCount=1, SubnetId=subred,
                             SecurityGroupIds=[sg], UserData=USER_DATA,
                             MetadataOptions={"HttpTokens": "required"},
                             TagSpecifications=[{"ResourceType": "instance", "Tags": TAGS + [{"Key": "Name", "Value": NOMBRE}]}])
    iid = inst["Instances"][0]["InstanceId"]
    creado["instancia"] = iid
    print("instancia", iid, flush=True)

    tg = elb.create_target_group(Name=NOMBRE, Protocol="TCP", Port=8080, VpcId=vpc["VpcId"], TargetType="instance",
                                 HealthCheckProtocol="TCP", HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
                                 Tags=TAGS)["TargetGroups"][0]["TargetGroupArn"]
    creado["tg"] = tg
    nlb = elb.create_load_balancer(Name=NOMBRE, Type="network", Scheme="internal", Subnets=[subred],
                                   Tags=TAGS)["LoadBalancers"][0]
    creado["nlb"] = nlb["LoadBalancerArn"]
    elb.create_listener(LoadBalancerArn=nlb["LoadBalancerArn"], Protocol="TCP", Port=80,
                        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}])
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    elb.register_targets(TargetGroupArn=tg, Targets=[{"Id": iid, "Port": 8080}])
    print("esperando al NLB…", flush=True)
    elb.get_waiter("load_balancer_available").wait(LoadBalancerArns=[nlb["LoadBalancerArn"]])

    frente = nlb
    if PRIVATELINK:
        frente = privatelink(vpc, subred, sg, nlb["LoadBalancerArn"])

    link = apigw.create_vpc_link(name=NOMBRE, targetArns=[frente["LoadBalancerArn"]], tags=TAGS_DICT)["id"]
    creado["vpc_link"] = link
    print("esperando al destino sano…", flush=True)
    elb.get_waiter("target_in_service").wait(TargetGroupArn=tg, Targets=[{"Id": iid, "Port": 8080}],
                                             WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    print("esperando al VPC Link (varios minutos)…", flush=True)
    t0 = time.time()
    while True:
        estado = apigw.get_vpc_link(vpcLinkId=link)
        if estado["status"] == "AVAILABLE":
            break
        if estado["status"] == "FAILED":
            raise RuntimeError("VPC Link FAILED: " + estado.get("statusMessage", ""))
        time.sleep(15)
    print(f"VPC Link listo en {time.time() - t0:.0f} s", flush=True)

    api = apigw.create_rest_api(name=NOMBRE, endpointConfiguration={"types": ["REGIONAL"]}, tags=TAGS_DICT)["id"]
    creado["api"] = api
    raiz = apigw.get_resources(restApiId=api)["items"][0]["id"]
    proxy = apigw.create_resource(restApiId=api, parentId=raiz, pathPart="{proxy+}")["id"]
    apigw.put_method(restApiId=api, resourceId=proxy, httpMethod="GET", authorizationType="NONE",
                     requestParameters={"method.request.path.proxy": True})
    apigw.put_integration(restApiId=api, resourceId=proxy, httpMethod="GET", type="HTTP_PROXY",
                          integrationHttpMethod="GET", connectionType="VPC_LINK", connectionId=link,
                          uri=f"http://{frente['DNSName']}/{{proxy}}",
                          requestParameters={"integration.request.path.proxy": "method.request.path.proxy"},
                          responseTransferMode="STREAM", timeoutInMillis=900000)
    integ = apigw.get_integration(restApiId=api, resourceId=proxy, httpMethod="GET")
    print(f"integración: responseTransferMode={integ.get('responseTransferMode')} timeoutInMillis={integ.get('timeoutInMillis')}",
          flush=True)
    apigw.create_deployment(restApiId=api, stageName="p")
    time.sleep(10)
    return f"{api}.execute-api.{REGION}.amazonaws.com"


def abrir(host, ruta, espera_max):
    """Lee el SSE línea a línea y devuelve (marcas, cómo terminó, segundos totales)."""
    t0 = time.time()
    marcas = []
    try:
        c = http.client.HTTPSConnection(host, timeout=espera_max)
        c.request("GET", "/p/" + ruta, headers={"Accept": "text/event-stream"})
        r = c.getresponse()
        if r.status != 200:
            return marcas, f"HTTP {r.status}: {r.read()[:200]!r}", time.time() - t0
        while True:
            linea = r.readline()
            if not linea:
                return marcas, "cerrado", time.time() - t0
            linea = linea.strip()
            if linea:
                marcas.append((round(time.time() - t0, 1), linea.decode()))
                if linea.startswith(b"event: fin"):
                    return marcas, "fin", time.time() - t0
    except Exception as e:
        return marcas, type(e).__name__ + ": " + str(e)[:120], time.time() - t0


def probar(host, rapida):
    # 1. Streaming: con un evento cada 2 s durante 10 s, el primero tiene que llegar enseguida.
    for intento in range(6):  # el despliegue recién hecho tarda en servir
        marcas, como, total = abrir(host, "sse?cada=2&dur=10", 60)
        if marcas:
            break
        print(f"     intento {intento + 1}: «{como}» a {total:.1f} s sin líneas; reintento en 20 s", flush=True)
        time.sleep(20)
    deltas = [m[0] for m in marcas if m[1].startswith("data:")]
    print("     marcas:", [m[0] for m in marcas if not m[1].startswith("data:")], flush=True)
    primera_delta = next((m[0] for m in marcas if m[1] == "event: delta"), None)
    anota("Streaming: los eventos llegan según se emiten", como == "fin" and primera_delta is not None and primera_delta < total - 6,  # llega antes del final
          f"primer delta a {primera_delta} s, fin a {total:.1f} s, {len(deltas)} líneas data")
    if rapida:
        return

    casos = {
        "sin latido, 4 min de silencio": ("sse?latido=0&dur=240", lambda como, t: como == "fin"),
        "sin latido, 20 min": ("sse?latido=0&dur=1200", None),
        "latido cada 240 s, 20 min": ("sse?latido=240&dur=1200", None),
        "latido cada 60 s, 20 min": ("sse?latido=60&dur=1200", None),
    }
    salida = {}

    def correr(nombre, ruta):
        salida[nombre] = abrir(host, ruta, 1500)

    hilos = [threading.Thread(target=correr, args=(n, r)) for n, (r, _) in casos.items()]
    print("lanzadas 4 conexiones largas a la vez; la más larga tarda unos 20 minutos…", flush=True)
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    for nombre, (ruta, _) in casos.items():
        marcas, como, total = salida[nombre]
        ultima = marcas[-1][0] if marcas else None
        print(f"     {nombre}: terminó «{como}» a {total:.0f} s; última línea a {ultima} s, {len(marcas)} líneas", flush=True)

    m, como, t = salida["sin latido, 4 min de silencio"]
    anota("4 min sin datos: el stream aguanta", como == "fin", f"«{como}» a {t:.0f} s")
    m, como, t = salida["sin latido, 20 min"]
    anota("Sin latido: se corta hacia los 5 min de silencio", como != "fin" and 280 <= t <= 330, f"«{como}» a {t:.0f} s")
    m, como, t = salida["latido cada 240 s, 20 min"]
    anota("Latido cada 240 s: dura hasta el tope de 15 min", como != "fin" and 880 <= t <= 930, f"«{como}» a {t:.0f} s")
    m, como, t = salida["latido cada 60 s, 20 min"]
    anota("Latido cada 60 s: dura hasta el tope de 15 min", como != "fin" and 880 <= t <= 930, f"«{como}» a {t:.0f} s")


def intenta(desc, fn):
    try:
        fn()
    except ClientError as e:
        print(f"   aviso al borrar {desc}:", e.response["Error"]["Code"], flush=True)


def borrar():
    print("borrando…", flush=True)
    if "api" in creado:
        intenta("api", lambda: apigw.delete_rest_api(restApiId=creado["api"]))
    if "vpc_link" in creado:
        intenta("vpc link", lambda: apigw.delete_vpc_link(vpcLinkId=creado["vpc_link"]))
        for _ in range(60):  # mientras el VPC Link exista, el NLB sigue en uso
            try:
                apigw.get_vpc_link(vpcLinkId=creado["vpc_link"])
                time.sleep(10)
            except ClientError:
                break
    if "nlb_f" in creado:
        intenta("nlb frontal", lambda: elb.delete_load_balancer(LoadBalancerArn=creado["nlb_f"]))
        intenta("nlb frontal", lambda: elb.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[creado["nlb_f"]]))
    if "tg_f" in creado:
        intenta("tg frontal", lambda: elb.delete_target_group(TargetGroupArn=creado["tg_f"]))
    if "endpoint" in creado:
        intenta("endpoint", lambda: ec2.delete_vpc_endpoints(VpcEndpointIds=[creado["endpoint"]]))
        for _ in range(40):
            eps = ec2.describe_vpc_endpoints(VpcEndpointIds=[creado["endpoint"]])["VpcEndpoints"]
            if not eps or eps[0]["State"] == "deleted":
                break
            time.sleep(10)
    if "svc" in creado:
        intenta("servicio de endpoint", lambda: ec2.delete_vpc_endpoint_service_configurations(ServiceIds=[creado["svc"]]))
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
                if e.response["Error"]["Code"] != "ResourceInUse":
                    print("   aviso al borrar tg:", e.response["Error"]["Code"])
                    break
                time.sleep(15)
    if "instancia" in creado:
        intenta("instancia", lambda: ec2.get_waiter("instance_terminated").wait(InstanceIds=[creado["instancia"]]))
    if "sg" in creado:
        for _ in range(40):  # las interfaces del NLB tardan en soltarse
            try:
                ec2.delete_security_group(GroupId=creado["sg"])
                break
            except ClientError as e:
                if e.response["Error"]["Code"] != "DependencyViolation":
                    print("   aviso al borrar sg:", e.response["Error"]["Code"])
                    break
                time.sleep(15)
        else:
            print("   aviso: el grupo de seguridad", creado["sg"], "sigue en uso; bórralo a mano")


if __name__ == "__main__":
    conservar = "--conservar" in sys.argv
    try:
        host = crear()
        print("API:", host, flush=True)
        probar(host, "--rapida" in sys.argv)
    finally:
        if not conservar:
            borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
