"""Contexto de un subagente as_tool(preserve_context=True): dónde queda su stash y qué guarda su snapshot,
con el modo automático y el S3Storage del principal (opción elegida), sin storage y sin gestor de contexto."""
import glob, json, os, re, tempfile
from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from fake import FakeModel, texto, herramienta


@tool
def buscar_documentos(q: str) -> str:
    """Devuelve un resultado enorme."""
    return "\n".join(f"Línea {i}: dato importante número {i} sobre {q}." for i in range(1500))  # ~78 KB


class GuionVivo(FakeModel):
    """Como FakeModel, pero un elemento del guion puede ser una función que recibe los mensajes."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.guion and callable(self.guion[0]):
            self.guion[0] = self.guion[0](messages)
        async for ev in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield ev


def pedir_lo_recortado(messages):
    ref = re.search(r"\[ref: ([^\]]+)\]", json.dumps(messages, ensure_ascii=False)).group(1)
    return herramienta("retrieve_context", {"reference": ref})


def montar(almac, m_principal, m_revisor, *, cm_revisor="auto", storage_revisor=True):
    """Lo que haría el kernel al montar el Agent del chat: principal y subagente, cada uno con su snapshot."""
    revisor = Agent(model=m_revisor, agent_id="revisor", name="revisor", description="Revisa documentos.",
                    tools=[buscar_documentos], callback_handler=None, context_manager=cm_revisor,
                    storage=almac if storage_revisor else None,
                    session_manager=SnapshotSessionManager("chat1", storage=almac, save_latest_on="message"))
    principal = Agent(model=m_principal, agent_id="principal", tools=[revisor.as_tool(preserve_context=True)],
                      callback_handler=None, context_manager="auto", storage=almac,
                      session_manager=SnapshotSessionManager("chat1", storage=almac, save_latest_on="message"))
    return principal, revisor


def ficheros(d, trozo):
    return {os.path.relpath(p, d): os.path.getsize(p) for p in glob.glob(f"{d}/**/*", recursive=True)
            if os.path.isfile(p) and trozo in os.path.relpath(p, d)}


def snapshot(d, agente):
    p = glob.glob(f"{d}/**/scopes/agent/{agente}/snapshots/snapshot_latest.json", recursive=True)
    return open(p[0]).read() if p else None


def test_opcion_3_stash_del_subagente_en_su_prefijo_y_se_recupera_al_restaurar():
    d = tempfile.mkdtemp()
    almac = LocalFileStorage(d)
    principal, _ = montar(almac,
                          FakeModel([herramienta("revisor", {"input": "revisa ventas"}), texto("hecho")]),
                          FakeModel([herramienta("buscar_documentos", {"q": "ventas"}), texto("tres errores")]))
    principal("revisa las ventas")

    stash_rev = ficheros(d, "context/chat1/scopes/agent/revisor/")
    stash_pri = ficheros(d, "context/chat1/scopes/agent/principal/")
    snap_rev = snapshot(d, "revisor")
    print("stash del revisor:", len(stash_rev), "entradas, la mayor", max(stash_rev.values()), "bytes")
    print("stash del principal:", len(stash_pri), "entradas, la mayor", max(stash_pri.values(), default=0), "bytes")
    print("snapshot del revisor:", len(snap_rev), "bytes")
    assert max(stash_rev.values()) > 70000                       # el original, en el stash del revisor
    assert max(stash_pri.values(), default=0) < 70000            # no en el del principal
    assert len(snap_rev) < 10000 and "Línea 700:" not in snap_rev

    # Nueva microVM: se monta todo desde S3 y el revisor recupera lo recortado con retrieve_context
    m_rev = GuionVivo([pedir_lo_recortado, texto("la línea 1499 confirma el error")])
    principal2, _ = montar(almac, FakeModel([herramienta("revisor", {"input": "¿y la última línea?"}), texto("ok")]), m_rev)
    principal2("mira la última línea")
    ofrecidas = m_rev.llamadas[0]["herramientas"]
    recuperado = json.dumps(m_rev.llamadas[-1]["mensajes"], ensure_ascii=False)
    print("herramientas del revisor tras restaurar:", ofrecidas)
    print("¿recupera la línea 1499?", "Línea 1499:" in recuperado)
    assert "retrieve_context" in ofrecidas and "buscar_documentos" in ofrecidas
    assert "Línea 1499:" in recuperado
    # Lo recuperado no se vuelve a recortar (Offload salta los resultados de retrieve_context): se queda
    # en la conversación, y en el snapshot, hasta que un resumen lo quite
    snap2 = snapshot(d, "revisor")
    print("snapshot del revisor tras recuperar:", len(snap2), "bytes | ¿lleva lo recuperado?", "Línea 1499:" in snap2)
    assert "Línea 1499:" in snap2 and len(snap2) > 30000


def test_opcion_2_sin_storage_el_stash_va_dentro_del_snapshot():
    d = tempfile.mkdtemp()
    almac = LocalFileStorage(d)
    principal, _ = montar(almac,
                          FakeModel([herramienta("revisor", {"input": "revisa ventas"}), texto("hecho")]),
                          FakeModel([herramienta("buscar_documentos", {"q": "ventas"}), texto("tres errores")]),
                          storage_revisor=False)
    principal("revisa las ventas")
    snap_rev = snapshot(d, "revisor")
    print("opción 2 | stash del revisor en disco:", len(ficheros(d, "context/chat1/scopes/agent/revisor/")),
          "| snapshot del revisor:", len(snap_rev), "bytes | ¿lleva la línea 700?", "Línea 700:" in snap_rev)
    assert not ficheros(d, "context/chat1/scopes/agent/revisor/")


def test_opcion_1_sin_gestor_de_contexto_el_snapshot_lleva_el_resultado_entero():
    d = tempfile.mkdtemp()
    almac = LocalFileStorage(d)
    m_rev = FakeModel([herramienta("buscar_documentos", {"q": "ventas"}), texto("tres errores")])
    principal, _ = montar(almac,
                          FakeModel([herramienta("revisor", {"input": "revisa ventas"}), texto("hecho")]),
                          m_rev, cm_revisor=None, storage_revisor=False)
    principal("revisa las ventas")
    snap_rev = snapshot(d, "revisor")
    print("opción 1 | herramientas del revisor:", m_rev.llamadas[0]["herramientas"],
          "| snapshot del revisor:", len(snap_rev), "bytes")
    assert "retrieve_context" not in m_rev.llamadas[0]["herramientas"]
    assert len(snap_rev) > 70000 and "Línea 1499:" in snap_rev
