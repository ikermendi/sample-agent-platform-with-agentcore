"""Sonda 22: si Claude de verdad delega en los subagentes (fila "Subagents" de "To verify").

Las pruebas locales usan un modelo falso con la llamada ya escrita en el guion, así que no miden la decisión de
delegar. Qué comprueba, con cada modelo de SONDA_MODELOS y el catálogo del diseño (worker, explorer, planner y
reviewer como `as_tool(preserve_context=False)`, cada uno con sus herramientas):

  1. Tareas en las que conviene delegar (4): llama al subagente esperado, con qué brief, y la respuesta acierta.
     Con el `reviewer`, que no tiene herramientas, el brief tiene que llevar el documento: es lo único que ve.
  2. Tareas en las que no conviene (4): no delega.
  3. Coste y contexto: las mismas tareas de 1 sin subagentes, para comparar los tokens de todo el run (principal y
     subagentes) y el mayor contexto que llega a tener el principal.

Las descripciones y las instrucciones son un primer borrador: el documento no las fija todavía, y la sonda mide
justo estos textos. Con SONDA_GUIA=1 repite todo con una guía de delegación en el prompt del sistema, para ver si
las descripciones bastan. Las herramientas son falsas y deterministas. No crea nada en AWS: solo llama a Bedrock
por el perfil de la región. Coste: varios dólares con los valores por defecto, porque los documentos son largos a
propósito (los tokens salen al final); baja con un solo modelo y SONDA_REPETICIONES=1. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_subagentes.py
    SONDA_MODELOS=eu.anthropic.claude-sonnet-4-6 SONDA_REPETICIONES=1 ...   # lo más barato
    SONDA_GUIA=1 ...                                                        # también con la guía
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.models.bedrock import BedrockModel
from strands.tools.executors import SequentialToolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kernel"))
from plan import Plan  # noqa: E402

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = "eu" if REGION.startswith("eu-") else "us"
BARATO = f"{PREFIJO}.anthropic.claude-haiku-4-5-20251001-v1:0"
MODELOS = os.environ.get("SONDA_MODELOS", f"{BARATO},{PREFIJO}.anthropic.claude-sonnet-4-6").split(",")
REPETICIONES = int(os.environ.get("SONDA_REPETICIONES", "2"))
SISTEMA = "Eres el asistente de operaciones de la empresa. Responde en español, con datos y sin rodeos."
GUIA = (
    " Delega en un subagente cuando la tarea exija leer mucho y solo necesites la conclusión, o cuando pida una "
    "revisión independiente; no delegues lo que resuelves con una o dos herramientas. El subagente no ve esta "
    "conversación: pon en su entrada todo lo que necesite."
)

resultados: list[tuple[bool, str, str]] = []


def comprobar(nombre: str, ok: bool, detalle: str = ""):
    resultados.append((ok, nombre, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


# --- documentos falsos: largos a propósito, para que leerlos llene el contexto -----------------------------------
def _relleno(semilla: str, parrafos: int = 24) -> str:
    return "\n".join(
        f"{semilla}, apartado {i}: las partes revisarán este punto cada {30 + i} días, conservarán la documentación "
        f"durante {i + 2} años y comunicarán cualquier cambio por escrito con {10 + i} días de antelación, salvo "
        f"acuerdo expreso en contrario recogido en el anexo {i} y firmado por los responsables de cada parte."
        for i in range(1, parrafos + 1)
    )


CONTRATOS = [f"C-0{i}" for i in range(1, 9)]
CON_RENOVACION = {"C-03", "C-07"}


def _contrato(c: str) -> str:
    final = ("El contrato se renueva automáticamente cada año salvo denuncia con 60 días de antelación."
             if c in CON_RENOVACION else "El contrato termina en su fecha de vencimiento sin prórroga.")
    return f"Contrato {c}\nVencimiento: 31-12-2027\n{_relleno(c)}\nDuración: {final}"


RIESGOS = {"norte": "proveedor", "sur": "impagos", "este": "rotación", "oeste": "logística"}
VENTAS = {"norte": 135_000, "sur": 91_000, "este": 150_000, "oeste": 84_000}


@tool
def listar_contratos() -> str:
    """Lista los contratos vigentes."""
    return ", ".join(CONTRATOS)


@tool
def leer_contrato(contrato: str) -> str:
    """Devuelve el texto completo de un contrato."""
    return _contrato(contrato.strip().upper()) if contrato.strip().upper() in CONTRATOS else "No existe."


@tool
def leer_informe(region: str) -> str:
    """Devuelve el informe mensual completo de una región (norte, sur, este u oeste)."""
    r = region.strip().lower()
    if r not in RIESGOS:
        return f"Región desconocida: {region}"
    return f"Informe de {r}\n{_relleno(r)}\nMayor riesgo del mes: {RIESGOS[r]}."


@tool
def consultar_ventas(region: str) -> str:
    """Ventas del mes de una región."""
    return f"{region}: {VENTAS.get(region.strip().lower(), 0)} €"


@tool
def generar_informe(titulo: str, contenido: str) -> str:
    """Genera un informe en PDF con el título y el contenido en Markdown."""
    return f"Informe generado: output/{titulo.lower().replace(' ', '-')[:40]}.pdf"


LECTURA = [listar_contratos, leer_contrato, leer_informe, consultar_ventas]
TODAS = LECTURA + [generar_informe]

# --- catálogo de subagentes (primer borrador de los textos) -------------------------------------------------------
NO_VE = " No ve esta conversación: pon en la entrada todo lo que necesite."
CATALOGO = {
    "worker": ("Hace cualquier tarea acotada que le describas por completo y devuelve el resultado." + NO_VE,
               "Haz la tarea que te encargan y devuelve solo el resultado.", TODAS, None),
    "explorer": ("Busca información en documentos y datos y devuelve lo que ha encontrado con sus fuentes, sin "
                 "cambiar nada. Úsalo cuando haya mucho que leer y solo necesites la conclusión." + NO_VE,
                 "Busca lo que te piden sin cambiar nada y devuelve lo encontrado con sus fuentes, en pocas líneas.",
                 LECTURA, BARATO),
    "planner": ("Estudia una tarea y devuelve un plan paso a paso, sin ejecutarlo." + NO_VE,
                "Estudia la tarea y devuelve un plan paso a paso. No ejecutes nada.", LECTURA, None),
    "reviewer": ("Revisa un texto o un resultado según los criterios que le des y devuelve una lista de problemas; "
                 "también sirve de árbitro. No tiene herramientas: solo ve lo que le pases." + NO_VE,
                 "Revisa lo que te pasan según los criterios indicados y devuelve una lista de problemas, o, de "
                 "árbitro, decide cuáles son reales.", [], None),
}

# --- tareas -------------------------------------------------------------------------------------------------------
BORRADOR = (
    "Informe de cierre. Ventas del trimestre: enero 120.000 €, febrero 135.000 €, marzo 140.000 €, total 437.500 €. "
    "El cierre se firmó el 31 de febrero de 2026. La región líder fue el norte, con el 36 % del total."
)
# nombre, tarea, subagentes que valen, palabras que debe llevar la respuesta, las que no, texto que debe llevar el brief
CONVIENE = [
    ("explorar", "¿Cuáles de nuestros contratos vigentes se renuevan automáticamente?", {"explorer", "worker"},
     CON_RENOVACION, set(CONTRATOS) - CON_RENOVACION, None),
    ("resumir", "Lee los informes de este mes de las cuatro regiones y dame solo una frase por región con su mayor "
     "riesgo.", {"explorer", "worker"}, set(RIESGOS.values()), set(), None),
    ("revisar", f"Revisa este informe y que un árbitro independiente decida qué errores son reales:\n\n{BORRADOR}",
     {"reviewer"}, {"febrero"}, set(), "437.500"),
    ("planificar", "Queremos renegociar a la vez todos los contratos vigentes. No hagas nada todavía: estudia cómo "
     "abordarlo y dame un plan paso a paso.", {"planner"}, set(), set(), None),
]
NO_CONVIENE = [
    ("dato", "¿Cuánto vendió la región norte este mes?", "135"),
    ("saludo", "Hola, ¿qué tal?", None),
    ("concepto", "¿Qué es el margen bruto? En una frase.", None),
    ("listar", "¿Qué contratos tenemos vigentes?", "C-08"),
]


def crear_modelo(modelo: str):
    return BedrockModel(model_id=modelo, boto_session=boto3.Session(region_name=REGION), max_tokens=2048,
                        boto_client_config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))


def ejecutar(modelo: str, tarea: str, con_subagentes: bool, guia: bool) -> dict:
    """Un run nuevo, como el kernel: herramientas, update_plan y, si toca, el catálogo como tools."""
    subs, herramientas = {}, []
    if con_subagentes:
        for nombre, (descripcion, instrucciones, tools, id_propio) in CATALOGO.items():
            subs[nombre] = Agent(model=crear_modelo(id_propio or modelo), name=nombre, description=descripcion,
                                 system_prompt=instrucciones, tools=list(tools), callback_handler=None,
                                 tool_executor=SequentialToolExecutor())
            herramientas.append(subs[nombre].as_tool(preserve_context=False))
    plan = Plan([])
    a = Agent(model=crear_modelo(modelo), system_prompt=SISTEMA + (GUIA if guia else ""), callback_handler=None,
              tools=[plan.herramienta] + TODAS + herramientas, tool_executor=SequentialToolExecutor())
    delegaciones: list[dict] = []
    directas: list[str] = []

    def antes(ev: BeforeToolCallEvent):
        n = ev.tool_use["name"]
        if n in subs:
            delegaciones.append({"sub": n, "brief": str(ev.tool_use["input"].get("input", "")), "resultado": ""})
        elif n != "update_plan":
            directas.append(n)

    def despues(ev: AfterToolCallEvent):
        if ev.tool_use["name"] in subs and ev.result:
            delegaciones[-1]["resultado"] = " ".join(b.get("text", "") for b in ev.result.get("content", []))

    a.hooks.add_callback(BeforeToolCallEvent, antes)
    a.hooks.add_callback(AfterToolCallEvent, despues)
    r = a(tarea)
    u = r.metrics.accumulated_usage
    # El uso del principal no incluye el de sus subagentes (comprobado con el modelo falso)
    us = [s.event_loop_metrics.accumulated_usage for s in subs.values()]
    ciclos = r.metrics.latest_agent_invocation.cycles if r.metrics.latest_agent_invocation else []
    return {"delegaciones": delegaciones, "directas": directas, "respuesta": str(r),
            "entrada": u["inputTokens"] + sum(x["inputTokens"] for x in us),
            "salida": u["outputTokens"] + sum(x["outputTokens"] for x in us),
            "contexto": max((c.usage["inputTokens"] for c in ciclos), default=0)}


def ronda(modelo: str, guia: bool) -> list[int]:
    corto = modelo.split(".anthropic.")[-1] + (" · guía" if guia else "")
    print(f"\n=== {corto} ===", flush=True)
    totales = [0, 0]
    for nombre, tarea, validos, deben, no_deben, en_brief in CONVIENE:
        con = []
        for rep in range(1, REPETICIONES + 1):
            r = ejecutar(modelo, tarea, True, guia)
            con.append(r)
            subs = [d["sub"] for d in r["delegaciones"]]
            print(f"  {nombre} {rep}: subagentes {subs or '-'} | directas {len(r['directas'])} | "
                  f"entrada {r['entrada']}, salida {r['salida']}, contexto máx. {r['contexto']}", flush=True)
            for d in r["delegaciones"]:
                print(f"    brief a {d['sub']} ({len(d['brief'])} car.): {d['brief'][:200]!r}")
            comprobar(f"{corto} · {nombre} {rep}: delega en el subagente esperado", bool(validos & set(subs)),
                      ", ".join(subs) or "no delega")
            texto = r["respuesta"].lower()
            faltan = [p for p in deben if p.lower() not in texto]
            sobran = [p for p in no_deben if p.lower() in texto]
            comprobar(f"{corto} · {nombre} {rep}: la respuesta acierta", not faltan and not sobran,
                      f"faltan {faltan}, sobran {sobran}" if faltan or sobran else "")
            if en_brief:
                briefs = [d for d in r["delegaciones"] if d["sub"] == "reviewer"]
                comprobar(f"{corto} · {nombre} {rep}: el brief lleva lo que el subagente no puede ver",
                          bool(briefs) and all(en_brief in d["brief"] for d in briefs),
                          f"{len(briefs)} llamadas al reviewer")
            totales[0] += r["entrada"]
            totales[1] += r["salida"]
        # La referencia sin subagentes: una vez, las mismas herramientas en el principal
        sin = ejecutar(modelo, tarea, False, guia)
        totales[0] += sin["entrada"]
        totales[1] += sin["salida"]
        media = lambda k: sum(x[k] for x in con) // len(con)
        print(f"  {nombre}, sin subagentes: entrada {sin['entrada']}, salida {sin['salida']}, contexto máx. "
              f"{sin['contexto']} · con ellos (media): entrada {media('entrada')}, salida {media('salida')}, "
              f"contexto máx. {media('contexto')}", flush=True)
    for nombre, tarea, debe in NO_CONVIENE:
        for rep in range(1, REPETICIONES + 1):
            r = ejecutar(modelo, tarea, True, guia)
            subs = [d["sub"] for d in r["delegaciones"]]
            comprobar(f"{corto} · {nombre} {rep}: no delega", not subs, ", ".join(subs))
            if debe:
                comprobar(f"{corto} · {nombre} {rep}: la respuesta acierta", debe in r["respuesta"].replace(".", ""))
            totales[0] += r["entrada"]
            totales[1] += r["salida"]
    print(f"  tokens de la ronda: {totales[0]} de entrada, {totales[1]} de salida", flush=True)
    return totales


def main():
    entrada = salida = 0
    for modelo in MODELOS:
        for guia in ([False, True] if os.environ.get("SONDA_GUIA") == "1" else [False]):
            e, s = ronda(modelo, guia)
            entrada += e
            salida += s
    fallos = [r for r in resultados if not r[0]]
    print(f"\n{len(resultados) - len(fallos)} de {len(resultados)} · tokens: {entrada} de entrada, {salida} de salida")
    for _, n, d in fallos:
        print("FALLA", n, d)


if __name__ == "__main__":
    main()
