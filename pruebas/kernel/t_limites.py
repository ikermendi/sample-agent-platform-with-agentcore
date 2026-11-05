"""Topes por run y Continuar: al llegar a `limits` el run para con `stop_reason = limit_…` sin lanzar nada, y el mensaje
siguiente empieza con el contador a cero sobre la misma conversación, en el mismo Agent o restaurado del snapshot."""
import tempfile

from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage

from fake import FakeModel, texto, herramienta


@tool
def buscar(consulta: str) -> str:
    """Busca en los documentos."""
    return f"resultados de {consulta}"


def bloques(mensaje, tipo):
    return [b for b in mensaje["content"] if tipo in b]


def test_el_tope_de_turnos_para_tras_la_herramienta_y_continuar_cuenta_de_cero():
    m = FakeModel([herramienta("buscar", {"consulta": "a"}), herramienta("buscar", {"consulta": "b"}),
                   herramienta("buscar", {"consulta": "c"}), texto("Hecho")])
    a = Agent(model=m, tools=[buscar], callback_handler=None)

    r = a("Tarea larga", limits={"turns": 2})
    assert r.stop_reason == "limit_turns"
    assert len(m.llamadas) == 2
    # Las herramientas pedidas terminan antes de parar: la conversación acaba en un toolResult, sin llamadas abiertas
    assert a.messages[-1]["role"] == "user" and bloques(a.messages[-1], "toolResult")

    r = a("Continúa", limits={"turns": 2})
    assert r.stop_reason == "end_turn"
    assert len(m.llamadas) == 4
    # Lo que recibe el modelo al continuar: dos mensajes de usuario seguidos (falta comprobarlo contra Claude)
    enviados = m.llamadas[2]["mensajes"]
    assert [x["role"] for x in enviados[-2:]] == ["user", "user"]
    assert bloques(enviados[-2], "toolResult") and enviados[-1]["content"] == [{"text": "Continúa"}]


def test_el_tope_de_salida_acumula_las_llamadas():
    m = FakeModel([herramienta("buscar", {"consulta": "a"}), herramienta("buscar", {"consulta": "b"}), texto("Hecho")])
    r = Agent(model=m, tools=[buscar], callback_handler=None)("Tarea larga", limits={"output_tokens": 20})
    assert r.stop_reason == "limit_output_tokens"  # el modelo falso gasta 10 por llamada
    assert len(m.llamadas) == 2


def test_continuar_con_el_agent_restaurado_del_snapshot():
    with tempfile.TemporaryDirectory() as d:
        def agente(m):
            return Agent(model=m, tools=[buscar], callback_handler=None,
                         session_manager=SnapshotSessionManager("chat1", storage=LocalFileStorage(d), save_latest_on="message"))

        r = agente(FakeModel([herramienta("buscar", {"consulta": "a"})]))("Tarea larga", limits={"turns": 1})
        assert r.stop_reason == "limit_turns"

        m = FakeModel([texto("Hecho")])
        r = agente(m)("Continúa", limits={"turns": 1})
        assert r.stop_reason == "end_turn"
        assert bloques(m.llamadas[0]["mensajes"][-2], "toolResult")
