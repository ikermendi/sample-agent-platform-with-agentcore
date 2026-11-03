"""Sonda 10 del nivel 2: el kernel lo más parecido al real, en Runtime V2 en la VPC sin salida.

Resuelve contra AWS dos filas de "To verify": "Runtime V2 (snapshot)" con la imagen completa y la parte
de límites y red de "Local code execution".

Imagen (arm64): Python 3.12, `tini` como PID 1 (con -s, por si no lo fuera), el kernel en /opt/kernel (Strands
1.63.0, SDK de AgentCore 1.25.2, boto3; directorio 0700, el usuario del Sandbox no puede usar ese Python) y el
entorno del modelo en /opt/datos (pandas, matplotlib, python-pptx, reportlab, openpyxl, python-docx; sin boto3
ni clientes HTTP ni pip). El kernel arranca con `umask 077`, carga Strands y calienta las librerías de datos
antes de responder sano en /ping (antes de la instantánea), y el código del modelo corre como `sandbox`.

  A. Runtime V2 con la imagen completa: tamaño, tiempo de crear el runtime, carga antes de la instantánea
     (< 120 s), arranque en frío de 20 sesiones nuevas (P50/P95) y en caliente, la misma medida con V1, si
     se vuelve a tirar de ECR al restaurar (CloudTrail de ECR y métricas de los endpoints), y una llamada a
     Bedrock (Haiku 4.5) con las credenciales STS de la carga por el VPC endpoint de bedrock-runtime, y la
     misma después de borrar ese endpoint.
  B. Sandbox: usuario y entorno limpio, memoria, CPU, procesos (fork bomb), tiempo fijo, /proc del kernel,
     ficheros del kernel, el Python del kernel, limpieza con kill(-1) y zombis.
  C. Red del Sandbox: reglas de nftables por uid (IMDS y 127.0.0.1:8080, y luego todo), con iptables como
     alternativa, y un namespace de red vacío (unshare) por comando.

Todo con el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2; se borra al acabar, aunque algo falle.
Necesita Docker (arm64) y boto3 >= 1.43. Unos 45 minutos (CloudTrail tarda en publicar); pocos dólares. Uso:

    SONDA_REGION=eu-north-1 AWS_PROFILE=sonda .venv312/bin/python sonda_kernel_real.py
    ... sonda_kernel_real.py --local    # solo construye la imagen y la prueba en Docker, sin AWS
    SONDA_VPC=vpc-... ...               # usa una VPC existente (si la cuenta llegó al límite de VPC) y solo
                                        # borra lo que crea en ella
"""
import base64
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone

from botocore.config import Config
from botocore.exceptions import ClientError

LOCAL = "--local" in sys.argv

DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini procps nftables iptables \\
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/kernel && /opt/kernel/bin/pip install --no-cache-dir strands-agents==1.63.0 \\
    bedrock-agentcore==1.25.2 boto3 && chmod 700 /opt/kernel
RUN python -m venv /opt/datos && /opt/datos/bin/pip install --no-cache-dir pandas matplotlib python-pptx reportlab \\
    openpyxl python-docx && /opt/datos/bin/pip uninstall -y pip
RUN /opt/datos/bin/python -c "import importlib.util as u, sys; m=[x for x in ('boto3','botocore','requests','httpx','urllib3','aiohttp','pip') if u.find_spec(x)]; sys.exit('sobran: %s' % m if m else 0)"
RUN mkdir /opt/mplcache && MPLCONFIGDIR=/opt/mplcache /opt/datos/bin/python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as p; p.figure()" \\
    && chmod -R a+rX /opt/mplcache
RUN useradd --system --no-create-home --shell /usr/sbin/nologin sandbox && mkdir -p /srv/chat /srv/kernel \\
    && chown sandbox:sandbox /srv/chat && chmod 700 /srv/chat /srv/kernel
COPY main.py red_sb.py hidra.py doc.py /app/
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["/bin/sh", "-c", "umask 077 && exec /opt/kernel/bin/python /app/main.py"]
"""

KERNEL = r'''
import asyncio, glob, json, os, pwd, resource, shutil, socket, subprocess, sys, time, urllib.request, uuid
T0 = time.time()
print("KERNEL_ARRANCA", flush=True)
import boto3
from botocore.config import Config
import strands
from strands import Agent
from strands.models.bedrock import BedrockModel
from importlib.metadata import version
T_STRANDS = round(time.time() - T0, 2)
CALIENTA = ("import pandas, numpy, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot, pptx, reportlab.pdfgen.canvas, "
            "openpyxl, docx; matplotlib.pyplot.figure()")
w = subprocess.run(["/opt/datos/bin/python", "-c", CALIENTA], capture_output=True, timeout=110,
                   env={"PATH": "/usr/bin:/bin", "MPLCONFIGDIR": "/opt/mplcache", "OPENBLAS_NUM_THREADS": "1"})
CALENTADO = {"rc": w.returncode, "err": w.stderr.decode()[-300:], "s": round(time.time() - T0 - T_STRANDS, 2)}
from bedrock_agentcore.runtime import BedrockAgentCoreApp
app = BedrockAgentCoreApp()
CARGA = round(time.time() - T0, 2)
ARRANQUE = uuid.uuid4().hex  # al arrancar a propósito: con V2 sale igual en todas las sesiones
print(f"KERNEL_LISTO carga={CARGA}s arranque={ARRANQUE}", flush=True)

SB = pwd.getpwnam("sandbox")
UID, GID = SB.pw_uid, SB.pw_gid
TIMEOUT, MEM, NPROC, CPU = 10, 1 << 30, 32, 5
ENV_SB = {"PATH": "/opt/datos/bin:/usr/bin:/bin", "HOME": "/srv/chat", "LANG": "C.UTF-8", "TMPDIR": "/tmp",
          "MPLCONFIGDIR": "/tmp/mpl", "OPENBLAS_NUM_THREADS": "1"}
DPY = "/opt/datos/bin/python"
E = {"vm": None}
RES = {}


def vm():
    if E["vm"] is None:
        E["vm"] = uuid.uuid4().hex[:12]
    return E["vm"]


def lee(p, n=300):
    try:
        with open(p, "rb") as f:
            return f.read(n).decode("latin1")
    except Exception as e:
        return "ERR " + type(e).__name__


def procesos(uid=UID):
    vivos, zombis = [], []
    for st in glob.glob("/proc/[0-9]*/status"):
        try:
            d = dict(l.split(":\t", 1) for l in open(st).read().splitlines() if ":\t" in l)
        except Exception:
            continue
        if int(d["Uid"].split()[0]) == uid:
            (zombis if d["State"].startswith("Z") else vivos).append(f"{st.split('/')[2]}:{d['Name']}")
    return vivos, zombis


def todos_zombis():
    z = []
    for st in glob.glob("/proc/[0-9]*/status"):
        try:
            d = dict(l.split(":\t", 1) for l in open(st).read().splitlines() if ":\t" in l)
            if d["State"].startswith("Z"):
                z.append(f"{st.split('/')[2]}:{d['Name']}:ppid{d['PPid']}")
        except Exception:
            pass
    return z


def info():
    st = dict(l.split(":\t", 1) for l in lee("/proc/self/status", 5000).splitlines() if ":\t" in l)
    um = os.umask(0); os.umask(um)
    mem = [l for l in lee("/proc/meminfo", 400).splitlines() if l.startswith(("MemTotal", "MemAvailable"))]
    return {"arranque": ARRANQUE, "vm": vm(), "carga_s": CARGA, "strands_s": T_STRANDS, "calentado": CALENTADO,
            "pid": os.getpid(), "ppid": os.getppid(), "pid1": lee("/proc/1/comm").strip(),
            "pid1_cmd": lee("/proc/1/cmdline", 120).replace("\x00", " ").strip(), "uid": os.getuid(),
            "umask": oct(um), "cap_eff": st.get("CapEff", "").strip(), "kernel_linux": os.uname().release,
            "mem": mem, "cpus": os.cpu_count(), "strands": version("strands-agents"),
            "agentcore": version("bedrock-agentcore"), "nft": shutil.which("nft"), "iptables": shutil.which("iptables"),
            "region": os.environ.get("AWS_REGION"), "ping": app.get_current_ping_status().value,
            "rlimit_nproc_kernel": resource.getrlimit(resource.RLIMIT_NPROC)}


def a_sandbox(red=None, limites=True):
    def pre():
        if limites:
            resource.setrlimit(resource.RLIMIT_AS, (MEM, MEM))
            resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
            resource.setrlimit(resource.RLIMIT_CPU, (CPU, CPU + 1))
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            resource.setrlimit(resource.RLIMIT_FSIZE, (200 << 20, 200 << 20))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # sin volcados de memoria en la carpeta del chat
        if red == "unshare":
            os.unshare(os.CLONE_NEWNET)
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)
    return pre


def limpia():
    """Proceso auxiliar como el usuario del Sandbox (sin su límite de procesos) que hace kill(-1) hasta vaciar."""
    antes = procesos()[0]
    t = time.time()
    # kill(-1) como usuario normal no da ESRCH mientras exista cualquier otro proceso (cuenta también los que no
    # puede matar), así que la condición de parada es que no quede ningún proceso vivo de ese uid en /proc
    codigo = ("import glob,os,time\nyo,uid=os.getpid(),os.getuid()\n"
              "def vivos():\n    n=0\n    for st in glob.glob('/proc/[0-9]*/status'):\n        try: d=dict(l.split(':\\t',1) for l in open(st).read().splitlines() if ':\\t' in l)\n"
              "        except Exception: continue\n        if int(d['Uid'].split()[0])==uid and not d['State'].startswith('Z') and int(st.split('/')[2])!=yo: n+=1\n    return n\n"
              "esrch=0\nfor i in range(500):\n    try: os.kill(-1, 9)\n    except ProcessLookupError: esrch+=1\n"
              "    if not vivos(): print(i+1, 'vueltas; ESRCH', esrch); break\n    time.sleep(0.005)\nelse: print('500 vueltas, quedan', vivos())\n")
    p = subprocess.run([DPY, "-I", "-c", codigo], preexec_fn=a_sandbox(limites=False), env={}, capture_output=True, timeout=30)
    s = round(time.time() - t, 2)
    time.sleep(0.5)
    despues, zombis = procesos()
    time.sleep(2)
    return {"antes": len(antes), "vueltas": p.stdout.decode().strip() or p.stderr.decode()[-200:], "s": s,
            "despues": despues, "zombis_sandbox": zombis, "zombis_todos_2s": todos_zombis()}


async def sandbox(cmd, timeout_modelo=None, red=None):
    t = time.time()
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", cmd, cwd="/srv/chat", env=ENV_SB, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=a_sandbox(red), start_new_session=True)
    pico = {"n": 0}

    async def muestrea():
        while True:
            pico["n"] = max(pico["n"], len(procesos()[0]))
            await asyncio.sleep(0.2)
    m = asyncio.create_task(muestrea())
    r = {"timeout_modelo": timeout_modelo, "timeout_plataforma": TIMEOUT}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)  # ignora el que pida el modelo
        r.update(rc=proc.returncode, out=out.decode(errors="replace")[-3000:], err=err.decode(errors="replace")[-1500:])
    except asyncio.TimeoutError:
        r["cortado"] = True
        r["limpia"] = await asyncio.to_thread(limpia)
        try:
            r["rc"] = await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            r["rc"] = "no terminó"
    m.cancel()
    r["s"] = round(time.time() - t, 3)
    r["pico_procesos"] = pico["n"]
    return r


def nft(variante):
    reglas = {"especifica": f"""
table inet sonda_sb {{
  chain salida {{
    type filter hook output priority 0; policy accept;
    meta skuid {UID} ip daddr 169.254.169.254 counter drop
    meta skuid {UID} ip daddr 127.0.0.1 tcp dport 8080 counter drop
  }}
}}
""", "todo": f"add rule inet sonda_sb salida meta skuid {UID} counter drop\n"}
    r = {}
    p = subprocess.run(["nft", "-f", "-"], input=reglas[variante].encode(), capture_output=True)
    r["nft"] = {"rc": p.returncode, "err": p.stderr.decode()[-600:]}
    if p.returncode != 0:
        if variante == "especifica":
            cmds = [["-A", "OUTPUT", "-m", "owner", "--uid-owner", str(UID), "-d", "169.254.169.254", "-j", "DROP"],
                    ["-A", "OUTPUT", "-m", "owner", "--uid-owner", str(UID), "-d", "127.0.0.1", "-p", "tcp", "--dport", "8080", "-j", "DROP"]]
        else:
            cmds = [["-A", "OUTPUT", "-m", "owner", "--uid-owner", str(UID), "-j", "DROP"]]
        for prog in ("iptables", "iptables-legacy"):
            res = [subprocess.run([prog] + c, capture_output=True) for c in cmds]
            r[prog] = [{"rc": x.returncode, "err": x.stderr.decode()[-400:]} for x in res]
            if all(x.returncode == 0 for x in res):
                break
    r["ruleset"] = subprocess.run(["nft", "list", "ruleset"], capture_output=True).stdout.decode()[-1500:]
    r["modulos"] = [l.split()[0] for l in lee("/proc/modules", 100000).splitlines() if l.startswith(("nf", "ip", "x_t", "xt"))][:40]
    r["proc_net_nf"] = os.path.exists("/proc/net/netfilter")
    return r


def prueba_url(url, datos=None, cab=None, metodo=None):
    t = time.time()
    try:
        req = urllib.request.Request(url, data=datos, headers=cab or {}, method=metodo)
        with urllib.request.urlopen(req, timeout=3) as x:
            return f"{x.status} en {time.time() - t:.2f} s"
    except Exception as e:
        return f"ERR {type(e).__name__} {str(e)[:80]} en {time.time() - t:.2f} s"


def red_kernel():
    reg = os.environ.get("AWS_REGION", "eu-north-1")
    try:
        ip = socket.gethostbyname(f"bedrock-runtime.{reg}.amazonaws.com")
    except Exception as e:
        ip = "ERR " + type(e).__name__
    return {"imds_token": prueba_url("http://169.254.169.254/latest/api/token", b"", {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, "PUT"),
            "local_8080": prueba_url("http://127.0.0.1:8080/ping"), "dns_bedrock": ip}


@app.async_task
async def bedrock(ident, c):
    t = time.time()
    reg = c.get("region") or os.environ.get("AWS_REGION")
    try:
        ip = socket.gethostbyname(f"bedrock-runtime.{reg}.amazonaws.com")
    except Exception as e:
        ip = "ERR " + type(e).__name__
    try:
        cr = c["cred"]
        ses = boto3.Session(aws_access_key_id=cr["AccessKeyId"], aws_secret_access_key=cr["SecretAccessKey"],
                            aws_session_token=cr["SessionToken"], region_name=reg)
        m = BedrockModel(model_id=c["modelo"], boto_session=ses, max_tokens=20,
                         boto_client_config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 1, "mode": "standard"}))
        a = Agent(model=m, callback_handler=None)
        r = await a.invoke_async("Responde solo con la palabra: hola")
        RES[ident] = {"ok": True, "texto": str(r).strip()[:80], "s": round(time.time() - t, 2), "ip_bedrock": ip,
                      "uso": r.metrics.accumulated_usage}
    except Exception as e:
        causa, cadena = e, []
        while causa is not None and len(cadena) < 5:
            cadena.append(f"{type(causa).__name__}: {str(causa)[:200]}")
            causa = causa.__cause__ or causa.__context__
        RES[ident] = {"ok": False, "error": cadena, "s": round(time.time() - t, 2), "ip_bedrock": ip}


@app.entrypoint
async def entrada(carga):
    acc = carga.get("accion", "info")
    if acc == "info":
        return info()
    if acc == "sb":
        return await sandbox(carga["cmd"], carga.get("timeout"), carga.get("red"))
    if acc == "nft":
        return await asyncio.to_thread(nft, carga["variante"])
    if acc == "red_kernel":
        return await asyncio.to_thread(red_kernel)
    if acc == "limpia":
        return await asyncio.to_thread(limpia)
    if acc == "procesos":
        v, z = procesos()
        return {"vivos": v, "zombis": z}
    if acc == "fichero":
        p = "/srv/kernel/secreto.json"
        with open(p, "w") as f:
            f.write(json.dumps({"cred": "FALSO-CRED-" + uuid.uuid4().hex[:8]}))
        return {"ruta": p, "modo": oct(os.stat(p).st_mode & 0o777), "pid": os.getpid()}
    if acc == "bedrock":
        ident = uuid.uuid4().hex
        asyncio.create_task(bedrock(ident, carga))
        await asyncio.sleep(0)
        return {"aceptada": ident, "ping": app.get_current_ping_status().value}
    if acc == "resultado":
        return RES.get(carga["id"]) or {"pendiente": True, "ping": app.get_current_ping_status().value}
    return {"error": "accion desconocida"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
'''

RED_SB = r'''
import json, socket, sys, time, urllib.request
r = {}
def url(n, u, d=None, cab=None, m=None):
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(u, data=d, headers=cab or {}, method=m), timeout=3) as x:
            r[n] = f"{x.status} en {time.time() - t:.2f} s"
    except Exception as e:
        r[n] = f"ERR {type(e).__name__} {str(e)[:70]} en {time.time() - t:.2f} s"
def tcp(n, h, p):
    t = time.time()
    try:
        socket.create_connection((h, p), timeout=3).close()
        r[n] = f"conecta en {time.time() - t:.2f} s"
    except Exception as e:
        r[n] = f"ERR {type(e).__name__} {str(e)[:70]} en {time.time() - t:.2f} s"
def dns(n, h):
    t = time.time()
    try:
        r[n] = socket.gethostbyname(h) + f" en {time.time() - t:.2f} s"
    except Exception as e:
        r[n] = f"ERR {type(e).__name__} {str(e)[:70]} en {time.time() - t:.2f} s"
reg = sys.argv[1] if len(sys.argv) > 1 else "eu-north-1"
url("imds_token", "http://169.254.169.254/latest/api/token", b"", {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, "PUT")
url("local_8080", "http://127.0.0.1:8080/ping")
dns("dns_s3", f"s3.{reg}.amazonaws.com")
tcp("tcp_s3_443", f"s3.{reg}.amazonaws.com", 443)
tcp("tcp_bedrock_443", f"bedrock-runtime.{reg}.amazonaws.com", 443)
print(json.dumps(r))
'''

HIDRA = r'''
import ctypes, os, time
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
ctypes.CDLL(None).prctl(15, b"kworker/0:1H", 0, 0, 0)
for i in range(3):
    if os.fork() == 0:
        break
while True:
    time.sleep(0.01)
    try:
        if os.fork():
            os._exit(0)
    except OSError:
        pass
'''

DOC = r'''
import json, os, time
t = time.time()
import numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pptx import Presentation
from pptx.util import Inches
from reportlab.pdfgen import canvas
imp = time.time() - t
df = pd.DataFrame({"mes": range(1, 13), "ventas": np.random.rand(12) * 100})
df.plot(x="mes", y="ventas")
plt.savefig("grafico.png")
p = Presentation()
s = p.slides.add_slide(p.slide_layouts[5])
s.shapes.add_picture("grafico.png", Inches(1), Inches(1.5), width=Inches(8))
p.save("informe.pptx")
c = canvas.Canvas("informe.pdf")
c.drawString(100, 750, "Informe")
c.drawImage("grafico.png", 100, 400, width=400, height=300)
c.save()
print(json.dumps({"import_s": round(imp, 2), "total_s": round(time.time() - t, 2),
                  "ficheros": {f: os.path.getsize(f) for f in ("grafico.png", "informe.pptx", "informe.pdf")}}))
'''

FICHEROS = {"Dockerfile": DOCKERFILE, "main.py": KERNEL, "red_sb.py": RED_SB, "hidra.py": HIDRA, "doc.py": DOC}


def construir(etiqueta):
    d = tempfile.mkdtemp()
    for n, t in FICHEROS.items():
        open(os.path.join(d, n), "w").write(t)
    t0 = time.time()
    p = subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", etiqueta, d], capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[-3000:])
    tam = int(subprocess.run(["docker", "image", "inspect", etiqueta, "--format", "{{.Size}}"], capture_output=True,
                             text=True).stdout.strip())
    print(f"imagen {etiqueta} construida en {time.time() - t0:.0f} s, {tam / 1e6:.0f} MB sin comprimir", flush=True)
    return tam


# ---------------------------------------------------------------- pruebas comunes (local o AWS)
def pruebas_sandbox(llama, region):
    """B: Sandbox. `llama(carga)` invoca la misma sesión y devuelve el dict de respuesta."""
    print("\n== B. Sandbox en la microVM", flush=True)
    i = llama({"accion": "info"})
    print("     info:", json.dumps(i, ensure_ascii=False), flush=True)
    kpid = i["pid"]
    res = {"info": i}

    def sb(nombre, cmd, **kw):
        r = llama(dict({"accion": "sb", "cmd": cmd}, **kw))
        print(f"     [{nombre}] {json.dumps(r, ensure_ascii=False)[:1500]}", flush=True)
        res[nombre] = r
        return r

    sb("usuario_entorno", "id; echo ---; env | sort; echo ---; umask")
    sb("doc_frio", "python /app/doc.py")
    sb("doc_caliente", "python /app/doc.py")
    sb("memoria_2g", "python -c \"b = bytearray(2 * 1024**3); print('reservado')\"")
    sb("memoria_pandas_800m", "python -c \"import numpy as np; a = np.ones((800, 1024, 1024), dtype=np.uint8); print('ok', a.nbytes)\"")
    sb("cpu", "python -c \"while True: pass\"")
    sb("fork_python", "python -c \"import os,time\nn=0\nfor i in range(100):\n    try:\n        pid=os.fork()\n    except OSError as e:\n"
                      "        print('corta en', n, e); break\n    if pid==0:\n        time.sleep(2); os._exit(0)\n    n+=1\nprint('hijos', n)\"")
    sb("fork_bomb", "python -c \"import os\nwhile True:\n    try: os.fork()\n    except OSError: pass\"")
    res["tras_bomba"] = llama({"accion": "procesos"})
    print("     tras la bomba:", res["tras_bomba"], flush=True)
    sb("sleep_60", "sleep 60; echo no-cortado", timeout=99999)
    llama({"accion": "fichero"})
    sb("proc_kernel", f"cat /proc/{kpid}/environ; echo; head -c 16 /proc/{kpid}/mem; echo; cat /proc/1/environ; echo; "
                      f"cat /srv/kernel/secreto.json; echo; ls /srv/kernel; echo; /opt/kernel/bin/python -c 'import boto3'; "
                      f"python -c 'import boto3'; python -c 'import urllib3'; pip --version; which curl wget nc dig aws")
    sb("lanza_hidra", "python /app/hidra.py > /dev/null 2>&1 < /dev/null; echo lanzada")
    time.sleep(3)
    res["hidra_viva"] = llama({"accion": "procesos"})
    print("     hidra viva:", res["hidra_viva"], flush=True)
    res["limpia"] = llama({"accion": "limpia"})
    print("     limpieza kill(-1):", json.dumps(res["limpia"], ensure_ascii=False), flush=True)
    return res


def pruebas_red(llama, llama_otra, region):
    """C: red del Sandbox. `llama` y `llama_otra` son dos sesiones (microVM) distintas."""
    print("\n== C. Red del Sandbox", flush=True)
    res = {}
    cmd = f"python /app/red_sb.py {region}"
    paso = lambda n, c: res.__setitem__(n, llama(c)) or print(f"     [{n}] {json.dumps(res[n], ensure_ascii=False)[:1800]}", flush=True)
    paso("base_sandbox", {"accion": "sb", "cmd": cmd})
    paso("base_kernel", {"accion": "red_kernel"})
    paso("nft_especifica", {"accion": "nft", "variante": "especifica"})
    paso("esp_sandbox", {"accion": "sb", "cmd": cmd})
    paso("esp_kernel", {"accion": "red_kernel"})
    paso("nft_todo", {"accion": "nft", "variante": "todo"})
    paso("todo_sandbox", {"accion": "sb", "cmd": cmd})
    paso("todo_kernel", {"accion": "red_kernel"})
    paso("todo_doc", {"accion": "sb", "cmd": "python /app/doc.py"})
    # Otra microVM, sin reglas: namespace de red vacío por comando
    res["unshare_sandbox"] = llama_otra({"accion": "sb", "cmd": cmd + "; cut -d: -f1 /proc/net/dev | tail -n +3 | tr -d ' ' | tr '\\n' ' '", "red": "unshare"})
    print(f"     [unshare_sandbox] {json.dumps(res['unshare_sandbox'], ensure_ascii=False)[:1800]}", flush=True)
    res["unshare_kernel"] = llama_otra({"accion": "red_kernel"})
    print(f"     [unshare_kernel] {res['unshare_kernel']}", flush=True)
    res["unshare_doc"] = llama_otra({"accion": "sb", "cmd": "python /app/doc.py", "red": "unshare"})
    print(f"     [unshare_doc] {json.dumps(res['unshare_doc'], ensure_ascii=False)[:600]}", flush=True)
    t_sin = [llama_otra({"accion": "sb", "cmd": "true"})["s"] for _ in range(8)]
    t_con = [llama_otra({"accion": "sb", "cmd": "true", "red": "unshare"})["s"] for _ in range(8)]
    res["coste_unshare"] = {"sin": statistics.median(t_sin), "con": statistics.median(t_con)}
    print("     comando vacío (mediana, s, incluye la invocación):", res["coste_unshare"], flush=True)
    return res


def local():
    tam = construir("sonda-kernel-real:local")
    nombre = "sonda-kernel-real-local"
    subprocess.run(["docker", "rm", "-f", nombre], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", nombre, "--privileged", "-p", "18080:8080", "sonda-kernel-real:local"],
                   check=True, capture_output=True)
    try:
        t0 = time.time()
        while True:
            try:
                urllib.request.urlopen("http://127.0.0.1:18080/ping", timeout=2).read()
                break
            except Exception:
                if time.time() - t0 > 180:
                    raise
                time.sleep(1)
        print(f"/ping sano a los {time.time() - t0:.1f} s", flush=True)

        def llama(c):
            req = urllib.request.Request("http://127.0.0.1:18080/invocations", data=json.dumps(c).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=120).read())
        pruebas_sandbox(llama, "eu-north-1")
        pruebas_red(llama, llama, "eu-north-1")
        print(subprocess.run(["docker", "logs", nombre], capture_output=True, text=True).stdout[-1500:])
    finally:
        subprocess.run(["docker", "rm", "-f", nombre], capture_output=True)
    print("tamaño sin comprimir:", tam)


if LOCAL:
    local()
    sys.exit(0)

# ---------------------------------------------------------------- AWS
import sonda_agentcore as base  # noqa: E402
import sonda_runtime_imagen as ri  # noqa: E402

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = base.REGION, base.NOMBRE, base.NOMBRE_, base.TAGS, base.TAGS_DICT
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
ses, ctl, ec2, iam, ecr, r53r = base.ses, base.ctl, base.ec2, base.iam, ri.ecr, ri.r53r
logs, ct, cw, bedrock_ctl, sts = (ses.client(x) for x in ("logs", "cloudtrail", "cloudwatch", "bedrock", "sts"))
dp = ses.client("bedrock-agentcore", config=Config(retries={"max_attempts": 1, "mode": "standard"}, read_timeout=180,
                                                     max_pool_connections=10))
MODELO = os.environ.get("SONDA_MODELO", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
CIDR = "10.83.0.0/16"
ROL_SESION = NOMBRE + "-sesion"
FASES = {}
SESIONES = []


def marca(fase):
    FASES[fase] = datetime.now(timezone.utc)
    print(f"--- {fase}: {FASES[fase].isoformat(timespec='seconds')}", flush=True)


def endpoints(vpc, subredes):
    sg = ec2.create_security_group(GroupName=f"{NOMBRE}-ep", Description="endpoints", VpcId=vpc)["GroupId"]
    base.etiqueta([sg])
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": CIDR}]}])
    creado.setdefault("sgs_ajenos", []).append(sg)
    ids = {}
    for servicio in ("ecr.api", "ecr.dkr", "logs", "bedrock-runtime"):
        ep = ec2.create_vpc_endpoint(VpcEndpointType="Interface", VpcId=vpc, ServiceName=f"com.amazonaws.{REGION}.{servicio}",
                                     SubnetIds=subredes, SecurityGroupIds=[sg], PrivateDnsEnabled=True)["VpcEndpoint"]
        base.etiqueta([ep["VpcEndpointId"]])
        ids[servicio] = ep["VpcEndpointId"]
    creado["ep_ids"] = ids
    print("esperando a los endpoints de interfaz…", flush=True)
    while True:
        eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["VpcEndpoints"]
        if all(e["State"] == "available" for e in eps):
            return ids
        time.sleep(15)


def imagen():
    repo = ecr.create_repository(repositoryName=NOMBRE, tags=TAGS)["repository"]["repositoryUri"]
    creado["repo"] = NOMBRE
    tok = ecr.get_authorization_token()["authorizationData"][0]
    usuario, clave = base64.b64decode(tok["authorizationToken"]).decode().split(":")
    subprocess.run(["docker", "login", "-u", usuario, "--password-stdin", tok["proxyEndpoint"]],
                   input=clave.encode(), check=True, capture_output=True)
    tam = construir(f"{repo}:v1")
    t0 = time.time()
    p = subprocess.run(["docker", "push", f"{repo}:v1"], capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[-1500:])
    comp = tam_ecr()
    print(f"subida en {time.time() - t0:.0f} s; {comp / 1e6:.0f} MB comprimida en ECR", flush=True)
    return f"{repo}:v1", tam, comp


def tam_ecr():
    """Suma de las capas comprimidas de la imagen arm64 (describe_images da 0 para el índice que crea buildx)."""
    tipos = ["application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
             "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"]
    m = json.loads(ecr.batch_get_image(repositoryName=NOMBRE, imageIds=[{"imageTag": "v1"}],
                                       acceptedMediaTypes=tipos)["images"][0]["imageManifest"])
    if "manifests" in m:
        hijo = [x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "arm64"][0]["digest"]
        m = json.loads(ecr.batch_get_image(repositoryName=NOMBRE, imageIds=[{"imageDigest": hijo}],
                                           acceptedMediaTypes=tipos)["images"][0]["imageManifest"])
    return sum(x["size"] for x in m["layers"])


def rol_sesion(cuenta):
    yo = sts.get_caller_identity()["Arn"]
    if ":assumed-role/" in yo:
        yo = f"arn:aws:iam::{cuenta}:role/" + yo.split(":assumed-role/")[1].split("/")[0]
    p = bedrock_ctl.get_inference_profile(inferenceProfileIdentifier=MODELO)
    recursos = [p["inferenceProfileArn"]] + [m["modelArn"] for m in p["models"]]
    iam.create_role(RoleName=ROL_SESION, Tags=TAGS, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": yo}, "Action": "sts:AssumeRole"}]}))
    creado["rol_sesion"] = ROL_SESION
    iam.put_role_policy(RoleName=ROL_SESION, PolicyName="modelo", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": recursos}]}))


def credenciales(cuenta):
    t0 = time.time()
    while True:
        try:
            return sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{ROL_SESION}", RoleSessionName="sonda-kernel-real",
                                   DurationSeconds=3600)["Credentials"]
        except ClientError as e:
            if codigo(e) != "AccessDenied" or time.time() - t0 > 120:
                raise
            time.sleep(5)


def llama(arn, sesion, carga):
    t0 = time.time()
    r = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sesion, contentType="application/json",
                                accept="application/json", payload=json.dumps(carga, default=str).encode())
    c = json.loads(r["response"].read())
    c["_s"] = round(time.time() - t0, 2)
    return c


def nueva():
    s = base.sesion_nueva()
    SESIONES.append(s)
    return s


def crear_runtime(nombre, uri, rol_arn, subredes, sg, version):
    t0 = time.time()
    r = ctl.create_agent_runtime(
        agentRuntimeName=nombre, roleArn=rol_arn, platformVersion=version,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {"subnets": subredes, "securityGroups": [sg]}},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 120, "maxLifetime": 3600}, tags=TAGS_DICT)
    creado.setdefault("runtimes", []).append(r["agentRuntimeId"])
    print(f"runtime {version} {r['agentRuntimeId']} preparándose…", flush=True)
    _, info = base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"]), maximo=1800)
    s = time.time() - t0
    print(f"READY en {s:.0f} s (platformVersion={info.get('platformVersion')})", flush=True)
    return r["agentRuntimeId"], r["agentRuntimeArn"], s


def arranques(arn, version, n=20):
    frio, ids, errores, ultimo = [], set(), [], {}
    for _ in range(n):
        try:
            c = llama(arn, nueva(), {"accion": "info"})
            frio.append(c["_s"])
            ids.add(c["arranque"])
            ultimo = c
        except ClientError as e:
            print("     error:", codigo(e), str(e)[:200], flush=True)
            errores.append(codigo(e))
            if len(errores) >= 5 and not frio:  # p. ej. V1 en la VPC sin salida: no arranca ninguna
                return {"p50": 0, "p95": 0, "max": 0, "min": 0, "n": 0, "caliente_p50": 0, "arranques_distintos": 0,
                        "info": {"carga_s": "?"}, "errores": errores}
    s = nueva()
    llama(arn, s, {"accion": "info"})
    caliente = [llama(arn, s, {"accion": "info"})["_s"] for _ in range(10)]
    o = sorted(frio)
    p95 = o[min(len(o) - 1, int(round(0.95 * len(o))) - 1)]
    print(f"     {version} frío (s): {o}\n     {version} caliente (s): {sorted(caliente)}", flush=True)
    return {"p50": statistics.median(o), "p95": p95, "max": o[-1], "min": o[0], "n": len(o),
            "caliente_p50": statistics.median(caliente), "arranques_distintos": len(ids), "info": ultimo, "errores": errores}


def prueba_bedrock(arn, cuenta, nombre):
    s = nueva()
    cred = credenciales(cuenta)
    a = llama(arn, s, {"accion": "bedrock", "modelo": MODELO, "region": REGION, "cred": cred})
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(3)
        r = llama(arn, s, {"accion": "resultado", "id": a["aceptada"]})
        if not r.get("pendiente"):
            break
    print(f"     [{nombre}] aceptada en {a['_s']} s (ping={a['ping']}); resultado: {json.dumps(r, ensure_ascii=False)}", flush=True)
    return a, r


def cloudtrail_ecr(desde):
    eventos = []
    kw = dict(LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": "ecr.amazonaws.com"}],
              StartTime=desde, EndTime=datetime.now(timezone.utc))
    while True:
        r = ct.lookup_events(**kw)
        for e in r["Events"]:
            d = json.loads(e["CloudTrailEvent"])
            if NOMBRE in e["CloudTrailEvent"] or d["eventName"] == "GetAuthorizationToken":
                eventos.append(d)
        if "NextToken" not in r:
            return eventos
        kw["NextToken"] = r["NextToken"]
        time.sleep(0.6)


def fase_de(t):
    orden = sorted(FASES.items(), key=lambda x: x[1])
    actual = "antes"
    for n, ti in orden:
        if t >= ti:
            actual = n
    return actual


def informe_ecr():
    print("\n== ¿Tira de ECR al restaurar? (CloudTrail de ECR, puede tardar hasta 15 min)", flush=True)
    desde = FASES["crear_v2"]
    objetivo = FASES.get("crear_v1")
    t0 = time.time()
    while True:
        ev = cloudtrail_ecr(desde)
        nuestros = [e for e in ev if NOMBRE in json.dumps(e) and e["eventName"] != "PutImage"]
        tiene_v1 = objetivo and any(datetime.fromisoformat(e["eventTime"].replace("Z", "+00:00")) >= objetivo for e in nuestros)
        if (tiene_v1 and time.time() - t0 > 240) or time.time() - t0 > 1200:
            break
        time.sleep(60)
    tabla = {}
    for e in ev:
        t = datetime.fromisoformat(e["eventTime"].replace("Z", "+00:00"))
        clave = (fase_de(t), e["eventName"], e.get("userIdentity", {}).get("arn", e.get("userIdentity", {}).get("invokedBy", "?"))[-60:],
                 e.get("vpcEndpointId", "-"))
        tabla[clave] = tabla.get(clave, 0) + 1
    for k, v in sorted(tabla.items()):
        print("    ", v, k, flush=True)
    return tabla


def metricas_endpoints():
    print("\n== Métricas de los endpoints de ECR (BytesProcessed por minuto)", flush=True)
    r = {}
    fin = datetime.now(timezone.utc)
    for servicio in ("ecr.api", "ecr.dkr", "bedrock-runtime"):
        epid = creado.get("ep_ids", {}).get(servicio)
        if not epid:
            continue
        ms = cw.list_metrics(Namespace="AWS/PrivateLinkEndpoints", MetricName="BytesProcessed",
                             Dimensions=[{"Name": "VPC Endpoint Id", "Value": epid}])["Metrics"]
        if not ms:
            r[servicio] = "sin métricas"
            continue
        puntos = []
        for m in ms:
            st = cw.get_metric_statistics(Namespace="AWS/PrivateLinkEndpoints", MetricName="BytesProcessed",
                                          Dimensions=m["Dimensions"], StartTime=FASES["crear_v2"], EndTime=fin,
                                          Period=60, Statistics=["Sum"])["Datapoints"]
            puntos += st
        porfase = {}
        for p in puntos:
            f = fase_de(p["Timestamp"])
            porfase[f] = porfase.get(f, 0) + p["Sum"]
        r[servicio] = {k: int(v) for k, v in porfase.items()}
        print("    ", servicio, r[servicio], flush=True)
    return r


def logs_arranques(rid):
    g = f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT"
    try:
        ev = logs.filter_log_events(logGroupName=g, filterPattern='"KERNEL_LISTO"', limit=200)["events"]
        return len(ev), sorted({e["message"].strip()[-60:] for e in ev})[:3]
    except ClientError as e:
        return codigo(e), []


def probar(cuenta):
    global CIDR
    if os.environ.get("SONDA_VPC"):  # una VPC ya existente (p. ej. resto de otra sonda, si se llegó al límite de VPC)
        vpc = os.environ["SONDA_VPC"]
        CIDR = ec2.describe_vpcs(VpcIds=[vpc])["Vpcs"][0]["CidrBlock"]
        subredes = [x["SubnetId"] for x in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]][:2]
        sg = ec2.create_security_group(GroupName=f"{NOMBRE}-rt", Description="sonda", VpcId=vpc)["GroupId"]
        base.etiqueta([sg])
        creado["sgs_ajenos"] = [sg]
        rt = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["RouteTables"][0]["RouteTableId"]
        gw = ec2.create_vpc_endpoint(VpcId=vpc, ServiceName=f"com.amazonaws.{REGION}.s3", VpcEndpointType="Gateway",
                                     RouteTableIds=[rt])["VpcEndpoint"]["VpcEndpointId"]
        base.etiqueta([gw])
        creado["eps_ajenos"] = [gw]
        print("usando la VPC existente", vpc, CIDR, subredes, flush=True)
    else:
        vpc, subredes, sg = base.vpc(CIDR, True, con_s3=True)
    endpoints(vpc, subredes)
    ri.dns_firewall(vpc)
    uri, tam, comp = imagen()
    rol_arn = ri.rol(cuenta)
    rol_sesion(cuenta)
    time.sleep(15)
    R = {"imagen_mb": (round(tam / 1e6), round(comp / 1e6))}

    # ---------------- A. V2
    marca("crear_v2")
    rid2, arn2, s2 = crear_runtime(NOMBRE_, uri, rol_arn, subredes, sg, "V2")
    marca("frio_v2")
    a2 = arranques(arn2, "V2")
    i = a2["info"]
    anota("A. Imagen completa en V2 por los endpoints de ECR: READY", True,
          f"imagen {R['imagen_mb'][0]} MB sin comprimir / {R['imagen_mb'][1]} MB en ECR; READY en {s2:.0f} s")
    anota("A. Carga antes de la instantánea < 120 s", i["carga_s"] < 120,
          f"{i['carga_s']} s (Strands {i['strands_s']} s, librerías de datos {i['calentado']['s']} s, rc {i['calentado']['rc']})")
    anota("A. V2: arranque en frío de 20 sesiones nuevas", True,
          f"P50 {a2['p50']:.2f} s · P95 {a2['p95']:.2f} s · mín {a2['min']:.2f} · máx {a2['max']:.2f} · caliente P50 "
          f"{a2['caliente_p50']:.2f} s · arranques distintos {a2['arranques_distintos']}")
    print("     info:", json.dumps(i, ensure_ascii=False), flush=True)

    # ---------------- B y C
    marca("pruebas")
    sb_ses, red_ses, otra = nueva(), nueva(), nueva()
    R["B"] = pruebas_sandbox(lambda c: llama(arn2, sb_ses, c), REGION)
    R["C"] = pruebas_red(lambda c: llama(arn2, red_ses, c), lambda c: llama(arn2, otra, c), REGION)

    # ---------------- Bedrock por el endpoint
    marca("bedrock")
    _, rbed = prueba_bedrock(arn2, cuenta, "con endpoint")
    anota("A. Bedrock (Haiku 4.5) con las credenciales de la carga por el VPC endpoint", rbed.get("ok") is True,
          f"{rbed.get('s')} s, IP {rbed.get('ip_bedrock')}, {rbed.get('texto') or rbed.get('error')}")

    # ---------------- V1
    time.sleep(60)
    marca("crear_v1")
    rid1, arn1, s1 = crear_runtime(NOMBRE_ + "_v1", uri, rol_arn, subredes, sg, "V1")
    marca("frio_v1")
    a1 = arranques(arn1, "V1")
    anota("A. V1 (comparación): arranque en frío de 20 sesiones nuevas", a1["n"] > 0,
          f"READY en {s1:.0f} s · P50 {a1['p50']:.2f} s · P95 {a1['p95']:.2f} s · máx {a1['max']:.2f} · caliente P50 "
          f"{a1['caliente_p50']:.2f} s · arranques distintos {a1['arranques_distintos']} · carga {a1['info']['carga_s']} s · "
          f"errores {a1['errores']}")
    R["logs_v2"], R["logs_v1"] = logs_arranques(rid2), logs_arranques(rid1)
    print("     KERNEL_LISTO en los logs: V2", R["logs_v2"], "· V1", R["logs_v1"], flush=True)

    # ---------------- Bedrock sin endpoint
    marca("sin_endpoint")
    ec2.delete_vpc_endpoints(VpcEndpointIds=[creado["ep_ids"]["bedrock-runtime"]])
    while ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-endpoint-id", "Values": [creado["ep_ids"]["bedrock-runtime"]]},
                                              {"Name": "vpc-endpoint-state", "Values": ["available", "deleting", "pending"]}])["VpcEndpoints"]:
        time.sleep(10)
    time.sleep(30)
    _, rsin = prueba_bedrock(arn2, cuenta, "sin endpoint")
    anota("A. Bedrock sin el endpoint: falla", rsin.get("ok") is False,
          f"{rsin.get('s')} s, IP {rsin.get('ip_bedrock')}, {rsin.get('error')}")
    marca("fin")
    print("parando las sesiones…", flush=True)
    for s in SESIONES:
        for arn in (arn2, arn1):
            try:
                dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=s)
            except Exception:
                pass
    json.dump(R, open(os.path.join(os.environ.get("SONDA_SALIDA", "/tmp"), f"{NOMBRE}.json"), "w"), default=str, indent=1)


def borrar():
    if "rol_sesion" in creado:
        intenta("rol sesión", lambda: iam.delete_role_policy(RoleName=ROL_SESION, PolicyName="modelo"))
        intenta("rol sesión", lambda: iam.delete_role(RoleName=ROL_SESION))
    ri.borrar()  # DNS Firewall, repo de ECR, runtimes, rol y VPC
    if os.environ.get("SONDA_VPC"):  # en una VPC ajena: solo lo que ha creado esta sonda
        ids = creado.get("eps_ajenos", []) + list(creado.get("ep_ids", {}).values())
        for i in ids:
            intenta("endpoint", lambda i=i: ec2.delete_vpc_endpoints(VpcEndpointIds=[i]))
        for _ in range(40):
            quedan = [g for g in creado.get("sgs_ajenos", []) if ec2.describe_security_groups(Filters=[{"Name": "group-id", "Values": [g]}])["SecurityGroups"]]
            for g in quedan:
                try:
                    ec2.delete_security_group(GroupId=g)
                except ClientError:
                    pass
            if not quedan:
                break
            time.sleep(30)
        print("grupos de seguridad sin borrar (ENI de AgentCore aún vivas):", quedan or "ninguno", flush=True)
    for rid in creado.get("runtimes", []):
        for g in logs.describe_log_groups(logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{rid}").get("logGroups", []):
            intenta("logs", lambda g=g: logs.delete_log_group(logGroupName=g["logGroupName"]))
    restos = []
    if [r for r in ctl.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"].startswith(NOMBRE_)]:
        restos.append("runtime")
    for v in ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [NOMBRE]}])["Vpcs"]:
        restos.append("vpc " + v["VpcId"])
    for nombre, f in (("ecr", lambda: ecr.describe_repositories(repositoryNames=[NOMBRE])),
                      ("rol", lambda: iam.get_role(RoleName=NOMBRE)), ("rol sesión", lambda: iam.get_role(RoleName=ROL_SESION))):
        try:
            f()
            restos.append(nombre)
        except ClientError:
            pass
    if [d for d in r53r.list_firewall_rule_groups()["FirewallRuleGroups"] if d["Name"] == NOMBRE]:
        restos.append("dns firewall")
    print("restos de esta sonda:", restos or "ninguno", flush=True)


if __name__ == "__main__":
    try:
        cuenta = sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, "·", NOMBRE, flush=True)
        probar(cuenta)
    except Exception as e:
        import traceback
        traceback.print_exc()
        anota("la sonda terminó con una excepción", False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        borrar()
        if "crear_v2" in FASES:
            try:
                metricas_endpoints()
                informe_ecr()
            except Exception as e:
                print("informe de ECR:", type(e).__name__, str(e)[:300], flush=True)
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
