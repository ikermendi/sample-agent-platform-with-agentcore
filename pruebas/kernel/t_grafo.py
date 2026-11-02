import asyncio, threading
from strands import Agent, tool
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status
from strands.types.tools import ToolContext
from fake import FakeModel, texto, herramienta


@tool
async def lenta() -> str:
    """Tarda un poco."""
    await asyncio.sleep(0.2); return "ok"


def _nodo(nombre, vueltas):
    m = FakeModel([herramienta("lenta")] * vueltas + [texto(f"conclusión de {nombre}")])
    return Agent(model=m, name=nombre, description=nombre, tools=[lenta], callback_handler=None), m


def _grafo(*nodos):
    b = GraphBuilder()
    for n in nodos:
        b.add_node(n, n.name)
    for a, z in zip(nodos, nodos[1:]):
        b.add_edge(a.name, z.name)
    b.set_entry_point(nodos[0].name)
    b.set_max_node_executions(10); b.set_execution_timeout(30)
    return b.build()


def test_graph_no_tiene_cancel():
    a, _ = _nodo("a", 0)
    g = _grafo(a)
    print("Graph tiene cancel:", hasattr(g, "cancel"))
    assert not hasattr(g, "cancel")


def test_nodos_comparten_limits_del_principal_solo_con_invocation_context():
    def montar(con_contexto):
        a, ma = _nodo("investigar", 3); r, mr = _nodo("redactar", 3)
        g = _grafo(a, r)

        @tool(context=True)
        def flujo(tool_context: ToolContext) -> str:
            """Ejecuta el grafo."""
            ctx = tool_context.invocation_context if con_contexto else None
            res = g("tarea", invocation_context=ctx)
            return str(res.status)

        p = Agent(model=FakeModel([herramienta("flujo"), texto("fin")]), tools=[flujo], callback_handler=None)
        res = p("empieza", limits={"turns": 4})
        return res, len(ma.llamadas) + len(mr.llamadas), g
    res_con, llamadas_con, _ = montar(True)
    res_sin, llamadas_sin, _ = montar(False)
    print("con invocation_context: llamadas de los nodos", llamadas_con, "| principal", res_con.stop_reason,
          "|| sin él: llamadas de los nodos", llamadas_sin, "| principal", res_sin.stop_reason)
    # Cada nodo quiere 4 llamadas (3 con herramienta + la final): 8 en total
    assert llamadas_sin == 8
    assert llamadas_con < 8


def test_cancelar_cada_nodo_con_su_agent_para_el_grafo():
    a, ma = _nodo("investigar", 20); r, mr = _nodo("redactar", 20)
    g = _grafo(a, r)
    resultado = {}

    def correr():
        resultado["r"] = g("tarea")
    hilo = threading.Thread(target=correr); hilo.start()
    while len(ma.llamadas) < 2:  # ya va por su segunda vuelta
        threading.Event().wait(0.02)
    for n in g.nodes.values():  # lo que haría el kernel: cancel() de cada nodo
        n.executor.cancel()
    hilo.join(10)
    res = resultado["r"]
    estados = {k: v.status for k, v in g.state.results.items()} if hasattr(g.state, "results") else {}
    print("grafo:", res.status, "| nodos:", {k: str(v) for k, v in estados.items()},
          "| llamadas investigar:", len(ma.llamadas), "| redactar:", len(mr.llamadas))
    assert not hilo.is_alive()
    assert len(ma.llamadas) < 21          # el nodo en marcha paró
    assert len(mr.llamadas) == 0          # el siguiente no llegó a empezar
    assert res.status == Status.FAILED


def test_cancel_signal_del_principal_no_llega_solo_a_los_nodos():
    a, ma = _nodo("investigar", 20)
    g = _grafo(a)

    @tool(context=True)
    def flujo(tool_context: ToolContext) -> str:
        """Ejecuta el grafo."""
        return str(g("tarea", invocation_context=tool_context.invocation_context).status)

    p = Agent(model=FakeModel([herramienta("flujo"), texto("fin")]), tools=[flujo], callback_handler=None)

    def cancelar_principal():
        while len(ma.llamadas) < 2:
            threading.Event().wait(0.02)
        p.cancel()
    threading.Thread(target=cancelar_principal).start()
    t = threading.Thread(target=lambda: p("empieza")); t.start()
    t.join(3)
    sigue = t.is_alive()
    llamadas_tras_cancelar = len(ma.llamadas)
    for n in g.nodes.values():
        n.executor.cancel()
    t.join(10)
    print("cancel() del principal: el grafo sigue corriendo:", sigue, "| llamadas del nodo:", llamadas_tras_cancelar)
    assert sigue and llamadas_tras_cancelar > 2
