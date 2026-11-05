import json, os, sys
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from strands import Agent
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent
from strands.tools.mcp import MCPClient
from strands.tools.executors import SequentialToolExecutor
from fake import FakeModel, texto, herramienta


def test_approval_id_viaja_en_meta_por_llamada_con_un_diccionario_mutable():
    meta = {}  # el diccionario del cliente: el hook lo rellena antes de cada llamada
    cliente = MCPClient(lambda: stdio_client(StdioServerParameters(command=sys.executable, args=[os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_eco.py")])), meta=meta)
    with cliente:
        m = FakeModel([herramienta("enviar_correo", {"para": "a@b"}, id="tu_1"),
                       herramienta("leer", {"x": "1"}, id="tu_2"), texto("fin")])
        a = Agent(model=m, tools=cliente.list_tools_sync(), callback_handler=None, tool_executor=SequentialToolExecutor())
        def antes(ev: BeforeToolCallEvent):
            if ev.tool_use["name"] == "enviar_correo":
                meta["approval_id"] = "apr_" + ev.tool_use["toolUseId"]
        def despues(ev: AfterToolCallEvent):
            meta.pop("approval_id", None)
        a.hooks.add_callback(BeforeToolCallEvent, antes, order=0)
        a.hooks.add_callback(AfterToolCallEvent, despues)
        a("haz cosas")
    res = {}
    for msg in a.messages:
        for b in msg["content"]:
            if "toolResult" in b:
                res[b["toolResult"]["toolUseId"]] = b["toolResult"]["content"][0]["text"]
    print("enviar_correo recibió:", res["tu_1"][:160])
    print("leer recibió:", res["tu_2"][:160])
    assert "apr_tu_1" in res["tu_1"]
    assert "approval_id" not in res["tu_2"]
