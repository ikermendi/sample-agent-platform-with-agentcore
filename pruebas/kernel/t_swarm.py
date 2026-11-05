import asyncio, threading
from strands import Agent, tool
from strands.hooks import BeforeNodeCallEvent
from strands.multiagent import Swarm
from strands.multiagent.base import Status
from strands.session.session_manager import SessionManager
from strands.types.tools import ToolContext
from fake import FakeModel, texto, herramienta


@tool
async def lenta() -> str:
    """Tarda un poco."""
    await asyncio.sleep(0.2); return "ok"


def _traspaso(a, mensaje="sigue tú", contexto=None):
    return herramienta("handoff_to_agent", {"agent_name": a, "message": mensaje, "context": contexto or {}})


def _nodo(nombre, guion):
    m = FakeModel(guion)
    return Agent(model=m, name=nombre, description=nombre, tools=[lenta], callback_handler=None), m


def _textos(llamada):
    return [b["text"] for x in llamada["mensajes"] for b in x["content"] if "text" in b]


def test_swarm_no_tiene_cancel():
    a, _ = _nodo("a", [texto("fin")])
    s = Swarm([a])
    print("Swarm tiene cancel:", hasattr(s, "cancel"), "| limites por defecto:", s.max_handoffs, s.max_iterations,
          s.execution_timeout, s.node_timeout)
    assert not hasattr(s, "cancel")


def test_nodos_rechazan_snapshot():
    class Nulo(SessionManager):
        def initialize(self, *a, **k): pass
        def append_message(self, *a, **k): pass
        def redact_latest_message(self, *a, **k): pass
        def sync_agent(self, *a, **k): pass
    a = Agent(model=FakeModel([texto("fin")]), name="a", session_manager=Nulo(), callback_handler=None)
    try:
        Swarm([a]); error = None
    except ValueError as e:
        error = str(e)
    print("nodo con session manager:", error)
    assert error and "not supported" in error


def test_cualquier_nodo_puede_traspasar_a_cualquiera_y_el_siguiente_no_ve_la_conversacion():
    a, ma = _nodo("a", [herramienta("lenta"), _traspaso("c", "revisa esto", {"dato": 1}), texto("secreto de a")])
    b, mb = _nodo("b", [texto("nunca")])
    c, mc = _nodo("c", [texto("conclusión de c")])
    res = Swarm([a, b, c], entry_point=a)("tarea")
    print("historial:", [n.node_id for n in res.node_history], "| entrada de c:", _textos(mc.llamadas[0]))
    print("herramientas de a:", ma.llamadas[0]["herramientas"])
    assert [n.node_id for n in res.node_history] == ["a", "c"]
    assert len(mb.llamadas) == 0
    assert "handoff_to_agent" in ma.llamadas[0]["herramientas"]
    entrada = " ".join(_textos(mc.llamadas[0]))
    assert "revisa esto" in entrada and "dato" in entrada and "tarea" in entrada
    assert "secreto de a" not in entrada


def test_volver_a_un_nodo_empieza_sin_su_conversacion():
    a, ma = _nodo("a", [_traspaso("b"), texto("lo que dijo a en su primer turno"), texto("segundo turno de a")])
    b, _ = _nodo("b", [_traspaso("a"), texto("vuelve")])
    res = Swarm([a, b], entry_point=a)("tarea")
    ultima = ma.llamadas[-1]
    print("historial:", [n.node_id for n in res.node_history], "| mensajes al volver a a:", len(ultima["mensajes"]))
    assert [n.node_id for n in res.node_history] == ["a", "b", "a"]
    assert "lo que dijo a en su primer turno" not in " ".join(_textos(ultima))


def test_max_handoffs_y_ping_pong_terminan_failed():
    def montar():
        a, ma = _nodo("a", [x for _ in range(20) for x in (_traspaso("b"), texto("a"))])
        b, mb = _nodo("b", [x for _ in range(20) for x in (_traspaso("a"), texto("b"))])
        return a, b, ma, mb
    a, b, ma, mb = montar()
    r1 = Swarm([a, b], entry_point=a, max_handoffs=5)("tarea")
    a, b, ma, mb = montar()
    r2 = Swarm([a, b], entry_point=a, max_handoffs=20, repetitive_handoff_detection_window=4,
               repetitive_handoff_min_unique_agents=3)("tarea")
    print("max_handoffs:", r1.status, len(r1.node_history), "| ping-pong:", r2.status, len(r2.node_history))
    assert r1.status == Status.FAILED and len(r1.node_history) == 5
    assert r2.status == Status.FAILED and len(r2.node_history) == 4


def test_nodos_comparten_limits_del_principal_solo_con_invocation_context():
    def montar(con_contexto):
        a, ma = _nodo("a", [herramienta("lenta")] * 3 + [_traspaso("b"), texto("a")])
        b, mb = _nodo("b", [herramienta("lenta")] * 3 + [texto("b")])
        s = Swarm([a, b], entry_point=a)

        @tool(context=True)
        def flujo(tool_context: ToolContext) -> str:
            """Ejecuta el swarm."""
            ctx = tool_context.invocation_context if con_contexto else None
            return str(s("tarea", invocation_context=ctx).status)

        p = Agent(model=FakeModel([herramienta("flujo"), texto("fin")]), tools=[flujo], callback_handler=None)
        res = p("empieza", limits={"turns": 4})
        return res, len(ma.llamadas) + len(mb.llamadas)
    res_con, con = montar(True)
    res_sin, sin = montar(False)
    print("con invocation_context: llamadas", con, res_con.stop_reason, "|| sin él:", sin, res_sin.stop_reason)
    assert sin == 9
    assert con < 9


def test_cancel_del_principal_no_llega_solo_a_los_nodos():
    a, ma = _nodo("a", [herramienta("lenta")] * 20)
    s = Swarm([a])

    @tool(context=True)
    def flujo(tool_context: ToolContext) -> str:
        """Ejecuta el swarm."""
        return str(s("tarea", invocation_context=tool_context.invocation_context).status)

    p = Agent(model=FakeModel([herramienta("flujo"), texto("fin")]), tools=[flujo], callback_handler=None)

    def cancelar_principal():
        while len(ma.llamadas) < 2:
            threading.Event().wait(0.02)
        p.cancel()
    threading.Thread(target=cancelar_principal).start()
    t = threading.Thread(target=lambda: p("empieza")); t.start()
    t.join(3)
    sigue = t.is_alive()
    llamadas = len(ma.llamadas)
    a.cancel()
    t.join(10)
    print("cancel() del principal: el swarm sigue corriendo:", sigue, "| llamadas del nodo:", llamadas)
    assert sigue and llamadas > 2


def test_cancelar_el_nodo_con_un_traspaso_pendiente():
    a, ma = _nodo("a", [_traspaso("b")] + [herramienta("lenta")] * 20)
    b, mb = _nodo("b", [texto("conclusión de b")])
    s = Swarm([a, b], entry_point=a)
    resultado = {}
    hilo = threading.Thread(target=lambda: resultado.update(r=s("tarea"))); hilo.start()
    while len(ma.llamadas) < 3:  # el traspaso ya está pedido y a sigue trabajando
        threading.Event().wait(0.02)
    for n in s.nodes.values():  # lo que haría el kernel: cancel() de cada nodo
        n.executor.cancel()
    hilo.join(10)
    res = resultado["r"]
    print("swarm:", res.status, "| nodos:", {k: str(v.status) for k, v in res.results.items()},
          "| parada de a:", res.results["a"].result.stop_reason,
          "| historial:", [n.node_id for n in res.node_history], "| llamadas b:", len(mb.llamadas))
    # A diferencia de Graph, un nodo cancelado cuenta como completado y el siguiente arranca igualmente
    assert res.results["a"].result.stop_reason == "cancelled"
    assert res.status == Status.COMPLETED
    assert len(mb.llamadas) == 1


def test_cancelar_con_hook_de_nodo_ademas_de_cancel_para_el_swarm():
    a, ma = _nodo("a", [_traspaso("b")] + [herramienta("lenta")] * 20)
    b, mb = _nodo("b", [texto("conclusión de b")])
    s = Swarm([a, b], entry_point=a)
    cancelado = threading.Event()

    def antes_de_nodo(evento):
        if cancelado.is_set():
            evento.cancel_node = "cancelado por el usuario"
    s.hooks.add_callback(BeforeNodeCallEvent, antes_de_nodo)
    resultado = {}
    hilo = threading.Thread(target=lambda: resultado.update(r=s("tarea"))); hilo.start()
    while len(ma.llamadas) < 3:
        threading.Event().wait(0.02)
    cancelado.set()
    a.cancel()
    hilo.join(10)
    res = resultado["r"]
    print("con hook: swarm:", res.status, "| llamadas a:", len(ma.llamadas), "| llamadas b:", len(mb.llamadas))
    assert res.status == Status.FAILED
    assert len(ma.llamadas) < 21
    assert len(mb.llamadas) == 0


def test_el_stream_identifica_nodo_y_traspaso():
    a, _ = _nodo("a", [herramienta("lenta"), _traspaso("b", "revisa"), texto("a")])
    b, _ = _nodo("b", [texto("b")])
    s = Swarm([a, b], entry_point=a)

    async def correr():
        return [e async for e in s.stream_async("tarea")]
    eventos = asyncio.run(correr())
    tipos = {e.get("type") for e in eventos}
    traspasos = [e for e in eventos if e.get("type") == "multiagent_handoff"]
    print("tipos:", sorted(str(t) for t in tipos))
    print("traspasos:", [{k: v for k, v in e.items() if k != "type"} for e in traspasos])
    assert len(traspasos) == 1
    assert "multiagent_node_stream" in tipos
