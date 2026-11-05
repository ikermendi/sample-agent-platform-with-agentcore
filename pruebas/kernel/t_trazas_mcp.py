"""Trazas a través del hub: kernel (Strands + MCPClient, mcp 2.x) → hub (proxy HTTP) → MCP (mcp 2.x).

El kernel usa los propagadores que AgentCore fija en el contenedor (`baggage,xray,tracecontext`), así que su
`_meta` lleva también `X-Amzn-Trace-Id`. Se comprueba qué tramo del kernel va en `_meta` (el de Strands o el
`MCP send` del SDK), que el hub cuelga de él y que el MCP cuelga del hub solo si el hub reescribe `_meta`.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from strands import Agent
from strands.tools.executors import SequentialToolExecutor
from strands.tools.mcp import MCPClient

from fake import FakeModel, herramienta, texto
from trazas_fichero import tramo_a_dict

AQUI = os.path.dirname(os.path.abspath(__file__))
MEMORIA = InMemorySpanExporter()
_p = TracerProvider()
_p.add_span_processor(SimpleSpanProcessor(MEMORIA))
trace.set_tracer_provider(_p)
propagate.set_global_textmap(CompositePropagator(
    [W3CBaggagePropagator(), AwsXRayPropagator(), TraceContextTextMapPropagator()]))


def _puerto():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _espera(puerto):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", puerto), 0.1).close(); return
        except OSError:
            time.sleep(0.1)


class Montaje:
    """MCP y hub en procesos aparte; cada uno escribe sus tramos en su fichero."""

    def __init__(self, reescribe):
        self.reescribe = reescribe

    def __enter__(self):
        self.f_mcp, self.f_hub = tempfile.mktemp(suffix=".jsonl"), tempfile.mktemp(suffix=".jsonl")
        for f in (self.f_mcp, self.f_hub):
            open(f, "w").close()
        p_mcp, p_hub = _puerto(), _puerto()
        self.procs = [subprocess.Popen([sys.executable, os.path.join(AQUI, "mcp_trazas.py"), str(p_mcp)],
                                       env={**os.environ, "TRAZAS": self.f_mcp}, cwd=AQUI)]
        _espera(p_mcp)
        self.procs.append(subprocess.Popen(
            [sys.executable, os.path.join(AQUI, "hub_trazas.py"), str(p_hub)], cwd=AQUI,
            env={**os.environ, "TRAZAS": self.f_hub, "HUB_DESTINO": f"http://127.0.0.1:{p_mcp}/mcp",
                 "HUB_REESCRIBE": "1" if self.reescribe else "0"}))
        _espera(p_hub)
        self.url = f"http://127.0.0.1:{p_hub}/mcp"
        return self

    def tramos(self, fichero):
        with open(fichero) as f:
            return [json.loads(x) for x in f if x.strip()]

    def __exit__(self, *a):
        for p in self.procs:
            p.terminate(); p.wait()


def _ejecuta(m):
    """Un mensaje del kernel dentro de un tramo raíz "ejecucion" que llama a `eco` a través del hub."""
    MEMORIA.clear()
    cliente = MCPClient(url=m.url)
    with trace.get_tracer("kernel").start_as_current_span("ejecucion") as raiz:
        with cliente:
            a = Agent(model=FakeModel([herramienta("eco", {"x": "1"}, id="tu_1"), texto("fin")]),
                      tools=cliente.list_tools_sync(), callback_handler=None, tool_executor=SequentialToolExecutor())
            a("llama a eco")
    meta = None
    for msg in a.messages:
        for b in msg["content"]:
            if "toolResult" in b:
                meta = json.loads(b["toolResult"]["content"][0]["text"].split("META=", 1)[1])
    kernel = [tramo_a_dict(s, "kernel") for s in MEMORIA.get_finished_spans()]
    time.sleep(0.3)
    hub, mcp = m.tramos(m.f_hub), m.tramos(m.f_mcp)
    todos = kernel + hub + mcp
    por_id = {t["tramo"]: t for t in todos}
    print(f"\n   traza {format(raiz.get_span_context().trace_id, '032x')}")
    for t in todos:
        if t["nombre"].startswith(("ejecucion", "execute_tool", "MCP send tools/call", "hub tools/call", "tools/call")):
            p = por_id.get(t["padre"], {})
            print(f"   [{t['servicio']}] {t['nombre']} ({t['tipo']}) {t['tramo']} ← {p.get('servicio', '?')}:{p.get('nombre', t['padre'])}")
    print("   _meta que ve el MCP:", meta)
    traza = format(raiz.get_span_context().trace_id, "032x")
    uno = lambda lista, nombre: next(t for t in lista if t["nombre"] == nombre)
    return traza, todos, kernel, hub, mcp, meta, uno


def test_el_hub_reescribe_meta_y_el_mcp_cuelga_del_hub():
    with Montaje(reescribe=True) as m:
        traza, todos, kernel, hub, mcp, meta, uno = _ejecuta(m)
    envio = uno(kernel, "MCP send tools/call eco")
    strands = next(t for t in kernel if t["nombre"].startswith("execute_tool"))
    h, s = uno(hub, "hub tools/call eco"), uno(mcp, "tools/call eco")
    assert {t["traza"] for t in todos} == {traza}, "todo en la misma traza"
    assert envio["padre"] == strands["tramo"], "el MCP send del SDK cuelga del execute_tool de Strands"
    assert h["padre"] == envio["tramo"], "el hub cuelga del MCP send (el SDK pisa lo que puso Strands)"
    assert s["padre"] == h["tramo"], "el MCP cuelga del hub"
    assert meta["traceparent"].split("-")[2] == h["tramo"]
    assert not any(k.lower() == "x-amzn-trace-id" for k in meta), "no queda el X-Amzn-Trace-Id del kernel"


def test_sin_reescribir_el_mcp_salta_el_hub():
    with Montaje(reescribe=False) as m:
        traza, todos, kernel, hub, mcp, meta, uno = _ejecuta(m)
    envio = uno(kernel, "MCP send tools/call eco")
    h, s = uno(hub, "hub tools/call eco"), uno(mcp, "tools/call eco")
    assert {t["traza"] for t in todos} == {traza}
    assert h["padre"] == envio["tramo"] and s["padre"] == envio["tramo"], "hub y MCP son hermanos"
    xray = next(v for k, v in meta.items() if k.lower() == "x-amzn-trace-id")
    assert envio["tramo"] in xray, "el _meta del kernel lleva también X-Amzn-Trace-Id con el mismo padre"
