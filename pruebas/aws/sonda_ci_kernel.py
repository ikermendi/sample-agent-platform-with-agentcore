"""Sonda 11 del nivel 2: el kernel con Code Interpreter de punta a punta, y ficheros grandes.

Resuelve la fila "Code Interpreter" de "Por verificar" en docs/arquitectura-agentes.md
("Ficheros del chat (kernel y Code Interpreter)" y "Ejecución de código del modelo"):

  1. Un `CodeInterpreterSandbox` propio (un `Sandbox` de Strands 1.63 sobre el cliente `CodeInterpreter`
     del SDK de AgentCore 1.25.2; Strands no trae uno) con la sincronización del documento: el kernel lleva
     la lista de lo que tiene la sesión (tamaño y SHA-256), antes de cada ejecución sube solo lo cambiado
     con `writeFiles` en lotes por debajo del límite por petición, y después lista la sesión y se trae lo
     nuevo o cambiado.
  2. Un `Agent` de Strands con ese Sandbox, `SequentialToolExecutor` y las herramientas `python_repl` y
     `shell` de Strands, con un modelo falso que pide leer un CSV adjunto de `entrada/` y generar un PDF en
     `salida/`: el kernel lo detecta como nuevo, lo baja y emite el evento `fichero` (aquí, una lista).
     Con `--haiku`, otra pasada con Claude Haiku 4.5 real en vez del modelo falso.
  3. Ficheros grandes (10, 20, 50 y 100 MB): subir (`writeFiles` en una petición, en trozos + `cat`, en
     paralelo) y bajar (`readFiles` directo, `readFiles` por trozos hechos con `split`, varios trozos en una
     llamada, `executeCommand` con `base64` por la salida estándar, y `readFiles` quitando el tope de 24 MiB
     del cliente de botocore).
  4. Con `--efs`, la otra vía documentada (`filesystemConfigurations` de CreateCodeInterpreter): un EFS
     montado en un segundo Code Interpreter de la misma VPC sin DNS.

Crea con boto3 su propia VPC sin DNS (como la sonda 5, reutilizando sus funciones) y su Code Interpreter;
prefijo "sonda-n2-", etiqueta sonda=nivel2; para la sesión (`StopCodeInterpreterSession`) y lo borra todo al
acabar aunque falle. Si queda la VPC: `sonda_agentcore.py --borrar-restos`. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_ci_kernel.py [--haiku] [--efs]

Requiere strands-agents==1.63.0 y bedrock-agentcore==1.25.2 en .venv312.
"""
import asyncio
import concurrent.futures as cf
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

import boto3
import botocore.eventstream
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel"))
import sonda_agentcore as s5  # noqa: E402  (VPC sin DNS, espera_estado, borrar_vpc, anota)
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter  # noqa: E402
from fake import FakeModel, herramienta, texto  # noqa: E402
from strands import Agent  # noqa: E402
from strands.hooks import AfterToolCallEvent  # noqa: E402
from strands.sandbox.base import Sandbox  # noqa: E402
from strands.sandbox.errors import SandboxTimeoutError  # noqa: E402
from strands.sandbox.types import ExecutionResult, FileInfo, StreamChunk  # noqa: E402
from strands.tools.executors import SequentialToolExecutor  # noqa: E402
from strands.vended_tools.python_repl import python_repl  # noqa: E402
from strands.vended_tools.shell import shell  # noqa: E402

REGION, NOMBRE, NOMBRE_, TAGS, TAGS_DICT = s5.REGION, s5.NOMBRE, s5.NOMBRE_, s5.TAGS, s5.TAGS_DICT
ses = s5.ses
ec2, ctl, dp, efs = s5.ec2, s5.ctl, s5.dp, ses.client("efs")
anota, codigo, resultados = s5.anota, s5.codigo, s5.resultados
creado = s5.creado  # vpcs, ci (el principal); lo demás, aquí
extra = {"cis": [], "sesiones": []}
HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
MB = 1024 * 1024
LOTE = 60 * MB  # por petición writeFiles (blob en base64 dentro del JSON: 60 MB -> ~80 MB de cuerpo)
TROZO = 16 * MB  # por readFiles: el cliente de botocore corta los eventos de más de 24 MiB (base64)
CARPETAS = ("entrada", "trabajo", "salida")


def sha(b):
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------------------------------
# El Sandbox propio
# --------------------------------------------------------------------------------------------------

class CodeInterpreterSandbox(Sandbox):
    """`Sandbox` de Strands sobre una sesión de Code Interpreter, con la sincronización del documento.

    `local` es la copia local del espacio del chat (entrada/, trabajo/, salida/) que el kernel sincroniza
    con S3. `remoto` es la lista del kernel de lo que tiene la sesión: ruta -> (tamaño, sha256).
    Tras cada ejecución, lo nuevo o cambiado de salida/ queda en `nuevos` para que el kernel lo suba a S3 y
    emita el evento `fichero`.
    """

    def __init__(self, ci: CodeInterpreter, local: str):
        self.ci, self.local = ci, local
        self.remoto: dict[str, tuple[int, str]] = {}
        self.nuevos: list[dict] = []
        self.traza: list[tuple] = []  # (operación, detalle) para la sonda

    # ---- llamadas a la API ----
    def _llamar(self, nombre, args):
        r = self.ci.invoke(nombre, args)
        res = []
        for ev in r["stream"]:
            if "result" in ev:
                res.append(ev["result"])
            else:
                raise RuntimeError(f"{nombre}: {ev}")
        return res

    def _comando(self, cmd):
        res = self._llamar("executeCommand", {"command": cmd})
        sc = res[-1].get("structuredContent") or {}
        return sc.get("exitCode", 1 if res[-1].get("isError") else 0), sc.get("stdout") or "", sc.get("stderr") or ""

    async def _a(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    # ---- sincronización ----
    def subir(self):
        """Sube lo que ha cambiado en local desde la última vez, en lotes por debajo del límite por petición."""
        pendientes = []
        for carpeta in CARPETAS:
            for raiz, _, fichs in os.walk(os.path.join(self.local, carpeta)):
                for f in fichs:
                    p = os.path.join(raiz, f)
                    rel = os.path.relpath(p, self.local)
                    datos = open(p, "rb").read()
                    if self.remoto.get(rel) != (len(datos), sha(datos)):
                        pendientes.append((rel, datos))
        lote, tam, llamadas = [], 0, 0
        for rel, datos in pendientes + [(None, b"")]:
            if rel is None or (lote and tam + len(datos) > LOTE):
                if lote:
                    self._llamar("writeFiles", {"content": [{"path": r, "blob": d} for r, d in lote]})
                    llamadas += 1
                    for r, d in lote:
                        self.remoto[r] = (len(d), sha(d))
                lote, tam = [], 0
            if rel is not None:
                if len(datos) > LOTE:  # uno solo no cabe: por trozos y `cat` dentro
                    subir_grande(self, rel, datos)
                    self.remoto[rel] = (len(datos), sha(datos))
                    llamadas += 1
                else:
                    lote.append((rel, datos))
                    tam += len(datos)
        self.traza.append(("subir", [r for r, _ in pendientes], llamadas))
        return len(pendientes)

    def estado_sesion(self):
        """Tamaño y SHA-256 de todo lo que hay en la sesión, en una sola llamada (listFiles no da hash)."""
        cmd = ("for d in " + " ".join(CARPETAS) + "; do [ -d $d ] && find $d -type f -not -path '*/.partes/*' "
               "-exec sh -c 'printf \"%s\\t\" $(stat -c %s \"$1\"); sha256sum \"$1\"' _ {} \\; ; done; true")
        _, out, _ = self._comando(cmd)
        estado = {}
        for linea in out.splitlines():
            t, resto = linea.split("\t", 1)
            h, ruta = resto.split(None, 1)
            estado[ruta.strip()] = (int(t), h)
        return estado

    def bajar(self):
        """Trae de la sesión lo nuevo o cambiado (por tamaño y hash) y lo deja en `nuevos` si es de salida/."""
        traidos = []
        for rel, (t, h) in self.estado_sesion().items():
            if self.remoto.get(rel) == (t, h):
                continue
            datos = bajar_grande(self, rel, t) if t > TROZO else self._leer(rel)
            assert sha(datos) == h, f"hash distinto al bajar {rel}"
            destino = os.path.join(self.local, rel)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            open(destino, "wb").write(datos)
            self.remoto[rel] = (t, h)
            traidos.append(rel)
            if rel.startswith("salida/"):
                self.nuevos.append({"ruta": rel, "tamaño": t, "sha256": h})
        self.traza.append(("bajar", traidos))
        return traidos

    def _leer(self, rel):
        for r in self._llamar("readFiles", {"paths": [rel]}):
            for c in r.get("content") or []:
                res = c.get("resource") or {}
                if "blob" in res:
                    return res["blob"]
                if "text" in res:
                    return res["text"].encode()
        raise FileNotFoundError(rel)

    # ---- primitivas de Strands ----
    def _envolver(self, cmd, timeout, cwd, env):
        if env:
            cmd = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " " + cmd
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        if timeout:  # la API no tiene timeout por llamada: coreutils `timeout` dentro de la sesión
            cmd = f"timeout -k 5 {int(timeout)} sh -c {shlex.quote(cmd)}"
        return cmd

    async def execute_streaming(self, command, *, timeout=None, cwd=None, env=None, **kw):
        await self._a(self.subir)
        codigo_salida, out, err = await self._a(self._comando, self._envolver(command, timeout, cwd, env))
        await self._a(self.bajar)
        if timeout and codigo_salida == 124:
            raise SandboxTimeoutError(timeout, out, err)
        if out:
            yield StreamChunk(data=out)
        if err:
            yield StreamChunk(data=err, stream_type="stderr")
        yield ExecutionResult(exit_code=codigo_salida, stdout=out, stderr=err)

    async def execute_code_streaming(self, code, language, *, timeout=None, cwd=None, env=None, **kw):
        # Como el python_repl de Strands: un intérprete nuevo por llamada (no el contexto de executeCode)
        interprete = "python3" if language.startswith("python") else language
        cmd = f"{interprete} - <<'__FIN_DEL_CODIGO__'\n{code}\n__FIN_DEL_CODIGO__"
        async for c in self.execute_streaming(cmd, timeout=timeout, cwd=cwd, env=env):
            yield c

    async def read_file(self, path, **kw):
        return await self._a(self._leer, path)

    async def write_file(self, path, content, **kw):
        await self._a(self._llamar, "writeFiles", {"content": [{"path": path, "blob": content}]})

    async def remove_file(self, path, **kw):
        await self._a(self._llamar, "removeFiles", {"paths": [path]})

    async def list_files(self, path, **kw):
        res = await self._a(self._llamar, "listFiles", {"directoryPath": path})
        fichs = []
        for r in res:
            for c in r.get("content") or []:
                if c.get("type") == "resource_link" or "name" in c:
                    fichs.append(FileInfo(name=c.get("name") or c.get("uri", "").rsplit("/", 1)[-1],
                                          is_dir=(c.get("mimeType") == "inode/directory") or None,
                                          size=c.get("size")))
        return fichs


# --------------------------------------------------------------------------------------------------
# Ficheros grandes
# --------------------------------------------------------------------------------------------------

def subir_grande(sb, rel, datos, trozo=50 * MB, hilos=1):
    partes = [datos[i:i + trozo] for i in range(0, len(datos), trozo)]
    nombres = [f".partes/{os.path.basename(rel)}.{i:04d}" for i in range(len(partes))]
    with cf.ThreadPoolExecutor(hilos) as ex:
        list(ex.map(lambda np: sb._llamar("writeFiles", {"content": [{"path": np[0], "blob": np[1]}]}),
                    zip(nombres, partes)))
    c, _, err = sb._comando(f"mkdir -p {shlex.quote(os.path.dirname(rel) or '.')} && cat {' '.join(nombres)} > "
                            f"{shlex.quote(rel)} && rm -f {' '.join(nombres)}")
    assert c == 0, err


def bajar_grande(sb, rel, tam, trozo=TROZO, hilos=4, por_llamada=1):
    c, _, err = sb._comando(f"mkdir -p .partes && rm -f .partes/b.* && split -b {trozo} -d -a 4 "
                            f"{shlex.quote(rel)} .partes/b.")
    assert c == 0, err
    n = (tam + trozo - 1) // trozo
    nombres = [f".partes/b.{i:04d}" for i in range(n)]
    grupos = [nombres[i:i + por_llamada] for i in range(0, n, por_llamada)]

    def leer(grupo):
        trozos = {}
        for r in sb._llamar("readFiles", {"paths": grupo}):
            for cc in r.get("content") or []:
                res = cc.get("resource") or {}
                if "blob" in res:
                    trozos[res.get("uri", "").replace("file://", "").rsplit("/", 1)[-1]] = res["blob"]
        return trozos

    juntos = {}
    with cf.ThreadPoolExecutor(hilos) as ex:
        for t in ex.map(leer, grupos):
            juntos.update(t)
    sb._comando("rm -rf .partes")
    return b"".join(juntos[os.path.basename(x)] for x in nombres)


def bajar_base64(sb, rel, tam, trozo):
    import base64
    partes = []
    for i in range(0, tam, trozo):
        c, out, err = sb._comando(f"dd if={shlex.quote(rel)} bs={trozo} skip={i // trozo} count=1 status=none | base64 -w0")
        assert c == 0, err[:200]
        partes.append(base64.b64decode(out))
    return b"".join(partes)


def cronometro(fn, *a, **k):
    t0 = time.time()
    r = fn(*a, **k)
    return time.time() - t0, r


def probar_grandes(sb):
    print("\n--- ficheros grandes ---", flush=True)
    # Subir
    for n, modo in ((10 * MB, "una"), (20 * MB, "una"), (50 * MB, "una"), (75_000_000, "una"), (95_000_000, "una"),
                    (100 * MB, "2x50 + cat"), (100 * MB, "4x25 en paralelo + cat")):
        datos = os.urandom(n)
        rel = f"trabajo/sube_{n}.bin"
        try:
            if modo == "una":
                t, _ = cronometro(sb._llamar, "writeFiles", {"content": [{"path": rel, "blob": datos}]})
            elif modo.startswith("2x50"):
                t, _ = cronometro(subir_grande, sb, rel, datos, 50 * MB, 1)
            else:
                t, _ = cronometro(subir_grande, sb, rel, datos, 25 * MB, 4)
            h = sb.estado_sesion().get(rel)
            anota(f"Subir {n:,} B ({modo})", h == (n, sha(datos)), f"{t:.1f} s, {n / MB / t:.1f} MB/s")
        except Exception as e:
            anota(f"Subir {n:,} B ({modo})", False, (codigo(e) + ": " + str(e))[:200])
        sb._comando(f"rm -f {rel}")

    # Generar en la sesión
    tams = {mb: mb * MB for mb in (10, 20, 50, 100)}
    sb._comando("mkdir -p salida && " + " && ".join(
        f"head -c {t} /dev/urandom > salida/g{mb}.bin" for mb, t in tams.items()))
    hashes = sb.estado_sesion()

    def ok(rel, datos):
        return hashes.get(rel) == (len(datos), sha(datos))

    for mb, t in tams.items():
        rel = f"salida/g{mb}.bin"
        try:
            d, datos = cronometro(sb._leer, rel)
            anota(f"readFiles directo, {mb} MB", ok(rel, datos), f"{d:.1f} s")
        except Exception as e:
            anota(f"readFiles directo, {mb} MB", False, (codigo(e) + ": " + str(e))[:160])
    for mb in (50, 100):
        rel, t = f"salida/g{mb}.bin", tams[mb]
        for hilos, por in ((1, 1), (4, 1), (8, 1), (4, 2)):
            try:
                d, datos = cronometro(bajar_grande, sb, rel, t, TROZO, hilos, por)
                anota(f"split + readFiles de 16 MB, {mb} MB, {hilos} hilo(s), {por} trozo(s) por llamada",
                      ok(rel, datos), f"{d:.1f} s, {mb / d:.1f} MB/s")
            except Exception as e:
                anota(f"split + readFiles de 16 MB, {mb} MB, {hilos} hilo(s), {por} trozo(s) por llamada", False,
                      (codigo(e) + ": " + str(e))[:160])
    # Tope del trozo con readFiles: justo por debajo y por encima de lo que cabe en 24 MiB en base64
    for tt in (17 * MB, 18 * MB):
        try:
            d, datos = cronometro(bajar_grande, sb, "salida/g50.bin", tams[50], tt, 4, 1)
            anota(f"split + readFiles con trozos de {tt // MB} MB", ok("salida/g50.bin", datos), f"{d:.1f} s")
        except Exception as e:
            anota(f"split + readFiles con trozos de {tt // MB} MB", False, (codigo(e) + ": " + str(e))[:160])
            sb._comando("rm -rf .partes")
    # executeCommand + base64 por la salida estándar
    for tt in (1 * MB, 4 * MB, 8 * MB, 12 * MB):
        try:
            d, datos = cronometro(bajar_base64, sb, "salida/g10.bin", tams[10], tt)
            anota(f"executeCommand + base64, trozos de {tt // MB} MB (10 MB)", ok("salida/g10.bin", datos),
                  f"{d:.1f} s, {10 / d:.1f} MB/s")
        except Exception as e:
            anota(f"executeCommand + base64, trozos de {tt // MB} MB (10 MB)", False, (codigo(e) + ": " + str(e))[:160])
    # readFiles quitando el tope de 24 MiB de botocore (¿lo envía el servicio?)
    viejo = botocore.eventstream._MAX_PAYLOAD_LENGTH
    botocore.eventstream._MAX_PAYLOAD_LENGTH = 1024 * MB
    try:
        for mb in (20, 50, 100):
            rel = f"salida/g{mb}.bin"
            try:
                d, datos = cronometro(sb._leer, rel)
                anota(f"readFiles directo sin el tope del cliente, {mb} MB", ok(rel, datos), f"{d:.1f} s")
            except Exception as e:
                anota(f"readFiles directo sin el tope del cliente, {mb} MB", False, (codigo(e) + ": " + str(e))[:160])
    finally:
        botocore.eventstream._MAX_PAYLOAD_LENGTH = viejo
    # La sincronización del kernel con un fichero de 100 MB nuevo en salida/
    sb.remoto = {k: v for k, v in hashes.items() if k != "salida/g100.bin"}
    d, traidos = cronometro(sb.bajar)
    anota("bajar() del kernel trae el de 100 MB por trozos", traidos == ["salida/g100.bin"]
          and os.path.getsize(os.path.join(sb.local, "salida/g100.bin")) == tams[100], f"{d:.1f} s")
    sb.nuevos.clear()
    sb._comando("rm -f salida/g*.bin")


# --------------------------------------------------------------------------------------------------
# El kernel (mínimo): Agent + Sandbox + evento `fichero`
# --------------------------------------------------------------------------------------------------

CSV = "mes,ventas\nenero,120\nfebrero,135\nmarzo,160\nabril,90\nmayo,210\n"
SCRIPT = r'''
import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import os
df = pd.read_csv("entrada/ventas.csv")
os.makedirs("trabajo", exist_ok=True); os.makedirs("salida", exist_ok=True)
df.plot(x="mes", y="ventas", kind="bar", legend=False); plt.tight_layout(); plt.savefig("trabajo/grafico.png")
c = canvas.Canvas("salida/informe.pdf", pagesize=A4)
c.drawString(72, 800, f"Ventas: total {df.ventas.sum()}, media {df.ventas.mean():.1f}")
c.drawImage("trabajo/grafico.png", 72, 400, width=450, height=340)
c.save()
print("filas", len(df), "total", df.ventas.sum())
'''


def kernel(sb, modelo, prompt, eventos):
    def al_acabar_herramienta(ev: AfterToolCallEvent):
        # Lo que el kernel hace tras cada herramienta: subir a S3 lo nuevo de salida/ (aquí, ya está en local),
        # registrarlo en DynamoDB y emitir un evento `fichero` por fichero.
        for f in sb.nuevos:
            eventos.append({"tipo": "fichero", "nombre": os.path.basename(f["ruta"]), "ruta": f["ruta"],
                            "tamaño": f["tamaño"], "sha256": f["sha256"],
                            "clave": f"espacios/usuario/chat/{f['ruta']}", "herramienta": ev.tool_use["name"]})
        sb.nuevos.clear()

    a = Agent(model=modelo, sandbox=sb, tools=[python_repl, shell], tool_executor=SequentialToolExecutor(),
              callback_handler=None,
              system_prompt="Ejecutas código en un sandbox. Los adjuntos del usuario están en entrada/ (solo lectura); "
                            "lo intermedio va a trabajo/ y lo que se entrega, a salida/. Rutas relativas.")
    a.add_hook(al_acabar_herramienta, AfterToolCallEvent)
    t0 = time.time()
    r = a(prompt)
    return time.time() - t0, a, r


def probar_kernel(cid, haiku):
    ci = CodeInterpreter(REGION, session=ses)
    t0 = time.time()
    sid = ci.start(identifier=cid, name="sonda-kernel", session_timeout_seconds=3600)
    extra["sesiones"].append((cid, sid))
    anota("Sesión de Code Interpreter (SDK 1.25.2, sessionTimeoutSeconds=3600)", True, f"en {time.time() - t0:.1f} s")
    local = tempfile.mkdtemp(prefix="sonda-ci-")
    os.makedirs(os.path.join(local, "entrada"))
    open(os.path.join(local, "entrada", "ventas.csv"), "w").write(CSV)
    sb = CodeInterpreterSandbox(ci, local)

    # Dónde caen las rutas relativas y qué da listFiles
    sb._llamar("writeFiles", {"content": [{"path": "trabajo/donde.txt", "text": "x"}]})
    c, out, err = sb._comando("pwd; ls -la trabajo; whoami; command -v python3 timeout split base64 sha256sum")
    print("     " + (out + err).replace("\n", "\n     "), flush=True)
    anota("writeFiles relativo y executeCommand comparten directorio", "donde.txt" in out, out.splitlines()[0])
    lf = sb._llamar("listFiles", {"directoryPath": "trabajo"})
    print("     listFiles(trabajo):", json.dumps(lf, default=str)[:600], flush=True)
    lf2 = sb._llamar("listFiles", {"directoryPath": ""})
    print("     listFiles(''):", json.dumps(lf2, default=str)[:400], flush=True)
    sb._comando("rm -rf trabajo")

    # Pasada con el modelo falso
    eventos = []
    guion = [herramienta("python_repl", {"code": SCRIPT, "timeout": 120}),
             herramienta("shell", {"command": "ls -la salida trabajo entrada"}),
             texto("He generado salida/informe.pdf.")]
    m = FakeModel(guion)
    t, a, r = kernel(sb, m, "Hazme un informe en PDF de entrada/ventas.csv", eventos)
    print("     traza de la sincronización:", sb.traza, flush=True)
    print("     eventos:", eventos, flush=True)
    resultado_repl = json.dumps(a.messages, ensure_ascii=False)
    anota("python_repl en Code Interpreter: lee el CSV de entrada/ (copiado por el kernel)",
          "filas 5 total 715" in resultado_repl, f"turno entero {t:.1f} s")
    subidas = [x for x in sb.traza if x[0] == "subir"]
    anota("Primera ejecución: sube el adjunto (y solo él)", subidas[0][1] == ["entrada/ventas.csv"], str(subidas[0]))
    anota("Siguiente ejecución: no vuelve a subir nada", subidas[1][1] == [], str(subidas[1]))
    pdf = os.path.join(local, "salida", "informe.pdf")
    es_pdf = os.path.exists(pdf) and open(pdf, "rb").read(5) == b"%PDF-"
    anota("El kernel baja salida/informe.pdf (y trabajo/grafico.png)", es_pdf and
          os.path.exists(os.path.join(local, "trabajo", "grafico.png")), str([x for x in sb.traza if x[0] == "bajar"][0]))
    ev = [e for e in eventos if e["ruta"] == "salida/informe.pdf"]
    anota("Evento `fichero` del PDF, uno solo, tras la herramienta python_repl",
          len(eventos) == 1 and len(ev) == 1 and ev[0]["herramienta"] == "python_repl"
          and ev[0]["tamaño"] == os.path.getsize(pdf), json.dumps(ev[0] if ev else eventos, ensure_ascii=False)[:300])
    # Segundo mensaje: el usuario cambia el adjunto; solo se sube ese, y el PDF regenerado da un evento nuevo
    open(os.path.join(local, "entrada", "ventas.csv"), "a").write("junio,300\n")
    open(os.path.join(local, "entrada", "otro.txt"), "w").write("nuevo adjunto")
    n_antes = len(sb.traza)
    m2 = FakeModel([herramienta("python_repl", {"code": SCRIPT, "timeout": 120}), texto("Actualizado.")])
    eventos2 = []
    kernel(sb, m2, "Otra vez con el CSV nuevo", eventos2)
    sub = [x for x in sb.traza[n_antes:] if x[0] == "subir"][0]
    anota("Adjunto cambiado y otro nuevo: sube solo esos dos", sorted(sub[1]) == ["entrada/otro.txt", "entrada/ventas.csv"],
          str(sub))
    anota("PDF regenerado (mismo nombre, otro contenido): nuevo evento `fichero`",
          len(eventos2) == 1 and eventos2[0]["sha256"] != ev[0]["sha256"], str([e["sha256"][:12] for e in eventos2]))
    # Timeout del modelo aplicado con coreutils dentro de la sesión
    try:
        asyncio.run(sb.execute("sleep 30", timeout=3))
        anota("Timeout de 3 s con `timeout` dentro de la sesión", False, "no cortó")
    except SandboxTimeoutError as e:
        anota("Timeout de 3 s con `timeout` dentro de la sesión", True, str(e)[:80])

    if haiku:
        from strands.models.bedrock import BedrockModel
        eventos3 = []
        modelo = BedrockModel(model_id=HAIKU, boto_session=ses, max_tokens=2000)
        t, a3, r3 = kernel(sb, modelo, "En entrada/ventas.csv tienes ventas por mes. Con la herramienta python_repl "
                                       "(pandas y reportlab están instalados), genera salida/resumen_haiku.pdf con una "
                                       "tabla de las ventas y el total. Responde en una frase.", eventos3)
        print("     Haiku:", str(r3)[:300], "| eventos:", eventos3, flush=True)
        anota("Claude Haiku 4.5 real: genera un PDF en salida/ y llega como evento `fichero`",
              any(e["ruta"].startswith("salida/") and e["ruta"].endswith(".pdf") for e in eventos3),
              f"{t:.1f} s, {[e['ruta'] for e in eventos3]}")

    probar_grandes(sb)
    shutil.rmtree(local, ignore_errors=True)


# --------------------------------------------------------------------------------------------------
# EFS montado en Code Interpreter (filesystemConfigurations)
# --------------------------------------------------------------------------------------------------

def preparar_efs(vpc_id, subredes, sg_ci):
    sg = ec2.create_security_group(GroupName=f"{NOMBRE}-efs", Description="sonda efs", VpcId=vpc_id)["GroupId"]
    s5.etiqueta([sg])
    extra["sg_efs"] = sg
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049, "UserIdGroupPairs": [{"GroupId": sg_ci}]}])
    fs = efs.create_file_system(CreationToken=NOMBRE, Encrypted=True, Tags=TAGS + [{"Key": "Name", "Value": NOMBRE}])
    fid = fs["FileSystemId"]
    extra["efs"] = fid
    while efs.describe_file_systems(FileSystemId=fid)["FileSystems"][0]["LifeCycleState"] != "available":
        time.sleep(5)
    for s in subredes:
        efs.create_mount_target(FileSystemId=fid, SubnetId=s, SecurityGroups=[sg])
    while any(m["LifeCycleState"] != "available" for m in efs.describe_mount_targets(FileSystemId=fid)["MountTargets"]):
        time.sleep(10)
    ap = efs.create_access_point(FileSystemId=fid, Tags=TAGS, RootDirectory={
        "Path": "/ci", "CreationInfo": {"OwnerUid": 0, "OwnerGid": 0, "Permissions": "777"}})
    while efs.describe_access_points(AccessPointId=ap["AccessPointId"])["AccessPoints"][0]["LifeCycleState"] != "available":
        time.sleep(3)
    return fs["FileSystemArn"], ap["AccessPointArn"]


def crear_ci(sufijo, subredes, sg, fsc=None):
    kw = {"filesystemConfigurations": fsc} if fsc else {}
    r = ctl.create_code_interpreter(name=NOMBRE_ + sufijo, description="sonda 11", tags=TAGS_DICT,
                                    networkConfiguration={"networkMode": "VPC", "vpcConfig": {
                                        "subnets": subredes, "securityGroups": [sg]}}, **kw)
    extra["cis"].append(r["codeInterpreterId"])
    return r["codeInterpreterId"]


def probar_efs(cid):
    try:
        s5.espera_estado(lambda: ctl.get_code_interpreter(codeInterpreterId=cid))
    except RuntimeError as e:
        anota("Code Interpreter con EFS en la VPC sin DNS: llega a READY", False, str(e)[:300])
        return
    anota("Code Interpreter con EFS en la VPC sin DNS: llega a READY", True)
    ci = CodeInterpreter(REGION, session=ses)
    try:
        sid = ci.start(identifier=cid, name="sonda-efs", session_timeout_seconds=900)
    except ClientError as e:
        anota("Sesión con EFS montado", False, codigo(e) + ": " + str(e)[:300])
        return
    extra["sesiones"].append((cid, sid))
    sb = CodeInterpreterSandbox(ci, tempfile.mkdtemp())
    c, out, err = sb._comando("mount | grep -i -e nfs -e efs; df -h /mnt/efs; "
                              "t0=$(date +%s.%N); head -c 104857600 /dev/urandom > /mnt/efs/g100.bin && sync; "
                              "t1=$(date +%s.%N); echo escribir100MB $(echo \"$t1 - $t0\" | bc); ls -la /mnt/efs")
    print("     " + (out + err).replace("\n", "\n     "), flush=True)
    anota("EFS montado y escribible en /mnt/efs desde la sesión", c == 0 and "g100.bin" in out, (out + err)[-200:])


# --------------------------------------------------------------------------------------------------

def borrar():
    print("borrando…", flush=True)
    for cid, sid in extra["sesiones"]:
        s5.intenta("sesión ci", lambda cid=cid, sid=sid: dp.stop_code_interpreter_session(
            codeInterpreterIdentifier=cid, sessionId=sid))
    for cid in extra["cis"]:
        s5.intenta("ci", lambda cid=cid: ctl.delete_code_interpreter(codeInterpreterId=cid))
    for cid in extra["cis"]:
        for _ in range(60):
            try:
                ctl.get_code_interpreter(codeInterpreterId=cid)
                time.sleep(10)
            except ClientError:
                break
    if "efs" in extra:
        fid = extra["efs"]
        for ap in efs.describe_access_points(FileSystemId=fid).get("AccessPoints", []):
            s5.intenta("ap", lambda ap=ap: efs.delete_access_point(AccessPointId=ap["AccessPointId"]))
        for m in efs.describe_mount_targets(FileSystemId=fid)["MountTargets"]:
            s5.intenta("mt", lambda m=m: efs.delete_mount_target(MountTargetId=m["MountTargetId"]))
        while efs.describe_mount_targets(FileSystemId=fid)["MountTargets"]:
            time.sleep(10)
        s5.intenta("efs", lambda: efs.delete_file_system(FileSystemId=fid))
    for v in creado.get("vpcs", []):
        s5.borrar_vpc(v)


if __name__ == "__main__":
    con_efs = "--efs" in sys.argv
    try:
        print("cuenta", s5.sts.get_caller_identity()["Account"], "·", REGION, "·", NOMBRE, flush=True)
        vpc_id, subredes, sg = s5.vpc("10.82.0.0/16", False, con_s3=False)
        t0 = time.time()
        cid = crear_ci("_ci", subredes, sg)
        cid_efs = None
        if con_efs:
            try:
                fs_arn, ap_arn = preparar_efs(vpc_id, subredes, sg)
                cid_efs = crear_ci("_efs", subredes, sg, [{"efsConfiguration": {
                    "fileSystemArn": fs_arn, "accessPointArn": ap_arn, "mountPath": "/mnt/efs"}}])
            except ClientError as e:
                anota("Code Interpreter con EFS: se crea", False, codigo(e) + ": " + str(e)[:300])
        s5.espera_estado(lambda: ctl.get_code_interpreter(codeInterpreterId=cid))
        anota("Code Interpreter en VPC sin DNS: READY", True, f"en {time.time() - t0:.0f} s")
        probar_kernel(cid, "--haiku" in sys.argv)
        if cid_efs:
            probar_efs(cid_efs)
    finally:
        borrar()
    fallos = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
