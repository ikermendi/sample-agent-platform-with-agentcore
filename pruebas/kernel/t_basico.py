import asyncio, tempfile
import pytest
from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from fake import FakeModel, texto, herramienta


def test_snapshot_restaura_prompt_de_sistema():
    d = tempfile.mkdtemp()
    # Primera ejecución del chat: prompt v1
    m1 = FakeModel([texto("hola")])
    a1 = Agent(model=m1, system_prompt="PROMPT v1", callback_handler=None,
               session_manager=SnapshotSessionManager("chat1", storage=LocalFileStorage(d), save_latest_on="message"))
    a1("primer mensaje")
    assert m1.llamadas[0]["system_prompt"] == "PROMPT v1"
    # Segunda ejecución (microVM nueva): el kernel construye con v2
    m2 = FakeModel([texto("otra"), texto("y otra")])
    a2 = Agent(model=m2, system_prompt="PROMPT v2", callback_handler=None,
               session_manager=SnapshotSessionManager("chat1", storage=LocalFileStorage(d), save_latest_on="message"))
    restaurado = a2.system_prompt
    a2("segundo mensaje")
    print("tras construir con v2, system_prompt =", restaurado, "| el modelo recibió:", m2.llamadas[0]["system_prompt"])
    assert restaurado == "PROMPT v1", "se esperaba que el snapshot restaurase v1"
    assert m2.llamadas[0]["system_prompt"] == "PROMPT v1"
    # Y la conversación anterior sí está
    assert any("primer mensaje" in str(m) for m in m2.llamadas[0]["mensajes"])
    # Arreglo del diseño: volver a fijarlo tras construir
    a2.system_prompt = "PROMPT v2"
    a2("tercer mensaje")
    assert m2.llamadas[1]["system_prompt"] == "PROMPT v2"


def test_cancel_en_before_model_call_termina_con_end_turn_sin_llamar_al_modelo():
    from strands.hooks import BeforeModelCallEvent
    m = FakeModel([texto("no debería salir")])
    a = Agent(model=m, callback_handler=None)
    marca = {}
    def corte(ev: BeforeModelCallEvent):
        ev.cancel = "has alcanzado tu límite mensual"
        marca["limite"] = True
    a.hooks.add_callback(BeforeModelCallEvent, corte)
    r = a("hola")
    print("stop_reason:", r.stop_reason, "| texto:", str(r), "| llamadas al modelo:", len(m.llamadas))
    assert len(m.llamadas) == 0
    assert r.stop_reason == "end_turn"
    assert marca["limite"]


def test_registrar_dos_veces_depende_del_tipo_de_herramienta():
    from strands.types.tools import AgentTool

    @tool
    def informe_pdf(x: str) -> str:
        """Genera un informe."""
        return x
    a = Agent(model=FakeModel([]), callback_handler=None)
    a.tool_registry.register_tool(informe_pdf)
    a.tool_registry.register_tool(informe_pdf)  # @tool: admite recarga, se sustituye sin error
    print("@tool registrada dos veces: sin error")

    class ScriptDeSkill(AgentTool):
        """Como haría el kernel con un script de tools.yaml."""
        def __init__(self, nombre):
            super().__init__(); self._n = nombre
        @property
        def tool_name(self): return self._n
        @property
        def tool_spec(self): return {"name": self._n, "description": "x", "inputSchema": {"json": {"type": "object"}}}
        @property
        def tool_type(self): return "skill_script"
        async def stream(self, tool_use, invocation_state, **kw):
            yield {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": "ok"}]}

    a.tool_registry.register_tool(ScriptDeSkill("generar_grafico"))
    with pytest.raises(ValueError) as e:
        a.tool_registry.register_tool(ScriptDeSkill("generar_grafico"))
    print("AgentTool propia registrada dos veces:", e.value)
    with pytest.raises(ValueError) as e2:
        a.tool_registry.register_tool(ScriptDeSkill("generar-grafico"))
    print("mismo nombre con guion:", e2.value)


def test_as_tool_sin_preserve_context_con_snapshot():
    d = tempfile.mkdtemp()
    sub = Agent(model=FakeModel([]), name="revisor", callback_handler=None,
                session_manager=SnapshotSessionManager("chat1", storage=LocalFileStorage(d)))
    with pytest.raises(ValueError) as e:
        sub.as_tool(preserve_context=False)
    print("as_tool(preserve_context=False) con snapshot:", e.value)
    sub.as_tool(preserve_context=True)  # este sí
