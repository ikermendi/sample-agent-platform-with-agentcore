"""Sonda 20: el plan en el chat con Claude de verdad (fila "Plan in the chat" de "To verify").

Qué comprueba, con cada modelo de SONDA_MODELOS:

  1. Tareas de varios pasos (3): el modelo abre un plan con update_plan antes de trabajar, lo va marcando a medida
     que avanza (hay algún plan con pasos hechos y pendientes a la vez, no solo uno al principio y otro al final)
     y termina con todos los pasos `completed`.
  2. Preguntas cortas (3): no abre ningún plan.
  3. Coste: las mismas tareas de varios pasos sin update_plan, para comparar llamadas al modelo y tokens.

La herramienta es la del prototipo del kernel (`pruebas/kernel/plan.py`), con su descripción, su validación y
sus hooks de eventos. Las demás herramientas son falsas y deterministas. No crea nada en AWS: solo llama a
Bedrock por el perfil de la región. Coste: céntimos. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_plan.py
    SONDA_MODELOS=eu.anthropic.claude-sonnet-4-6 ...   # solo un modelo
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent
from strands.models.bedrock import BedrockModel
from strands.tools.executors import SequentialToolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kernel"))
from plan import Plan  # noqa: E402

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = "eu" if REGION.startswith("eu-") else "us"
MODELOS = os.environ.get(
    "SONDA_MODELOS",
    f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0,{PREFIJO}.anthropic.claude-sonnet-4-6",
).split(",")
SISTEMA = "Eres el analista de ventas de la empresa. Responde en español, con cifras y sin rodeos."

resultados: list[tuple[bool, str, str]] = []


def comprobar(nombre: str, ok: bool, detalle: str = ""):
    resultados.append((ok, nombre, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


# --- herramientas falsas ---------------------------------------------------------------------------------------
VENTAS = {"norte": (120_000, 135_000), "sur": (98_000, 91_000), "este": (143_000, 150_000), "oeste": (77_000, 84_000)}


@tool
def consultar_ventas(region: str, mes: str = "") -> str:
    """Ventas de una región (norte, sur, este u oeste); con mes (enero o febrero), solo las de ese mes."""
    r = VENTAS.get(region.strip().lower())
    if not r:
        return f"Región desconocida: {region}"
    if mes.lower().startswith("ene"):
        return f"{region}, enero: {r[0]} €"
    if mes.lower().startswith("feb"):
        return f"{region}, febrero: {r[1]} €"
    return f"{region}, trimestre: {sum(r) + r[1]} € (enero {r[0]} €, febrero {r[1]} €)"


@tool
def buscar_documentos(consulta: str) -> str:
    """Busca en los documentos de la empresa."""
    if "descuento" in consulta.lower():
        return "Política de descuentos 2026: como mucho un 8 % de descuento medio al mes por región."
    return "Sin resultados relevantes."


@tool
def descuento_medio(region: str, mes: str) -> str:
    """Descuento medio aplicado en una región y un mes."""
    return f"{region}, {mes}: descuento medio del 9,5 %"


@tool
def buscar_clientes(region: str, cuantos: int = 3) -> str:
    """Los clientes con más ventas de una región."""
    return "; ".join(f"Cliente {region[:1].upper()}{i} ({100 - 10 * i} mil €)" for i in range(1, cuantos + 1))


@tool
def ver_oportunidades(cliente: str) -> str:
    """Oportunidades abiertas de un cliente."""
    n = sum(map(ord, cliente)) % 3 + 1
    return f"{cliente}: {n} oportunidades abiertas, {n * 25} mil € en total, la mayor cierra en {n * 10} días"


@tool
def generar_informe(titulo: str, contenido: str) -> str:
    """Genera un informe en PDF con el título y el contenido en Markdown."""
    return f"Informe generado: output/{titulo.lower().replace(' ', '-')[:40]}.pdf"


HERRAMIENTAS = [consultar_ventas, buscar_documentos, descuento_medio, buscar_clientes, ver_oportunidades,
                generar_informe]

LARGAS = [
    "Prepara el informe de ventas del trimestre: consulta las ventas de las regiones norte, sur y este, calcula el "
    "total y el porcentaje de cada una y genera el informe en PDF con un resumen.",
    "Compara las ventas de enero y febrero de la región oeste, busca en los documentos la política de descuentos, "
    "comprueba si el descuento medio de febrero en el oeste la cumple y genera un informe con la conclusión.",
    "Busca los tres clientes con más ventas del sur, mira las oportunidades abiertas de cada uno, dime a cuál "
    "llamar primero y por qué, y déjalo en un informe.",
]
CORTAS = [
    "¿Cuánto vendió la región norte este trimestre?",
    "Hola, ¿qué tal?",
    "¿Qué es el margen bruto? En una frase.",
]


def ejecutar(modelo: str, pregunta: str, con_plan: bool):
    eventos: list[dict] = []
    plan = Plan(eventos)
    errores_plan: list[str] = []
    llamadas_modelo = [0]
    bm = BedrockModel(model_id=modelo, boto_session=boto3.Session(region_name=REGION), max_tokens=2048,
                      boto_client_config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
    a = Agent(model=bm, system_prompt=SISTEMA, callback_handler=None, tool_executor=SequentialToolExecutor(),
              tools=([plan.herramienta] if con_plan else []) + HERRAMIENTAS)
    plan.registrar_hooks(a)

    def error_plan(ev: AfterToolCallEvent):
        if ev.tool_use["name"] == "update_plan" and ev.result["status"] == "error":
            errores_plan.append(ev.result["content"][0]["text"][:120])

    a.hooks.add_callback(AfterToolCallEvent, error_plan)
    r = a(pregunta)
    plan.cerrar()  # lo que hace el kernel al terminar con reason = ok
    u = r.metrics.accumulated_usage
    return {"eventos": eventos, "errores": errores_plan, "ciclos": r.metrics.cycle_count,
            "entrada": u["inputTokens"], "salida": u["outputTokens"]}


def secuencia(eventos):
    """Lo que vería el usuario: P(hechos/total) por cada plan y el nombre de cada herramienta."""
    s = []
    for e in eventos:
        if e["type"] == "plan":
            hechos = sum(p["status"] == "completed" for p in e["steps"])
            s.append(f"P({hechos}/{len(e['steps'])})")
        elif e["type"] == "tool_start":
            s.append(e["name"])
    return " ".join(s)


def main():
    for modelo in MODELOS:
        corto = modelo.split(".anthropic.")[-1]
        print(f"\n=== {corto} ===", flush=True)
        con, sin = [], []
        for i, t in enumerate(LARGAS, 1):
            r = ejecutar(modelo, t, con_plan=True)
            con.append(r)
            planes = [e for e in r["eventos"] if e["type"] == "plan"]
            print(f"  larga {i}: {secuencia(r['eventos'])}", flush=True)
            if r["errores"]:
                print(f"    errores de update_plan: {r['errores']}")
            comprobar(f"{corto} · larga {i}: abre un plan", bool(planes), f"{len(planes)} actualizaciones")
            if not planes:
                continue
            progresivo = any(0 < sum(p["status"] == "completed" for p in e["steps"]) < len(e["steps"])
                             for e in planes)
            comprobar(f"{corto} · larga {i}: lo marca a medida que avanza", progresivo)
            final = planes[-1]["steps"]
            comprobar(f"{corto} · larga {i}: termina con todo completed",
                      all(p["status"] == "completed" for p in final),
                      " · ".join(f"{p['status'][:4]} {p['text']}" for p in final))
            comprobar(f"{corto} · larga {i}: lo abre antes de trabajar", r["eventos"][0]["type"] == "plan")
        for i, t in enumerate(CORTAS, 1):
            r = ejecutar(modelo, t, con_plan=True)
            planes = [e for e in r["eventos"] if e["type"] == "plan"]
            comprobar(f"{corto} · corta {i}: no abre plan", not planes, secuencia(r["eventos"]) or "sin herramientas")
        for t in LARGAS:
            sin.append(ejecutar(modelo, t, con_plan=False))
        e_con, e_sin = sum(r["entrada"] for r in con), sum(r["entrada"] for r in sin)
        s_con, s_sin = sum(r["salida"] for r in con), sum(r["salida"] for r in sin)
        c_con, c_sin = sum(r["ciclos"] for r in con), sum(r["ciclos"] for r in sin)
        print(f"  coste de las 3 largas · con plan: {c_con} llamadas, {e_con} de entrada, {s_con} de salida · "
              f"sin plan: {c_sin} llamadas, {e_sin} de entrada, {s_sin} de salida · "
              f"entrada +{(e_con / e_sin - 1) * 100:.0f} %, salida +{(s_con / s_sin - 1) * 100:.0f} %", flush=True)

    fallos = [r for r in resultados if not r[0]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)}")
    for _, n, d in fallos:
        print("FALLA", n, d)


if __name__ == "__main__":
    main()
