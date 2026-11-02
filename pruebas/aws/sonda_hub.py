"""Sonda 13 del nivel 2: el MCP Hub (fila "MCP Hub" de Por verificar), de punta a punta en AWS.

Montaje:
  - VPC de integraciones (sin NAT ni puerta de enlace; endpoints de ECR, Logs, KMS, ECS y gateways de S3 y
    DynamoDB): el hub (Python, `sonda_hub/hub.py`) en ECS Fargate con 2 tareas detrás de un NLB interno
    publicado como servicio de endpoint de PrivateLink; dos MCP de juguete (`hora`, identidad obo, y `docs`,
    identidad de ejecución con `borrar` marcada `confirmar`) y un `/internal/obo` simulado, alcanzados por el
    hub con ECS Service Connect (namespace de Cloud Map).
  - VPC de agentes (sin NAT; endpoints de ECR y Logs; DNS Firewall): un runtime de AgentCore V2 en modo VPC
    con el kernel de prueba (Strands 1.63.0, modelo falso), que llega al hub por un VPC endpoint de interfaz al
    servicio de PrivateLink, por un nombre fijo de una zona privada de Route 53.
  - Token de ejecución: JWT ES256 firmado aquí con kms:Sign (hace de `api`); el hub y el OBO verifican con la
    clave pública (kms:GetPublicKey una vez). Aprobaciones en una tabla DynamoDB.

Comprueba: (1) enrutado por ruta y tools/list filtrado; (2) token válido pasa; sin token, firma mala,
caducado, MCP no permitido y herramienta fuera de allow se deniegan (error de herramienta) y se auditan;
(3) OBO; (4) confirmar (sin approval_id, usada, otra ejecución, argumentos distintos, válida) y bytes
iguales kernel → hub → MCP; (5) reparto entre las dos tareas; (6) latencia añadida por el hub; (7) el nombre
fijo resuelve desde la microVM y el DNS Firewall lo deja pasar (y bloquea otro nombre de la misma zona).

Necesita Docker (arm64) y .venv312. Todo con el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2; se
borra al acabar aunque falle (la clave de KMS queda con borrado programado a 7 días; si queda una VPC,
`python sonda_agentcore.py --borrar-restos`). Unos 40 minutos; unos pocos dólares. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_hub.py
"""
import base64
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError

import sonda_agentcore as base

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "sonda_hub"))
import token_ejecucion as te  # noqa: E402

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = base.REGION, base.NOMBRE, base.NOMBRE_, base.TAGS, base.TAGS_DICT
ses = base.ses
ec2, iam, ctl, sts = base.ec2, base.iam, base.ctl, base.sts
ecr, ecs, elb, r53, r53r, kms, ddb, logs, sd = (ses.client(x) for x in (
    "ecr", "ecs", "elbv2", "route53", "route53resolver", "kms", "dynamodb", "logs", "servicediscovery"))
dp = ses.client("bedrock-agentcore", config=Config(read_timeout=300, retries={"max_attempts": 1}))
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
CIDR_INT, CIDR_AG = "10.83.0.0/16", "10.84.0.0/16"
ZONA = f"{NOMBRE}.interno"
HUB_HOST, OTRO_HOST = f"hub.{ZONA}", f"otro.{ZONA}"
GRUPO_LOGS = f"/{NOMBRE}"
TAGS_KV = [{"key": "sonda", "value": "nivel2"}]


# ------------------------------------------------------------------ red
def endpoints_interfaz(vpc, subredes, cidr, servicios):
    sg = ec2.create_security_group(GroupName=f"{NOMBRE}-ep-{cidr.split('.')[1]}", Description="endpoints", VpcId=vpc)["GroupId"]
    base.etiqueta([sg])
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": cidr}]}])
    for s in servicios:
        ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc, ServiceName=f"com.amazonaws.{REGION}.{s}",
                                     SubnetIds=subredes, SecurityGroupIds=[sg], PrivateDnsEnabled=True)["VpcEndpoint"]
        base.etiqueta([ep["VpcEndpointId"]])


def espera_endpoints(vpcs):
    while True:
        eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": vpcs}])["VpcEndpoints"]
        if all(e["State"].lower() == "available" for e in eps):
            return
        if [e for e in eps if e["State"].lower() == "failed"]:
            raise RuntimeError("endpoint fallido")
        time.sleep(15)


def dns_firewall(vpc):
    perm = r53r.create_firewall_domain_list(CreatorRequestId=NOMBRE + "p", Name=NOMBRE + "-perm", Tags=TAGS)["FirewallDomainList"]["Id"]
    creado["dl_perm"] = perm
    r53r.update_firewall_domains(FirewallDomainListId=perm, Operation="ADD", Domains=["amazonaws.com", "*.amazonaws.com", HUB_HOST])
    todo = r53r.create_firewall_domain_list(CreatorRequestId=NOMBRE + "t", Name=NOMBRE + "-todo", Tags=TAGS)["FirewallDomainList"]["Id"]
    creado["dl_todo"] = todo
    r53r.update_firewall_domains(FirewallDomainListId=todo, Operation="ADD", Domains=["*"])
    g = r53r.create_firewall_rule_group(CreatorRequestId=NOMBRE + "g", Name=NOMBRE, Tags=TAGS)["FirewallRuleGroup"]["Id"]
    creado["rg"] = g
    r53r.create_firewall_rule(CreatorRequestId=NOMBRE + "r1", FirewallRuleGroupId=g, FirewallDomainListId=perm,
                              Priority=1, Action="ALLOW", Name="permitidos")
    r53r.create_firewall_rule(CreatorRequestId=NOMBRE + "r2", FirewallRuleGroupId=g, FirewallDomainListId=todo,
                              Priority=2, Action="BLOCK", BlockResponse="NXDOMAIN", Name="todo")
    a = r53r.associate_firewall_rule_group(CreatorRequestId=NOMBRE + "a", FirewallRuleGroupId=g, VpcId=vpc,
                                           Priority=101, Name=NOMBRE, Tags=TAGS)["FirewallRuleGroupAssociation"]["Id"]
    creado["asoc"] = a


# ------------------------------------------------------------------ imagen, roles, datos
def imagen():
    repo = ecr.create_repository(repositoryName=NOMBRE, tags=TAGS)["repository"]["repositoryUri"]
    creado["repo"] = NOMBRE
    tok = ecr.get_authorization_token()["authorizationData"][0]
    u, c = base64.b64decode(tok["authorizationToken"]).decode().split(":")
    subprocess.run(["docker", "login", "-u", u, "--password-stdin", tok["proxyEndpoint"]], input=c.encode(), check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as d:
        for f in os.listdir(os.path.join(AQUI, "sonda_hub")):
            if f.endswith(".py") or f == "Dockerfile":
                shutil.copy(os.path.join(AQUI, "sonda_hub", f), d)
        shutil.copy(os.path.join(AQUI, "..", "kernel", "fake.py"), d)
        t0 = time.time()
        subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", f"{repo}:v1", d], check=True, capture_output=True)
        subprocess.run(["docker", "push", f"{repo}:v1"], check=True, capture_output=True)
    print(f"imagen {repo}:v1 subida en {time.time() - t0:.0f} s", flush=True)
    return f"{repo}:v1"


def rol(nombre, servicio, politica=None, administradas=()):
    confianza = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": servicio},
                                                         "Action": "sts:AssumeRole"}]}
    arn = iam.create_role(RoleName=nombre, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)["Role"]["Arn"]
    creado.setdefault("roles", []).append(nombre)
    if politica:
        iam.put_role_policy(RoleName=nombre, PolicyName="p", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": politica}))
    for a in administradas:
        iam.attach_role_policy(RoleName=nombre, PolicyArn=a)
    return arn


def roles(cuenta, clave_arn, tabla_arn):
    r = {"runtime": rol(f"{NOMBRE}-rt", "bedrock-agentcore.amazonaws.com", [
            {"Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
             "Resource": f"arn:aws:ecr:{REGION}:{cuenta}:repository/{NOMBRE}"},
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                           "logs:DescribeLogStreams", "logs:DescribeLogGroups"], "Resource": "*"}]),
         "ejec": rol(f"{NOMBRE}-ejec", "ecs-tasks.amazonaws.com",
                     administradas=["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"]),
         "hub": rol(f"{NOMBRE}-hub", "ecs-tasks.amazonaws.com", [
             {"Effect": "Allow", "Action": "kms:GetPublicKey", "Resource": clave_arn},
             {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Resource": tabla_arn}]),
         "obo": rol(f"{NOMBRE}-obo", "ecs-tasks.amazonaws.com", [
             {"Effect": "Allow", "Action": "kms:GetPublicKey", "Resource": clave_arn}])}
    return r


# ------------------------------------------------------------------ ECS
def tarea(familia, uri, r, comando, entorno, rol_tarea=None):
    d = dict(family=familia, networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu="256", memory="512",
             runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
             executionRoleArn=r["ejec"], tags=TAGS_KV,
             containerDefinitions=[{"name": "app", "image": uri, "essential": True, "command": comando,
                                    "portMappings": [{"containerPort": 8080, "name": "http", "appProtocol": "http"}],
                                    "environment": [{"name": k, "value": v} for k, v in {**entorno, "AWS_REGION": REGION}.items()],
                                    "logConfiguration": {"logDriver": "awslogs", "options": {
                                        "awslogs-group": GRUPO_LOGS, "awslogs-region": REGION,
                                        "awslogs-stream-prefix": familia.rsplit("-", 1)[-1]}}}])
    if rol_tarea:
        d["taskRoleArn"] = rol_tarea
    arn = ecs.register_task_definition(**d)["taskDefinition"]["taskDefinitionArn"]
    creado.setdefault("tareas_def", []).append(arn)
    return arn


def servicio(nombre, td, subredes, sg, n=1, sc_servidor=True, tg=None):
    sc = {"enabled": True, "namespace": NOMBRE}
    if sc_servidor:
        sc["services"] = [{"portName": "http", "discoveryName": nombre, "clientAliases": [{"port": 8080, "dnsName": nombre}]}]
    kw = dict(cluster=NOMBRE, serviceName=nombre, taskDefinition=td, desiredCount=n, launchType="FARGATE",
              networkConfiguration={"awsvpcConfiguration": {"subnets": subredes, "securityGroups": [sg], "assignPublicIp": "DISABLED"}},
              serviceConnectConfiguration=sc, tags=TAGS_KV)
    if tg:
        kw["loadBalancers"] = [{"targetGroupArn": tg, "containerName": "app", "containerPort": 8080}]
        kw["healthCheckGracePeriodSeconds"] = 60
    for intento in range(12):  # el namespace del cluster y los roles nuevos tardan en estar
        try:
            ecs.create_service(**kw)
            break
        except ClientError as e:
            if intento == 11:
                raise
            print("   reintento create_service:", codigo(e), str(e)[:150], flush=True)
            time.sleep(10)
    creado.setdefault("servicios", []).append(nombre)


def espera_servicios(nombres, maximo=900):
    t0 = time.time()
    while time.time() - t0 < maximo:
        s = ecs.describe_services(cluster=NOMBRE, services=nombres)["services"]
        if all(x["runningCount"] == x["desiredCount"] and len(x["deployments"]) == 1 for x in s):
            return time.time() - t0
        paradas = ecs.list_tasks(cluster=NOMBRE, desiredStatus="STOPPED")["taskArns"]
        if paradas:
            for t in ecs.describe_tasks(cluster=NOMBRE, tasks=paradas[:5])["tasks"]:
                print("   tarea parada:", t["group"], t.get("stoppedReason"),
                      [c.get("reason") for c in t.get("containers", [])], flush=True)
            if len(paradas) >= 6:
                raise RuntimeError("las tareas se paran: " + str(ecs.describe_tasks(cluster=NOMBRE, tasks=paradas[:1])["tasks"][0].get("stoppedReason")))
        time.sleep(20)
    raise RuntimeError("los servicios no se estabilizan")


def ip_de(servicio_):
    t = ecs.list_tasks(cluster=NOMBRE, serviceName=servicio_)["taskArns"]
    det = ecs.describe_tasks(cluster=NOMBRE, tasks=t)["tasks"]
    return {x["taskArn"].rsplit("/", 1)[-1]: [d["value"] for a in x["attachments"] for d in a["details"]
                                              if d["name"] == "privateIPv4Address"][0] for x in det}


# ------------------------------------------------------------------ logs
def lineas(prefijo, desde, filtro=None):
    out, kw = [], dict(logGroupName=GRUPO_LOGS, logStreamNamePrefix=prefijo, startTime=int(desde * 1000))
    if filtro:
        kw["filterPattern"] = filtro
    for pag in logs.get_paginator("filter_log_events").paginate(**kw):
        for e in pag["events"]:
            try:
                out.append(json.loads(e["message"]))
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------ la prueba
def vpc_con_espera(cidr):
    """La cuota de VPC por región (5) se comparte con otras sondas: espera hasta 40 min a que haya hueco."""
    for _ in range(80):
        try:
            return base.vpc(cidr, True, con_s3=True)
        except ClientError as e:
            if codigo(e) != "VpcLimitExceeded":
                raise
            time.sleep(30)
    raise RuntimeError("sin hueco para otra VPC")


def probar(cuenta):
    vi, si, _ = vpc_con_espera(CIDR_INT)
    va, sa, sg_rt = vpc_con_espera(CIDR_AG)
    rt = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vi]}])["RouteTables"][0]["RouteTableId"]
    base.etiqueta([ec2.create_vpc_endpoint(VpcId=vi, ServiceName=f"com.amazonaws.{REGION}.dynamodb", VpcEndpointType="Gateway",
                                           RouteTableIds=[rt])["VpcEndpoint"]["VpcEndpointId"]])
    endpoints_interfaz(vi, si, CIDR_INT, ["ecr.api", "ecr.dkr", "logs", "kms", "ecs", "ecs-agent", "ecs-telemetry"])
    endpoints_interfaz(va, sa, CIDR_AG, ["ecr.api", "ecr.dkr", "logs"])
    clave = kms.create_key(KeySpec="ECC_NIST_P256", KeyUsage="SIGN_VERIFY", Description=NOMBRE,
                           Tags=[{"TagKey": "sonda", "TagValue": "nivel2"}])["KeyMetadata"]
    creado["kms"] = clave["KeyId"]
    tabla = ddb.create_table(TableName=NOMBRE, BillingMode="PAY_PER_REQUEST", Tags=TAGS,
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                             KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])["TableDescription"]["TableArn"]
    creado["tabla"] = NOMBRE
    logs.create_log_group(logGroupName=GRUPO_LOGS, tags=TAGS_DICT)
    creado["logs"] = GRUPO_LOGS
    logs.put_retention_policy(logGroupName=GRUPO_LOGS, retentionInDays=1)
    uri = imagen()
    r = roles(cuenta, clave["Arn"], tabla)
    print("esperando a los endpoints de interfaz y a IAM…", flush=True)
    espera_endpoints([vi, va])
    time.sleep(10)

    # runtime (tarda ~9 min: se crea ya y se espera al final)
    t_rt = time.time()
    for intento in range(6):
        try:
            rr = ctl.create_agent_runtime(
                agentRuntimeName=NOMBRE_, roleArn=r["runtime"], platformVersion="V2",
                agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
                networkConfiguration={"networkMode": "VPC", "networkModeConfig": {"subnets": sa, "securityGroups": [sg_rt]}},
                protocolConfiguration={"serverProtocol": "HTTP"},
                lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 3600}, tags=TAGS_DICT)
            break
        except ClientError as e:
            if intento == 5 or "role" not in str(e).lower():
                raise
            time.sleep(10)
    rid, rarn = rr["agentRuntimeId"], rr["agentRuntimeArn"]
    creado.setdefault("runtimes", []).append(rid)

    # ECS: cluster con namespace de Service Connect, hub detrás de un NLB interno
    ecs.create_cluster(clusterName=NOMBRE, serviceConnectDefaults={"namespace": NOMBRE}, tags=TAGS_KV)
    creado["cluster"] = NOMBRE
    sg_t = ec2.create_security_group(GroupName=f"{NOMBRE}-tareas", Description="tareas", VpcId=vi)["GroupId"]
    base.etiqueta([sg_t])
    ec2.authorize_security_group_ingress(GroupId=sg_t, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": CIDR_INT}]}])
    nlb = elb.create_load_balancer(Name=f"{NOMBRE}-hub", Subnets=si, Scheme="internal", Type="network", Tags=TAGS)["LoadBalancers"][0]
    creado["nlb"] = nlb["LoadBalancerArn"]
    tg = elb.create_target_group(Name=f"{NOMBRE}-hub", Protocol="TCP", Port=8080, VpcId=vi, TargetType="ip",
                                 HealthCheckProtocol="TCP", HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
                                 Tags=TAGS)["TargetGroups"][0]["TargetGroupArn"]
    creado["tg"] = tg
    elb.modify_target_group_attributes(TargetGroupArn=tg, Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "10"}])
    creado["listener"] = elb.create_listener(LoadBalancerArn=nlb["LoadBalancerArn"], Protocol="TCP", Port=80,
                                             DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}])["Listeners"][0]["ListenerArn"]
    kid = clave["KeyId"]
    catalogo = {"hora": {"url": "http://hora:8080/mcp", "identidad": "obo", "audiencia": "api://mcp-hora", "confirmar": []},
                "docs": {"url": "http://docs:8080/mcp", "identidad": "ejecucion", "confirmar": ["borrar"]}}
    td = {"hora": tarea(f"{NOMBRE}-hora", uri, r, ["python", "mcp_juguete.py"], {"MCP_NOMBRE": "hora"}),
          "docs": tarea(f"{NOMBRE}-docs", uri, r, ["python", "mcp_juguete.py"], {"MCP_NOMBRE": "docs"}),
          "obo": tarea(f"{NOMBRE}-obo", uri, r, ["python", "obo.py"],
                       {"HUB_KID": kid, "OBO_AUDIENCIAS": json.dumps({"api://mcp-hora": "hora"})}, r["obo"]),
          "hub": tarea(f"{NOMBRE}-hub", uri, r, ["python", "hub.py"],
                       {"HUB_KID": kid, "HUB_TABLA": NOMBRE, "HUB_CATALOGO": json.dumps(catalogo),
                        "HUB_OBO_URL": "http://obo:8080/internal/obo"}, r["hub"]),
          "cliente": tarea(f"{NOMBRE}-cliente", uri, r, ["python", "cliente.py"], {})}
    for s in ("hora", "docs", "obo"):
        servicio(s, td[s], si, sg_t)
    t0 = time.time()
    try:
        espera_servicios(["hora", "docs", "obo"])
    except RuntimeError as e:
        anota("MCP y OBO en Fargate sin NAT, con Service Connect", False, str(e)[:300])
        return
    anota("MCP y OBO en Fargate sin NAT, con Service Connect (endpoints ecs, ecs-agent, ecs-telemetry, ECR, Logs, KMS)",
          True, f"estables en {time.time() - t0:.0f} s")
    servicio("hub", td["hub"], si, sg_t, n=2, sc_servidor=False, tg=tg)
    elb.get_waiter("load_balancer_available").wait(LoadBalancerArns=[nlb["LoadBalancerArn"]])
    svc = ec2.create_vpc_endpoint_service_configuration(NetworkLoadBalancerArns=[nlb["LoadBalancerArn"]], AcceptanceRequired=False,
                                                        TagSpecifications=[{"ResourceType": "vpc-endpoint-service", "Tags": TAGS}])["ServiceConfiguration"]
    creado["svc"] = svc["ServiceId"]
    sg_ep = ec2.create_security_group(GroupName=f"{NOMBRE}-hubep", Description="hub endpoint", VpcId=va)["GroupId"]
    base.etiqueta([sg_ep])
    ec2.authorize_security_group_ingress(GroupId=sg_ep, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": CIDR_AG}]}])
    ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=va, ServiceName=svc["ServiceName"], SubnetIds=sa,
                                 SecurityGroupIds=[sg_ep], PrivateDnsEnabled=False)["VpcEndpoint"]
    creado["ep_hub"] = ep["VpcEndpointId"]
    base.etiqueta([ep["VpcEndpointId"]])
    t0 = time.time()
    try:
        espera_servicios(["hub"])
    except RuntimeError as e:
        anota("Hub (2 tareas) estable detrás del NLB", False, str(e)[:300])
        return
    anota("Hub (2 tareas) estable detrás del NLB", True, f"en {time.time() - t0:.0f} s (lee la clave pública por el endpoint de KMS)")
    espera_endpoints([va])
    ep = ec2.describe_vpc_endpoints(VpcEndpointIds=[creado["ep_hub"]])["VpcEndpoints"][0]
    # nombre fijo: zona privada de Route 53 con un alias al endpoint, y otro nombre que el firewall no permite
    zona = r53.create_hosted_zone(Name=ZONA, CallerReference=NOMBRE, VPC={"VPCRegion": REGION, "VPCId": va},
                                  HostedZoneConfig={"PrivateZone": True, "Comment": NOMBRE})["HostedZone"]["Id"]
    creado["zona"] = zona
    dns0 = ep["DnsEntries"][0]
    r53.change_resource_record_sets(HostedZoneId=zona, ChangeBatch={"Changes": [
        {"Action": "CREATE", "ResourceRecordSet": {"Name": HUB_HOST, "Type": "A", "AliasTarget": {
            "HostedZoneId": dns0["HostedZoneId"], "DNSName": dns0["DnsName"], "EvaluateTargetHealth": False}}},
        {"Action": "CREATE", "ResourceRecordSet": {"Name": OTRO_HOST, "Type": "A", "TTL": 60,
                                                   "ResourceRecords": [{"Value": "10.84.0.200"}]}}]})
    dns_firewall(va)
    while r53r.get_firewall_rule_group_association(FirewallRuleGroupAssociationId=creado["asoc"])["FirewallRuleGroupAssociation"]["Status"] != "COMPLETE":
        time.sleep(10)
    try:
        base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=rid))
    except RuntimeError as e:
        anota("Runtime V2 en la VPC de agentes: READY", False, str(e)[:300])
        return
    print(f"runtime READY ({time.time() - t_rt:.0f} s desde su creación)", flush=True)

    # tokens de ejecución (firmados con KMS, como haría api) y aprobaciones (como las dejaría api al aprobar)
    oid, oid2 = uuid.uuid4().hex, uuid.uuid4().hex
    run1, run2, run9 = f"{oid}.R1{uuid.uuid4().hex[:8]}", f"{oid2}.R2{uuid.uuid4().hex[:8]}", f"{oid}.R9{uuid.uuid4().hex[:8]}"
    firma = te.firmador_kms(kms, kid)

    def tok(allow, exp=900, sub=oid, run=run1, f=firma, k=kid):
        return te.firmar({"iss": "api", "aud": "mcp-hub", "sub": sub, "run_id": run, "agente": "agente-sonda", "version": 3,
                          "grupos": ["g-sonda"], "iat": int(time.time()), "exp": int(time.time()) + exp, "allow": allow}, k, f)
    from cryptography.hazmat.primitives.asymmetric import ec
    impostora = te.firmador_local(ec.generate_private_key(ec.SECP256R1()))
    TODO = ["hora/hora_actual", "hora/eco", "hora/quien_soy", "docs/buscar_documentos", "docs/borrar"]
    T = {"valido": tok(TODO), "parcial": tok(["hora/hora_actual", "hora/quien_soy", "docs/buscar_documentos"]),
         "firma_mala": tok(TODO, f=impostora), "caducado": tok(TODO, exp=-5),
         "solo_hora": tok(["hora/hora_actual", "hora/quien_soy"]), "solo_docs": tok(["docs/buscar_documentos"]),
         "otro_usuario": tok(TODO, sub=oid2, run=run2), "ninguno": None}
    ARGS = {"ruta": "informes/ñandú 2026.pdf", "motivo": "limpieza \"anual\"",
            "opciones": {"z": 1, "a": 2.5, "lista": [1, 2.0, "é", None, True], "anidado": {"b": {"c": [1e-7, 12345678901234567890]}}}}
    # lo guardado: otro orden de claves, escapes \u, 2.50, 1.0 y 1E-7 (otra serialización del mismo JSON)
    GUARDADO = ('{"opciones":{"anidado":{"b":{"c":[1E-7,12345678901234567890]}},"lista":[1,2.00,"\\u00e9",null,true],'
                '"a":2.50,"z":1.0},"motivo":"limpieza \\"anual\\"","ruta":"informes/\\u00f1and\\u00fa 2026.pdf"}')
    assert json.loads(GUARDADO) == ARGS
    ap = {"buena": (run1, GUARDADO, "concedida"), "args_distintos": (run1, json.dumps({**ARGS, "ruta": "otra.pdf"}), "concedida"),
          "otra_ejecucion": (run9, GUARDADO, "concedida"), "pendiente": (run1, GUARDADO, "pendiente")}
    A = {}
    for k, (run, args, estado) in ap.items():
        A[k] = f"apr_{uuid.uuid4().hex[:12]}"
        ddb.put_item(TableName=NOMBRE, Item={"pk": {"S": A[k]}, "run_id": {"S": run}, "herramienta": {"S": "docs/borrar"},
                                             "argumentos": {"S": args}, "estado": {"S": estado}, "usada": {"BOOL": False}})
    marca = uuid.uuid4().hex[:6]
    comun = {"hub": f"http://{HUB_HOST}/mcp", "tokens": T, "aprobaciones": A, "args": ARGS, "marca": marca, "n": 60, "n_nuevas": 30,
             "nombres": {"hub": HUB_HOST, "otro_misma_zona": OTRO_HOST, "externo": "example.com",
                         "endpoint_regional": dns0["DnsName"]}}
    sesion = base.sesion_nueva()
    desde = time.time() - 5
    R = {}
    for p in ("dns", "enrutado", "token", "obo", "confirmar", "reparto"):
        t0 = time.time()
        try:
            resp = dp.invoke_agent_runtime(agentRuntimeArn=rarn, runtimeSessionId=sesion, contentType="application/json",
                                           accept="application/json", payload=json.dumps({**comun, "prueba": p}).encode())
            R[p] = json.loads(resp["response"].read())
        except ClientError as e:
            R[p] = {"excepcion": codigo(e) + ": " + str(e)[:300]}
        print(f"--- {p} ({time.time() - t0:.1f} s):", json.dumps(R[p], ensure_ascii=False)[:2500], flush=True)

    # (6) latencia desde la VPC de integraciones: por el hub (NLB) y directo al MCP (IP de la tarea)
    ip_hora = list(ip_de("hora").values())[0]
    carga_c = {"hub_url": f"http://{nlb['DNSName']}/mcp/hora", "directo_url": f"http://{ip_hora}:8080/mcp", "token": T["valido"], "n": 100}
    tc = ecs.run_task(cluster=NOMBRE, taskDefinition=td["cliente"], launchType="FARGATE", count=1,
                      networkConfiguration={"awsvpcConfiguration": {"subnets": si, "securityGroups": [sg_t], "assignPublicIp": "DISABLED"}},
                      overrides={"containerOverrides": [{"name": "app", "environment": [{"name": "CLIENTE_CARGA", "value": json.dumps(carga_c)}]}]})["tasks"][0]
    creado["cliente"] = tc["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(cluster=NOMBRE, tasks=[tc["taskArn"]], WaiterConfig={"Delay": 10, "MaxAttempts": 60})

    print("esperando 20 s a que lleguen los logs…", flush=True)
    time.sleep(20)
    evalua(R, T, A, ARGS, marca, desde, ip_de("hub"), oid, oid2, run1, dns0)


def evalua(R, T, A, ARGS, marca, desde, hub_ips, oid, oid2, run1, dns0):
    hub = lineas("hub/", desde)
    hub = [x for x in hub if x.get("auditoria") == "hub"]
    mcp_docs = [x for x in lineas("docs/", desde) if x.get("auditoria") == "mcp-docs"]
    obo = [x for x in lineas("obo/", desde) if x.get("auditoria") == "obo"]
    cli = [x for x in lineas("cliente/", desde) if "latencia" in x]
    print(f"auditoría: {len(hub)} líneas del hub, {len(mcp_docs)} de mcp-docs, {len(obo)} de obo", flush=True)

    # (7) DNS
    d = R["dns"]
    anota("(7) El nombre fijo de Route 53 resuelve al endpoint desde la microVM, con DNS Firewall",
          isinstance(d.get("hub"), list) and all(ip.startswith("10.84.") for ip in d["hub"]),
          f"{HUB_HOST} → {d.get('hub')}")
    anota("(7) DNS Firewall bloquea otro nombre de la misma zona privada y los externos",
          str(d.get("otro_misma_zona")).startswith("error") and str(d.get("externo")).startswith("error"),
          f"otro={d.get('otro_misma_zona')}, example.com={d.get('externo')}, endpoint regional={d.get('endpoint_regional')}")

    # (1) enrutado
    e = R["enrutado"]
    ag = e.get("agente", {})
    anota("(1) Un MCPClient por MCP, por su ruta: el agente llama a hora y docs",
          ag.get("tu_h", {}).get("status") == "success" and ag.get("tu_d", {}).get("status") == "success",
          f"hora_actual={ag.get('tu_h', {}).get('texto', '')[:40]}, buscar_documentos={ag.get('tu_d', {}).get('texto', '')[:60]}")
    anota("(1) tools/list filtrado a lo permitido (sin eco ni borrar)",
          e.get("lista_hora") == ["hora_actual", "quien_soy"] and e.get("lista_docs") == ["buscar_documentos"],
          f"hora={e.get('lista_hora')}, docs={e.get('lista_docs')}, el modelo vio {e.get('modelo_vio')}")
    filtr = [(x["mcp"], x.get("herramientas_mcp"), x.get("herramientas_filtradas")) for x in hub
             if x.get("metodo") == "tools/list" and x.get("run_id") == run1 and x.get("herramientas_filtradas") is not None]
    anota("(1) Herramienta fuera de allow llamada a mano: error de herramienta",
          all("fuera de allow" in e.get(k, {}).get("texto", "") and e.get(k, {}).get("status") == "error"
              for k in ("eco_fuera_de_allow", "borrar_fuera_de_allow")),
          f"{e.get('eco_fuera_de_allow')} · tools/list en la auditoría (mcp, antes, después): {sorted(set(filtr))}")

    # (2) token
    t = R["token"]
    ab = t.get("abrir", {})
    ok_abrir = all(not ab.get(k, {}).get("abre", True) for k in ("ninguno", "firma_mala", "caducado", "docs_no_permitido")) \
        and ab.get("docs_valido", {}).get("abre")
    anota("(2) Al conectar: sin token, firma mala, caducado y MCP no permitido no abren; válido sí", ok_abrir,
          "; ".join(f"{k}: {v.get('causa') or v.get('herramientas')}" for k, v in ab.items()))
    m = t.get("a_mitad", {})
    esperado = {"ninguno": "sin token", "firma_mala": "firma", "caducado": "caducado", "solo_docs": "MCP no permitido"}
    ok_mitad = m.get("valido", {}).get("status") == "success" and m.get("valido_2", {}).get("status") == "success" and \
        all(m.get(k, {}).get("status") == "error" and v in m.get(k, {}).get("texto", "") for k, v in esperado.items())
    anota("(2) Token cambiado a mitad (mismo MCPClient): cada caso, error de herramienta; el válido vuelve a pasar", ok_mitad,
          "; ".join(f"{k}={v.get('status')}:{v.get('texto', '')[:45]}" for k, v in m.items()))
    motivos = {}
    for x in hub:
        if x.get("decision") == "denegada":
            motivos[x.get("motivo")] = motivos.get(x.get("motivo"), 0) + 1
    anota("(2) Cada denegación queda en la auditoría del hub",
          all(any(v in (k or "") for k in motivos) for v in ("sin token", "firma", "caducado", "MCP no permitido", "fuera de allow")),
          json.dumps(motivos, ensure_ascii=False))

    # (3) OBO
    q = R["obo"].get("quien_soy", [])
    auths = [x.get("authorization") or "" for x in q]
    ok = len(q) == 4 and all(a.startswith(f"Bearer obo-falso.{oid}.api://mcp-hora") for a in auths[:3]) and \
        auths[3].startswith(f"Bearer obo-falso.{oid2}.") and not any(T["valido"] in a for a in auths) and not any(x.get("x_hub") for x in q)
    anota("(3) OBO: hora recibe el token de /internal/obo (no el de ejecución), del usuario de cada token", ok,
          f"{auths[0][:60]}… · otro usuario {auths[3][:50]}…")
    emit = [x for x in obo if x.get("decision") == "emitido"]
    anota("(3) /internal/obo pedido con el token de ejecución (oid y run_id del token), con caché por tarea del hub",
          {x.get("oid") for x in emit} >= {oid, oid2} and len(emit) <= 4,
          f"{len(emit)} emisiones: " + ", ".join(f"{x['oid'][:6]}…/{(x.get('run_id') or '')[-10:]} desde {x.get('pedido_desde')}" for x in emit))

    # (4) confirmar
    c = R["confirmar"]
    casos = {"sin_approval_id": "falta approval_id", "ya_usada": "ya usada", "args_distintos": "argumentos distintos",
             "otra_ejecucion": "otra ejecución", "desconocida": "desconocida", "no_concedida": "no concedida"}
    anota("(4) confirmar: sin approval_id, usada, de otra ejecución, con otros argumentos, desconocida o pendiente → denegada",
          all(c.get(k, {}).get("status") == "error" and v in c.get(k, {}).get("texto", "") for k, v in casos.items()),
          "; ".join(f"{k}: {c.get(k, {}).get('texto', '')[18:70]}" for k in casos))
    buena = c.get("con_aprobacion_valida", {})
    try:
        cuerpo = json.loads(buena.get("texto", "{}"))
    except ValueError:
        cuerpo = {}
    it = ddb.get_item(TableName=NOMBRE, Key={"pk": {"S": A["buena"]}}, ConsistentRead=True)["Item"]
    anota("(4) Con una aprobación válida pasa (guardada con otra serialización: orden, \\u, 2.50, 1E-7) y queda usada",
          buena.get("status") == "success" and it["usada"]["BOOL"],
          f"usada={it['usada']['BOOL']} por la tarea {it.get('usada_por', {}).get('S')}")
    meta = cuerpo.get("meta") or {}
    anota("(4) El MCP recibe los argumentos y el _meta (con approval_id) iguales; identidad de ejecución en X-Hub-*",
          cuerpo.get("argumentos") == ARGS and meta.get("approval_id") == A["buena"] and cuerpo.get("authorization") is None
          and (cuerpo.get("x_hub") or {}).get("x-hub-usuario") == oid,
          f"_meta={sorted(meta)}, x_hub={cuerpo.get('x_hub')}")
    env = c.get("enviado_por_el_kernel") or {}
    h = [x for x in hub if x.get("approval_id") == A["buena"] and x.get("decision") == "permitida"]
    mm = [x for x in mcp_docs if x.get("cuerpo_sha256") == env.get("sha256")]
    anota("(4) Byte a byte: el cuerpo del tools/call sale del kernel, pasa por el hub y llega al MCP idéntico (sha256)",
          bool(env) and len(h) == 1 and h[0]["cuerpo_sha256"] == env["sha256"] and len(mm) == 1 and mm[0]["cuerpo_b64"] == env["b64"],
          f"sha256 {env.get('sha256', '')[:16]}… kernel = hub ({len(h)}) = mcp-docs ({len(mm)})")

    # (5) reparto
    rp = R["reparto"]
    por = {}
    for x in hub:
        if (x.get("marca") or "").startswith(marca) and x.get("metodo") == "tools/call":
            por.setdefault(x["marca"], {}).setdefault(x["tarea"][:8], 0)
            por[x["marca"]][x["tarea"][:8]] += 1
    nuevas = por.get(marca + "-nuevas", {})
    anota("(5) 60 llamadas por un MCPClient y 30 con un cliente nuevo cada una: sin errores", not rp.get("errores") and
          sum(por.get(marca + "-una", {}).values()) == 60 and sum(nuevas.values()) == 30,
          f"errores={rp.get('errores')}; por tarea del hub: {por}")
    anota("(5) Las conexiones nuevas se reparten entre las dos tareas (sin sesión)", len(nuevas) == 2,
          f"una conexión → {len(por.get(marca + '-una', {}))} tarea(s); nuevas → {nuevas}")

    # (6) latencia
    lat = (cli[0] if cli else {}).get("latencia", {})
    anota("(6) Latencia añadida por el hub (P50, tools/call eco, desde la VPC de integraciones)", bool(lat),
          f"hub {lat.get('hub')} · directo {lat.get('directo')} · añadido {lat.get('anadido_p50_ms')} ms · "
          f"desde la microVM por PrivateLink: P50 {rp.get('p50_ms_una_conexion')} ms (P90 {rp.get('p90_ms_una_conexion')}), "
          f"cliente nuevo por llamada {rp.get('p50_ms_cliente_nuevo')} ms; errores del cliente {(cli[0] if cli else {}).get('errores')}")
    ms = [x["ms"] for x in hub if x.get("ms") is not None and x.get("metodo") == "tools/call"]
    if ms:
        print(f"     tiempo dentro del hub (tools/call, incluye el MCP): P50 {statistics.median(ms):.1f} ms", flush=True)


# ------------------------------------------------------------------ borrado
def borrar():
    print("borrando…", flush=True)
    if "cliente" in creado:
        intenta("tarea cliente", lambda: ecs.stop_task(cluster=NOMBRE, task=creado["cliente"]))
    for s in creado.get("servicios", []):
        intenta("servicio", lambda s=s: ecs.update_service(cluster=NOMBRE, service=s, desiredCount=0))
        intenta("servicio", lambda s=s: ecs.delete_service(cluster=NOMBRE, service=s, force=True))
    if creado.get("servicios"):
        intenta("esperar servicios", lambda: ecs.get_waiter("services_inactive").wait(
            cluster=NOMBRE, services=creado["servicios"], WaiterConfig={"Delay": 15, "MaxAttempts": 60}))
    for arn in creado.get("tareas_def", []):
        intenta("task def", lambda arn=arn: ecs.deregister_task_definition(taskDefinition=arn))
    if creado.get("tareas_def"):
        intenta("task defs", lambda: ecs.delete_task_definitions(taskDefinitions=creado["tareas_def"]))
    if "cluster" in creado:
        for _ in range(20):
            try:
                ecs.delete_cluster(cluster=NOMBRE)
                break
            except ClientError as e:
                print("   aviso cluster:", codigo(e), flush=True)
                time.sleep(15)
    for ns in sd.list_namespaces()["Namespaces"]:
        if ns["Name"] == NOMBRE:
            for s in sd.list_services(Filters=[{"Name": "NAMESPACE_ID", "Values": [ns["Id"]]}])["Services"]:
                try:  # ECS puede estar borrando el servicio de Cloud Map a la vez
                    instancias = sd.list_instances(ServiceId=s["Id"]).get("Instances", [])
                except ClientError:
                    instancias = []
                for i in instancias:
                    intenta("instancia cloud map", lambda s=s, i=i: sd.deregister_instance(ServiceId=s["Id"], InstanceId=i["Id"]))
                time.sleep(5)
                intenta("servicio cloud map", lambda s=s: sd.delete_service(Id=s["Id"]))
            intenta("namespace", lambda ns=ns: sd.delete_namespace(Id=ns["Id"]))
    if "zona" in creado:
        z = creado["zona"]
        for rs in r53.list_resource_record_sets(HostedZoneId=z)["ResourceRecordSets"]:
            if rs["Type"] not in ("NS", "SOA"):
                intenta("registro", lambda rs=rs: r53.change_resource_record_sets(HostedZoneId=z, ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rs}]}))
        intenta("zona", lambda: r53.delete_hosted_zone(Id=z))
    if "asoc" in creado:
        intenta("asociación dns firewall", lambda: r53r.disassociate_firewall_rule_group(FirewallRuleGroupAssociationId=creado["asoc"]))
        for _ in range(30):
            try:
                r53r.get_firewall_rule_group_association(FirewallRuleGroupAssociationId=creado["asoc"])
                time.sleep(10)
            except ClientError:
                break
    if "rg" in creado:
        for dl in ("dl_perm", "dl_todo"):
            if dl in creado:
                intenta("regla", lambda dl=dl: r53r.delete_firewall_rule(FirewallRuleGroupId=creado["rg"], FirewallDomainListId=creado[dl]))
        intenta("grupo de reglas", lambda: r53r.delete_firewall_rule_group(FirewallRuleGroupId=creado["rg"]))
    for dl in ("dl_perm", "dl_todo"):
        if dl in creado:
            intenta("lista de dominios", lambda dl=dl: r53r.delete_firewall_domain_list(FirewallDomainListId=creado[dl]))
    if "ep_hub" in creado:
        intenta("endpoint del hub", lambda: ec2.delete_vpc_endpoints(VpcEndpointIds=[creado["ep_hub"]]))
        for _ in range(40):
            e = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-endpoint-id", "Values": [creado["ep_hub"]]}])["VpcEndpoints"]
            if not e or e[0]["State"].lower() == "deleted":
                break
            time.sleep(10)
    if "svc" in creado:
        intenta("servicio de endpoint", lambda: ec2.delete_vpc_endpoint_service_configurations(ServiceIds=[creado["svc"]]))
    if "nlb" in creado:
        intenta("nlb", lambda: elb.delete_load_balancer(LoadBalancerArn=creado["nlb"]))
        intenta("esperar nlb", lambda: elb.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[creado["nlb"]]))
    if "tg" in creado:
        for _ in range(10):
            try:
                elb.delete_target_group(TargetGroupArn=creado["tg"])
                break
            except ClientError:
                time.sleep(10)
    if "repo" in creado:
        intenta("repo ecr", lambda: ecr.delete_repository(repositoryName=NOMBRE, force=True))
    if "tabla" in creado:
        intenta("tabla", lambda: ddb.delete_table(TableName=NOMBRE))
    if "logs" in creado:
        intenta("logs", lambda: logs.delete_log_group(logGroupName=GRUPO_LOGS))
    if "kms" in creado:
        intenta("kms", lambda: kms.schedule_key_deletion(KeyId=creado["kms"], PendingWindowInDays=7))
    for n in creado.get("roles", []):
        for p in iam.list_attached_role_policies(RoleName=n)["AttachedPolicies"]:
            intenta("rol", lambda n=n, p=p: iam.detach_role_policy(RoleName=n, PolicyArn=p["PolicyArn"]))
        for p in iam.list_role_policies(RoleName=n)["PolicyNames"]:
            intenta("rol", lambda n=n, p=p: iam.delete_role_policy(RoleName=n, PolicyName=p))
        intenta("rol", lambda n=n: iam.delete_role(RoleName=n))
    base.borrar()  # runtime y VPC (con sus endpoints)


if __name__ == "__main__":
    try:
        cuenta = sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, "·", NOMBRE, flush=True)
        probar(cuenta)
    except Exception as e:
        import traceback
        traceback.print_exc()
        anota("La sonda llega al final sin excepciones", False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
