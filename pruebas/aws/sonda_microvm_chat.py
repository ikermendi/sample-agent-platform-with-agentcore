"""Sonda 9 del nivel 2: una microVM por chat (Runtime V2, varias invocaciones a la misma sesión).

Comprueba la fila "Una microVM por chat" de "Por verificar" (y lo que dan por hecho "Runtime V2 y lo que
exige al kernel" y la Lambda vigilante):

  1. Dos invocaciones a la vez a la misma sesión: con una ejecución en segundo plano (HealthyBusy), con
     una invocación que tarda en responder, y dos primeras invocaciones simultáneas a una sesión que aún
     no existe (¿una microVM o dos?, ¿409?).
  2. La carrera entre StopRuntimeSession y una invocación nueva al mismo id, con desfases de -200, 0,
     100, 500 y 2.000 ms: qué devuelve cada una y dónde cae la invocación (marca en memoria y en /tmp).
  3. Limpieza del Sandbox entre mensajes: el "código del modelo" (un proceso como el usuario `sandbox`,
     con entorno limpio, desacoplado con setsid y doble fork) intenta leer el entorno, /proc/<kernel>/
     environ y mem, /tmp, la red local y el endpoint de credenciales; el kernel lo mata al acabar el
     mensaje y en el siguiente comprueba que no queda nada y qué llegó a leer. Variantes: cambia de
     nombre, salta de pid con forks continuos, y sin limpieza (sigue vivo durante el mensaje 2).
  4. Tiempo de una invocación a una sesión viva (caliente) frente a una sesión nueva, con Strands
     cargado en la imagen (la de la sonda 6, más el SDK de AgentCore).

El kernel de prueba es un BedrockAgentCoreApp con entrypoint async; la ejecución larga va con
asyncio.create_task + @app.async_task (/ping = HealthyBusy), como el kernel del diseño. No llama a Bedrock.
Va en modo PUBLIC (nada de lo que se mide depende de la VPC; la VPC sin salida ya la cubren las sondas 5 y
6): solo crea el repo de ECR, el rol y el runtime. Necesita Docker (arm64) y boto3 >= 1.43. Todo con el
prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2; se borra al acabar, aunque algo falle (también el
grupo de logs del runtime). Unos 15 minutos; céntimos. Uso:

    SONDA_REGION=eu-north-1 AWS_PROFILE=sonda .venv312/bin/python sonda_microvm_chat.py
"""
import base64
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from botocore.config import Config
from botocore.exceptions import ClientError

import sonda_agentcore as base
import sonda_runtime_imagen as ri

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = base.REGION, base.NOMBRE, base.NOMBRE_, base.TAGS, base.TAGS_DICT
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
ses, ctl, ecr = base.ses, base.ctl, ri.ecr
logs = ses.client("logs")
# Sin reintentos: queremos ver los 409 y los 5xx tal como llegan
dp = ses.client("bedrock-agentcore", config=Config(retries={"max_attempts": 1, "mode": "standard"},
                                                     read_timeout=180, max_pool_connections=20))
SESIONES = []

DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir strands-agents==1.63.0 boto3 bedrock-agentcore==1.25.2
RUN useradd --system --no-create-home --shell /usr/sbin/nologin sandbox && mkdir -p /srv/espia && chown sandbox /srv/espia
COPY main.py espia.py /app/
WORKDIR /app
EXPOSE 8080
CMD ["python", "main.py"]
"""

KERNEL = r'''
import asyncio, glob, json, os, pwd, re, socket, subprocess, sys, time, uuid
t0 = time.time()
import boto3, strands  # lo caro, antes de la instantánea
CARGA = round(time.time() - t0, 2)
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
SB = pwd.getpwnam("sandbox")
SALIDA = "/srv/espia/salida.jsonl"
E = {"vm": None, "n": 0, "en_curso": 0, "marca": None, "cred": None}
TAREAS = set()


def vm():
    if E["vm"] is None:  # perezoso: el de arranque saldría igual en todas (instantánea)
        E["vm"] = uuid.uuid4().hex[:12]
    return E["vm"]


def lee(p, n=200):
    try:
        with open(p, "rb") as f:
            return f.read(n).decode("latin1")
    except Exception as e:
        return "ERR " + type(e).__name__


def procesos(uid):
    vivos, zombis = [], []
    for st in glob.glob("/proc/[0-9]*/status"):
        try:
            d = dict(l.split(":\t", 1) for l in open(st).read().splitlines() if ":\t" in l)
        except Exception:
            continue
        if int(d["Uid"].split()[0]) == uid:
            (zombis if d["State"].startswith("Z") else vivos).append(f"{st.split('/')[2]}:{d['Name']}")
    return vivos, zombis


def recoge():
    n = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return n
        if pid == 0:
            return n
        n += 1


def entorno():
    caps = [l for l in lee("/proc/self/status", 4000).splitlines() if l.startswith(("CapEff", "Uid"))]
    um = os.umask(0); os.umask(um)
    try:
        metodo = boto3.Session().get_credentials().method
    except Exception as e:
        metodo = "ERR " + type(e).__name__
    return {"uid": os.getuid(), "caps": caps, "umask": oct(um), "pid": os.getpid(), "ppid": os.getppid(),
            "pid1": lee("/proc/1/comm").strip(), "yama": lee("/proc/sys/kernel/yama/ptrace_scope").strip(),
            "cred_metodo": metodo, "env_aws": sorted(k for k in os.environ if k.startswith(("AWS", "ECS")))}


def lanza(modo, cred, token_fichero):
    os.environ["CRED_KERNEL"] = cred  # un kernel descuidado: la credencial en su entorno
    with open("/tmp/kernel_defecto.json", "w") as f:  # umask por defecto
        json.dump({"token": token_fichero}, f)
    fd = os.open("/tmp/kernel_0600.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, cred.encode()); os.close(fd)
    if os.path.exists(SALIDA):
        os.remove(SALIDA)
    p = subprocess.run([sys.executable, "/app/espia.py", str(os.getpid()), SALIDA, modo],
                       user=SB.pw_uid, group=SB.pw_gid, extra_groups=[], env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                       start_new_session=True, close_fds=True, cwd="/tmp", stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    return {"lanzado_rc": p.returncode}


def limpia(estrategia):
    r = {"antes": procesos(SB.pw_uid)[0]}
    t = time.time()
    if estrategia in ("pkill", "pkill+bucle"):
        r["pkill_rc"] = subprocess.run(["pkill", "-KILL", "-u", "sandbox"]).returncode
        time.sleep(1)
        r["tras_pkill"] = procesos(SB.pw_uid)[0]
    if estrategia in ("bucle", "pkill+bucle"):
        # kill(-1) como el propio usuario: llega a todos sus procesos de una vez (salvo al que lo llama)
        codigo = ("import os,time\nfor i in range(200):\n    try: os.kill(-1, 9)\n    except ProcessLookupError: print(i); break\n"
                  "    time.sleep(0.005)\nelse: print('200 (quedan zombis sin recoger)')\n")
        p = subprocess.run([sys.executable, "-c", codigo], user=SB.pw_uid, group=SB.pw_gid, extra_groups=[],
                           env={}, capture_output=True, timeout=20)
        r["bucle_vueltas"] = p.stdout.decode().strip() or p.stderr.decode()[-200:]
        time.sleep(1)
    r["recogidos"] = recoge()
    r["despues"], r["zombis"] = procesos(SB.pw_uid)
    borrados = []
    for d in ("/tmp", "/var/tmp", "/dev/shm"):
        for raiz, dirs, fichs in os.walk(d, topdown=False):
            for nombre in fichs + dirs:
                ruta = os.path.join(raiz, nombre)
                try:
                    st = os.lstat(ruta)
                    if st.st_uid == SB.pw_uid or nombre.startswith("kernel_"):
                        (os.rmdir if os.path.isdir(ruta) and not os.path.islink(ruta) else os.remove)(ruta)
                        borrados.append(ruta)
                except Exception:
                    pass
    os.environ.pop("CRED_KERNEL", None)
    r["borrados"] = borrados
    r["s"] = round(time.time() - t, 2)
    E["fin_limpieza"] = time.time()
    return r


def comprueba(secretos):
    vivos, zombis = procesos(SB.pw_uid)
    r = {"vivos": vivos, "zombis": zombis}
    if not os.path.exists(SALIDA):
        r["salida"] = "no existe"
        return r
    texto = open(SALIDA, errors="replace").read()
    lineas = [json.loads(l) for l in texto.splitlines() if l.startswith("{")]
    r["salida_bytes"], r["salida_lineas"] = len(texto), len(lineas)
    r["contiene"] = {k: (v in texto) for k, v in secretos.items()}
    r["pids_espia"] = sorted({l["pid"] for l in lineas})[:40]
    r["n_pids_espia"] = len({l["pid"] for l in lineas})
    r["comm_espia"] = sorted({l.get("comm", "") for l in lineas})
    r["ultima_linea_hace_s"] = round(time.time() - lineas[-1]["t"], 1) if lineas else None
    fin = E.get("fin_limpieza")
    r["lineas_tras_limpieza"] = sum(1 for l in lineas if fin and l["t"] > fin) if fin else None
    z0 = len(zombis)
    time.sleep(2)
    r["zombis_nuevos_en_2s"] = len(procesos(SB.pw_uid)[1]) - z0
    r["zombis"] = len(zombis)
    vistos = {}
    for l in lineas:
        for k in ("environ_kernel", "mem_kernel", "maps_kernel", "fd_kernel", "environ_pid1", "cmdline_kernel"):
            vistos.setdefault(k, set()).add(l.get(k, "")[:80])
        for p, c in l.get("tmp", {}).items():
            vistos.setdefault("tmp:" + p, set()).add(c[:80])
        if l.get("red"):
            r["red_espia"] = l["red"]
        if "env" in l:
            r["env_espia"] = l["env"]
        vistos.setdefault("otros_environ_legibles", set()).add(str(l.get("otros_environ_legibles")))
    r["visto"] = {k: sorted(v)[:4] for k, v in vistos.items()}
    return r


@app.async_task
async def larga(segundos):
    await asyncio.sleep(segundos)


@app.entrypoint
async def entrada(carga):
    llegada = time.time()
    E["n"] += 1
    E["en_curso"] += 1
    al_entrar = E["en_curso"]
    try:
        r = {"vm": vm(), "pid": os.getpid(), "n": E["n"], "en_curso_al_entrar": al_entrar,
             "tareas_al_entrar": len(app._active_tasks), "ping": app.get_current_ping_status().value,
             "carga_s": CARGA}
        acc = carga.get("accion")
        if acc == "larga":
            t = asyncio.create_task(larga(carga["segundos"]))
            TAREAS.add(t)
            t.add_done_callback(TAREAS.discard)
            await asyncio.sleep(0)
            r["aceptada"] = True
        elif acc == "lenta":
            await asyncio.sleep(carga["segundos"])
        elif acc == "marca":
            E["marca"] = carga["marca"]
            open("/tmp/marca", "w").write(carga["marca"])
        elif acc == "entorno":
            r["entorno"] = entorno()
        elif acc == "mensaje":
            E["cred"] = carga["cred"]  # en memoria, como las credenciales STS del mensaje
            if carga.get("lanzar"):
                r["lanza"] = await asyncio.to_thread(lanza, carga["lanzar"], carga["cred"], carga["token_fichero"])
            if carga.get("espera"):
                await asyncio.sleep(carga["espera"])
            if carga.get("comprobar"):
                r["comprueba"] = await asyncio.to_thread(comprueba, carga["secretos"])
            if carga.get("limpiar"):
                r["limpia"] = await asyncio.to_thread(limpia, carga["limpiar"])
        r["marca"] = E["marca"]
        r["marca_tmp"] = lee("/tmp/marca")
        r["ping_al_salir"] = app.get_current_ping_status().value
        r["entrada"], r["salida"] = llegada, time.time()
        return r
    finally:
        E["en_curso"] -= 1


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
'''

ESPIA = r'''
import ctypes, glob, json, os, re, sys, time, urllib.request
kpid, salida, modo = int(sys.argv[1]), sys.argv[2], sys.argv[3]
PAT = re.compile(rb"FALSO-[A-Za-z0-9_-]{4,60}")


def desacopla():
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    os.chdir("/")


def lee(p, n=300):
    try:
        with open(p, "rb") as f:
            return f.read(n).decode("latin1")
    except Exception as e:
        return "ERR " + type(e).__name__ + " " + str(getattr(e, "errno", ""))


def mem():
    try:
        mapas = open(f"/proc/{kpid}/maps").read().splitlines()
    except Exception as e:
        return "ERR maps " + type(e).__name__
    vistos = set()
    try:
        with open(f"/proc/{kpid}/mem", "rb", 0) as m:
            for l in mapas:
                a, perm = l.split()[:2]
                if "rw" not in perm:
                    continue
                ini, fin = (int(x, 16) for x in a.split("-"))
                try:
                    m.seek(ini)
                    vistos.update(x.decode() for x in PAT.findall(m.read(min(fin - ini, 64 << 20))))
                except Exception:
                    pass
        return "LEIDO " + ",".join(sorted(vistos))
    except Exception as e:
        return "ERR mem " + type(e).__name__


def red():
    r = {}
    def prueba(nombre, url, datos=None, cab=None, metodo=None):
        try:
            req = urllib.request.Request(url, data=datos, headers=cab or {}, method=metodo)
            with urllib.request.urlopen(req, timeout=2) as x:
                cuerpo = x.read(2000).decode("latin1")
                r[nombre] = f"{x.status} " + re.sub(r'"(SecretAccessKey|Token|SessionToken)"\s*:\s*"[^"]+"', r'"\1":"<oculto>"', cuerpo)[:200]
                return cuerpo
        except Exception as e:
            r[nombre] = "ERR " + type(e).__name__ + " " + str(e)[:80]
    prueba("ping_local", "http://127.0.0.1:8080/ping")
    prueba("invoca_local", "http://127.0.0.1:8080/invocations", b'{"accion":"marca_espia"}', {"Content-Type": "application/json"})
    tok = prueba("imds_token", "http://169.254.169.254/latest/api/token", b"", {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, "PUT")
    prueba("imds_rol", "http://169.254.169.254/latest/meta-data/iam/security-credentials/", None,
           {"X-aws-ec2-metadata-token": tok} if tok and not tok.startswith("ERR") else {})
    prueba("ecs_cred", "http://169.254.170.2/v2/credentials")
    rol = r.get("imds_rol", "")[4:].strip().splitlines()[0] if r.get("imds_rol", "").startswith("200") else ""
    if rol:
        c = prueba("imds_cred", "http://169.254.169.254/latest/meta-data/iam/security-credentials/" + rol, None,
                   {"X-aws-ec2-metadata-token": tok})
        try:
            c = json.loads(c)
            r["imds_cred"] = {"AccessKeyId": c["AccessKeyId"][:4] + "…", "Secret": "SecretAccessKey" in c,
                              "Token": "Token" in c, "Expiration": c.get("Expiration")}
            import boto3
            r["sts_con_ellas"] = boto3.client("sts", region_name="eu-north-1", aws_access_key_id=c["AccessKeyId"],
                                              aws_secret_access_key=c["SecretAccessKey"],
                                              aws_session_token=c["Token"]).get_caller_identity()["Arn"]
        except Exception as e:
            r["sts_con_ellas"] = "ERR " + type(e).__name__ + " " + str(e)[:100]
    return r


def vuelta(primera):
    d = {"t": time.time(), "pid": os.getpid(), "comm": lee("/proc/self/comm").strip(), "env": sorted(os.environ),
         "environ_kernel": lee(f"/proc/{kpid}/environ"), "cmdline_kernel": lee(f"/proc/{kpid}/cmdline", 120),
         "environ_pid1": lee("/proc/1/environ"), "maps_kernel": lee(f"/proc/{kpid}/maps", 80),
         "mem_kernel": mem(), "tmp": {}}
    try:
        d["fd_kernel"] = str(os.listdir(f"/proc/{kpid}/fd"))[:80]
    except Exception as e:
        d["fd_kernel"] = "ERR " + type(e).__name__
    for p in glob.glob("/tmp/**", recursive=True):
        if os.path.isfile(p):
            d["tmp"][p] = lee(p, 200)
    legibles = []
    for e in glob.glob("/proc/[0-9]*/environ"):
        c = lee(e, 4000)
        if not c.startswith("ERR"):
            legibles.append(e.split("/")[2])
            d.setdefault("secretos_environ", []).extend(x.decode() for x in PAT.findall(c.encode("latin1")))
    d["otros_environ_legibles"] = len(legibles)
    todo = json.dumps(d).encode()
    d["secretos_vistos"] = sorted({x.decode() for x in PAT.findall(todo)})
    with open(salida, "a") as f:
        f.write(json.dumps(d) + "\n")
    if primera:
        try:
            open("/tmp/espia_dejado.txt", "w").write("hola")
        except Exception:
            pass
        r = red()
        with open(salida, "a") as f:
            f.write(json.dumps({"t": time.time(), "pid": os.getpid(), "comm": lee("/proc/self/comm").strip(), "red": r}) + "\n")


desacopla()
if "renombra" in modo:
    ctypes.CDLL(None).prctl(15, b"kworker/0:1H", 0, 0, 0)
if "hidra" in modo:
    linaje = 0
    for i in range(4):  # cinco linajes que saltan de pid cada 10 ms
        if os.fork() == 0:
            linaje = i + 1
            break
    ultima, primera = 0, linaje == 0  # la red, solo desde un linaje
    while True:
        if time.time() - ultima > 1:
            vuelta(primera)
            primera, ultima = False, time.time()
        time.sleep(0.01)
        if os.fork():
            os._exit(0)
else:
    primera = True
    while True:
        vuelta(primera)
        primera = False
        time.sleep(1)
'''


def imagen():
    repo = ecr.create_repository(repositoryName=NOMBRE, tags=TAGS)["repository"]["repositoryUri"]
    creado["repo"] = NOMBRE
    tok = ecr.get_authorization_token()["authorizationData"][0]
    usuario, clave = base64.b64decode(tok["authorizationToken"]).decode().split(":")
    subprocess.run(["docker", "login", "-u", usuario, "--password-stdin", tok["proxyEndpoint"]],
                   input=clave.encode(), check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as d:
        for nombre, texto in (("Dockerfile", DOCKERFILE), ("main.py", KERNEL), ("espia.py", ESPIA)):
            open(os.path.join(d, nombre), "w").write(texto)
        t0 = time.time()
        for cmd in (["docker", "build", "--platform", "linux/arm64", "-t", f"{repo}:v1", d], ["docker", "push", f"{repo}:v1"]):
            p = subprocess.run(cmd, capture_output=True)
            if p.returncode:
                raise RuntimeError(p.stderr.decode()[-1500:])
    print(f"imagen {repo}:v1 subida en {time.time() - t0:.0f} s", flush=True)
    return f"{repo}:v1"


def llama(arn, sesion, carga):
    t0 = time.time()
    try:
        r = dp.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sesion, contentType="application/json",
                                    accept="application/json", payload=json.dumps(carga).encode())
        cuerpo = r["response"].read()
        try:
            c = json.loads(cuerpo)
        except ValueError:
            c = {"crudo": cuerpo[:300].decode("latin1")}
        return {"ok": True, "t0": t0, "t1": time.time(), "s": round(time.time() - t0, 2), "r": c,
                "http": r.get("statusCode")}
    except ClientError as e:
        return {"ok": False, "t0": t0, "t1": time.time(), "s": round(time.time() - t0, 2), "error": codigo(e),
                "http": e.response.get("ResponseMetadata", {}).get("HTTPStatusCode"), "msg": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "t0": t0, "t1": time.time(), "s": round(time.time() - t0, 2), "error": type(e).__name__,
                "msg": str(e)[:300]}


def para(arn, sesion):
    t0 = time.time()
    try:
        r = dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=sesion)
        return {"ok": True, "t0": t0, "t1": time.time(), "s": round(time.time() - t0, 2), "http": r.get("statusCode")}
    except ClientError as e:
        return {"ok": False, "t0": t0, "t1": time.time(), "s": round(time.time() - t0, 2), "error": codigo(e),
                "http": e.response.get("ResponseMetadata", {}).get("HTTPStatusCode"), "msg": str(e)[:300]}


def nueva():
    s = base.sesion_nueva()
    SESIONES.append(s)
    return s


def resume(x):
    if not x["ok"]:
        return f"{x.get('error')} (HTTP {x.get('http')}) en {x['s']} s: {x.get('msg', '')[:160]}"
    r = x["r"]
    return (f"{x['s']} s · vm={r.get('vm')} pid={r.get('pid')} n={r.get('n')} en_curso={r.get('en_curso_al_entrar')} "
            f"tareas={r.get('tareas_al_entrar')} ping={r.get('ping')} marca={r.get('marca')} tmp={r.get('marca_tmp', '')[:30]}")


# ---------------------------------------------------------------- 1. Dos invocaciones a la vez
def punto1(arn):
    print("\n== 1. Dos invocaciones a la vez a la misma sesión", flush=True)
    s = nueva()
    a = llama(arn, s, {"accion": "larga", "segundos": 90})
    time.sleep(2)
    b = llama(arn, s, {"accion": "larga", "segundos": 90})
    c = llama(arn, s, {"accion": "informa"})
    for n, x in (("larga 1", a), ("larga 2 (con la 1 en marcha)", b), ("informa", c)):
        print(f"     {n}: {resume(x)}", flush=True)
    mismo = a["ok"] and b["ok"] and a["r"]["vm"] == b["r"]["vm"] and a["r"]["pid"] == b["r"]["pid"]
    anota("1a. Con una ejecución en segundo plano (HealthyBusy), la segunda invocación entra en el mismo proceso",
          mismo and b["r"]["ping"] == "HealthyBusy",
          f"2ª: {resume(b)}; después: tareas={c['r'].get('tareas_al_entrar') if c['ok'] else '?'}")

    s = nueva()
    llama(arn, s, {"accion": "informa"})
    plan = [(0, {"accion": "lenta", "segundos": 20}), (2, {"accion": "informa"}), (3, {"accion": "lenta", "segundos": 5}),
            (4, {"accion": "larga", "segundos": 30})]
    t0 = time.time()

    def lanza(d, carga):
        time.sleep(max(0, t0 + d - time.time()))
        return llama(arn, s, carga)
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda p: lanza(*p), plan))
    for (d, carga), x in zip(plan, res):
        k = x.get("r", {})
        print(f"     +{d}s {carga['accion']}: {resume(x)} · en el kernel {k.get('entrada', 0) - t0:.1f}–{k.get('salida', 0) - t0:.1f} s",
              flush=True)
    lenta = res[0].get("r", {})
    solapan = [x for x in res[1:] if x["ok"] and x["r"]["entrada"] < lenta.get("salida", 0)]
    anota("1b. Con una invocación sin responder (20 s), otras tres al mismo id entran en paralelo en el mismo proceso",
          len(solapan) == 3 and len({x["r"]["pid"] for x in res if x["ok"]}) == 1,
          f"{len(solapan)} de 3 entraron antes de que acabara la lenta; en_curso al entrar: "
          f"{[x['r']['en_curso_al_entrar'] for x in res if x['ok']]}; errores: {[x.get('error') for x in res if not x['ok']]}")

    filas = []
    for rep in range(5):
        s = nueva()
        barrera = threading.Barrier(3)

        def va():
            barrera.wait()
            return llama(arn, s, {"accion": "informa"})
        with ThreadPoolExecutor(3) as ex:
            res = list(ex.map(lambda _: va(), range(3)))
        vms = {x["r"]["vm"] for x in res if x["ok"]}
        errs = [f"{x.get('error')}/{x.get('http')}" for x in res if not x["ok"]]
        filas.append((len(vms), errs, [x["s"] for x in res]))
        print(f"     rep {rep}: microVM distintas={len(vms)} errores={errs} tiempos={[x['s'] for x in res]} "
              f"n={[x['r']['n'] for x in res if x['ok']]}", flush=True)
        if errs:
            print("       ", [x.get("msg") for x in res if not x["ok"]][0], flush=True)
    anota("1c. Tres primeras invocaciones simultáneas a una sesión nueva: una sola microVM",
          all(f[0] <= 1 for f in filas), "; ".join(f"{f[0]} vm, errores {f[1]}" for f in filas))


# ---------------------------------------------------------------- 2. Stop + Invoke
def punto2(arn):
    print("\n== 2. StopRuntimeSession y una invocación al mismo id", flush=True)
    filas = []
    for desfase in (-0.2, 0, 0.1, 0.5, 2.0):
        for rep in range(3):
            s = nueva()
            m = llama(arn, s, {"accion": "marca", "marca": "M-" + uuid.uuid4().hex[:6]})
            llama(arn, s, {"accion": "larga", "segundos": 120})  # una ejecución "colgada" en marcha
            vm0 = m["r"]["vm"] if m["ok"] else None
            t0 = time.time() + 0.3

            def stop():
                time.sleep(max(0, t0 + max(0, -desfase) - time.time()))
                return para(arn, s)

            def inv():
                time.sleep(max(0, t0 + max(0, desfase) - time.time()))
                return llama(arn, s, {"accion": "informa"})
            with ThreadPoolExecutor(2) as ex:
                fs, fi = ex.submit(stop), ex.submit(inv)
                st, iv = fs.result(), fi.result()
            time.sleep(3)
            despues = llama(arn, s, {"accion": "informa"})
            donde = ("error" if not iv["ok"] else "vieja" if iv["r"]["vm"] == vm0 else "nueva")
            sigue = ("error" if not despues["ok"] else "vieja" if despues["r"]["vm"] == vm0 else
                     "la de la invocación" if iv["ok"] and despues["r"]["vm"] == iv["r"]["vm"] else "otra nueva")
            fila = {"desfase": desfase, "stop": "ok" if st["ok"] else f"{st['error']}/{st['http']}", "stop_s": st["s"],
                    "invoca": donde if iv["ok"] else f"{iv['error']}/{iv['http']}", "invoca_s": iv["s"],
                    "marca": iv["r"].get("marca") if iv["ok"] else None,
                    "tmp": iv["r"].get("marca_tmp", "")[:20] if iv["ok"] else None,
                    "a_los_3s": sigue, "n_3s": despues["r"].get("n") if despues["ok"] else despues.get("error")}
            if not iv["ok"]:
                fila["msg"] = iv["msg"][:200]
            if not st["ok"]:
                fila["msg_stop"] = st["msg"][:200]
            filas.append(fila)
            print("    ", json.dumps(fila, ensure_ascii=False), flush=True)
    vieja_perdida = [f for f in filas if f["invoca"] == "vieja" and f["a_los_3s"] != "vieja"]
    errores = sorted({f["invoca"] for f in filas if "/" in f["invoca"]} | {f["stop"] for f in filas if f["stop"] != "ok"})
    anota("2. Stop + Invoke: la invocación nunca cae aceptada en la microVM que se está parando",
          not vieja_perdida,
          f"{len(filas)} pruebas; invocación en vm vieja={sum(f['invoca'] == 'vieja' for f in filas)}, "
          f"nueva={sum(f['invoca'] == 'nueva' for f in filas)}, error={sum('/' in f['invoca'] for f in filas)}; "
          f"aceptadas en la vieja y perdidas a los 3 s={len(vieja_perdida)}; errores vistos={errores}")
    nuevas = [f for f in filas if f["invoca"] == "nueva"]
    anota("2b. Si cae en una microVM nueva, está vacía (sin marca en memoria ni en /tmp)",
          all(f["marca"] is None and f["tmp"].startswith("ERR") for f in nuevas), f"{len(nuevas)} casos")
    return filas


# ---------------------------------------------------------------- 3. Limpieza del Sandbox
def punto3(arn):
    print("\n== 3. Limpieza del Sandbox entre mensajes", flush=True)
    s = nueva()
    e = llama(arn, s, {"accion": "entorno"})
    print("     entorno del kernel:", json.dumps(e.get("r", {}).get("entorno"), ensure_ascii=False), flush=True)
    escenarios = [("simple", "pkill"), ("renombra", "pkill"), ("hidra", "pkill"), ("simple", None)]
    if "--solo-3" in sys.argv:
        escenarios = [("hidra", "pkill"), ("renombra", "pkill"), ("simple", None)]
    for modo, estrategia in escenarios:
        s = nueva()
        sec = {"cred1": "FALSO-CRED1-" + uuid.uuid4().hex[:12], "cred2": "FALSO-CRED2-" + uuid.uuid4().hex[:12],
               "fichero1": "FALSO-FICH1-" + uuid.uuid4().hex[:12]}
        m1 = llama(arn, s, {"accion": "mensaje", "cred": sec["cred1"], "token_fichero": sec["fichero1"], "lanzar": modo,
                            "espera": 8, "limpiar": estrategia})
        m2 = llama(arn, s, {"accion": "mensaje", "cred": sec["cred2"], "espera": 6, "comprobar": True, "secretos": sec})
        nombre = f"{modo}, limpieza {estrategia or 'ninguna'}"
        if not (m1["ok"] and m2["ok"]):
            anota(f"3. {nombre}", False, f"m1: {resume(m1)} · m2: {resume(m2)}")
            continue
        r1, r2 = m1["r"], m2["r"]
        print(f"     [{nombre}] m1 vm={r1['vm']} pid={r1['pid']} lanza={r1.get('lanza')}", flush=True)
        print(f"     [{nombre}] limpieza: {json.dumps(r1.get('limpia'), ensure_ascii=False)[:900]}", flush=True)
        c = r2["comprueba"]
        print(f"     [{nombre}] m2 vm={r2['vm']}: {json.dumps(c, ensure_ascii=False)[:3000]}", flush=True)
        mismo = r1["vm"] == r2["vm"]
        print(f"     [{nombre}] red del espía: {json.dumps(c.get('red_espia'), ensure_ascii=False)[:1500]}", flush=True)
        vivo = bool(c["vivos"]) or bool(c.get("lineas_tras_limpieza")) or c.get("zombis_nuevos_en_2s", 0) > 0
        if estrategia:
            anota(f"3. {nombre}: en el mensaje 2 no queda ningún proceso del sandbox", mismo and not vivo,
                  f"vivos={c['vivos'][:5]} líneas escritas tras la limpieza={c.get('lineas_tras_limpieza')} "
                  f"zombis={c['zombis']} (+{c.get('zombis_nuevos_en_2s')} en 2 s) tras pkill={r1['limpia'].get('tras_pkill', [])[:5]} "
                  f"después={r1['limpia'].get('despues', [])[:5]}")
        anota(f"3. {nombre}: el hijo no leyó las credenciales del kernel (mensaje 1 ni 2)",
              not c.get("contiene", {}).get("cred1") and not c.get("contiene", {}).get("cred2"),
              f"contiene={c.get('contiene')}, {c.get('salida_lineas')} líneas de {c.get('n_pids_espia')} pids")
        if not estrategia or vivo:
            fin = llama(arn, s, {"accion": "mensaje", "cred": "FALSO-X-fin", "limpiar": "bucle"})
            lf = fin.get("r", {}).get("limpia", {})
            m3 = llama(arn, s, {"accion": "mensaje", "cred": "FALSO-X-fin2", "espera": 3, "comprobar": True, "secretos": sec})
            c3 = m3.get("r", {}).get("comprueba", {})
            lf = dict(lf, despues=lf.get("despues", []) + c3.get("vivos", []),
                      lineas_tras=c3.get("lineas_tras_limpieza"), zombis_nuevos=c3.get("zombis_nuevos_en_2s"))
            if c3.get("lineas_tras_limpieza") or c3.get("zombis_nuevos_en_2s"):
                lf["despues"].append("sigue activo")
            print(f"     [{nombre}] limpieza con kill(-1) como el usuario: {json.dumps(lf, ensure_ascii=False)[:600]}",
                  flush=True)
            anota(f"3. {nombre}: kill(-1) como el usuario del sandbox lo mata todo", fin["ok"] and not lf.get("despues"),
                  f"vivos después={lf.get('despues')}, vueltas={lf.get('bucle_vueltas')}, zombis={len(lf.get('zombis', []))}")


# ---------------------------------------------------------------- 4. Caliente frente a nueva
def punto4(arn):
    print("\n== 4. Sesión viva frente a sesión nueva", flush=True)
    frio = [llama(arn, nueva(), {"accion": "informa"}) for _ in range(8)]
    s = nueva()
    llama(arn, s, {"accion": "informa"})
    caliente = [llama(arn, s, {"accion": "informa"}) for _ in range(8)]
    acepta = [llama(arn, s, {"accion": "larga", "segundos": 5}) for _ in range(3)]
    f = sorted(x["s"] for x in frio if x["ok"])
    c = sorted(x["s"] for x in caliente if x["ok"])
    print(f"     nueva (s): {f}\n     caliente (s): {c}\n     larga aceptada (s): {[x['s'] for x in acepta]}", flush=True)
    anota("4. Invocación a sesión viva frente a nueva", bool(f and c),
          f"nueva P50 {statistics.median(f):.2f} s (máx {f[-1]:.2f}) · caliente P50 {statistics.median(c):.2f} s "
          f"(máx {c[-1]:.2f}) · 'aceptada' en caliente {statistics.median([x['s'] for x in acepta]):.2f} s · "
          f"Strands cargado en {frio[0]['r'].get('carga_s')} s antes de la instantánea")


def probar(cuenta):
    uri = imagen()
    rol_arn = ri.rol(cuenta)
    time.sleep(15)
    t0 = time.time()
    r = ctl.create_agent_runtime(
        agentRuntimeName=NOMBRE_, roleArn=rol_arn, platformVersion="V2",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
        networkConfiguration={"networkMode": "PUBLIC"}, protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600}, tags=TAGS_DICT)
    rid, arn = r["agentRuntimeId"], r["agentRuntimeArn"]
    creado.setdefault("runtimes", []).append(rid)
    print("runtime", rid, "preparando la instantánea…", flush=True)
    _, info = base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=rid))
    print(f"READY en {time.time() - t0:.0f} s, platformVersion={info.get('platformVersion')}", flush=True)
    for paso in ((punto3,) if "--solo-3" in sys.argv else (punto4, punto1, punto2, punto3)):
        try:
            paso(arn)
        except Exception as e:
            anota(paso.__name__, False, f"{type(e).__name__}: {str(e)[:300]}")
    print("parando las sesiones…", flush=True)
    for s in SESIONES:
        try:
            dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=s)
        except ClientError:
            pass


def borrar():
    ri.borrar()  # repo de ECR, runtime, rol
    for rid in creado.get("runtimes", []):
        for g in logs.describe_log_groups(logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{rid}").get("logGroups", []):
            intenta("logs", lambda g=g: logs.delete_log_group(logGroupName=g["logGroupName"]))
    restos = []
    if [r for r in ctl.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"] == NOMBRE_]:
        restos.append("runtime")
    try:
        ecr.describe_repositories(repositoryNames=[NOMBRE])
        restos.append("ecr")
    except ClientError:
        pass
    try:
        base.iam.get_role(RoleName=NOMBRE)
        restos.append("rol")
    except ClientError:
        pass
    if logs.describe_log_groups(logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{NOMBRE_}").get("logGroups"):
        restos.append("logs")
    print("restos de esta sonda:", restos or "ninguno", flush=True)


if __name__ == "__main__":
    try:
        cuenta = base.sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, "·", NOMBRE, flush=True)
        probar(cuenta)
    finally:
        borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
