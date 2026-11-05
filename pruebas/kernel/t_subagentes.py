import asyncio, time
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent, BeforeModelCallEvent
from strands.tools.executors import SequentialToolExecutor
from strands.vended_interventions.hitl import HumanInTheLoop
from fake import FakeModel, texto, herramienta

intervalos = []

@tool
async def lenta_a() -> str:
    """Tarda un poco."""
    t0 = time.monotonic(); await asyncio.sleep(0.3); intervalos.append(("a", t0, time.monotonic())); return "a"

@tool
async def lenta_b() -> str:
    """Tarda un poco."""
    t0 = time.monotonic(); await asyncio.sleep(0.3); intervalos.append(("b", t0, time.monotonic())); return "b"


def _solapan():
    (_, a0, a1), (_, b0, b1) = intervalos[-2:]
    return a0 < b1 and b0 < a1


def test_ejecutor_por_defecto_es_concurrente_y_el_secuencial_no():
    guion = lambda: [[herramienta("lenta_a"), herramienta("lenta_b")], texto("hecho")]
    Agent(model=FakeModel(guion()), tools=[lenta_a, lenta_b], callback_handler=None)("x")
    concurrente = _solapan()
    Agent(model=FakeModel(guion()), tools=[lenta_a, lenta_b], callback_handler=None,
          tool_executor=SequentialToolExecutor())("x")
    secuencial = _solapan()
    print("por defecto solapan:", concurrente, "| con SequentialToolExecutor solapan:", secuencial)
    assert concurrente and not secuencial


def test_subagente_consume_los_limits_del_principal_y_no_hereda_hooks():
    # Subagente: 3 vueltas con herramienta y una final
    sub_model = FakeModel([herramienta("lenta_a"), herramienta("lenta_a"), herramienta("lenta_a"), texto("informe del revisor")])
    sub = Agent(model=sub_model, name="revisor", description="Revisa", tools=[lenta_a], callback_handler=None)
    # Principal: llama al subagente y luego contesta
    prin_model = FakeModel([herramienta("revisor", {"input": "revisa esto"}), texto("respuesta final")])
    prin = Agent(model=prin_model, tools=[sub.as_tool()], callback_handler=None)
    vistos_por_hook_principal = []
    prin.hooks.add_callback(BeforeModelCallEvent, lambda e: vistos_por_hook_principal.append(e.agent.name))
    r = prin("empieza", limits={"turns": 3})
    print("principal stop_reason:", r.stop_reason, "| llamadas modelo principal:", len(prin_model.llamadas),
          "| llamadas modelo subagente:", len(sub_model.llamadas),
          "| BeforeModelCallEvent visto por el hook del principal, agentes:", set(vistos_por_hook_principal))
    # Con turns=3 compartido, el subagente no puede hacer sus 4 vueltas
    assert len(sub_model.llamadas) < 4
    # El hook del principal no ve las llamadas al modelo del subagente
    assert set(vistos_por_hook_principal) <= {prin.name}


def test_eventos_del_subagente_llegan_como_AgentAsToolStreamEvent():
    from strands.types._events import AgentAsToolStreamEvent
    sub = Agent(model=FakeModel([texto("soy el subagente")]), name="revisor", description="Revisa", callback_handler=None)
    prin = Agent(model=FakeModel([herramienta("revisor", {"input": "x"}), texto("fin")]), tools=[sub.as_tool()], callback_handler=None)
    tipos = []
    async def run():
        async for ev in prin.stream_async("hola"):
            if isinstance(ev, dict) and "tool_stream_event" in ev:
                tipos.append(type(ev["tool_stream_event"]).__name__ if not isinstance(ev["tool_stream_event"], dict) else "dict")
    captured = []
    orig = AgentAsToolStreamEvent.__init__
    def spy(self, *a, **k):
        captured.append(1); return orig(self, *a, **k)
    AgentAsToolStreamEvent.__init__ = spy
    try:
        asyncio.run(run())
    finally:
        AgentAsToolStreamEvent.__init__ = orig
    print("AgentAsToolStreamEvent creados:", len(captured))
    assert len(captured) > 0


def test_hook_de_orden_0_captura_la_llamada_antes_que_el_ask_de_hitl():
    orden = []
    @tool
    def enviar_correo(para: str, asunto: str) -> str:
        """Envía un correo."""
        orden.append("herramienta"); return "enviado"
    @tool
    def leer(x: str) -> str:
        """Lee."""
        orden.append("leer"); return "leído"
    pendiente = {}
    def capturar(ev: BeforeToolCallEvent):
        if ev.tool_use["name"] == "enviar_correo":
            pendiente.update(id=ev.tool_use["toolUseId"], args=ev.tool_use["input"]); orden.append("hook0")
    prompts = []
    async def ask(texto_aviso):
        prompts.append(texto_aviso); orden.append(f"ask(ve id={pendiente.get('id')})"); return "y"
    m = FakeModel([herramienta("leer", {"x": "1"}), herramienta("enviar_correo", {"para": "a@b", "asunto": "hola"}, id="tu_123"), texto("fin")])
    a = Agent(model=m, tools=[enviar_correo, leer], callback_handler=None, tool_executor=SequentialToolExecutor(),
              interventions=[HumanInTheLoop(allowed_tools=["*", "!enviar_correo"], ask=ask)])
    a.hooks.add_callback(BeforeToolCallEvent, capturar, order=0)
    a("haz cosas")
    print("orden:", orden, "| el ask recibió:", repr(prompts[0][:120]))
    assert orden == ["leer", "hook0", "ask(ve id=tu_123)", "herramienta"]
    assert "tu_123" not in prompts[0]  # el ask no recibe el id: solo texto


def test_registrar_herramienta_a_mitad_de_ejecucion():
    @tool
    def skills(nombre: str) -> str:
        """Activa una skill."""
        return f"SKILL.md de {nombre}"
    @tool
    def generar_grafico(datos: str) -> str:
        """Script de la skill."""
        return "grafico.png"
    m = FakeModel([herramienta("skills", {"nombre": "graficos"}), herramienta("generar_grafico", {"datos": "1,2"}), texto("listo")])
    a = Agent(model=m, tools=[skills], callback_handler=None)
    def al_activar(ev: AfterToolCallEvent):
        if ev.tool_use["name"] == "skills":
            ev.agent.tool_registry.register_tool(generar_grafico)
    a.hooks.add_callback(AfterToolCallEvent, al_activar)
    a("haz un gráfico")
    print("herramientas en cada llamada:", [c["herramientas"] for c in m.llamadas])
    assert "generar_grafico" not in m.llamadas[0]["herramientas"]
    assert "generar_grafico" in m.llamadas[1]["herramientas"]
    assert any("grafico.png" in str(msg) for msg in a.messages)


def test_cancel_del_principal_para_al_subagente():
    import threading
    sub_model = FakeModel([herramienta("lenta_a")] * 20 + [texto("informe")])
    sub = Agent(model=sub_model, name="revisor", description="Revisa", tools=[lenta_a], callback_handler=None)
    prin = Agent(model=FakeModel([herramienta("revisor", {"input": "x"}), texto("fin")]),
                 tools=[sub.as_tool()], callback_handler=None)
    def cancelar():
        while len(sub_model.llamadas) < 2:
            threading.Event().wait(0.02)
        prin.cancel()
    threading.Thread(target=cancelar).start()
    r = prin("empieza")
    print("principal:", r.stop_reason, "| llamadas del subagente:", len(sub_model.llamadas))
    assert r.stop_reason == "cancelled" and len(sub_model.llamadas) < 5
