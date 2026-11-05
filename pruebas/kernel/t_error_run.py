"""Un run que acaba en error: qué queda en memoria y en el snapshot, si el kernel puede limpiarlo y reescribirlo, y si
el Agent reutilizado y el reconstruido mandan lo mismo al modelo en el mensaje siguiente."""
import asyncio
import glob
import tempfile

import pytest
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage

from fake import FakeModel, texto, herramienta


@tool
def buscar(consulta: str) -> str:
    """Busca en los documentos."""
    return f"resultados de {consulta}"


@tool
def rompe(consulta: str) -> str:
    """Falla siempre."""
    raise RuntimeError("fallo de la herramienta")


class Almacen(LocalFileStorage):
    """Falla en la escritura número `falla_en` (desde 1) y en las siguientes, hasta que se apague."""
    def __init__(self, d):
        super().__init__(d)
        self.falla_en, self.escrituras = None, 0

    async def write(self, key, data):
        self.escrituras += 1
        if self.falla_en and self.escrituras >= self.falla_en:
            raise OSError("S3 caído")
        await super().write(key, data)


def agente(m, almacen):
    return Agent(model=m, tools=[buscar, rompe], callback_handler=None,
                 session_manager=SnapshotSessionManager("chat1", storage=almacen, save_latest_on="message"))


def forma(mensajes):
    return [(m["role"], [next(iter(b)) for b in m["content"]]) for m in mensajes]


def quitar_mensaje_colgado(a):
    """Lo que hace el kernel: el texto del usuario sin respuesta al final, no un toolResult."""
    if a.messages and a.messages[-1]["role"] == "user" and "text" in a.messages[-1]["content"][0]:
        a.messages.pop()


U, A = "user", "assistant"
PREGUNTA, TOOL_USE, TOOL_RESULT = (U, ["text"]), (A, ["toolUse"]), (U, ["toolResult"])
OTRA = (U, ["text"])


def fallar_turno(guion, d, hook=False):
    almacen = Almacen(d)
    a = agente(FakeModel(guion), almacen)
    if hook:
        def lanza(_):
            raise RuntimeError("fallo del hook")
        a.hooks.add_callback(BeforeToolCallEvent, lanza)
    with pytest.raises(Exception):
        a("Pregunta")
    return a, almacen


def test_el_modelo_falla_en_la_primera_llamada_y_el_mensaje_queda_colgado_tambien_en_el_snapshot():
    with tempfile.TemporaryDirectory() as d:
        a, almacen = fallar_turno([RuntimeError("Bedrock")], d)
        assert forma(a.messages) == [PREGUNTA]
        m = FakeModel([texto("ok")])
        b = agente(m, almacen)
        assert forma(b.messages) == [PREGUNTA]  # sin reescribir, reconstruir trae el mensaje colgado
        b("Otra")
        assert forma(m.llamadas[0]["mensajes"]) == [PREGUNTA, OTRA]  # dos mensajes de usuario seguidos


def test_limpiar_y_reescribir_el_snapshot_tras_el_error_deja_la_conversacion_limpia():
    with tempfile.TemporaryDirectory() as d:
        a, almacen = fallar_turno([RuntimeError("Bedrock")], d)
        quitar_mensaje_colgado(a)
        asyncio.run(a._session_manager.save_snapshot(a, is_latest=True))
        m = FakeModel([texto("ok")])
        b = agente(m, almacen)
        assert b.messages == []
        b("Otra")
        assert forma(m.llamadas[0]["mensajes"]) == [OTRA]


def test_el_modelo_falla_tras_una_herramienta_y_la_conversacion_acaba_en_tool_result():
    with tempfile.TemporaryDirectory() as d:
        a, almacen = fallar_turno([herramienta("buscar", {"consulta": "a"}), RuntimeError("Bedrock")], d)
        assert forma(a.messages) == [PREGUNTA, TOOL_USE, TOOL_RESULT]
        m = FakeModel([texto("ok")])
        agente(m, almacen)("Otra")
        assert forma(m.llamadas[0]["mensajes"]) == [PREGUNTA, TOOL_USE, TOOL_RESULT, OTRA]


def test_una_herramienta_que_lanza_no_es_un_error_del_run():
    with tempfile.TemporaryDirectory() as d:
        r = agente(FakeModel([herramienta("rompe", {"consulta": "a"}), texto("Vale")]), Almacen(d))("Pregunta")
        assert r.stop_reason == "end_turn"


def test_un_hook_que_lanza_deja_el_tool_use_sin_resultado_y_strands_lo_repara_en_los_dos_caminos():
    with tempfile.TemporaryDirectory() as d:
        a, almacen = fallar_turno([herramienta("buscar", {"consulta": "a"})], d, hook=True)
        assert forma(a.messages) == [PREGUNTA, TOOL_USE]
        reconstruido, reutilizado = FakeModel([texto("ok")]), FakeModel([texto("ok")])
        agente(reconstruido, almacen)("Otra")
        a.model = reutilizado
        a.hooks._registered_callbacks[BeforeToolCallEvent].clear()
        a("Otra")
        esperado = [PREGUNTA, TOOL_USE, TOOL_RESULT, OTRA]
        assert forma(reconstruido.llamadas[0]["mensajes"]) == esperado
        assert forma(reutilizado.llamadas[0]["mensajes"]) == esperado


@pytest.mark.parametrize("falla_en", [1, 2, 3])
def test_un_put_del_snapshot_que_falla_corta_el_run(falla_en):
    with tempfile.TemporaryDirectory() as d:
        almacen = Almacen(d)
        almacen.falla_en = falla_en
        m = FakeModel([herramienta("buscar", {"consulta": "a"}), texto("Hecho")])
        with pytest.raises(Exception) as e:
            agente(m, almacen)("Pregunta")
        assert "S3 caído" in str(e.value)
        assert len(m.llamadas) == ([0, 1, 1][falla_en - 1])


def test_si_la_reescritura_falla_el_snapshot_conserva_el_mensaje_colgado_y_la_memoria_no():
    with tempfile.TemporaryDirectory() as d:
        a, almacen = fallar_turno([RuntimeError("Bedrock")], d)
        quitar_mensaje_colgado(a)
        almacen.falla_en = 1
        with pytest.raises(OSError):
            asyncio.run(a._session_manager.save_snapshot(a, is_latest=True))
        almacen.falla_en = None
        assert a.messages == []
        assert forma(agente(FakeModel([]), almacen).messages) == [PREGUNTA]
        assert len(glob.glob(d + "/**/snapshot_latest.json", recursive=True)) == 1
