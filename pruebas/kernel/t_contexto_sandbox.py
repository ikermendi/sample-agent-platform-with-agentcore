import asyncio, json, os, pwd, resource, tempfile, glob
import pytest
from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.sandbox.types import ExecutionResult, StreamChunk
from strands.sandbox.errors import SandboxTimeoutError
from strands.vended_tools.shell import shell
from fake import FakeModel, texto, herramienta


# ---------------- Contexto: modo automático con stash duradero ----------------

@tool
def buscar_documentos(q: str) -> str:
    """Devuelve un resultado enorme."""
    return "\n".join(f"Línea {i}: dato importante número {i} sobre {q}." for i in range(1500))  # ~60 KB


def test_modo_auto_recorta_resultado_grande_y_lo_guarda_en_el_stash_duradero():
    d = tempfile.mkdtemp()
    almac = LocalFileStorage(d)
    m = FakeModel([herramienta("buscar_documentos", {"q": "ventas"}), texto("resumen"), texto("otra")])
    a = Agent(model=m, tools=[buscar_documentos], callback_handler=None, context_manager="auto", storage=almac,
              session_manager=SnapshotSessionManager("chat1", storage=almac, save_latest_on="message"))
    a("busca")
    a("y ahora?")
    herramientas_vistas = m.llamadas[0]["herramientas"]
    # Lo que el modelo recibió en la última llamada como resultado de la herramienta
    ultima = json.dumps(m.llamadas[-1]["mensajes"], ensure_ascii=False)
    snap = glob.glob(f"{d}/**/snapshot_latest.json", recursive=True)
    stash = [p for p in glob.glob(f"{d}/**/*", recursive=True) if os.path.isfile(p) and "context" in p]
    tam_snap = os.path.getsize(snap[0]) if snap else None
    print("herramientas ofrecidas:", herramientas_vistas)
    print("tamaño del resultado enviado al modelo (chars):", len(ultima), "| ¿lleva la línea 1499?", "Línea 1499" in ultima)
    print("snapshot:", [os.path.relpath(p, d) for p in snap], "bytes:", tam_snap)
    print("ficheros del stash:", len(stash), [os.path.relpath(p, d) for p in stash[:3]])
    resultados = [b["toolResult"]["content"][0]["text"] for msg in m.llamadas[-1]["mensajes"]
                  for b in msg["content"] if "toolResult" in b]
    print("resultado enviado al modelo:", len(resultados[0]), "chars, empieza por", repr(resultados[0][:40]))
    print("entradas del stash (una por bloque):", len(stash), "| la mayor:", max(os.path.getsize(p) for p in stash), "bytes")
    assert "retrieve_context" in herramientas_vistas
    assert resultados[0].startswith("[Truncated") and len(resultados[0]) < 5000   # recortado (78.779 -> ~3.100)
    assert max(os.path.getsize(p) for p in stash) > 70000                          # el original, en el stash
    assert tam_snap < 10000 and "Línea 700:" not in open(snap[0]).read()           # el snapshot no lo lleva
    assert len(stash) >= 4                                                         # también cada mensaje de texto


# ---------------- Graph: nodos con snapshot ----------------

def test_graph_rechaza_nodos_con_session_manager():
    from strands.multiagent import GraphBuilder
    d = tempfile.mkdtemp()
    nodo = Agent(model=FakeModel([]), name="arbitro", callback_handler=None,
                 session_manager=SnapshotSessionManager("chat1", storage=LocalFileStorage(d)))
    b = GraphBuilder()
    with pytest.raises(ValueError) as e:
        b.add_node(nodo, "arbitro")
    print("Graph con nodo con snapshot:", e.value)


# ---------------- Shell local ----------------

def test_shell_por_defecto_hereda_el_entorno_y_el_timeout_lo_pone_el_modelo():
    os.environ["SECRETO_DEL_KERNEL"] = "credencial-de-prueba"
    m = FakeModel([herramienta("shell", {"command": "echo valor=$SECRETO_DEL_KERNEL", "timeout": 99999}), texto("fin")])
    a = Agent(model=m, tools=[shell], callback_handler=None)
    a("ejecuta")
    salida = json.dumps(a.messages, ensure_ascii=False)
    print("¿la shell ve la variable del kernel?", "valor=credencial-de-prueba" in salida)
    print("sandbox por defecto:", type(a.sandbox).__name__)
    assert "valor=credencial-de-prueba" in salida


class SandboxPlataforma(NotASandboxLocalEnvironment):
    """Prototipo del Sandbox propio del diseño: entorno limpio, límites y timeout fijo."""
    TIMEOUT = 2
    MEM = 256 * 1024 * 1024
    NPROC = 20

    def __init__(self, usuario=None):
        super().__init__()
        self.usuario = usuario

    async def execute_streaming(self, command, *, timeout=None, cwd=None, env=None, **kw):
        uid = pwd.getpwnam(self.usuario).pw_uid if self.usuario else None
        def limites():
            resource.setrlimit(resource.RLIMIT_AS, (self.MEM, self.MEM))
            resource.setrlimit(resource.RLIMIT_NPROC, (self.NPROC, self.NPROC))
            resource.setrlimit(resource.RLIMIT_CPU, (self.TIMEOUT + 1, self.TIMEOUT + 1))
            if uid is not None:
                os.setgid(uid); os.setuid(uid)
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", command, cwd=cwd or tempfile.gettempdir(),
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},  # sin credenciales ni AWS_*
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=limites)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.TIMEOUT)  # ignora el del modelo
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
            raise SandboxTimeoutError(f"más de {self.TIMEOUT} s")
        o, e = out.decode(errors="replace"), err.decode(errors="replace")
        if o: yield StreamChunk(data=o)
        yield ExecutionResult(exit_code=proc.returncode, stdout=o, stderr=e)


async def _run(sb, cmd):
    res = None
    try:
        async for c in sb.execute_streaming(cmd, timeout=99999):
            res = c
    except SandboxTimeoutError as ex:
        return ("timeout", str(ex))
    return (res.exit_code, (res.stdout + res.stderr)[-200:])


def test_sandbox_propio_entorno_limpio_memoria_y_timeout_fijo():
    os.environ["SECRETO_DEL_KERNEL"] = "credencial-de-prueba"
    sb = SandboxPlataforma()
    env = asyncio.run(_run(sb, "echo valor=$SECRETO_DEL_KERNEL; env | grep -c AWS_ || true"))
    mem = asyncio.run(_run(sb, "python3 -c \"x = bytearray(1024*1024*1024); print('reservado')\""))
    tiempo = asyncio.run(_run(sb, "sleep 10"))
    print("entorno:", env)
    print("1 GB de memoria con límite de 256 MB:", mem)
    print("sleep 10 con timeout del modelo 99999:", tiempo)
    assert "credencial-de-prueba" not in env[1] and env[1].strip().endswith("0")
    assert "MemoryError" in mem[1] and mem[0] != 0
    assert tiempo[0] == "timeout"


class SandboxLento(SandboxPlataforma):
    TIMEOUT = 8


def test_limite_de_procesos_solo_vale_con_otro_usuario():
    # Cuenta cuántos procesos hijos consigue crear (de 60 pedidos) con NPROC = 20
    py = ("import os,time\nn=0\nfor i in range(60):\n    try:\n        pid=os.fork()\n    except OSError:\n        break\n"
          "    if pid==0:\n        time.sleep(1); os._exit(0)\n    n+=1\nprint(n)")
    cmd = f"/usr/bin/python3 -c '{py}'"
    como_yo = asyncio.run(_run(SandboxLento(), cmd))
    print("usuario:", pwd.getpwuid(os.getuid()).pw_name, "| hijos creados con NPROC=20:", como_yo[1].strip())
    if os.getuid() == 0:
        como_nobody = asyncio.run(_run(SandboxLento(usuario="nobody"), cmd))
        print("como nobody, hijos creados con NPROC=20:", como_nobody[1].strip())
        assert int(como_yo[1].strip()) == 60          # root ignora RLIMIT_NPROC
        assert int(como_nobody[1].strip()) < 20       # con otro usuario sí se limita


def test_otro_usuario_no_lee_entorno_ni_memoria_del_kernel():
    if os.getuid() != 0:
        pytest.skip("hace falta root para cambiar de usuario")
    os.environ["SECRETO_DEL_KERNEL"] = "credencial-de-prueba"
    yo = os.getpid()
    sb = SandboxLento(usuario="nobody")
    env = asyncio.run(_run(sb, f"cat /proc/{yo}/environ"))
    mem = asyncio.run(_run(sb, f"head -c 10 /proc/{yo}/mem"))
    print("environ del kernel como nobody:", env, "| mem:", mem)
    assert "credencial-de-prueba" not in env[1] and "Permission denied" in env[1]
    assert "Permission denied" in mem[1]
