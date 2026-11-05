"""Sonda 5 del nivel 2: AgentCore Runtime V2 en modo VPC sin salida, y Code Interpreter en su VPC sin DNS.

Comprueba contra AWS lo que el documento de arquitectura da por hecho sobre el motor
(secciones "Runtime V2 and what it requires of the kernel", "Code Interpreter" y "Runtime V2 (snapshot)"
en "To verify"):

Runtime (un kernel de unas veinte líneas, solo con la librería estándar, en zip desde S3):
  1. Un runtime con platformVersion=V2 en una VPC sin NAT ni puerta de enlace (solo el gateway de S3)
     llega a READY y responde.
  2. Arranque en frío de sesiones nuevas (P50/P95) y llamada en caliente a la misma sesión.
  3. Instantánea: todas las sesiones parten de la misma memoria (un uuid4() hecho al arrancar sale igual
     en todas), con su contador de peticiones a cero.
  4. Sin salida: desde la microVM no se conecta a internet (y qué pasa con el DNS público).
  5. Variables de entorno de más de 2,5 KB: se rechazan al crear.
  6. StopRuntimeSession y volver a invocar la misma sesión; inactividad de 60 s.

Code Interpreter (propio, en modo VPC, en una VPC con el DNS desactivado y sin endpoints):
  7. La sesión arranca (sessionTimeoutSeconds=3600) y ejecuta Python.
  8. Trae `timeout` de coreutils, Python 3 y alguna librería de PDF; genera un PDF.
  9. writeFiles/readFiles con 1, 10 y 50 MB: si caben y cuánto tardan.

Todo con el prefijo "sonda-n2-<sufijo>" (o "sonda_n2_<sufijo>" donde no se admiten guiones) y la etiqueta
sonda=nivel2; se borra al acabar, aunque algo falle. Las interfaces de red que AgentCore deja en las
subredes pueden tardar en soltarse: si la VPC no se puede borrar, se avisa y se borra luego con
--borrar-restos. Coste: unos pocos dólares como mucho. Requiere boto3 >= 1.43 (Python 3.10+). Uso:

    SONDA_REGION=eu-north-1 python sonda_agentcore.py
    SONDA_REGION=eu-north-1 python sonda_agentcore.py --solo-runtime | --solo-ci | --conservar
    SONDA_REGION=eu-north-1 python sonda_agentcore.py --borrar-restos
"""
import io
import json
import os
import statistics
import sys
import time
import uuid
import zipfile

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("SONDA_REGION", "us-east-1")
SUF = uuid.uuid4().hex[:8]
NOMBRE = f"sonda-n2-{SUF}"
NOMBRE_ = f"sonda_n2_{SUF}"
TAGS = [{"Key": "sonda", "Value": "nivel2"}]
TAGS_DICT = {"sonda": "nivel2"}

ses = boto3.Session(region_name=REGION)
sts, iam, ec2, s3, ctl, dp = (ses.client(x) for x in
                              ("sts", "iam", "ec2", "s3", "bedrock-agentcore-control", "bedrock-agentcore"))
creado = {}
resultados = []

KERNEL = r'''
import json, os, socket, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARRANQUE = uuid.uuid4().hex  # a propósito al arrancar: con V2 tiene que salir igual en todas las sesiones
CUENTA = {"n": 0}

def red():
    r = {}
    for nombre, host in (("dns_externo", "example.com"), ("dns_s3", "s3." + os.environ.get("AWS_REGION", "") + ".amazonaws.com")):
        try:
            r[nombre] = socket.gethostbyname(host)
        except Exception as e:
            r[nombre] = "error: " + type(e).__name__
    try:
        socket.create_connection(("93.184.215.14", 443), timeout=3).close()
        r["tcp_externo"] = "conecta"
    except Exception as e:
        r["tcp_externo"] = "error: " + type(e).__name__
    return r

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
        self.responde({"status": "Healthy"})
    def do_POST(self):
        largo = int(self.headers.get("Content-Length", 0))
        cuerpo = json.loads(self.rfile.read(largo) or b"{}")
        CUENTA["n"] += 1
        r = {"arranque": ARRANQUE, "peticion": uuid.uuid4().hex, "n": CUENTA["n"], "pid": os.getpid(),
             "env_bytes": sum(len(k) + len(v) for k, v in os.environ.items())}
        if cuerpo.get("red"):
            r["red"] = red()
        if cuerpo.get("dormir"):
            time.sleep(cuerpo["dormir"])
        self.responde(r)

ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def codigo(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__


def etiqueta(ids):
    ec2.create_tags(Resources=ids, Tags=TAGS + [{"Key": "Name", "Value": NOMBRE}])


def vpc(cidr, dns, con_s3):
    v = ec2.create_vpc(CidrBlock=cidr)["Vpc"]["VpcId"]
    creado.setdefault("vpcs", []).append(v)
    etiqueta([v])
    ec2.get_waiter("vpc_available").wait(VpcIds=[v])
    ec2.modify_vpc_attribute(VpcId=v, EnableDnsSupport={"Value": dns})
    if dns:
        ec2.modify_vpc_attribute(VpcId=v, EnableDnsHostnames={"Value": True})
    zonas = [z["ZoneName"] for z in ec2.describe_availability_zones()["AvailabilityZones"] if z["State"] == "available"][:2]
    base = cidr.split(".")
    subredes = []
    for i, z in enumerate(zonas):
        s = ec2.create_subnet(VpcId=v, AvailabilityZone=z, CidrBlock=f"{base[0]}.{base[1]}.{i}.0/24")["Subnet"]["SubnetId"]
        subredes.append(s)
    etiqueta(subredes)
    sg = ec2.create_security_group(GroupName=f"{NOMBRE}-{cidr.split('.')[1]}", Description="sonda", VpcId=v)["GroupId"]
    etiqueta([sg])
    if con_s3:
        rt = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [v]}])["RouteTables"][0]["RouteTableId"]
        ep = ec2.create_vpc_endpoint(VpcId=v, ServiceName=f"com.amazonaws.{REGION}.s3", VpcEndpointType="Gateway",
                                     RouteTableIds=[rt])["VpcEndpoint"]["VpcEndpointId"]
        etiqueta([ep])
        creado.setdefault("endpoints", []).append(ep)
    return v, subredes, sg


def rol_runtime(cuenta):
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": cuenta}}}]}
    iam.create_role(RoleName=NOMBRE, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
    creado["rol"] = NOMBRE
    politica = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{NOMBRE}/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                       "logs:DescribeLogStreams", "logs:DescribeLogGroups"], "Resource": "*"}]}
    iam.put_role_policy(RoleName=NOMBRE, PolicyName="runtime", PolicyDocument=json.dumps(politica))
    return f"arn:aws:iam::{cuenta}:role/{NOMBRE}"


def espera_estado(obtener, listo=("READY",), fallo=("CREATE_FAILED", "UPDATE_FAILED", "FAILED"), maximo=1200):
    t0 = time.time()
    while time.time() - t0 < maximo:
        r = obtener()
        if r["status"] in listo:
            return time.time() - t0, r
        if r["status"] in fallo:
            raise RuntimeError(f"{r['status']}: {r.get('failureReason', r)}")
        time.sleep(10)
    raise RuntimeError("no llegó a " + "/".join(listo))


def invoca(arn, sesion, carga=None):
    t0 = time.time()
    r = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sesion, contentType="application/json",
                                accept="application/json", payload=json.dumps(carga or {}).encode())
    cuerpo = r["response"].read()
    return time.time() - t0, json.loads(cuerpo)


def sesion_nueva():
    return "sonda-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]  # al menos 33 caracteres


def probar_runtime(cuenta):
    _, subredes, sg = vpc("10.80.0.0/16", True, con_s3=True)
    rol = rol_runtime(cuenta)
    s3.create_bucket(Bucket=NOMBRE, CreateBucketConfiguration={"LocationConstraint": REGION}) if REGION != "us-east-1" \
        else s3.create_bucket(Bucket=NOMBRE)
    creado["bucket"] = NOMBRE
    s3.put_bucket_tagging(Bucket=NOMBRE, Tagging={"TagSet": TAGS})
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("main.py", KERNEL)
    s3.put_object(Bucket=NOMBRE, Key="codigo/kernel.zip", Body=zbuf.getvalue())
    print("esperando 15 s a que IAM propague el rol…", flush=True)
    time.sleep(15)

    base = dict(agentRuntimeArtifact={"codeConfiguration": {
                    "code": {"s3": {"bucket": NOMBRE, "prefix": "codigo/kernel.zip"}},
                    "runtime": "PYTHON_3_12", "entryPoint": ["main.py"]}},
                roleArn=rol, platformVersion="V2",
                networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
                    "subnets": subredes, "securityGroups": [sg]}},  # requireServiceS3Endpoint no se admite al crear
                protocolConfiguration={"serverProtocol": "HTTP"},
                lifecycleConfiguration={"idleRuntimeSessionTimeout": 60}, tags=TAGS_DICT)

    # 5. Variables de entorno de más de 2,5 KB (se espera que lo rechace sin crear nada)
    try:
        r = ctl.create_agent_runtime(agentRuntimeName=NOMBRE_ + "_env", environmentVariables={"GRANDE": "x" * 3000}, **base)
        creado.setdefault("runtimes", []).append(r["agentRuntimeId"])
        anota("Variables de entorno de 3 KB: se rechazan al crear", False, "se aceptó")
    except ClientError as e:
        anota("Variables de entorno de 3 KB: se rechazan al crear",
              codigo(e) == "ValidationException" and "nviron" in str(e), codigo(e) + ": " + str(e)[:200])

    t0 = time.time()
    try:
        r = ctl.create_agent_runtime(agentRuntimeName=NOMBRE_, **base)
    except ClientError as e:
        anota("Runtime V2 en VPC sin salida: se crea", False, codigo(e) + ": " + str(e)[:300])
        return
    rid, arn = r["agentRuntimeId"], r["agentRuntimeArn"]
    creado.setdefault("runtimes", []).append(rid)
    print("runtime", rid, "preparando la instantánea…", flush=True)
    try:
        listo, info = espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=rid))
    except RuntimeError as e:
        anota("Runtime V2 en VPC sin salida: llega a READY", False, str(e)[:400])
        return
    anota("Runtime V2 en VPC sin salida: llega a READY", info.get("platformVersion") in ("V2", None),
          f"en {time.time() - t0:.0f} s, platformVersion={info.get('platformVersion')}")

    # 2 y 3. Arranques en frío de sesiones nuevas
    tiempos, respuestas = [], []
    for i in range(8):
        try:
            t, c = invoca(arn, sesion_nueva(), {"red": i == 0})
        except ClientError as e:
            anota("Invocar una sesión nueva", False, codigo(e) + ": " + str(e)[:300])
            return
        tiempos.append(t)
        respuestas.append(c)
    tiempos_o = sorted(tiempos)
    p50, p95 = statistics.median(tiempos_o), tiempos_o[-1]
    print(f"     arranques en frío (s): {', '.join(f'{t:.2f}' for t in tiempos)}", flush=True)
    anota("Arranque en frío de una sesión nueva (8 sesiones)", True, f"P50 {p50:.2f} s · máx {p95:.2f} s")
    arranques = {c["arranque"] for c in respuestas}
    anota("Instantánea: todas las sesiones parten de la misma memoria",
          len(arranques) == 1 and all(c["n"] == 1 for c in respuestas),
          f"{len(arranques)} valor(es) de uuid4() hecho al arrancar; n={[c['n'] for c in respuestas]}")
    anota("Instantánea: uuid4() por petición sale distinto", len({c['peticion'] for c in respuestas}) == len(respuestas))
    print("     env_bytes del contenedor:", respuestas[0]["env_bytes"], flush=True)

    red = respuestas[0].get("red", {})
    print("     red desde la microVM:", red, flush=True)
    anota("Sin salida: no conecta a internet", red.get("tcp_externo", "").startswith("error"), str(red))

    s = sesion_nueva()
    invoca(arn, s)
    t, c = invoca(arn, s)
    anota("Misma sesión en caliente", c["n"] == 2, f"{t:.2f} s, n={c['n']}")

    # 6. StopRuntimeSession y volver a invocar
    try:
        dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=s)
        try:
            t, c = invoca(arn, s)
            detalle = f"invocación justo después: {t:.2f} s, n={c['n']} ({'microVM nueva' if c['n'] == 1 else 'la misma'})"
        except ClientError as e:
            detalle = "invocación justo después: " + codigo(e)
        anota("StopRuntimeSession", True, detalle)
    except ClientError as e:
        anota("StopRuntimeSession", False, codigo(e) + ": " + str(e)[:200])

    s = sesion_nueva()
    invoca(arn, s)
    print("     esperando 90 s sin invocar (inactividad configurada: 60 s)…", flush=True)
    time.sleep(90)
    t, c = invoca(arn, s)
    anota("Inactividad de 60 s: la sesión se libera", c["n"] == 1, f"tras 90 s, n={c['n']} en {t:.2f} s")


def ci_ejecuta(cid, sid, nombre, args):
    t0 = time.time()
    r = dp.invoke_code_interpreter(codeInterpreterIdentifier=cid, sessionId=sid, name=nombre, arguments=args)
    salida = []
    for ev in r["stream"]:
        if "result" in ev:
            salida.append(ev["result"])
        else:
            salida.append(ev)
    return time.time() - t0, salida


def texto_de(salida):
    partes = []
    for r in salida:
        sc = r.get("structuredContent") if isinstance(r, dict) else None
        if sc:
            partes.append((sc.get("stdout") or "") + (sc.get("stderr") or ""))
        for c in (r.get("content") or []) if isinstance(r, dict) else []:
            if c.get("type") == "text" and not sc:
                partes.append(c.get("text", ""))
    return "".join(partes)


def probar_ci():
    _, subredes, sg = vpc("10.81.0.0/16", False, con_s3=False)
    t0 = time.time()
    try:
        r = ctl.create_code_interpreter(name=NOMBRE_ + "_ci", description="sonda",
                                        networkConfiguration={"networkMode": "VPC", "vpcConfig": {
                                            "subnets": subredes, "securityGroups": [sg]}}, tags=TAGS_DICT)
    except ClientError as e:
        anota("Code Interpreter en VPC sin DNS: se crea", False, codigo(e) + ": " + str(e)[:300])
        return
    cid = r["codeInterpreterId"]
    creado["ci"] = cid
    try:
        espera_estado(lambda: ctl.get_code_interpreter(codeInterpreterId=cid))
    except RuntimeError as e:
        anota("Code Interpreter en VPC sin DNS: llega a READY", False, str(e)[:300])
        return
    anota("Code Interpreter en VPC sin DNS: llega a READY", True, f"en {time.time() - t0:.0f} s")

    t0 = time.time()
    try:
        sid = dp.start_code_interpreter_session(codeInterpreterIdentifier=cid, name="sonda",
                                                sessionTimeoutSeconds=3600)["sessionId"]
    except ClientError as e:
        anota("La sesión arranca con sessionTimeoutSeconds=3600", False, codigo(e) + ": " + str(e)[:300])
        return
    creado["ci_sesion"] = sid
    anota("La sesión arranca con sessionTimeoutSeconds=3600", True, f"en {time.time() - t0:.1f} s")

    codigo_py = r'''
import sys, shutil, importlib, subprocess, socket
print("python", sys.version.split()[0])
print("timeout", shutil.which("timeout"))
for m in ["reportlab", "fpdf", "pypdf", "PyPDF2", "pdfplumber", "fitz", "matplotlib", "pandas", "docx", "openpyxl", "PIL"]:
    try:
        importlib.import_module(m); print("lib", m, "si")
    except Exception:
        print("lib", m, "no")
try:
    socket.gethostbyname("example.com"); print("dns", "resuelve")
except Exception as e:
    print("dns", type(e).__name__)
print("pip", len(subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True).stdout.splitlines()))
'''
    t, salida = ci_ejecuta(cid, sid, "executeCode", {"language": "python", "code": codigo_py})
    txt = texto_de(salida)
    print("     " + txt.replace("\n", "\n     "), flush=True)
    anota("Ejecuta Python", "python 3." in txt, f"{t:.1f} s")
    anota("Trae `timeout` de coreutils", "timeout /" in txt)
    pdf_libs = [m for m in ("reportlab", "fpdf", "pypdf", "PyPDF2", "fitz") if f"lib {m} si" in txt]
    anota("Trae alguna librería de PDF", bool(pdf_libs), ", ".join(pdf_libs))

    if "reportlab" in pdf_libs or "fpdf" in pdf_libs:
        gen = ("import os; os.makedirs('salida', exist_ok=True)\n" + (
            "from reportlab.pdfgen import canvas\nc = canvas.Canvas('salida/informe.pdf'); c.drawString(72, 720, 'Sonda'); c.save()\n"
            if "reportlab" in pdf_libs else
            "from fpdf import FPDF\np = FPDF(); p.add_page(); p.set_font('Helvetica', size=12); p.cell(40, 10, 'Sonda'); p.output('salida/informe.pdf')\n")
            + "print(os.path.getsize('salida/informe.pdf'))")
        _, salida = ci_ejecuta(cid, sid, "executeCode", {"language": "python", "code": gen})
        txt = texto_de(salida).strip()
        anota("Genera un PDF en salida/", txt.isdigit() and int(txt) > 0, f"{txt} bytes")

    for mb in (1, 10, 50, 100):
        datos = os.urandom(mb * 1024 * 1024)
        ruta = f"entrada/f{mb}.bin"
        try:
            tw, salida = ci_ejecuta(cid, sid, "writeFiles", {"content": [{"path": ruta, "blob": datos}]})
            err = [x for x in salida if isinstance(x, dict) and x.get("isError")]
            _, salida = ci_ejecuta(cid, sid, "executeCode", {"language": "python",
                                   "code": f"import os; print(os.path.getsize('{ruta}'))"})
            tam = texto_de(salida).strip()
            anota(f"writeFiles con {mb} MB", not err and tam == str(len(datos)),
                  f"{tw:.1f} s, dentro mide {tam} bytes" + (f" · {err[0]}"[:200] if err else ""))
        except ClientError as e:
            anota(f"writeFiles con {mb} MB", False, codigo(e) + ": " + str(e)[:160])
            break
    for mb in (10, 20, 25, 30, 40, 50):
        ruta = f"salida/l{mb}.bin"
        ci_ejecuta(cid, sid, "executeCode", {"language": "python",
                   "code": f"import os; os.makedirs('salida', exist_ok=True); open('{ruta}','wb').write(os.urandom({mb}*1024*1024))"})
        try:
            tr, leido = ci_ejecuta(cid, sid, "readFiles", {"paths": [ruta]})
            tam = 0
            for x in leido:
                for c in (x.get("content") or []) if isinstance(x, dict) else []:
                    res = c.get("resource") or {}
                    tam = max(tam, len(res.get("blob") or b""), len(res.get("text") or ""), len(c.get("data") or b""))
            anota(f"readFiles con {mb} MB", tam == mb * 1024 * 1024, f"{tr:.1f} s, leído {tam} bytes")
        except Exception as e:  # el cliente corta los eventos de más de 24 MiB (InvalidPayloadLength)
            anota(f"readFiles con {mb} MB (límite de la respuesta)", False, codigo(e) + ": " + str(e)[:120])
            break


def intenta(desc, fn):
    try:
        fn()
    except ClientError as e:
        print(f"   aviso al borrar {desc}:", codigo(e), str(e)[:160], flush=True)


def borrar_vpc(v):
    for _ in range(40):  # las ENI de AgentCore tardan en soltarse
        enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [v]}])["NetworkInterfaces"]
        for e in enis:
            if e["Status"] == "available":
                intenta("eni", lambda e=e: ec2.delete_network_interface(NetworkInterfaceId=e["NetworkInterfaceId"]))
        if not [e for e in enis if e["Status"] != "available"]:
            break
        time.sleep(30)
    for ep in ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [v]}])["VpcEndpoints"]:
        intenta("endpoint", lambda ep=ep: ec2.delete_vpc_endpoints(VpcEndpointIds=[ep["VpcEndpointId"]]))
    for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [v]}])["Subnets"]:
        intenta("subred", lambda s=s: ec2.delete_subnet(SubnetId=s["SubnetId"]))
    for g in ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [v]}])["SecurityGroups"]:
        if g["GroupName"] != "default":
            intenta("sg", lambda g=g: ec2.delete_security_group(GroupId=g["GroupId"]))
    try:
        ec2.delete_vpc(VpcId=v)
    except ClientError as e:
        print(f"   aviso: la VPC {v} no se pudo borrar ({codigo(e)}); repite luego con --borrar-restos", flush=True)


def borrar():
    print("borrando…", flush=True)
    if "ci_sesion" in creado:
        intenta("sesión ci", lambda: dp.stop_code_interpreter_session(codeInterpreterIdentifier=creado["ci"],
                                                                      sessionId=creado["ci_sesion"]))
    if "ci" in creado:
        intenta("ci", lambda: ctl.delete_code_interpreter(codeInterpreterId=creado["ci"]))
    for rid in creado.get("runtimes", []):
        intenta("runtime", lambda rid=rid: ctl.delete_agent_runtime(agentRuntimeId=rid))
    for rid in creado.get("runtimes", []):
        for _ in range(60):
            try:
                ctl.get_agent_runtime(agentRuntimeId=rid)
                time.sleep(10)
            except ClientError:
                break
    if "ci" in creado:
        for _ in range(60):
            try:
                ctl.get_code_interpreter(codeInterpreterId=creado["ci"])
                time.sleep(10)
            except ClientError:
                break
    if "bucket" in creado:
        intenta("objetos", lambda: [s3.delete_object(Bucket=NOMBRE, Key=o["Key"])
                                    for o in s3.list_objects_v2(Bucket=NOMBRE).get("Contents", [])])
        intenta("bucket", lambda: s3.delete_bucket(Bucket=NOMBRE))
    if "rol" in creado:
        intenta("rol", lambda: iam.delete_role_policy(RoleName=NOMBRE, PolicyName="runtime"))
        intenta("rol", lambda: iam.delete_role(RoleName=NOMBRE))
    for v in creado.get("vpcs", []):
        borrar_vpc(v)


def borrar_restos():
    """Borra las VPC de sondas que ya no use ningún runtime ni Code Interpreter (las de una sonda en marcha, no)."""
    en_uso = set()
    for r in ctl.list_agent_runtimes()["agentRuntimes"]:
        red = ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"]).get("networkConfiguration", {})
        en_uso.update(red.get("networkModeConfig", {}).get("subnets", []))
    for c in ctl.list_code_interpreters().get("codeInterpreterSummaries", []):
        red = ctl.get_code_interpreter(codeInterpreterId=c["codeInterpreterId"]).get("networkConfiguration", {})
        en_uso.update(red.get("vpcConfig", {}).get("subnets", []))
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:sonda", "Values": ["nivel2"]}])["Vpcs"]
    for v in vpcs:
        subredes = {s["SubnetId"] for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [v["VpcId"]]}])["Subnets"]}
        if subredes & en_uso:
            print("VPC en uso por AgentCore, no se toca:", v["VpcId"])
            continue
        print("borrando VPC de sondas:", v["VpcId"])
        borrar_vpc(v["VpcId"])


if __name__ == "__main__":
    if "--borrar-restos" in sys.argv:
        borrar_restos()
        sys.exit(0)
    conservar = "--conservar" in sys.argv
    try:
        cuenta = sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, flush=True)
        if "--solo-ci" not in sys.argv:
            probar_runtime(cuenta)
        if "--solo-runtime" not in sys.argv:
            probar_ci()
    finally:
        if not conservar:
            borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
