"""Hub MCP mínimo para las pruebas de trazas: proxy HTTP que lee el cuerpo y lo reenvía, como el de la sonda 13.

Por cada petición JSON-RPC abre un tramo SERVER con el padre que trae `params._meta`. Con HUB_REESCRIBE=1
quita del `_meta` las claves de contexto que llegan (`traceparent`, `tracestate`, `baggage`,
`X-Amzn-Trace-Id`) y escribe las de su propio tramo antes de reenviar; con HUB_REESCRIBE=0 reenvía los mismos
bytes. Los tramos van, uno por línea, al fichero de TRAZAS.

    TRAZAS=/ruta/tramos.jsonl HUB_DESTINO=http://127.0.0.1:8765/mcp HUB_REESCRIBE=1 python hub_trazas.py 8766
"""
import json
import os
import sys

import httpx
import uvicorn
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import trazas_fichero

trazas_fichero.configurar("hub", os.environ["TRAZAS"])
DESTINO = os.environ["HUB_DESTINO"]
REESCRIBE = os.environ.get("HUB_REESCRIBE") == "1"
CONTEXTO = {"traceparent", "tracestate", "baggage", "x-amzn-trace-id"}
SALTO = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "upgrade", "accept-encoding"}
tr = trace.get_tracer("hub")


async def mcp(req: Request):
    cuerpo = await req.body()
    try:
        j = json.loads(cuerpo) if cuerpo else None
    except ValueError:
        j = None
    params = (j or {}).get("params") or {} if isinstance(j, dict) else {}
    meta = params.get("_meta") or {}
    nombre = f"hub {j.get('method')}" + (f" {params['name']}" if params.get("name") else "") if isinstance(j, dict) else "hub"
    with tr.start_as_current_span(nombre, kind=SpanKind.SERVER, context=propagate.extract(meta) if meta else None):
        if REESCRIBE and isinstance(j, dict) and "method" in j:
            nuevo = {k: v for k, v in meta.items() if k.lower() not in CONTEXTO}
            propagate.inject(nuevo)
            params["_meta"] = nuevo
            j["params"] = params
            cuerpo = json.dumps(j).encode()
        cab = {k: v for k, v in req.headers.items() if k.lower() not in SALTO}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(req.method, DESTINO, content=cuerpo, headers=cab)
    salida = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "mcp-session-id")}
    return Response(r.content, status_code=r.status_code, headers=salida)


app = Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
