"""Sonda 23: Continuar tras un tope por run con Claude de verdad (fila "Continue on long tasks" de "To verify").

Cuando `Limits` para el run justo después de una herramienta, la conversación acaba en un `toolResult` y el mensaje
"Continúa" del usuario llega al modelo como un segundo mensaje de usuario seguido. Qué comprueba, con cada modelo de
SONDA_MODELOS y por los dos caminos del kernel (el mismo `Agent` reutilizado y el `Agent` restaurado del snapshot):

  1. Con `turns` = 2 una tarea de cuatro consultas para con `limit_turns` y deja un `toolResult` al final.
  2. Bedrock acepta el "Continúa" (dos mensajes de usuario seguidos), sin ValidationException.
  3. Solo con esa palabra el modelo retoma la tarea: no repite consultas ya hechas y acaba con las cuatro cifras.

Las herramientas son falsas y deterministas. No crea nada en AWS: solo llama a Bedrock. Coste: céntimos. Uso:

    env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY AWS_PROFILE=sonda SONDA_REGION=eu-north-1 \\
      .venv312/bin/python sonda_continuar.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections import Counter

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = "eu" if REGION.startswith("eu-") else "us"
MODELOS = os.environ.get(
    "SONDA_MODELOS",
    f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0,{PREFIJO}.anthropic.claude-sonnet-4-6",
).split(",")
SISTEMA = "Eres el analista de ventas de la empresa. Responde en español. Consulta una sola región en cada turno."
TAREA = "Dame las ventas de las regiones norte, sur, este y oeste, una consulta por región, y al final las cuatro cifras."
LIMITES = {"turns": 2}
MAX_CONTINUAR = 4
VENTAS = {"norte": "120483", "sur": "98217", "este": "143659", "oeste": "77341"}

resultados: list[tuple[bool, str, str]] = []
consultas: Counter = Counter()


def comprobar(nombre: str, ok: bool, detalle: str = ""):
    resultados.append((ok, nombre, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


@tool
def consultar_ventas(region: str) -> str:
    """Ventas en euros de una región: norte, sur, este u oeste."""
    consultas[region.strip().lower()] += 1
    return f"{region}: {VENTAS.get(region.strip().lower(), 'desconocida')} €"


def modelo(model_id: str) -> BedrockModel:
    return BedrockModel(model_id=model_id, boto_session=boto3.Session(region_name=REGION), max_tokens=1024,
                        boto_client_config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))


def probar(model_id: str, camino: str, crear):
    nombre = f"{model_id.split('.')[-1][:22]} · {camino}"
    consultas.clear()
    a = crear()
    r = a(TAREA, limits=LIMITES)
    ultimo = a.messages[-1]
    comprobar(f"{nombre}: para con limit_turns tras una herramienta",
              r.stop_reason == "limit_turns" and any("toolResult" in b for b in ultimo["content"]),
              f"stop_reason={r.stop_reason}")
    if r.stop_reason != "limit_turns":
        return

    continuados = 0
    try:
        while r.stop_reason != "end_turn" and continuados < MAX_CONTINUAR:
            continuados += 1
            a = crear() if camino == "snapshot" else a
            r = a("Continúa", limits=LIMITES)
    except Exception as e:
        comprobar(f"{nombre}: Bedrock acepta Continúa", False, f"{type(e).__name__}: {str(e)[:300]}")
        return
    comprobar(f"{nombre}: Bedrock acepta Continúa", True, f"{continuados} Continúa")

    final = str(r)
    faltan = [v for v in VENTAS.values() if v not in final.replace(".", "").replace(",", "").replace(" ", "")]
    comprobar(f"{nombre}: termina la tarea con las cuatro cifras", r.stop_reason == "end_turn" and not faltan,
              f"stop_reason={r.stop_reason}, faltan={faltan}")
    repetidas = {k: n for k, n in consultas.items() if n > 1}
    comprobar(f"{nombre}: no repite consultas", not repetidas and set(consultas) == set(VENTAS), str(dict(consultas)))


def main():
    for model_id in MODELOS:
        probar(model_id, "mismo Agent",
               lambda: Agent(model=modelo(model_id), system_prompt=SISTEMA, tools=[consultar_ventas],
                             callback_handler=None))
        with tempfile.TemporaryDirectory() as d:
            sesion = f"sonda-{abs(hash(model_id))}"
            probar(model_id, "snapshot",
                   lambda: Agent(model=modelo(model_id), system_prompt=SISTEMA, tools=[consultar_ventas],
                                 callback_handler=None,
                                 session_manager=SnapshotSessionManager(sesion, storage=LocalFileStorage(d),
                                                                        save_latest_on="message")))
    bien = sum(ok for ok, _, _ in resultados)
    print(f"\n{bien} de {len(resultados)}")
    sys.exit(0 if bien == len(resultados) else 1)


if __name__ == "__main__":
    main()
