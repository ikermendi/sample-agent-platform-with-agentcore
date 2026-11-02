"""Borra de la cuenta de pruebas todo lo que crearon las sondas del nivel 2.

Encuentra los recursos por la etiqueta sonda=nivel2 y por el prefijo de nombre de las sondas
("sonda-n2-" y "sonda_n2_"). No toca nada más de la cuenta.

Por defecto solo LISTA lo que borraría. Para borrar de verdad:

    SONDA_REGIONES=eu-north-1,us-east-1 python limpiar_cuenta.py            # lista
    SONDA_REGIONES=eu-north-1,us-east-1 python limpiar_cuenta.py --borrar   # borra

Borra en orden de dependencias (AgentCore y API Gateway antes que balanceadores y VPC; IAM al
final) y reintenta lo que falla por dependencias. Las claves de KMS no se pueden borrar al
momento: se programa su borrado a 7 días (el mínimo) y se desactivan. Si una VPC no se puede
borrar porque AgentCore aún no ha soltado sus interfaces de red, vuelve a ejecutarlo en unos
minutos: el script es repetible.
"""
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

PREFIJOS = ("sonda-n2-", "sonda_n2_")
REGIONES = [r.strip() for r in os.environ.get("SONDA_REGIONES", os.environ.get("SONDA_REGION", "eu-north-1,us-east-1")).split(",") if r.strip()]
BORRAR = "--borrar" in sys.argv
pendiente = []  # (orden, región, descripción, función)


def es_nuestro(nombre):
    return bool(nombre) and any(p in nombre for p in PREFIJOS)


def tiene_tag(tags):
    if isinstance(tags, dict):
        return tags.get("sonda") == "nivel2"
    return any((t.get("Key") or t.get("key")) == "sonda" and (t.get("Value") or t.get("value")) == "nivel2" for t in tags or [])


def plan(orden, region, desc, fn):
    pendiente.append((orden, region, desc, fn))


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def seguro(fn, *a, **k):
    try:
        return fn(*a, **k)
    except ClientError as e:
        print(f"   (no se pudo listar: {codigo(e)})")
        return None


# ---------------------------------------------------------------- inventario por servicio

def agentcore(r, s):
    ctl = s.client("bedrock-agentcore-control")
    for rt in (seguro(ctl.list_agent_runtimes) or {}).get("agentRuntimes", []):
        if es_nuestro(rt["agentRuntimeName"]):
            rid = rt["agentRuntimeId"]
            for ep in (seguro(ctl.list_agent_runtime_endpoints, agentRuntimeId=rid) or {}).get("runtimeEndpoints", []):
                if ep["name"] != "DEFAULT":
                    plan(10, r, f"AgentCore endpoint {ep['name']} de {rt['agentRuntimeName']}",
                         lambda rid=rid, n=ep["name"]: ctl.delete_agent_runtime_endpoint(agentRuntimeId=rid, endpointName=n))
            plan(11, r, f"AgentCore runtime {rt['agentRuntimeName']}", lambda rid=rid: ctl.delete_agent_runtime(agentRuntimeId=rid))
    for ci in (seguro(ctl.list_code_interpreters) or {}).get("codeInterpreterSummaries", []):
        if es_nuestro(ci.get("name")):
            cid = ci["codeInterpreterId"]
            plan(11, r, f"AgentCore Code Interpreter {ci['name']}", lambda cid=cid: ctl.delete_code_interpreter(codeInterpreterId=cid))


def apigateway(r, s):
    ag = s.client("apigateway")
    for api in (seguro(ag.get_rest_apis, limit=500) or {}).get("items", []):
        if es_nuestro(api.get("name")) or tiene_tag(api.get("tags", {})):
            plan(12, r, f"API Gateway {api['name']}", lambda i=api["id"]: ag.delete_rest_api(restApiId=i))
    for vl in (seguro(ag.get_vpc_links, limit=500) or {}).get("items", []):
        if es_nuestro(vl.get("name")) or tiene_tag(vl.get("tags", {})):
            plan(13, r, f"API Gateway VPC Link {vl['name']}", lambda i=vl["id"]: ag.delete_vpc_link(vpcLinkId=i))


def ecs_y_lambda(r, s):
    ecs, lam = s.client("ecs"), s.client("lambda")
    for arn in (seguro(ecs.list_clusters) or {}).get("clusterArns", []):
        if es_nuestro(arn.split("/")[-1]):
            for sv in (seguro(ecs.list_services, cluster=arn) or {}).get("serviceArns", []):
                plan(14, r, f"ECS servicio {sv.split('/')[-1]}",
                     lambda a=arn, sv=sv: (ecs.update_service(cluster=a, service=sv, desiredCount=0),
                                           ecs.delete_service(cluster=a, service=sv, force=True)))
            plan(16, r, f"ECS cluster {arn.split('/')[-1]}", lambda a=arn: ecs.delete_cluster(cluster=a))
    for td in (seguro(ecs.list_task_definitions, status="ACTIVE") or {}).get("taskDefinitionArns", []):
        if es_nuestro(td.split("/")[-1]):
            plan(15, r, f"ECS definición de tarea {td.split('/')[-1]}", lambda t=td: ecs.deregister_task_definition(taskDefinition=t))
    for pg in lam.get_paginator("list_functions").paginate():
        for f in pg["Functions"]:
            if es_nuestro(f["FunctionName"]):
                plan(14, r, f"Lambda {f['FunctionName']}", lambda n=f["FunctionName"]: lam.delete_function(FunctionName=n))
    for m in (seguro(lam.list_event_source_mappings) or {}).get("EventSourceMappings", []):
        if es_nuestro(m.get("FunctionArn", "").split(":")[-1]):
            plan(13, r, f"Lambda suscripción {m['UUID']}", lambda u=m["UUID"]: lam.delete_event_source_mapping(UUID=u))


def balanceadores(r, s):
    elb = s.client("elbv2")
    lbs = (seguro(elb.describe_load_balancers) or {}).get("LoadBalancers", [])
    for lb in lbs:
        if es_nuestro(lb["LoadBalancerName"]):
            plan(20, r, f"Balanceador {lb['LoadBalancerName']}", lambda a=lb["LoadBalancerArn"]: elb.delete_load_balancer(LoadBalancerArn=a))
    for tg in (seguro(elb.describe_target_groups) or {}).get("TargetGroups", []):
        if es_nuestro(tg["TargetGroupName"]):
            plan(22, r, f"Grupo de destino {tg['TargetGroupName']}", lambda a=tg["TargetGroupArn"]: elb.delete_target_group(TargetGroupArn=a))


def ec2_sin_vpc(r, s):
    ec2 = s.client("ec2")
    f = [{"Name": "tag:sonda", "Values": ["nivel2"]}]
    for res in ec2.describe_instances(Filters=f + [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]:
        for i in res["Instances"]:
            plan(18, r, f"EC2 instancia {i['InstanceId']}", lambda i=i["InstanceId"]: ec2.terminate_instances(InstanceIds=[i]))
    for ep in ec2.describe_vpc_endpoints(Filters=f)["VpcEndpoints"]:
        if ep["State"] not in ("deleted", "deleting"):
            plan(19, r, f"VPC endpoint {ep['VpcEndpointId']} ({ep['ServiceName']})",
                 lambda i=ep["VpcEndpointId"]: ec2.delete_vpc_endpoints(VpcEndpointIds=[i]))
    for sc in ec2.describe_vpc_endpoint_service_configurations(Filters=f)["ServiceConfigurations"]:
        def borra_servicio(sid=sc["ServiceId"]):
            conns = ec2.describe_vpc_endpoint_connections(Filters=[{"Name": "service-id", "Values": [sid]}])["VpcEndpointConnections"]
            ids = [c["VpcEndpointId"] for c in conns if c["VpcEndpointState"] not in ("rejected", "deleted")]
            if ids:
                ec2.reject_vpc_endpoint_connections(ServiceId=sid, VpcEndpointIds=ids)
            ec2.delete_vpc_endpoint_service_configurations(ServiceIds=[sid])
        plan(21, r, f"Servicio de endpoint (PrivateLink) {sc['ServiceId']}", borra_servicio)
    for ng in ec2.describe_nat_gateways(Filters=f)["NatGateways"]:
        if ng["State"] not in ("deleted", "deleting"):
            plan(19, r, f"NAT Gateway {ng['NatGatewayId']}", lambda i=ng["NatGatewayId"]: ec2.delete_nat_gateway(NatGatewayId=i))
    for a in ec2.describe_addresses(Filters=f)["Addresses"]:
        plan(25, r, f"IP elástica {a.get('PublicIp')}", lambda i=a["AllocationId"]: ec2.release_address(AllocationId=i))
    for kp in ec2.describe_key_pairs(Filters=f)["KeyPairs"]:
        plan(25, r, f"Par de claves {kp['KeyName']}", lambda n=kp["KeyName"]: ec2.delete_key_pair(KeyName=n))


def borrar_vpc(ec2, vpc):
    """Borra una VPC con todo lo que cuelga de ella. Repetible."""
    fv = [{"Name": "vpc-id", "Values": [vpc]}]
    for eni in ec2.describe_network_interfaces(Filters=fv)["NetworkInterfaces"]:
        att = eni.get("Attachment")
        if eni.get("RequesterManaged"):
            continue  # las de AgentCore, Lambda o endpoints las suelta el servicio
        if att and att.get("DeleteOnTermination") is False and att.get("AttachmentId"):
            try:
                ec2.detach_network_interface(AttachmentId=att["AttachmentId"], Force=True)
                time.sleep(3)
            except ClientError:
                pass
        try:
            ec2.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])
        except ClientError:
            pass
    for ig in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}])["InternetGateways"]:
        ec2.detach_internet_gateway(InternetGatewayId=ig["InternetGatewayId"], VpcId=vpc)
        ec2.delete_internet_gateway(InternetGatewayId=ig["InternetGatewayId"])
    for sn in ec2.describe_subnets(Filters=fv)["Subnets"]:
        ec2.delete_subnet(SubnetId=sn["SubnetId"])
    for rt in ec2.describe_route_tables(Filters=fv)["RouteTables"]:
        if not any(a.get("Main") for a in rt.get("Associations", [])):
            for a in rt.get("Associations", []):
                ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
    sgs = [g for g in ec2.describe_security_groups(Filters=fv)["SecurityGroups"] if g["GroupName"] != "default"]
    for g in sgs:  # quitar referencias cruzadas entre grupos antes de borrarlos
        if g.get("IpPermissions"):
            ec2.revoke_security_group_ingress(GroupId=g["GroupId"], IpPermissions=g["IpPermissions"])
        if g.get("IpPermissionsEgress"):
            ec2.revoke_security_group_egress(GroupId=g["GroupId"], IpPermissions=g["IpPermissionsEgress"])
    for g in sgs:
        ec2.delete_security_group(GroupId=g["GroupId"])
    for acl in ec2.describe_network_acls(Filters=fv)["NetworkAcls"]:
        if not acl["IsDefault"]:
            ec2.delete_network_acl(NetworkAclId=acl["NetworkAclId"])
    ec2.delete_vpc(VpcId=vpc)


def vpcs(r, s):
    ec2 = s.client("ec2")
    for v in ec2.describe_vpcs(Filters=[{"Name": "tag:sonda", "Values": ["nivel2"]}])["Vpcs"]:
        plan(40, r, f"VPC {v['VpcId']} (con subredes, grupos de seguridad e interfaces)", lambda v=v["VpcId"]: borrar_vpc(ec2, v))


def colas_y_horarios(r, s):
    sch, sqs = s.client("scheduler"), s.client("sqs")
    for pg in sch.get_paginator("list_schedules").paginate():
        for h in pg["Schedules"]:
            if es_nuestro(h["Name"]) or es_nuestro(h.get("GroupName")):
                plan(14, r, f"Horario {h.get('GroupName')}/{h['Name']}",
                     lambda n=h["Name"], g=h.get("GroupName", "default"): sch.delete_schedule(Name=n, GroupName=g))
    for pg in sch.get_paginator("list_schedule_groups").paginate():
        for g in pg["ScheduleGroups"]:
            if es_nuestro(g["Name"]):
                plan(15, r, f"Grupo de horarios {g['Name']}", lambda n=g["Name"]: sch.delete_schedule_group(Name=n))
    for url in (seguro(sqs.list_queues, QueueNamePrefix="sonda-n2-") or {}).get("QueueUrls", []):
        plan(16, r, f"Cola SQS {url.split('/')[-1]}", lambda u=url: sqs.delete_queue(QueueUrl=u))


def datos(r, s):
    ddb, efs, ecr = s.client("dynamodb"), s.client("efs"), s.client("ecr")
    for pg in ddb.get_paginator("list_tables").paginate():
        for t in pg["TableNames"]:
            if es_nuestro(t):
                plan(30, r, f"DynamoDB {t}", lambda t=t: ddb.delete_table(TableName=t))
    for fs in (seguro(efs.describe_file_systems) or {}).get("FileSystems", []):
        if es_nuestro(fs.get("Name")) or tiene_tag(fs.get("Tags", [])):
            def borra_efs(i=fs["FileSystemId"]):
                for ap in efs.describe_access_points(FileSystemId=i).get("AccessPoints", []):
                    efs.delete_access_point(AccessPointId=ap["AccessPointId"])
                for mt in efs.describe_mount_targets(FileSystemId=i)["MountTargets"]:
                    efs.delete_mount_target(MountTargetId=mt["MountTargetId"])
                for _ in range(40):
                    if not efs.describe_mount_targets(FileSystemId=i)["MountTargets"]:
                        break
                    time.sleep(5)
                efs.delete_file_system(FileSystemId=i)
            plan(17, r, f"EFS {fs['FileSystemId']} ({fs.get('Name')})", borra_efs)
    for pg in ecr.get_paginator("describe_repositories").paginate():
        for repo in pg["repositories"]:
            if es_nuestro(repo["repositoryName"]):
                plan(30, r, f"ECR {repo['repositoryName']}", lambda n=repo["repositoryName"]: ecr.delete_repository(repositoryName=n, force=True))


def dns(r, s):
    rr, r53, sd = s.client("route53resolver"), s.client("route53"), s.client("servicediscovery")
    for a in (seguro(rr.list_firewall_rule_group_associations) or {}).get("FirewallRuleGroupAssociations", []):
        if es_nuestro(a.get("Name")):
            plan(23, r, f"DNS Firewall asociación {a['Name']}", lambda i=a["Id"]: rr.disassociate_firewall_rule_group(FirewallRuleGroupAssociationId=i))
    for g in (seguro(rr.list_firewall_rule_groups) or {}).get("FirewallRuleGroups", []):
        if es_nuestro(g.get("Name")):
            def borra_grupo(i=g["Id"]):
                for _ in range(40):  # la desasociación tarda
                    try:
                        for regla in rr.list_firewall_rules(FirewallRuleGroupId=i)["FirewallRules"]:
                            rr.delete_firewall_rule(FirewallRuleGroupId=i, FirewallDomainListId=regla["FirewallDomainListId"])
                        rr.delete_firewall_rule_group(FirewallRuleGroupId=i)
                        return
                    except ClientError as e:
                        if codigo(e) not in ("ConflictException",):
                            raise
                        time.sleep(10)
            plan(24, r, f"DNS Firewall grupo {g['Name']}", borra_grupo)
    for d in (seguro(rr.list_firewall_domain_lists) or {}).get("FirewallDomainLists", []):
        if es_nuestro(d.get("Name")):
            plan(25, r, f"DNS Firewall lista {d['Name']}", lambda i=d["Id"]: rr.delete_firewall_domain_list(FirewallDomainListId=i))
    for ns in (seguro(sd.list_namespaces) or {}).get("Namespaces", []):
        if es_nuestro(ns["Name"]):
            def borra_ns(i=ns["Id"]):
                for sv in sd.list_services(Filters=[{"Name": "NAMESPACE_ID", "Values": [i]}])["Services"]:
                    for inst in sd.list_instances(ServiceId=sv["Id"])["Instances"]:
                        sd.deregister_instance(ServiceId=sv["Id"], InstanceId=inst["Id"])
                    time.sleep(5)
                    sd.delete_service(Id=sv["Id"])
                sd.delete_namespace(Id=i)
            plan(26, r, f"Cloud Map {ns['Name']}", borra_ns)
    if r == REGIONES[0]:  # Route 53 es global
        for z in r53.list_hosted_zones()["HostedZones"]:
            if es_nuestro(z["Name"]):
                def borra_zona(zid=z["Id"]):
                    cambios = [{"Action": "DELETE", "ResourceRecordSet": rs}
                               for rs in r53.list_resource_record_sets(HostedZoneId=zid)["ResourceRecordSets"]
                               if rs["Type"] not in ("SOA", "NS")]
                    if cambios:
                        r53.change_resource_record_sets(HostedZoneId=zid, ChangeBatch={"Changes": cambios})
                    r53.delete_hosted_zone(Id=zid)
                plan(27, "global", f"Route 53 zona {z['Name']}", borra_zona)


def registros_y_claves(r, s):
    logs, kms, cw, ssm, bed = s.client("logs"), s.client("kms"), s.client("cloudwatch"), s.client("ssm"), s.client("bedrock")
    for pg in logs.get_paginator("describe_log_groups").paginate():
        for g in pg["logGroups"]:
            if es_nuestro(g["logGroupName"]):
                plan(35, r, f"Logs {g['logGroupName']}", lambda n=g["logGroupName"]: logs.delete_log_group(logGroupName=n))
    alarmas = [a["AlarmName"] for pg in cw.get_paginator("describe_alarms").paginate() for a in pg["MetricAlarms"] if es_nuestro(a["AlarmName"])]
    if alarmas:
        plan(35, r, f"CloudWatch alarmas {alarmas}", lambda a=alarmas: cw.delete_alarms(AlarmNames=a))
    for pg in ssm.get_paginator("describe_parameters").paginate():
        for p in pg["Parameters"]:
            if es_nuestro(p["Name"]):
                plan(35, r, f"SSM {p['Name']}", lambda n=p["Name"]: ssm.delete_parameter(Name=n))
    for pg in kms.get_paginator("list_aliases").paginate():
        for a in pg["Aliases"]:
            if es_nuestro(a["AliasName"]) and a.get("TargetKeyId"):
                def borra_clave(alias=a["AliasName"], k=a["TargetKeyId"]):
                    kms.delete_alias(AliasName=alias)
                    estado = kms.describe_key(KeyId=k)["KeyMetadata"]["KeyState"]
                    if estado not in ("PendingDeletion", "PendingReplicaDeletion"):
                        kms.schedule_key_deletion(KeyId=k, PendingWindowInDays=7)
                plan(36, r, f"KMS {a['AliasName']} (borrado programado a 7 días)", borra_clave)
    # claves sin alias, por etiqueta
    for pg in kms.get_paginator("list_keys").paginate():
        for k in pg["Keys"]:
            try:
                md = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
                if md["KeyManager"] != "CUSTOMER" or md["KeyState"] in ("PendingDeletion", "PendingReplicaDeletion"):
                    continue
                if tiene_tag([{"Key": t["TagKey"], "Value": t["TagValue"]} for t in kms.list_resource_tags(KeyId=k["KeyId"])["Tags"]]):
                    def programa(i=k["KeyId"]):  # puede haberla programado ya la de su alias
                        if kms.describe_key(KeyId=i)["KeyMetadata"]["KeyState"] not in ("PendingDeletion", "PendingReplicaDeletion"):
                            kms.schedule_key_deletion(KeyId=i, PendingWindowInDays=7)
                    plan(37, r, f"KMS {k['KeyId']} por etiqueta (borrado programado a 7 días)", programa)
            except ClientError:
                pass
    cfg = (seguro(bed.get_model_invocation_logging_configuration) or {}).get("loggingConfig")
    if cfg and es_nuestro(str(cfg)):
        plan(34, r, "Bedrock: el registro de invocaciones apunta a recursos de las sondas; se quita",
             lambda: bed.delete_model_invocation_logging_configuration())


def s3_global(s):
    s3 = s.client("s3")
    for b in s3.list_buckets()["Buckets"]:
        if es_nuestro(b["Name"]):
            def vacia_y_borra(n=b["Name"]):
                bk = boto3.resource("s3").Bucket(n)
                bk.object_versions.delete()
                bk.objects.all().delete()
                s3.delete_bucket(Bucket=n)
            plan(31, "global", f"S3 {b['Name']}", vacia_y_borra)


def iam_global(s):
    iam = s.client("iam")
    for pg in iam.get_paginator("list_roles").paginate():
        for rol in pg["Roles"]:
            if es_nuestro(rol["RoleName"]):
                def borra_rol(n=rol["RoleName"]):
                    for p in iam.list_role_policies(RoleName=n)["PolicyNames"]:
                        iam.delete_role_policy(RoleName=n, PolicyName=p)
                    for p in iam.list_attached_role_policies(RoleName=n)["AttachedPolicies"]:
                        iam.detach_role_policy(RoleName=n, PolicyArn=p["PolicyArn"])
                    for ip in iam.list_instance_profiles_for_role(RoleName=n)["InstanceProfiles"]:
                        iam.remove_role_from_instance_profile(InstanceProfileName=ip["InstanceProfileName"], RoleName=n)
                        iam.delete_instance_profile(InstanceProfileName=ip["InstanceProfileName"])
                    iam.delete_role(RoleName=n)
                plan(50, "global", f"IAM rol {rol['RoleName']}", borra_rol)
    for pg in iam.get_paginator("list_policies").paginate(Scope="Local"):
        for p in pg["Policies"]:
            if es_nuestro(p["PolicyName"]):
                def borra_pol(a=p["Arn"]):
                    for v in iam.list_policy_versions(PolicyArn=a)["Versions"]:
                        if not v["IsDefaultVersion"]:
                            iam.delete_policy_version(PolicyArn=a, VersionId=v["VersionId"])
                    ent = iam.list_entities_for_policy(PolicyArn=a)
                    for x in ent["PolicyRoles"]:
                        iam.detach_role_policy(RoleName=x["RoleName"], PolicyArn=a)
                    for x in ent["PolicyUsers"]:
                        iam.detach_user_policy(UserName=x["UserName"], PolicyArn=a)
                    for x in ent["PolicyGroups"]:
                        iam.detach_group_policy(GroupName=x["GroupName"], PolicyArn=a)
                    iam.delete_policy(PolicyArn=a)
                plan(49, "global", f"IAM política {p['PolicyName']}", borra_pol)


# ---------------------------------------------------------------- ejecución

def ejecutar():
    pendiente.sort(key=lambda x: x[0])
    restantes = list(pendiente)
    for ronda in range(1, 7):
        fallidos = []
        for orden, region, desc, fn in restantes:
            try:
                fn()
                print(f"   borrado [{region}] {desc}")
            except ClientError as e:
                c = codigo(e)
                if c in ("ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket", "InvalidVpcID.NotFound",
                         "NotFoundException", "QueueDoesNotExist", "AWS.SimpleQueueService.NonExistentQueue",
                         "RepositoryNotFoundException", "FileSystemNotFound", "LoadBalancerNotFound", "TargetGroupNotFound"):
                    print(f"   ya no existía [{region}] {desc}")
                else:
                    fallidos.append((orden, region, desc, fn, c))
            except Exception as e:  # noqa: BLE001
                fallidos.append((orden, region, desc, fn, type(e).__name__ + ": " + str(e)[:80]))
        if not fallidos:
            return []
        print(f"\nronda {ronda}: {len(fallidos)} pendientes por dependencias; espero 30 s y reintento")
        for f in fallidos:
            print(f"   [{f[1]}] {f[2]}: {f[4]}")
        restantes = [f[:4] for f in fallidos]
        time.sleep(30)
    return restantes


if __name__ == "__main__":
    base = boto3.Session()
    print("cuenta:", base.client("sts", region_name=REGIONES[0]).get_caller_identity()["Account"],
          "· regiones:", ", ".join(REGIONES), "· modo:", "BORRAR" if BORRAR else "solo listar")
    for r in REGIONES:
        s = boto3.Session(region_name=r)
        print(f"\nbuscando en {r}…")
        for f in (agentcore, apigateway, ecs_y_lambda, balanceadores, ec2_sin_vpc, vpcs, colas_y_horarios, datos, dns, registros_y_claves):
            try:
                f(r, s)
            except Exception as e:  # noqa: BLE001  (un servicio que no existe en la región no debe parar el resto)
                print(f"   {f.__name__}: no se pudo listar ({codigo(e)})")
    s0 = boto3.Session(region_name=REGIONES[0])
    s3_global(s0)
    iam_global(s0)

    pendiente.sort(key=lambda x: x[0])
    print(f"\n{len(pendiente)} recursos de las sondas:")
    for orden, region, desc, _ in pendiente:
        print(f"   [{region}] {desc}")
    if not BORRAR:
        print("\nNo se ha borrado nada. Para borrar: --borrar")
        sys.exit(0)
    print("\nborrando…")
    quedan = ejecutar()
    if quedan:
        print(f"\nQuedan {len(quedan)} sin borrar (normalmente VPC con interfaces de AgentCore que aún no se han soltado). Vuelve a ejecutarlo en unos minutos.")
        sys.exit(1)
    print("\nTodo lo de las sondas borrado.")
