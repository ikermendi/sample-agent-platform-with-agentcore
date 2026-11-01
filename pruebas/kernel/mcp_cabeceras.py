"""Servidor MCP local por Streamable HTTP que anota cada petición HTTP que recibe.

Por cada petición escribe una línea JSON en el fichero de MCP_REGISTRO con el método HTTP, la cabecera
Authorization, la cabecera Mcp-Session-Id y el método JSON-RPC del cuerpo (si lo hay). Así la prueba ve qué
token llevó cada petición y si la sesión MCP es la misma entre dos llamadas al agente.

Con MCP_LEGADO=1 contesta "Method not found" a `server/discover` (como un servidor anterior a la versión
2026-07-28 del protocolo): el cliente cae al `initialize` de siempre, con Mcp-Session-Id y el stream GET.

    MCP_REGISTRO=/ruta/registro.jsonl python mcp_cabeceras.py 8765
"""
import json
import os
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

REGISTRO = os.environ["MCP_REGISTRO"]
LEGADO = os.environ.get("MCP_LEGADO") == "1"
srv = MCPServer("cabeceras")


@srv.tool()
def buscar(q: str) -> str:
    """Busca (de mentira)."""
    return f"resultado de {q}"


@srv.tool()
def resumir(texto: str) -> str:
    """Resume (de mentira)."""
    return f"resumen de {texto}"


def anotar(app):
    async def envoltorio(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        cab = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        cuerpo = b""
        mas = True
        mensajes = []
        while mas:  # se lee el cuerpo entero y se le devuelve tal cual a la app
            m = await receive()
            mensajes.append(m)
            cuerpo += m.get("body", b"")
            mas = m.get("more_body", False)
        try:
            j = json.loads(cuerpo) if cuerpo else {}
            rpc = j.get("method")
        except Exception:
            j, rpc = {}, None
        with open(REGISTRO, "a") as f:
            f.write(json.dumps({"http": scope["method"], "rpc": rpc, "auth": cab.get("authorization"),
                                "sesion": cab.get("mcp-session-id")}) + "\n")
        if LEGADO and rpc == "server/discover":
            err = json.dumps({"jsonrpc": "2.0", "id": j.get("id"),
                              "error": {"code": -32601, "message": "Method not found"}}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": err})

        async def recibir():
            return mensajes.pop(0) if mensajes else await receive()
        return await app(scope, recibir, send)
    return envoltorio


if __name__ == "__main__":
    uvicorn.run(anotar(srv.streamable_http_app()), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
