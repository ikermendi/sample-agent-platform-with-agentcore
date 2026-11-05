"""Sonda 24: mensajes durante un run con Claude de verdad (fila "Messages during a run" de "Verified").

Qué comprueba, con cada modelo de SONDA_MODELOS, enviando el mensaje mientras corre una herramienta (lo recoge el
hook de `pruebas/kernel/mensajes_run.py` antes de la llamada siguiente, junto al resultado):

  1. Ampliar: "añade también el este" tras consultar el norte; consulta el este y responde con las dos.
  2. Cambiar de idea: "no generes el informe, dame solo la cifra"; no llama a generar_informe.
  3. Parar lo que sobra: en una tarea de tres regiones, "olvida el sur"; no consulta el sur.
  4. Con el plan de `pruebas/kernel/plan.py`: "añade un paso: comparar con el este"; el modelo manda una lista con
     ese paso y lo hace. Cierra con Plan.cerrar(), como el kernel.
  5. Bedrock acepta el orden de bloques: en todas, ninguna llamada falla.

No crea nada en AWS: solo llama a Bedrock por el perfil de la región. Coste: céntimos. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_mensajes_run.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.tools.executors import SequentialToolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kernel"))
from mensajes_run import Buzon  # noqa: E402
from plan import Plan  # noqa: E402

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = "eu" if REGION.startswith("eu-") else "us"
MODELOS = os.environ.get(
    "SONDA_MODELOS",
    f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0,{PREFIJO}.anthropic.claude-sonnet-4-6",
).split(",")
SISTEMA = "Eres el analista de ventas de la empresa. Responde en español, con cifras y sin rodeos."
VENTAS = {"norte": 390, "sur": 280, "este": 443, "oeste": 245}

resultados: list[tuple[bool, str, str]] = []


def comprobar(nombre: str, ok: bool, detalle: str = ""):
    resultados.append((ok, nombre, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def ejecutar(modelo, pregunta, region_que_dispara, mensaje, con_plan=False):
    eventos, llamadas = [], []
    buzon = Buzon(eventos)

    @tool
    def consultar_ventas(region: str) -> str:
        """Ventas del trimestre de una región (norte, sur, este u oeste)."""
        region = region.strip().lower()
        llamadas.append(region)
        if region == region_que_dispara and not buzon.pendientes:
            buzon.pendientes.append(("m1", mensaje))
        return f"{region}: {VENTAS.get(region, 0)} mil €"

    @tool
    def generar_informe(titulo: str, contenido: str) -> str:
        """Genera un informe en PDF."""
        llamadas.append("informe")
        return "Informe generado: output/informe.pdf"

    plan = Plan(eventos)
    bm = BedrockModel(model_id=modelo, boto_session=boto3.Session(region_name=REGION), max_tokens=1024,
                      boto_client_config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
    a = Agent(model=bm, system_prompt=SISTEMA, callback_handler=None, tool_executor=SequentialToolExecutor(),
              tools=([plan.herramienta] if con_plan else []) + [consultar_ventas, generar_informe])
    buzon.registrar(a)
    plan.registrar_hooks(a)
    r = a(pregunta)
    plan.cerrar()
    entregado = any(e["type"] == "user_message" for e in eventos)
    planes = [e["steps"] for e in eventos if e["type"] == "plan"]
    return {"llamadas": llamadas, "texto": str(r), "entregado": entregado, "planes": planes, "fin": r.stop_reason}


def main():
    for modelo in MODELOS:
        corto = modelo.split(".anthropic.")[-1]
        print(f"\n=== {corto} ===", flush=True)

        r = ejecutar(modelo, "Dime las ventas de la región norte.", "norte", "Añade también la región este.")
        comprobar(f"{corto} · ampliar", r["entregado"] and "este" in r["llamadas"] and "443" in r["texto"],
                  str(r["llamadas"]))

        r = ejecutar(modelo, "Consulta las ventas de la región norte y genera un informe en PDF con ellas.", "norte",
                     "Cambio de idea: no generes el informe, dame solo la cifra.")
        comprobar(f"{corto} · cambiar de idea", r["entregado"] and "informe" not in r["llamadas"], str(r["llamadas"]))

        r = ejecutar(modelo, "Consulta una a una, en este orden, las ventas de norte, sur y este y dame el total.",
                     "norte", "Olvida el sur, solo norte y este.")
        comprobar(f"{corto} · quitar", r["entregado"] and "sur" not in r["llamadas"] and "este" in r["llamadas"],
                  str(r["llamadas"]))

        r = ejecutar(modelo, "Haz esto con un plan: consulta las ventas del norte, consulta las del sur y genera un "
                             "informe en PDF con ambas.", "norte",
                     "Añade un paso antes del informe: consultar también las ventas del este.", con_plan=True)
        menciona = ["este" in " ".join(p["text"].lower() for p in plan) for plan in r["planes"]]
        comprobar(f"{corto} · el plan recoge el paso nuevo",
                  r["entregado"] and "este" in r["llamadas"] and menciona[-1],
                  f"menciona el este por plan {menciona} · {r['llamadas']}")
        comprobar(f"{corto} · el plan termina completo",
                  bool(r["planes"]) and all(p["status"] == "completed" for p in r["planes"][-1]))

    fallos = [r for r in resultados if not r[0]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)}")
    for _, n, d in fallos:
        print("FALLA", n, d)


if __name__ == "__main__":
    main()
