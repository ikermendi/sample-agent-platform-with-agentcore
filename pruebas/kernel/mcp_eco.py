"""Servidor MCP mínimo: devuelve el _meta que recibe en cada llamada."""
import json
from mcp.server.mcpserver import MCPServer, Context

srv = MCPServer("eco")

@srv.tool()
def enviar_correo(para: str, ctx: Context) -> str:
    """Envía un correo (de mentira)."""
    meta = ctx.request_context.meta
    return "META=" + json.dumps(meta.model_dump() if hasattr(meta, "model_dump") else meta, default=str)

@srv.tool()
def leer(x: str, ctx: Context) -> str:
    """Lee (de mentira)."""
    meta = ctx.request_context.meta
    return "META=" + json.dumps(meta.model_dump() if hasattr(meta, "model_dump") else meta, default=str)

if __name__ == "__main__":
    srv.run()
