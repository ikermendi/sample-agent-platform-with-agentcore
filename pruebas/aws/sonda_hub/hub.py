"""MCP Hub de la sonda 13: una ruta por MCP (/mcp/{nombre}), sin juntar listas de herramientas, sin estado.

En cada petición: valida el token de ejecución (ES256 con la clave pública de KMS en memoria: firma, iss/aud,
caducidad), comprueba que el MCP de la ruta está en `allow`; en tools/call, que la herramienta también y, si
está marcada `confirmar`, que el approval_id de `_meta` es una aprobación concedida, sin usar, de esa
ejecución, con la misma herramienta y argumentos iguales (igualdad profunda), y la marca como usada; pone la
identidad (OBO pedido a /internal/obo con el token de ejecución, o cabeceras X-Hub-* con identidad de
ejecución; nunca reenvía el token de ejecución); reenvía el cuerpo tal cual (mismos bytes: `_meta` y
argumentos sin tocar); filtra tools/list a lo permitido; audita una línea JSON por petición; falla cerrado.

Entorno: HUB_CATALOGO (JSON {nombre: {url, identidad: obo|ejecucion, audiencia?, confirmar: [..]}}),
HUB_KID (id de la clave de KMS), HUB_OBO_URL, HUB_TABLA (DynamoDB de aprobaciones; sin ella, en memoria
desde HUB_APROBACIONES), HUB_CLAVE_PEM (solo en local, en lugar de KMS).
"""
import contextlib
import hashlib
import json
import os
import socket
import time
import uuid

import anyio
import httpx
from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from token_ejecucion import TokenInvalido, clave_publica_kms, verificar

CATALOGO = json.loads(os.environ["HUB_CATALOGO"])
KID = os.environ["HUB_KID"]
OBO_URL = os.environ.get("HUB_OBO_URL")
TABLA = os.environ.get("HUB_TABLA")
SALTO = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
         "authorization", "proxy-authorization", "accept-encoding"}
E = {"clave": None, "http": None, "ddb": None, "tarea": socket.gethostname(), "obo": {}}
MEMORIA = json.loads(os.environ.get("HUB_APROBACIONES", "{}"))  # solo en local


def audita(**kw):
    print(json.dumps({"auditoria": "hub", "ts": round(time.time(), 3), "tarea": E["tarea"], **kw},
                     ensure_ascii=False), flush=True)


@contextlib.asynccontextmanager
async def vida(app):
    meta = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if meta:
        async with httpx.AsyncClient() as c:
            E["tarea"] = (await c.get(meta + "/task")).json()["TaskARN"].rsplit("/", 1)[-1]
    if os.environ.get("HUB_CLAVE_PEM"):
        E["clave"] = serialization.load_pem_public_key(os.environ["HUB_CLAVE_PEM"].encode())
    else:
        import boto3
        E["clave"] = clave_publica_kms(boto3.client("kms"), KID)  # una vez; luego todo en memoria
    if TABLA:
        import boto3
        E["ddb"] = boto3.client("dynamodb")
    E["http"] = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5),
                                  limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
    print(json.dumps({"hub": "listo", "tarea": E["tarea"], "mcp": list(CATALOGO)}), flush=True)
    yield
    await E["http"].aclose()


# ------------------------------------------------------------------ aprobaciones
def _aprobacion(aid):
    if E["ddb"] is None:
        return MEMORIA.get(aid)
    it = E["ddb"].get_item(TableName=TABLA, Key={"pk": {"S": aid}}, ConsistentRead=True).get("Item")
    if not it:
        return None
    return {"run_id": it["run_id"]["S"], "herramienta": it["herramienta"]["S"], "estado": it["estado"]["S"],
            "argumentos": it["argumentos"]["S"], "usada": it["usada"]["BOOL"]}


def _marcar(aid):
    if E["ddb"] is None:
        a = MEMORIA[aid]
        if a["usada"]:
            return False
        a["usada"] = True
        return True
    try:
        E["ddb"].update_item(TableName=TABLA, Key={"pk": {"S": aid}},
                             UpdateExpression="SET usada = :t, usada_en = :ahora, usada_por = :tarea",
                             ConditionExpression="usada = :f AND estado = :c",
                             ExpressionAttributeValues={":t": {"BOOL": True}, ":f": {"BOOL": False},
                                                        ":c": {"S": "concedida"}, ":ahora": {"N": str(time.time())},
                                                        ":tarea": {"S": E["tarea"]}})
        return True
    except E["ddb"].exceptions.ConditionalCheckFailedException:
        return False


async def comprueba_aprobacion(claims, mcp, herramienta, params):
    aid = ((params.get("_meta") or {}).get("approval_id"))
    if not aid:
        return "confirmar: falta approval_id", None
    a = await anyio.to_thread.run_sync(_aprobacion, aid)
    if not a:
        return "confirmar: aprobación desconocida", aid
    if a["estado"] != "concedida":
        return "confirmar: aprobación no concedida", aid
    if a["usada"]:
        return "confirmar: aprobación ya usada", aid
    if a["run_id"] != claims["run_id"]:
        return "confirmar: aprobación de otra ejecución", aid
    if a["herramienta"] != f"{mcp}/{herramienta}":
        return "confirmar: otra herramienta", aid
    if json.loads(a["argumentos"]) != (params.get("arguments") or {}):  # igualdad profunda, sin hashes
        return "confirmar: argumentos distintos", aid
    if not await anyio.to_thread.run_sync(_marcar, aid):
        return "confirmar: aprobación ya usada (carrera)", aid
    return None, aid


# ------------------------------------------------------------------ identidad hacia el MCP
async def token_obo(token, claims, audiencia):
    clave = (claims["sub"], audiencia)
    t = E["obo"].get(clave)
    if t and t["expira"] - 300 > time.time():
        return t["token"], "cache"
    r = await E["http"].post(OBO_URL, json={"audiencia": audiencia}, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    t = r.json()
    E["obo"][clave] = t
    return t["token"], "pedido"


# ------------------------------------------------------------------ respuestas
def deniega(cuerpo, metodo, motivo):
    pid = cuerpo.get("id") if isinstance(cuerpo, dict) else None
    texto = f"MCP Hub: denegado ({motivo})"
    if metodo == "tools/call" and pid is not None:  # error de herramienta: el modelo lo ve y sigue
        return JSONResponse({"jsonrpc": "2.0", "id": pid,
                             "result": {"content": [{"type": "text", "text": texto}], "isError": True,
                                        "resultType": "complete"}})  # 2026-07-28 exige resultType
    return JSONResponse({"jsonrpc": "2.0", "id": pid, "error": {"code": -32001, "message": texto}}, status_code=403)


def filtra_lista(datos, mcp, allow):
    def f(j):
        if isinstance(j, dict) and isinstance(j.get("result"), dict) and isinstance(j["result"].get("tools"), list):
            antes = len(j["result"]["tools"])
            j["result"]["tools"] = [t for t in j["result"]["tools"] if f"{mcp}/{t.get('name')}" in allow]
            return j, antes, len(j["result"]["tools"])
        return j, None, None
    return f(datos)


async def mcp(request: Request):
    t0 = time.perf_counter()
    nombre = request.path_params["nombre"]
    crudo = await request.body()
    info = {"peticion": uuid.uuid4().hex[:12], "mcp": nombre, "http": request.method,
            "cuerpo_sha256": hashlib.sha256(crudo).hexdigest(), "marca": request.headers.get("x-sonda-marca"),
            "cliente": request.client.host if request.client else None}
    cuerpo, metodo = {}, None
    try:
        if crudo:
            cuerpo = json.loads(crudo)
            metodo = cuerpo.get("method") if isinstance(cuerpo, dict) else "lote"
        info["metodo"] = metodo
        if metodo == "lote":
            raise ValueError("lotes JSON-RPC no admitidos")
        destino = CATALOGO.get(nombre)
        if not destino:
            audita(**info, decision="denegada", motivo="MCP desconocido")
            return deniega(cuerpo, metodo, "MCP desconocido")
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        try:
            claims = verificar(token, E["clave"], KID)
        except TokenInvalido as e:
            audita(**info, decision="denegada", motivo=f"token: {e}")
            return deniega(cuerpo, metodo, f"token: {e}")
        allow = claims["allow"]
        info.update(run_id=claims.get("run_id"), oid=claims.get("sub"), agente=claims.get("agente"))
        if not any(a.startswith(nombre + "/") for a in allow):
            audita(**info, decision="denegada", motivo="MCP no permitido")
            return deniega(cuerpo, metodo, "MCP no permitido")
        params = (cuerpo.get("params") if isinstance(cuerpo, dict) else None) or {}
        if metodo == "tools/call":
            herr = params.get("name")
            info["herramienta"] = herr
            if f"{nombre}/{herr}" not in allow:
                audita(**info, decision="denegada", motivo="herramienta fuera de allow")
                return deniega(cuerpo, metodo, "herramienta fuera de allow")
            if herr in destino.get("confirmar", []):
                motivo, info["approval_id"] = await comprueba_aprobacion(claims, nombre, herr, params)
                if motivo:
                    audita(**info, decision="denegada", motivo=motivo)
                    return deniega(cuerpo, metodo, motivo)
        cab = {k: v for k, v in request.headers.items() if k.lower() not in SALTO and not k.lower().startswith("x-hub-")}
        if destino["identidad"] == "obo":
            try:
                obo, info["obo"] = await token_obo(token, claims, destino["audiencia"])
            except Exception as e:
                audita(**info, decision="denegada", motivo=f"OBO: {type(e).__name__}")
                return deniega(cuerpo, metodo, "sin token OBO")
            cab["authorization"] = f"Bearer {obo}"
        else:
            cab.update({"x-hub-usuario": claims["sub"], "x-hub-grupos": ",".join(claims.get("grupos", [])),
                        "x-hub-agente": claims.get("agente", ""), "x-hub-run": claims.get("run_id", "")})
        try:
            r = await E["http"].request(request.method, destino["url"], headers=cab, content=crudo)
        except Exception as e:
            audita(**info, decision="denegada", motivo=f"MCP de destino: {type(e).__name__}")
            return deniega(cuerpo, metodo, "MCP de destino no disponible")
        salida = r.content
        if metodo == "tools/list" and r.status_code == 200:
            ct = r.headers.get("content-type", "")
            if ct.startswith("application/json"):
                j, antes, despues = filtra_lista(r.json(), nombre, allow)
                salida = json.dumps(j).encode()
            else:  # text/event-stream: filtra cada evento data:
                lineas, antes, despues = [], None, None
                for ln in r.text.split("\n"):
                    if ln.startswith("data:"):
                        j, a, d = filtra_lista(json.loads(ln[5:]), nombre, allow)
                        antes, despues = (a, d) if a is not None else (antes, despues)
                        ln = "data: " + json.dumps(j)
                    lineas.append(ln)
                salida = "\n".join(lineas).encode()
            info.update(herramientas_mcp=antes, herramientas_filtradas=despues)
        sal = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "mcp-session-id", "cache-control")}
        audita(**info, decision="permitida", status=r.status_code, ms=round((time.perf_counter() - t0) * 1000, 2))
        return Response(salida, status_code=r.status_code, headers=sal)
    except Exception as e:  # falla cerrado
        audita(**info, decision="denegada", motivo=f"error del hub: {type(e).__name__}: {str(e)[:120]}")
        return deniega(cuerpo, metodo, "error del hub")


async def salud(request):
    return JSONResponse({"ok": True, "tarea": E["tarea"]})


app = Starlette(routes=[Route("/mcp/{nombre}", mcp, methods=["GET", "POST", "DELETE"]), Route("/salud", salud)],
                lifespan=vida)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")
