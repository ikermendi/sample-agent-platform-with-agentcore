"""/internal/obo simulado: valida el token de ejecución (misma clave pública de KMS) y que la audiencia es de
un MCP permitido en su `allow`; devuelve un token OBO falso y anota quién lo pidió (una línea JSON)."""
import json
import os
import time
import uuid

from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from token_ejecucion import TokenInvalido, clave_publica_kms, verificar

KID = os.environ["HUB_KID"]
AUDIENCIAS = json.loads(os.environ["OBO_AUDIENCIAS"])  # {audiencia: mcp}
if os.environ.get("HUB_CLAVE_PEM"):
    CLAVE = serialization.load_pem_public_key(os.environ["HUB_CLAVE_PEM"].encode())
else:
    import boto3
    CLAVE = clave_publica_kms(boto3.client("kms"), KID)


async def obo(request):
    datos = await request.json()
    aud = datos.get("audiencia")
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    reg = {"auditoria": "obo", "ts": round(time.time(), 3), "audiencia": aud,
           "pedido_desde": request.client.host if request.client else None}
    try:
        c = verificar(token, CLAVE, KID)
    except TokenInvalido as e:
        print(json.dumps({**reg, "decision": "denegado", "motivo": str(e)}), flush=True)
        return JSONResponse({"error": str(e)}, status_code=403)
    reg.update(oid=c["sub"], run_id=c.get("run_id"))
    mcp = AUDIENCIAS.get(aud)
    if not mcp or not any(a.startswith(mcp + "/") for a in c["allow"]):
        print(json.dumps({**reg, "decision": "denegado", "motivo": "audiencia no permitida"}), flush=True)
        return JSONResponse({"error": "audiencia no permitida"}, status_code=403)
    t = f"obo-falso.{c['sub']}.{aud}.{uuid.uuid4().hex[:8]}"
    print(json.dumps({**reg, "decision": "emitido", "token": t}), flush=True)
    return JSONResponse({"token": t, "expira": time.time() + 3600})


app = Starlette(routes=[Route("/internal/obo", obo, methods=["POST"])])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")
