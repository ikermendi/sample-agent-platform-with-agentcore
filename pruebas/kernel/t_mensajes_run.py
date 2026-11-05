"""Mensajes durante un run: lo que el usuario envía llega a la llamada siguiente junto al resultado de la herramienta,
sin dos mensajes seguidos del usuario; lo que llega tarde no se entrega y la app lo manda como un mensaje normal."""
import tempfile

from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from strands.tools.executors import SequentialToolExecutor

from fake import FakeModel, texto, herramienta
from mensajes_run import Buzon


def agente(guion, buzon, al_ejecutar=None, **kw):
    @tool
    def buscar(consulta: str) -> str:
        """Busca en los documentos."""
        if al_ejecutar:
            al_ejecutar(consulta)
        return f"resultados de {consulta}"

    m = FakeModel(guion)
    a = Agent(model=m, tools=[buscar], callback_handler=None, tool_executor=SequentialToolExecutor(), **kw)
    buzon.registrar(a)
    return a, m


def alternan(mensajes):
    roles = [x["role"] for x in mensajes]
    return all(x != y for x, y in zip(roles, roles[1:]))


def test_llega_a_la_llamada_siguiente_junto_al_resultado():
    eventos = []
    b = Buzon(eventos)
    a, m = agente([herramienta("buscar", {"consulta": "norte"}), texto("hecho")], b,
                  lambda _: b.pendientes.append(("m1", "añade también el este")))
    a("busca el norte")
    ultimo = m.llamadas[1]["mensajes"][-1]
    print("bloques:", [list(x)[0] for x in ultimo["content"]])
    assert [list(x)[0] for x in ultimo["content"]] == ["toolResult", "text"]
    assert ultimo["content"][1]["text"] == "añade también el este"
    assert alternan(m.llamadas[1]["mensajes"])
    assert eventos == [{"type": "user_message", "message_id": "m1", "text": "añade también el este"}]


def test_varios_en_orden_y_cada_uno_una_vez():
    eventos = []
    b = Buzon(eventos)

    def enviar(consulta):
        b.pendientes.append((f"m-{consulta}", f"mensaje {consulta}"))
        if consulta == "a":
            b.pendientes.append(("m-a2", "mensaje a2"))

    a, m = agente([herramienta("buscar", {"consulta": "a"}), herramienta("buscar", {"consulta": "b"}), texto("fin")],
                  b, enviar)
    a("empieza")
    print("entregados:", [e["message_id"] for e in eventos])
    assert [e["message_id"] for e in eventos] == ["m-a", "m-a2", "m-b"]
    assert str(m.llamadas[2]["mensajes"]).count("mensaje a2") == 1
    assert alternan(m.llamadas[2]["mensajes"])


def test_pendiente_antes_de_empezar_va_con_el_primer_mensaje():
    eventos = []
    b = Buzon(eventos)
    b.pendientes.append(("m0", "y en euros"))
    a, m = agente([texto("vale")], b)
    a("dime las ventas")
    assert [list(x)[0] for x in m.llamadas[0]["mensajes"][-1]["content"]] == ["text", "text"]
    assert len(eventos) == 1


def test_el_que_llega_tras_la_ultima_llamada_no_se_entrega():
    eventos = []
    b = Buzon(eventos)
    a, m = agente([texto("respuesta final")], b)
    a("hola")
    b.pendientes.append(("tarde", "esto llega tarde"))  # api lo aceptó, pero ya no hay más llamadas al modelo
    assert eventos == []  # sin user_message antes de `end`: la app lo manda como un mensaje normal
    assert "esto llega tarde" not in str(a.messages)


def test_con_el_tope_del_run_no_se_entrega():
    eventos = []
    b = Buzon(eventos)
    a, _ = agente([herramienta("buscar", {"consulta": "a"})], b,
                  lambda _: b.pendientes.append(("m1", "otra cosa")))
    r = a("tarea larga", limits={"turns": 1})
    print("fin:", r.stop_reason, "| eventos:", eventos)
    assert r.stop_reason == "limit_turns" and eventos == []


def test_sobrevive_al_snapshot():
    d = tempfile.mkdtemp()
    sm = lambda: SnapshotSessionManager("chat-msg", storage=LocalFileStorage(d), save_latest_on="message")
    b = Buzon([])
    a1, _ = agente([herramienta("buscar", {"consulta": "a"}), texto("listo")], b,
                   lambda _: b.pendientes.append(("m1", "dato enviado a mitad")), session_manager=sm())
    a1("empieza")
    a2, m2 = agente([texto("sigo")], Buzon([]), session_manager=sm())
    a2("otro mensaje")
    print("mensajes restaurados:", len(m2.llamadas[0]["mensajes"]))
    assert "dato enviado a mitad" in str(m2.llamadas[0]["mensajes"])
    assert alternan(m2.llamadas[0]["mensajes"])
