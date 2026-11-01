"""Sonda 1 del nivel 2: IAM de la sesión, DynamoDB y S3, en una cuenta de pruebas.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre las credenciales
de una ejecución (secciones "Credenciales del kernel" y "Cómo cabe en IAM"):

  1. La lanzadora asume el rol de sesión con etiquetas (oid, chat, run_id…) y SourceIdentity;
     una etiqueta no permitida se rechaza.
  2. La política del rol con ${aws:PrincipalTag/...} limita DynamoDB con dynamodb:LeadingKeys
     a RUN#{run_id}, también dentro de TransactWriteItems.
  3. Y limita S3 a conversacion/ de su chat y memoria/ de su usuario; con ListBucket, una clave
     que no existe da 404 (NoSuchKey) y no 403.
  4. PutObject con If-Match: dos escritores a la vez, uno gana y el otro recibe 412 (o 409);
     If-None-Match: * sobre un objeto existente da 412.
  5. Cuánto ocupa la sesión (SessionTokenUtilization) con una política de sesión realista.

Crea todo con el prefijo "sonda-n2-<sufijo>", lo etiqueta con sonda=nivel2 y lo borra al acabar,
aunque algo falle. Coste: céntimos. Uso:

    python sonda_iam_datos.py            # crea, prueba y borra
    python sonda_iam_datos.py --conservar  # no borra (para mirar en la consola)
    SONDA_REGION=eu-north-1 python sonda_iam_datos.py  # otra región (por defecto us-east-1)
"""
import json
import os
import sys
import time
import uuid
import concurrent.futures as cf

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
CLAVES_PERMITIDAS = ["oid", "chat", "run_id", "agente", "version", "mes"]

ses = boto3.Session(region_name=REGION)
sts, iam, ddb, s3 = (ses.client(x) for x in ("sts", "iam", "dynamodb", "s3"))
resultados = []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "")


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def espera_denegado(nombre, fn):
    try:
        fn()
        anota(nombre, False, "se permitió y debía denegarse")
    except ClientError as e:
        c = codigo(e)
        anota(nombre, c in ("AccessDeniedException", "AccessDenied"), c)


def espera_permitido(nombre, fn):
    try:
        r = fn()
        anota(nombre, True)
        return r
    except ClientError as e:
        anota(nombre, False, codigo(e) + ": " + str(e)[:160])


def crear():
    yo = sts.get_caller_identity()
    cuenta, mi_arn = yo["Account"], yo["Arn"]
    print("cuenta", cuenta, "·", mi_arn)
    if ":assumed-role/" in mi_arn:  # el principal de confianza tiene que ser el rol, no la sesión
        rol = mi_arn.split(":assumed-role/")[1].split("/")[0]
        mi_arn = f"arn:aws:iam::{cuenta}:role/{rol}"

    ddb.create_table(TableName=NOMBRE, BillingMode="PAY_PER_REQUEST", Tags=TAGS,
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                           {"AttributeName": "sk", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                {"AttributeName": "sk", "KeyType": "RANGE"}])
    s3.create_bucket(Bucket=NOMBRE, **({} if REGION == "us-east-1" else
                     {"CreateBucketConfiguration": {"LocationConstraint": REGION}}))
    s3.put_bucket_tagging(Bucket=NOMBRE, Tagging={"TagSet": TAGS})

    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": mi_arn},
        "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
        "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": CLAVES_PERMITIDAS}}}]}
    iam.create_role(RoleName=NOMBRE, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)

    tabla = f"arn:aws:dynamodb:{REGION}:{cuenta}:table/{NOMBRE}"
    b = f"arn:aws:s3:::{NOMBRE}"
    politica = {"Version": "2012-10-17", "Statement": [
        {"Sid": "RunPropio", "Effect": "Allow",
         "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem",
                    "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"],
         "Resource": tabla,
         "Condition": {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["RUN#${aws:PrincipalTag/run_id}"]}}},
        {"Sid": "ConversacionYMemoria", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": [f"{b}/espacios/${{aws:PrincipalTag/oid}}/${{aws:PrincipalTag/chat}}/conversacion/*",
                      f"{b}/espacios/${{aws:PrincipalTag/oid}}/memoria/*"]},
        {"Sid": "ListarPropio", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": b,
         "Condition": {"StringLike": {"s3:prefix": [
             "espacios/${aws:PrincipalTag/oid}/${aws:PrincipalTag/chat}/conversacion/*",
             "espacios/${aws:PrincipalTag/oid}/memoria/*"]}}}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="sesion", PolicyDocument=json.dumps(politica))
    ddb.get_waiter("table_exists").wait(TableName=NOMBRE)
    print("esperando 15 s a que IAM propague el rol…")
    time.sleep(15)
    return cuenta


def politica_de_sesion_realista(cuenta):
    """Modelos y skills de una versión de agente, como la pondría la lanzadora."""
    modelos = [f"arn:aws:bedrock:{REGION}:{cuenta}:inference-profile/us.anthropic.claude-modelo-{i}" for i in range(2)]
    modelos += [f"arn:aws:bedrock:us-{r}-{n}::foundation-model/anthropic.claude-modelo-{i}"
                for i in range(2) for r, n in (("east", 1), ("east", 2), ("west", 2))]
    skills = [f"arn:aws:s3:::{NOMBRE}/skills/skill-{i:02d}/v{i}/*" for i in range(6)]
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": modelos},
        # La política de sesión se intersecta con la del rol: para no bloquear conversacion/ y memoria/,
        # permite espacios/* en general y deja que la del rol lo acote con las etiquetas.
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": skills + [f"arn:aws:s3:::{NOMBRE}/espacios/*"]},
        {"Effect": "Allow", "NotAction": ["bedrock:*", "s3:GetObject"], "Resource": "*"}]}


def asumir(cuenta, oid, chat, run_id, extra=None, con_politica=True):
    tags = [{"Key": "oid", "Value": oid}, {"Key": "chat", "Value": chat}, {"Key": "run_id", "Value": run_id},
            {"Key": "agente", "Value": "ventas"}, {"Key": "version", "Value": "4"}, {"Key": "mes", "Value": "2026-11"}]
    if extra:
        tags.append(extra)
    kw = {"Policy": json.dumps(politica_de_sesion_realista(cuenta), separators=(",", ":"))} if con_politica else {}
    r = sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{NOMBRE}", RoleSessionName=run_id[:64],
                        SourceIdentity=oid, Tags=tags, DurationSeconds=3600, **kw)
    c = r["Credentials"]
    s = boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                      aws_session_token=c["SessionToken"], region_name=REGION)
    return s, r


def probar(cuenta):
    oid, otro_oid = str(uuid.uuid4()), str(uuid.uuid4())
    chat, otro_chat = str(uuid.uuid4()), str(uuid.uuid4())
    run, otro_run = str(uuid.uuid4()), str(uuid.uuid4())

    # 1. Asumir con etiquetas y SourceIdentity; tamaño de la sesión
    sesion, r = asumir(cuenta, oid, chat, run)
    anota("AssumeRole con etiquetas y SourceIdentity", r.get("SourceIdentity") == oid)
    pol = json.dumps(politica_de_sesion_realista(cuenta), separators=(",", ":"))
    print(f"     política de sesión: {len(pol)} caracteres · SessionTokenUtilization={r.get('SessionTokenUtilization')} "
          f"· PackedPolicySize={r.get('PackedPolicySize')} · SessionTokenSize={r.get('SessionTokenSize')}")
    espera_denegado("AssumeRole con una etiqueta no permitida",
                    lambda: asumir(cuenta, oid, chat, run, extra={"Key": "admin", "Value": "si"}))

    d, s3s = sesion.client("dynamodb"), sesion.client("s3")
    T = NOMBRE

    # 2. DynamoDB con LeadingKeys
    espera_permitido("DynamoDB: PutItem en RUN# propio",
                     lambda: d.put_item(TableName=T, Item={"pk": {"S": f"RUN#{run}"}, "sk": {"S": "EVT#000001"}}))
    espera_denegado("DynamoDB: PutItem en RUN# de otra ejecución",
                    lambda: d.put_item(TableName=T, Item={"pk": {"S": f"RUN#{otro_run}"}, "sk": {"S": "EVT#000001"}}))
    espera_denegado("DynamoDB: Query de otra ejecución",
                    lambda: d.query(TableName=T, KeyConditionExpression="pk = :p",
                                    ExpressionAttributeValues={":p": {"S": f"RUN#{otro_run}"}}))
    espera_permitido("DynamoDB: transacción estado + fin en RUN# propio", lambda: d.transact_write_items(TransactItems=[
        {"Update": {"TableName": T, "Key": {"pk": {"S": f"RUN#{run}"}, "sk": {"S": "REG"}},
                    "UpdateExpression": "SET estado = :t", "ExpressionAttributeValues": {":t": {"S": "terminada"}}}},
        {"Put": {"TableName": T, "Item": {"pk": {"S": f"RUN#{run}"}, "sk": {"S": "EVT#999999"}},
                 "ConditionExpression": "attribute_not_exists(pk)"}}]))
    espera_denegado("DynamoDB: transacción que toca otra ejecución", lambda: d.transact_write_items(TransactItems=[
        {"Put": {"TableName": T, "Item": {"pk": {"S": f"RUN#{run}"}, "sk": {"S": "EVT#000002"}}}},
        {"Put": {"TableName": T, "Item": {"pk": {"S": f"RUN#{otro_run}"}, "sk": {"S": "EVT#000002"}}}}]))
    espera_denegado("DynamoDB: escribir USAGE# (solo lectura en el diseño; aquí sin permiso)",
                    lambda: d.put_item(TableName=T, Item={"pk": {"S": f"USAGE#{oid}#2026-11"}, "sk": {"S": "X"}}))

    # 3. S3 por prefijo
    conv = f"espacios/{oid}/{chat}/conversacion/session/{chat}/snapshot_latest.json"
    mem = f"espacios/{oid}/memoria/preferencias.md"
    espera_permitido("S3: escribir conversacion/ de su chat", lambda: s3s.put_object(Bucket=T, Key=conv, Body=b"{}"))
    espera_permitido("S3: escribir memoria/ de su usuario", lambda: s3s.put_object(Bucket=T, Key=mem, Body=b"- tono: directo\n"))
    espera_denegado("S3: memoria/ de otro usuario",
                    lambda: s3s.put_object(Bucket=T, Key=f"espacios/{otro_oid}/memoria/preferencias.md", Body=b"x"))
    espera_denegado("S3: conversacion/ de otro chat del mismo usuario",
                    lambda: s3s.get_object(Bucket=T, Key=f"espacios/{oid}/{otro_chat}/conversacion/x.json"))
    try:
        s3s.get_object(Bucket=T, Key=f"espacios/{oid}/memoria/hechos.md")
        anota("S3: clave que no existe en su prefijo da 404", False, "existía")
    except ClientError as e:
        anota("S3: clave que no existe en su prefijo da 404 (no 403)", codigo(e) == "NoSuchKey", codigo(e))

    # 4. Escritura condicional
    etag = s3s.head_object(Bucket=T, Key=mem)["ETag"]
    def escribe(texto):
        try:
            s3s.put_object(Bucket=T, Key=mem, Body=texto.encode(), IfMatch=etag)
            return "ok"
        except ClientError as e:
            return codigo(e)
    with cf.ThreadPoolExecutor(2) as ex:
        res = list(ex.map(escribe, ["- tono: directo\n- idioma: es\n", "- tono: directo\n- formato: tablas\n"]))
    anota("S3 If-Match: dos escritores a la vez, gana uno", sorted(res)[0] in ("PreconditionFailed", "ConditionalRequestConflict")
          and res.count("ok") == 1, str(res))
    try:
        s3s.put_object(Bucket=T, Key=mem, Body=b"x", IfNoneMatch="*")
        anota("S3 If-None-Match: * sobre un objeto existente da 412", False, "se escribió")
    except ClientError as e:
        anota("S3 If-None-Match: * sobre un objeto existente da 412", codigo(e) == "PreconditionFailed", codigo(e))


def borrar():
    print("borrando…")
    for paso in (
        lambda: iam.delete_role_policy(RoleName=NOMBRE, PolicyName="sesion"),
        lambda: iam.delete_role(RoleName=NOMBRE),
        lambda: ddb.delete_table(TableName=NOMBRE),
        lambda: [s3.delete_object(Bucket=NOMBRE, Key=o["Key"])
                 for o in s3.list_objects_v2(Bucket=NOMBRE).get("Contents", [])],
        lambda: s3.delete_bucket(Bucket=NOMBRE),
    ):
        try:
            paso()
        except ClientError as e:
            print("   aviso al borrar:", codigo(e))


if __name__ == "__main__":
    conservar = "--conservar" in sys.argv
    try:
        cuenta = crear()
        probar(cuenta)
    finally:
        if not conservar:
            borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
