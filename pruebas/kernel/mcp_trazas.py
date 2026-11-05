"""Servidor MCP local por Streamable HTTP (sin sesión, respuestas JSON) con OpenTelemetry.

El SDK de MCP (2.x) abre un tramo SERVER por petición con el padre que llega en `_meta` (SEP-414). La
herramienta `eco` devuelve el `_meta` que recibe. Los tramos van, uno por línea, al fichero de TRAZAS.

    TRAZAS=/ruta/tramos.jsonl python mcp_trazas.py 8765
"""
import json
import os
import sys

import uvicorn

import trazas_fichero

trazas_fichero.configurar("mcp", os.environ["TRAZAS"])

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402  después de fijar el proveedor

srv = MCPServer("trazas")


@srv.tool()
def eco(x: str, ctx: Context) -> str:
    """Devuelve el _meta que recibe."""
    meta = ctx.request_context.meta
    return "META=" + json.dumps(meta.model_dump() if hasattr(meta, "model_dump") else meta, default=str)


if __name__ == "__main__":
    uvicorn.run(srv.streamable_http_app(json_response=True, stateless_http=True), host="127.0.0.1",
                port=int(sys.argv[1]), log_level="warning")
