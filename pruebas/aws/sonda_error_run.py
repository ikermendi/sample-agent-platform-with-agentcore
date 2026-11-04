"""Sonda 25: lo que queda de un run en error, contra Claude. Coste: céntimos.

Con la conversación que deja cada fallo (t_error_run.py), un mensaje nuevo con cada modelo de SONDA_MODELOS:
  1. Mensaje colgado: dos mensajes de usuario seguidos, sin limpiar el snapshot.
  2. El modelo falló tras una herramienta: toolResult y mensaje de usuario seguidos.
  3. Un hook falló antes de la herramienta: toolUse sin resultado, reparado por Strands.

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_error_run.py
"""
import os

import boto3
from strands import Agent, tool
from strands.models import BedrockModel

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = "eu" if REGION.startswith("eu-") else "us"
MODELOS = os.environ.get(
    "SONDA_MODELOS",
    f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0,{PREFIJO}.anthropic.claude-sonnet-4-6",
).split(",")


@tool
def buscar(consulta: str) -> str:
    """Busca en los documentos."""
    return f"resultados de {consulta}"


def usuario(t):
    return {"role": "user", "content": [{"text": t}]}


TOOL_USE = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "tooluse_a1", "name": "buscar",
                                                           "input": {"consulta": "ventas"}}}]}
TOOL_RESULT = {"role": "user", "content": [{"toolResult": {"toolUseId": "tooluse_a1", "status": "success",
                                                           "content": [{"text": "ventas: 42"}]}}]}

CASOS = {
    "colgado": [usuario("Mi color favorito es el verde.")],
    "tras herramienta": [usuario("¿Cuánto vendimos?"), TOOL_USE, TOOL_RESULT],
    "tool_use reparado": [usuario("¿Cuánto vendimos?"), TOOL_USE],
}
PREGUNTA = {"colgado": "¿Cuál es mi color favorito? Responde con una palabra.",
            "tras herramienta": "¿Qué cifra salió? Responde solo con ella.",
            "tool_use reparado": "Responde solo: ok"}

sesion = boto3.Session(region_name=REGION)
bien = total = 0
for modelo in MODELOS:
    for nombre, mensajes in CASOS.items():
        total += 1
        a = Agent(model=BedrockModel(model_id=modelo, boto_session=sesion, max_tokens=100), tools=[buscar],
                  messages=[dict(m) for m in mensajes], callback_handler=None)
        try:
            r = a(PREGUNTA[nombre])
            bien += 1
            print(f"OK  {modelo[:32]:32} {nombre:18} {r.stop_reason} {str(r).strip()[:50]!r}")
        except Exception as e:
            print(f"MAL {modelo[:32]:32} {nombre:18} {type(e).__name__}: {str(e)[:120]}")
print(f"{bien} de {total}")
