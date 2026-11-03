"""Memoria del usuario con el MemoryManager de Strands: el agente propone con add_memory
y no escribe nada; quien guarda es api cuando el usuario confirma en la app."""
import tempfile

from strands import Agent
from strands.memory import MemoryManager
from strands.memory.types import MemoryEntry
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage

from fake import FakeModel, texto, herramienta


class AlmacenPropuestas:
    """Almacén propio: search lee la memoria (en el kernel, de S3 con solo GetObject)
    y add no escribe: deja una propuesta (en el kernel, un evento memory_proposal)."""

    name = "user"
    description = "Memoria del usuario; lo que añadas queda propuesto hasta que el usuario lo confirme en la app."
    max_search_results = None
    writable = True
    extraction = None

    def __init__(self, entradas):
        self.entradas = list(entradas)
        self.propuestas = []

    async def search(self, query, options=None):
        return [MemoryEntry(content=e, store_name=self.name) for e in self.entradas]

    async def add(self, content, metadata=None):
        self.propuestas.append(content)
        return {"proposed": content}


def gestor(almacen):
    return MemoryManager(
        stores=[almacen],
        search_tool_config=False,
        add_tool_config={"description": "Propone guardar en la memoria del usuario un dato estable (una preferencia o un hecho). "
                                        "No se guarda hasta que el usuario lo confirma en la app: díselo así."},
        injection=False,
    )


def test_add_memory_propone_sin_escribir_y_sin_search():
    almacen = AlmacenPropuestas(["Prefiere respuestas cortas"])
    m = FakeModel([herramienta("add_memory", {"entries": ["Trabaja en el equipo de ventas de Bilbao"]}),
                   texto("Te lo propongo para tu memoria; confírmalo en la app.")])
    a = Agent(model=m, system_prompt="MEMORIA: Prefiere respuestas cortas", callback_handler=None,
              memory_manager=gestor(almacen))
    a("recuerda que trabajo en ventas en Bilbao")
    herramientas = m.llamadas[0]["herramientas"]
    print("herramientas:", herramientas, "| propuestas:", almacen.propuestas)
    assert "add_memory" in herramientas and "search_memory" not in herramientas
    assert almacen.propuestas == ["Trabaja en el equipo de ventas de Bilbao"]
    # El resultado que ve el modelo
    resultado = [b for msg in m.llamadas[1]["mensajes"] for b in msg["content"] if "toolResult" in b][0]["toolResult"]
    print("resultado de add_memory:", resultado)
    assert resultado["status"] == "success"
    # Sin inyección: la memoria solo va en el prompt de sistema, no en el mensaje del usuario
    ultimo_usuario = m.llamadas[0]["mensajes"][-1]
    assert "<memory>" not in str(ultimo_usuario)


def test_snapshot_con_memory_manager():
    d = tempfile.mkdtemp()
    sm = lambda: SnapshotSessionManager("chat-mem", storage=LocalFileStorage(d), save_latest_on="message")
    a1 = Agent(model=FakeModel([herramienta("add_memory", {"entries": ["Usa euros"]}), texto("ok")]),
               callback_handler=None, session_manager=sm(), memory_manager=gestor(AlmacenPropuestas([])))
    a1("recuerda que uso euros")
    almacen2 = AlmacenPropuestas([])
    m2 = FakeModel([texto("sigo")])
    a2 = Agent(model=m2, callback_handler=None, session_manager=sm(), memory_manager=gestor(almacen2))
    a2("otro mensaje")
    print("mensajes restaurados:", len(m2.llamadas[0]["mensajes"]), "| herramientas:", m2.llamadas[0]["herramientas"])
    assert any("add_memory" in str(msg) for msg in m2.llamadas[0]["mensajes"])
    assert "add_memory" in m2.llamadas[0]["herramientas"]
    assert almacen2.propuestas == []
