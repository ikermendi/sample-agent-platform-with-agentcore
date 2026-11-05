"""Tarea de una vez en la VPC de integraciones: latencia de tools/call (eco) por el hub (NLB) frente al MCP
directo (IP de la tarea), con un MCPClient de Strands cada uno, alternando. Escribe una línea JSON."""
import json
import os
import statistics
import time

import httpx
from strands.tools.mcp import MCPClient


class Token(httpx.Auth):
    def __init__(self, v):
        self.v = v

    def auth_flow(self, r):
        if self.v:
            r.headers["Authorization"] = f"Bearer {self.v}"
        yield r


c = json.loads(os.environ["CLIENTE_CARGA"])
t = {"hub": [], "directo": []}
err = []
with MCPClient(url=c["hub_url"], auth_provider=Token(c["token"])) as h, MCPClient(url=c["directo_url"]) as d:
    for i in range(c.get("n", 100) + 5):
        for k, cli in (("hub", h), ("directo", d)):
            t0 = time.perf_counter()
            r = cli.call_tool_sync(f"{k}{i}", "eco", {"texto": str(i)})
            ms = (time.perf_counter() - t0) * 1000
            if r.get("status") != "success":
                err.append((k, str(r)[:200]))
            if i >= 5:  # las 5 primeras, de calentamiento
                t[k].append(ms)
out = {k: {"p50": round(statistics.median(v), 2), "p90": round(sorted(v)[int(len(v) * 0.9)], 2), "n": len(v)} for k, v in t.items()}
out["anadido_p50_ms"] = round(out["hub"]["p50"] - out["directo"]["p50"], 2)
print(json.dumps({"latencia": out, "errores": err}), flush=True)
