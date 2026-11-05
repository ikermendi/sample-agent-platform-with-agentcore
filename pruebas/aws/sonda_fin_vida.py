"""Sonda 26 del nivel 2: qué recibe el kernel al llegar a maxLifetime y cuánto tarda la limpieza.

Comprueba dos filas de "To verify before building":

  1. maxLifetime: con el run ocupado (HealthyBusy), ¿cuándo se corta contando desde la primera invocación,
     que es desde donde el kernel calcula su vida restante?, ¿recibe alguna señal antes?, ¿qué pasa con las
     invocaciones alrededor del corte?, ¿y con una microVM desocupada que recibe invocaciones?
  2. La limpieza antes de `end`, en una microVM real: matar lo que deja el código del modelo (usuario
     `sandbox`) y borrar temporales, con tope de 5 s en el diseño.

El kernel de prueba deja un latido por segundo en S3 y anota las señales que le llegan a uvicorn y a un
proceso hijo aparte. No llama a Bedrock. Va en modo PUBLIC, como la sonda 9. Reutiliza sus piezas, y se
borra todo al acabar aunque algo falle. Unos 15 minutos; céntimos. Uso:

    SONDA_REGION=eu-north-1 AWS_PROFILE=sonda .venv312/bin/python sonda_fin_vida.py
"""
import json
import statistics
import sys
import threading
import time

from botocore.exceptions import ClientError

import sonda_microvm_chat as mc

base, ri, ctl, dp = mc.base, mc.ri, mc.ctl, mc.dp
REGION, NOMBRE, NOMBRE_, TAGS_DICT = mc.REGION, mc.NOMBRE, mc.NOMBRE_, mc.TAGS_DICT
creado, anota = mc.creado, mc.anota
s3 = base.s3
VIDA = 240  # maxLifetime de la prueba, en segundos

KERNEL = r'''
import asyncio, json, os, pwd, shutil, signal, subprocess, sys, threading, time, uuid
import boto3, strands, uvicorn
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
BUCKET = os.environ["BUCKET"]
SB = pwd.getpwnam("sandbox")
E = {"vm": None, "primera": None, "n": 0, "s3": None}
TAREAS = set()


def pon(clave, datos):
    E["s3"].put_object(Bucket=BUCKET, Key=f"latidos/{E['vm']}/{clave}.json", Body=json.dumps(datos).encode())


def registra(que):
    d = {"que": que, "t": time.time(), "edad": round(time.time() - E["primera"], 2) if E["primera"] else None}
    print("SENAL", json.dumps(d), flush=True)
    if E["vm"]:
        threading.Thread(target=pon, args=(f"senal-{uuid.uuid4().hex[:6]}", d), daemon=True).start()


original = uvicorn.Server.handle_exit
def handle_exit(self, sig, frame):
    registra(f"uvicorn:{sig}")
    return original(self, sig, frame)
uvicorn.Server.handle_exit = handle_exit


VIGIA = """
import boto3, json, os, signal, sys, time
vm, bucket, primera = sys.argv[1], sys.argv[2], float(sys.argv[3])
s3 = boto3.client("s3")
def h(n, f):
    s3.put_object(Bucket=bucket, Key=f"latidos/{vm}/vigia-{n}-{int(time.time())}.json",
                  Body=json.dumps({"que": f"hijo:{n}", "t": time.time(), "edad": round(time.time() - primera, 2)}).encode())
for n in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2, signal.SIGCONT):
    signal.signal(n, h)
while True:
    time.sleep(1)
"""


def latidos():
    while True:
        try:
            pon("latido", {"vm": E["vm"], "t": time.time(), "edad": round(time.time() - E["primera"], 2), "n": E["n"]})
        except Exception as e:
            print("latido", e, flush=True)
        time.sleep(1)


def primera_vez():
    if E["vm"]:
        return
    E["primera"] = time.time()
    E["s3"] = boto3.client("s3")
    E["vm"] = uuid.uuid4().hex[:10]
    threading.Thread(target=latidos, daemon=True).start()
    subprocess.Popen([sys.executable, "-c", VIGIA, E["vm"], BUCKET, str(E["primera"])], start_new_session=True)


@app.async_task
async def larga(segundos):
    await asyncio.sleep(segundos)


def como_sandbox(codigo):
    return subprocess.Popen([sys.executable, "-c", codigo], user=SB.pw_uid, group=SB.pw_gid, extra_groups=[],
                            env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, cwd="/tmp", start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sus_procesos():
    vivos = 0
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open(f"/proc/{p}/status") as f:
                    t = f.read()
                if f"Uid:\t{SB.pw_uid}\t" in t and "State:\tZ" not in t:
                    vivos += 1
            except OSError:
                pass
    return vivos


def recoge():
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def prepara(caso):
    os.makedirs("/tmp/obra", exist_ok=True)
    os.chown("/tmp/obra", SB.pw_uid, SB.pw_gid)
    if caso == "procesos_20" or caso == "procesos_200":
        n = int(caso.split("_")[1])
        como_sandbox(f"import os,time\nfor i in range({n - 1}):\n    if os.fork()==0: break\ntime.sleep(600)\n")
    elif caso == "saltos":  # cambia de pid sin parar, para que no lo alcance una lista de pids
        como_sandbox("import os,time\nwhile True:\n    if os.fork(): os._exit(0)\n    time.sleep(0.002)\n")
    elif caso == "ficheros_10000":
        como_sandbox("import os\nfor i in range(10000):\n    open(f'/tmp/obra/f{i}','w').write('x'*1024)\nimport time\ntime.sleep(600)\n")
    elif caso == "megas_500":
        como_sandbox("import os\nb=os.urandom(1<<20)\nf=open('/tmp/obra/g','wb')\nfor i in range(500):\n    f.write(b)\nf.close()\nimport time\ntime.sleep(600)\n")
    objetivo = {"ficheros_10000": lambda: len(os.listdir("/tmp/obra")) >= 10000,
                "megas_500": lambda: os.path.exists("/tmp/obra/g") and os.path.getsize("/tmp/obra/g") >= 500 << 20,
                "procesos_20": lambda: sus_procesos() >= 20, "procesos_200": lambda: sus_procesos() >= 200,
                "saltos": lambda: sus_procesos() >= 1}.get(caso, lambda: True)
    fin = time.time() + 60
    while not objetivo() and time.time() < fin:
        time.sleep(0.05)
    return sus_procesos()


def limpia():
    r = {}
    t0 = time.perf_counter()
    subprocess.run(["pkill", "-KILL", "-u", "sandbox"])
    # kill(-1) como el propio usuario: alcanza también a lo que salta de pid
    subprocess.run([sys.executable, "-S", "-c", "import os,time\nfor i in range(400):\n    try: os.kill(-1, 9)\n    except ProcessLookupError: break\n    time.sleep(0.005)\n"],
                   user=SB.pw_uid, group=SB.pw_gid, extra_groups=[], env={}, timeout=20)
    r["matar_s"] = round(time.perf_counter() - t0, 3)
    recoge()
    t1 = time.perf_counter()
    for d in ("/tmp", "/var/tmp", "/dev/shm"):
        for nombre in os.listdir(d):
            ruta = os.path.join(d, nombre)
            if os.path.isdir(ruta) and not os.path.islink(ruta):
                shutil.rmtree(ruta, ignore_errors=True)
            else:
                try:
                    os.remove(ruta)
                except OSError:
                    pass
    r["borrar_s"] = round(time.perf_counter() - t1, 3)
    r["total_s"] = round(time.perf_counter() - t0, 3)
    r["quedan"] = sus_procesos()
    return r


@app.entrypoint
async def entrada(carga):
    primera_vez()
    E["n"] += 1
    r = {"vm": E["vm"], "edad": round(time.time() - E["primera"], 2), "n": E["n"]}
    acc = carga.get("accion")
    if acc == "larga":
        t = asyncio.create_task(larga(carga["segundos"]))
        TAREAS.add(t)
        t.add_done_callback(TAREAS.discard)
        await asyncio.sleep(0)
        r["ping"] = app.get_current_ping_status().value
    elif acc == "limpieza":
        r["antes"] = await asyncio.to_thread(prepara, carga["caso"])
        r["limpieza"] = await asyncio.to_thread(limpia)
    return r


app.run()
'''


def lee(vm, clave):
    try:
        return json.loads(s3.get_object(Bucket=NOMBRE, Key=f"latidos/{vm}/{clave}.json")["Body"].read())
    except ClientError:
        return None


def senales(vm):
    r = []
    for o in s3.list_objects_v2(Bucket=NOMBRE, Prefix=f"latidos/{vm}/").get("Contents", []):
        if "/latido" not in o["Key"]:
            r.append(json.loads(s3.get_object(Bucket=NOMBRE, Key=o["Key"])["Body"].read()))
    return sorted(r, key=lambda x: x["t"])


def espera_fin_latido(vm, hasta):
    """Devuelve el último latido, cuando lleva 15 s sin cambiar."""
    ultimo, visto = None, time.time()
    while time.time() < hasta:
        d = lee(vm, "latido")
        if d and (not ultimo or d["t"] != ultimo["t"]):
            ultimo, visto = d, time.time()
        if ultimo and time.time() - visto > 15:
            break
        time.sleep(1)
    return ultimo


def corte_ocupado(arn):
    print(f"\n--- 1. run ocupado hasta maxLifetime ({VIDA} s)", flush=True)
    s = mc.nueva()
    t0 = time.time()
    r = mc.llama(arn, s, {"accion": "larga", "segundos": VIDA * 3})
    vm = r["r"]["vm"]
    print("     lanzada:", json.dumps(r["r"]), f"en {r['s']} s", flush=True)
    u = espera_fin_latido(vm, t0 + VIDA + 180)
    edad = u["edad"] if u else 0
    sn = senales(vm)
    print(f"     último latido: edad {edad} s desde la primera invocación; señales: {json.dumps(sn)}", flush=True)
    anota("El corte llega a maxLifetime contado desde la primera invocación", abs(edad - VIDA) <= 30,
          f"edad al último latido {edad} s, maxLifetime {VIDA} s")
    anota("Con 5 min de margen el kernel acabaría antes del corte", edad >= VIDA - 300 + 5 or VIDA < 300,
          f"el corte llega {round(VIDA - edad, 1)} s antes o después del límite teórico")
    if sn:
        avisos = [round(u["t"] - x["t"], 1) for x in sn]
        print(f"     segundos entre cada señal y el último latido: {avisos}", flush=True)
    d = mc.llama(arn, s, {"accion": "informa"})
    print("     invocación tras el corte:", json.dumps({k: d.get(k) for k in ("ok", "s", "error", "http", "r")})[:300], flush=True)
    anota("Tras el corte, el mismo id arranca una microVM nueva", d["ok"] and d["r"].get("vm") != vm,
          f"{d.get('r')} {d.get('error')}")


def alrededor_del_corte(arn):
    print("\n--- 2. invocaciones cada 5 s alrededor del corte, y microVM sin run", flush=True)
    s = mc.nueva()
    t0 = time.time()
    r = mc.llama(arn, s, {"accion": "informa"})
    vm = r["r"]["vm"]
    filas = []
    # cada 60 s hasta acercarse: 'no se reinicia con las invocaciones' y que no caduque por inactividad
    while time.time() - t0 < VIDA - 30:
        mc.llama(arn, s, {"accion": "informa"})
        time.sleep(min(60, max(0.1, VIDA - 30 - (time.time() - t0))))
    while time.time() - t0 < VIDA + 30:
        c = time.time()
        d = mc.llama(arn, s, {"accion": "informa"})
        filas.append({"a_los": round(c - t0, 1), "ok": d["ok"], "s": d["s"], "err": d.get("error"), "http": d.get("http"),
                      "vm": (d.get("r") or {}).get("vm", "")[:6], "edad": (d.get("r") or {}).get("edad")})
        time.sleep(max(0, 5 - (time.time() - c)))
    for f in filas:
        print("    ", json.dumps(f), flush=True)
    nuevas = [f for f in filas if f["ok"] and f["vm"] and f["vm"] != vm[:6]]
    fallos = [f for f in filas if not f["ok"]]
    print(f"     vm inicial {vm[:6]}; primera nueva a los {nuevas[0]['a_los'] if nuevas else '-'} s; fallos: {len(fallos)}", flush=True)
    anota("Las invocaciones no reinician maxLifetime", bool(nuevas) and abs(nuevas[0]["a_los"] - VIDA) <= 35,
          f"primera en microVM nueva a los {nuevas[0]['a_los'] if nuevas else '-'} s")
    print(f"     señales de la microVM desocupada: {json.dumps(senales(vm))}", flush=True)


def limpieza(arn):
    print("\n--- 3. limpieza en una microVM real", flush=True)
    s = mc.nueva()
    mc.llama(arn, s, {"accion": "informa"})
    peor = 0
    for caso in ("nada", "procesos_20", "procesos_200", "saltos", "ficheros_10000", "megas_500"):
        v = []
        for _ in range(3):
            d = mc.llama(arn, s, {"accion": "limpieza", "caso": caso})
            if not d["ok"]:
                print("     fallo:", caso, d.get("error"), d.get("msg"), flush=True)
                continue
            v.append(d["r"])
        tot = [x["limpieza"]["total_s"] for x in v]
        print(f"     {caso}: antes={[x['antes'] for x in v]} total={tot} matar={[x['limpieza']['matar_s'] for x in v]} "
              f"borrar={[x['limpieza']['borrar_s'] for x in v]} quedan={[x['limpieza']['quedan'] for x in v]}", flush=True)
        if tot:
            peor = max(peor, max(tot))
            anota(f"Limpieza {caso}: no queda nada", not any(x["limpieza"]["quedan"] for x in v),
                  f"mediana {statistics.median(tot)} s, máx {max(tot)} s")
    anota("La limpieza cabe en el tope de 5 s", 0 < peor <= 5, f"peor caso {peor} s")


def probar(cuenta):
    mc.KERNEL, mc.ESPIA = KERNEL, "# sin espía\n"
    uri = mc.imagen()
    rol_arn = ri.rol(cuenta)
    s3.create_bucket(Bucket=NOMBRE, CreateBucketConfiguration={"LocationConstraint": REGION}) if REGION != "us-east-1" \
        else s3.create_bucket(Bucket=NOMBRE)
    creado["bucket"] = NOMBRE
    time.sleep(15)
    r = ctl.create_agent_runtime(
        agentRuntimeName=NOMBRE_, roleArn=rol_arn, platformVersion="V2",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
        networkConfiguration={"networkMode": "PUBLIC"}, protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables={"BUCKET": NOMBRE},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 120, "maxLifetime": VIDA}, tags=TAGS_DICT)
    rid, arn = r["agentRuntimeId"], r["agentRuntimeArn"]
    creado.setdefault("runtimes", []).append(rid)
    print("runtime", rid, "preparando la instantánea…", flush=True)
    base.espera_estado(lambda: ctl.get_agent_runtime(agentRuntimeId=rid))
    hilos = [threading.Thread(target=f, args=(arn,)) for f in (corte_ocupado, alrededor_del_corte)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    limpieza(arn)
    for s in mc.SESIONES:
        try:
            dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=s)
        except ClientError:
            pass


if __name__ == "__main__":
    try:
        cuenta = base.sts.get_caller_identity()["Account"]
        print("cuenta ok ·", REGION, "·", NOMBRE, flush=True)
        probar(cuenta)
    finally:
        mc.borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
