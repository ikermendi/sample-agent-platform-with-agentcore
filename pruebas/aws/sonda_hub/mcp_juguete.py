"""MCP de juguete (Streamable HTTP, sin estado, respuestas JSON). MCP_NOMBRE=hora | docs.

hora (identidad obo): hora_actual, eco, quien_soy (devuelve la Authorization y las X-Hub-* recibidas).
docs (identidad de ejecución): buscar_documentos, borrar (marcada `confirmar`; devuelve argumentos y _meta
tal como llegan). Un envoltorio ASGI anota cada petición: método JSON-RPC, sha256 del cuerpo y el cuerpo.
"""
import base64
import datetime
import hashlib
import json
import os
import socket
import sys
import time
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

NOMBRE = os.environ["MCP_NOMBRE"]
srv = MCPServer(f"mcp-{NOMBRE}")


def cabeceras(ctx):
    h = dict(ctx.headers or {})
    return {"authorization": h.get("authorization"), "x_hub": {k: v for k, v in h.items() if k.startswith("x-hub-")}}


if NOMBRE == "hora":
    @srv.tool()
    def hora_actual(zona: str = "Europe/Madrid") -> str:
        """Hora actual en una zona."""
        return datetime.datetime.now(ZoneInfo(zona)).isoformat()

    @srv.tool()
    def eco(texto: str) -> str:
        """Devuelve el texto."""
        return json.dumps({"texto": texto, "recibido": time.time()})

    @srv.tool()
    def quien_soy(ctx: Context) -> str:
        """Lo que el MCP recibe como identidad."""
        return json.dumps(cabeceras(ctx))
else:
    @srv.tool()
    def buscar_documentos(consulta: str) -> str:
        """Busca documentos (de mentira)."""
        return json.dumps({"consulta": consulta, "resultados": ["doc-1", "doc-2"]})

    @srv.tool()
    def borrar(ruta: str, motivo: str, opciones: dict, ctx: Context) -> str:
        """Borra un documento (marcada confirmar en el hub)."""
        meta = ctx.request_context.meta
        meta = meta.model_dump(by_alias=True, exclude_none=True) if hasattr(meta, "model_dump") else meta
        return json.dumps({"argumentos": {"ruta": ruta, "motivo": motivo, "opciones": opciones},
                           "meta": meta, **cabeceras(ctx)}, ensure_ascii=False)


def anota(app):
    async def envoltorio(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        mensajes, cuerpo, mas = [], b"", True
        while mas:
            m = await receive()
            mensajes.append(m)
            cuerpo += m.get("body", b"")
            mas = m.get("more_body", False)
        try:
            rpc = json.loads(cuerpo).get("method") if cuerpo else None
        except Exception:
            rpc = None
        print(json.dumps({"auditoria": f"mcp-{NOMBRE}", "ts": round(time.time(), 3), "tarea": socket.gethostname(),
                          "http": scope["method"], "rpc": rpc, "cuerpo_sha256": hashlib.sha256(cuerpo).hexdigest(),
                          "cuerpo_b64": base64.b64encode(cuerpo).decode() if rpc == "tools/call" else None}), flush=True)

        async def recibir():
            return mensajes.pop(0) if mensajes else await receive()
        return await app(scope, recibir, send)
    return envoltorio


if __name__ == "__main__":
    app = srv.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host="0.0.0.0",
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    uvicorn.run(anota(app), host="0.0.0.0", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8080, log_level="warning")
