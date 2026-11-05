"""Lo que hace el kernel de prueba (Strands 1.63.0, modelo falso) contra el hub: un MCPClient por MCP permitido,
Streamable HTTP sin sesión, con el token de ejecución puesto por auth_provider en cada petición.

`ejecuta(carga)` -> dict. carga = {"prueba": ..., "hub": "http://hub.../mcp", "tokens": {...}, ...}.
El auth_provider anota también el sha256 (y, en tools/call, el cuerpo) de cada petición que sale del kernel,
para comparar byte a byte con lo que reciben el hub y el MCP.
"""
import base64
import hashlib
import json
import socket
import statistics
import time
import uuid

import httpx
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.tools.executors import SequentialToolExecutor
from strands.tools.mcp import MCPClient

from fake import FakeModel, herramienta, texto


class Token(httpx.Auth):
    def __init__(self, valor=None, marca=None):
        self.valor, self.marca, self.enviados = valor, marca, []

    def auth_flow(self, request):
        if self.valor:
            request.headers["Authorization"] = f"Bearer {self.valor}"
        if self.marca:
            request.headers["x-sonda-marca"] = self.marca
        cuerpo = request.content if request.method == "POST" else b""
        rpc = None
        try:
            rpc = json.loads(cuerpo).get("method") if cuerpo else None
        except Exception:
            pass
        self.enviados.append({"rpc": rpc, "sha256": hashlib.sha256(cuerpo).hexdigest(),
                              "b64": base64.b64encode(cuerpo).decode() if rpc == "tools/call" else None})
        yield request


def cliente(carga, mcp, token, meta=None):
    return MCPClient(url=f"{carga['hub']}/{mcp}", auth_provider=token, meta=meta, startup_timeout=20)


def res(r):
    """MCPToolResult -> {status, texto}"""
    return {"status": r.get("status"), "texto": " ".join(c.get("text", "") for c in r.get("content", []))[:600]}


def abre(carga, mcp, valor):
    """Intenta abrir un cliente y listar; devuelve lo que pasó."""
    try:
        with cliente(carga, mcp, Token(valor)) as c:
            return {"abre": True, "herramientas": [t.tool_name for t in c.list_tools_sync()]}
    except Exception as e:
        causa = e
        while causa.__cause__ is not None or causa.__context__ is not None:
            causa = causa.__cause__ or causa.__context__
        return {"abre": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "causa": f"{type(causa).__name__}: {str(causa)[:200]}"}


def resultados_del_agente(agente):
    r = {}
    for m in agente.messages:
        for b in m["content"]:
            if "toolResult" in b:
                tr = b["toolResult"]
                r[tr["toolUseId"]] = {"status": tr.get("status"), "texto": " ".join(c.get("text", "") for c in tr["content"])[:800]}
    return r


def p_dns(carga):
    def resuelve(h):
        try:
            return sorted({x[4][0] for x in socket.getaddrinfo(h, 80, proto=socket.IPPROTO_TCP)})
        except Exception as e:
            return "error: " + type(e).__name__
    return {k: resuelve(v) for k, v in carga["nombres"].items()}


def p_enrutado(carga):
    T = carga["tokens"]
    out = {}
    with cliente(carga, "hora", Token(T["parcial"])) as ch, cliente(carga, "docs", Token(T["parcial"])) as cd:
        th, td = ch.list_tools_sync(), cd.list_tools_sync()
        out["lista_hora"] = [t.tool_name for t in th]
        out["lista_docs"] = [t.tool_name for t in td]
        m = FakeModel([herramienta("hora_actual", {"zona": "UTC"}, id="tu_h"),
                       herramienta("buscar_documentos", {"consulta": "vacaciones"}, id="tu_d"), texto("fin")])
        a = Agent(model=m, tools=th + td, callback_handler=None, tool_executor=SequentialToolExecutor())
        a("usa las dos")
        out["agente"] = resultados_del_agente(a)
        out["modelo_vio"] = m.llamadas[0]["herramientas"]
        # un kernel comprometido llama a lo que no tiene en su lista
        out["eco_fuera_de_allow"] = res(ch.call_tool_sync("x1", "eco", {"texto": "hola"}))
        out["borrar_fuera_de_allow"] = res(cd.call_tool_sync("x2", "borrar", {"ruta": "a", "motivo": "b", "opciones": {}}))
    return out


def p_token(carga):
    T = carga["tokens"]
    out = {"abrir": {k: abre(carga, "hora", T.get(k)) for k in ("ninguno", "firma_mala", "caducado")}}
    out["abrir"]["docs_no_permitido"] = abre(carga, "docs", T["solo_hora"])
    out["abrir"]["docs_valido"] = abre(carga, "docs", T["valido"])
    tk = Token(T["valido"])
    seq = {}
    with cliente(carga, "hora", tk) as c:
        for k in ("valido", "ninguno", "firma_mala", "caducado", "solo_docs", "valido"):
            tk.valor = T.get(k)
            try:
                seq[k if k not in seq else k + "_2"] = res(c.call_tool_sync(uuid.uuid4().hex[:8], "hora_actual", {}))
            except Exception as e:
                seq[k] = {"excepcion": f"{type(e).__name__}: {str(e)[:200]}"}
    out["a_mitad"] = seq
    return out


def p_obo(carga):
    T = carga["tokens"]
    with cliente(carga, "hora", Token(T["valido"])) as c:
        q = [json.loads(c.call_tool_sync(f"q{i}", "quien_soy", {})["content"][0]["text"]) for i in range(3)]
    with cliente(carga, "hora", Token(T["otro_usuario"])) as c:
        q.append(json.loads(c.call_tool_sync("q9", "quien_soy", {})["content"][0]["text"]))
    return {"quien_soy": q}


def p_confirmar(carga):
    T, A, ARGS = carga["tokens"], carga["aprobaciones"], carga["args"]
    out = {}
    meta = {}
    tk = Token(T["valido"])
    with cliente(carga, "docs", tk, meta=meta) as c:
        out["sin_approval_id"] = res(c.call_tool_sync("c0", "borrar", ARGS))
        # camino real: el modelo pide la herramienta y el hook de orden 0 pone el approval_id en el meta del cliente
        m = FakeModel([herramienta("borrar", ARGS, id="tu_borrar"), texto("hecho")])
        a = Agent(model=m, tools=c.list_tools_sync(), callback_handler=None, tool_executor=SequentialToolExecutor())

        def antes(ev: BeforeToolCallEvent):
            if ev.tool_use["name"] == "borrar":
                meta["approval_id"] = A["buena"]

        def despues(ev: AfterToolCallEvent):
            meta.pop("approval_id", None)
        a.hooks.add_callback(BeforeToolCallEvent, antes, order=0)
        a.hooks.add_callback(AfterToolCallEvent, despues)
        n0 = len(tk.enviados)
        a("borra")
        out["con_aprobacion_valida"] = resultados_del_agente(a)["tu_borrar"]
        llamada = [e for e in tk.enviados[n0:] if e["rpc"] == "tools/call"]
        out["enviado_por_el_kernel"] = llamada[0] if llamada else None
        out["args_del_modelo"] = ARGS
        out["ya_usada"] = res(c.call_tool_sync("c2", "borrar", ARGS, meta={"approval_id": A["buena"]}))
        out["args_distintos"] = res(c.call_tool_sync("c3", "borrar", ARGS, meta={"approval_id": A["args_distintos"]}))
        out["otra_ejecucion"] = res(c.call_tool_sync("c4", "borrar", ARGS, meta={"approval_id": A["otra_ejecucion"]}))
        out["desconocida"] = res(c.call_tool_sync("c5", "borrar", ARGS, meta={"approval_id": "apr_no_existe"}))
        out["no_concedida"] = res(c.call_tool_sync("c6", "borrar", ARGS, meta={"approval_id": A["pendiente"]}))
    return out


def p_reparto(carga):
    T, n = carga["tokens"], carga.get("n", 60)
    marca = carga["marca"]
    una, errores, t_una = Token(T["valido"], marca + "-una"), [], []
    with cliente(carga, "hora", una) as c:
        for i in range(n):
            t0 = time.perf_counter()
            r = c.call_tool_sync(f"r{i}", "eco", {"texto": f"{i}"})
            t_una.append((time.perf_counter() - t0) * 1000)
            if r.get("status") != "success":
                errores.append(res(r))
    t_nuevas = []
    for i in range(carga.get("n_nuevas", 30)):
        t0 = time.perf_counter()
        try:
            with cliente(carga, "hora", Token(T["valido"], marca + "-nuevas")) as c:
                r = c.call_tool_sync(f"n{i}", "eco", {"texto": f"{i}"})
            if r.get("status") != "success":
                errores.append(res(r))
        except Exception as e:
            errores.append(f"{type(e).__name__}: {str(e)[:120]}")
        t_nuevas.append((time.perf_counter() - t0) * 1000)
    return {"errores": errores, "p50_ms_una_conexion": round(statistics.median(t_una), 1),
            "p90_ms_una_conexion": round(sorted(t_una)[int(len(t_una) * 0.9)], 1),
            "p50_ms_cliente_nuevo": round(statistics.median(t_nuevas), 1)}


PRUEBAS = {"dns": p_dns, "enrutado": p_enrutado, "token": p_token, "obo": p_obo, "confirmar": p_confirmar,
           "reparto": p_reparto}


def ejecuta(carga):
    t0 = time.time()
    try:
        r = PRUEBAS[carga["prueba"]](carga)
    except Exception as e:
        import traceback
        r = {"excepcion": f"{type(e).__name__}: {e}", "traza": traceback.format_exc()[-1500:]}
    r["segundos"] = round(time.time() - t0, 2)
    return r
