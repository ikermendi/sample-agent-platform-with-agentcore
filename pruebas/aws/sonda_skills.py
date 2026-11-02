"""Sonda 16: herramientas de skills con Claude de verdad (fila "Herramientas de skills" de "Por verificar").

Qué comprueba:

  1. Panel, sin AWS: el esquema de tools.yaml (`sonda_skills/esquema_tools.json`) y lo que el panel valida al
     subir una versión (scripts dentro de scripts/, imports declarados, librerías de la imagen, nombres, resumen
     con campos de la salida, SKILL.md); la diferencia que enseña la revisión (informe-pdf 3 → 4); y, al publicar
     un agente, que ningún nombre de herramienta se repite entre sus skills, el hub y el kernel.
  2. Tokens: la primera llamada de un agente con 10 skills (informe-pdf y 9 de relleno con 3 herramientas cada
     una) con carga progresiva frente a todas las herramientas registradas desde el principio.
  3. La tarea completa con carga progresiva y caché de prompts: el modelo activa informe-pdf, el kernel registra
     generar_pdf en AfterToolCallEvent, aparece en la llamada siguiente, Claude la usa y el PDF queda en salida/;
     qué pasa con la caché en cada llamada; y volver a activar la skill no registra dos veces.
  4. La misma tarea con todas las herramientas desde el principio, para comparar tokens y coste.
  5. Montar el Agent desde el snapshot (microVM nueva): con las skills de get_activated_skills registradas al
     montar, el modelo usa generar_pdf sin volver a activar; y qué pasa si el kernel no las registra.
  6. Strands no valida la entrada de una AgentTool propia: la valida el kernel.

No crea nada en AWS: solo llama a Bedrock (Claude Sonnet 4.6 por defecto, por el perfil de la región). Coste:
céntimos a menos de un dólar. Necesita strands-agents==1.63.0, pyyaml y jsonschema en el entorno. Uso:

    AWS_PROFILE=sonda SONDA_REGION=eu-north-1 .venv312/bin/python sonda_skills.py
    SONDA_MODELO=eu.anthropic.claude-haiku-4-5-20251001-v1:0 ...   # con Haiku 4.5
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import boto3
from botocore.config import Config
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent
from strands.models.bedrock import BedrockModel
from strands.models.model import CacheConfig
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from strands.tools.executors.sequential import SequentialToolExecutor
from strands.vended_plugins.skills import AgentSkills, Skill

sys.path.insert(0, str(Path(__file__).parent / "sonda_skills"))
import tools_yaml as ty  # noqa: E402

REGION = os.environ.get("SONDA_REGION", "us-east-1")
PREFIJO = REGION.split("-")[0] if not REGION.startswith("us-") else "us"
MODELO = os.environ.get("SONDA_MODELO", f"{PREFIJO}.anthropic.claude-sonnet-4-6")
# $ por millón: entrada, salida, escritura en caché (5 min), lectura de caché
PRECIOS = {"haiku": (1.0, 5.0, 1.25, 0.10), "sonnet": (3.0, 15.0, 3.75, 0.30)}
PRECIO = PRECIOS["haiku" if "haiku" in MODELO else "sonnet"]
SKILLS_DIR = Path(__file__).parent / "sonda_skills" / "skills"
PAQUETES_IMAGEN = {"pandas", "matplotlib", "pptx", "reportlab"}  # módulos del venv de datos de la imagen (sonda 10)

resultados = []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def info(*a):
    print("     ", *a, flush=True)


# ---------------------------------------------------------------- el modelo, midiendo cada llamada

class Medido(BedrockModel):
    """BedrockModel que apunta, por llamada, las herramientas enviadas y el uso que devuelve Bedrock."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.llamadas: list[dict] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        reg = {"herramientas": [t["name"] for t in tool_specs or []], "uso": {}, "t0": time.monotonic()}
        self.llamadas.append(reg)
        async for ev in super().stream(messages, tool_specs, system_prompt, **kw):
            if "metadata" in ev:
                reg["uso"] = ev["metadata"].get("usage", {})
            yield ev
        reg["s"] = time.monotonic() - reg.pop("t0")


def uso_total(u: dict) -> int:
    return u.get("inputTokens", 0) + u.get("cacheReadInputTokens", 0) + u.get("cacheWriteInputTokens", 0)


def coste(llamadas) -> float:
    pe, ps, pw, pr = PRECIO
    return sum(l["uso"].get("inputTokens", 0) * pe + l["uso"].get("outputTokens", 0) * ps
               + l["uso"].get("cacheWriteInputTokens", 0) * pw + l["uso"].get("cacheReadInputTokens", 0) * pr
               for l in llamadas) / 1e6


def tabla(llamadas, desde=0):
    for i, l in enumerate(llamadas[desde:], desde + 1):
        u = l["uso"]
        skill_tools = [h for h in l["herramientas"] if h in TODAS_LAS_DE_SKILLS]
        info(f"llamada {i}: {len(l['herramientas'])} herramientas ({len(skill_tools)} de skills) · "
             f"entrada {u.get('inputTokens', 0)} · caché leída {u.get('cacheReadInputTokens', 0)} · "
             f"caché escrita {u.get('cacheWriteInputTokens', 0)} · salida {u.get('outputTokens', 0)} · {l.get('s', 0):.1f} s")


# ---------------------------------------------------------------- lo que el agente tiene siempre

INSTRUCCIONES = """Eres el analista de ventas de la empresa. Respondes en español, con cifras, y citas de dónde salen.
Si te piden un informe, usa la skill informe-pdf. No inventes cifras.

Política de respuesta:
""" + "\n".join(
    f"- Norma {i}: cuando el usuario pregunte por la línea de producto {i} ({n}), indica su margen bruto del último "
    f"trimestre, compáralo con el del mismo trimestre del año anterior y señala si la diferencia supera el {i % 7 + 2} %; "
    f"si no hay datos, dilo y propón la consulta que haría falta en el CRM."
    for i, n in enumerate(["climatización", "iluminación", "cableado", "domótica", "seguridad", "energía solar",
                           "bombas de calor", "ventilación", "aislamiento", "fontanería"] * 8, 1))


def herramientas_hub(espacio: Path):
    @tool
    def buscar_documentos(consulta: str, max_resultados: int = 5) -> str:
        """Busca en la Knowledge Base de ventas (informes trimestrales, tarifas, políticas comerciales) y devuelve
        los fragmentos más relevantes con su documento de origen y su fecha.

        Args:
            consulta: Lo que se busca, en lenguaje natural.
            max_resultados: Cuántos fragmentos devolver (1-20).
        """
        return "Sin resultados."

    @tool
    def buscar_cliente(nombre: str, pais: str | None = None) -> str:
        """Busca un cliente en el CRM por nombre o CIF y devuelve su ficha: sector, comercial asignado y facturación.

        Args:
            nombre: Nombre comercial, razón social o CIF.
            pais: Código ISO del país, si se conoce.
        """
        return "Sin resultados."

    @tool
    def ver_oportunidad(id_oportunidad: str) -> str:
        """Devuelve una oportunidad del CRM: importe, fase, probabilidad, fecha prevista de cierre y notas.

        Args:
            id_oportunidad: Identificador de la oportunidad en el CRM.
        """
        return "No existe."

    @tool
    def consultar_ventas(trimestre: str, zona: str | None = None, linea: str | None = None) -> str:
        """Devuelve las ventas agregadas de un trimestre, opcionalmente por zona y línea de producto.

        Args:
            trimestre: Trimestre en formato AAAA-Tn.
            zona: Norte, Sur, Este u Oeste.
            linea: Línea de producto.
        """
        return "Sin datos para ese trimestre."

    @tool
    def escribir_fichero(ruta: str, contenido: str) -> str:
        """Escribe un fichero de texto en el espacio del chat (por ejemplo, trabajo/informe.md).

        Args:
            ruta: Ruta relativa al espacio del chat.
            contenido: Texto del fichero.
        """
        destino = (espacio / ruta).resolve()
        if not destino.is_relative_to(espacio.resolve()):
            return "Ruta fuera del espacio del chat."
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido)
        return f"Guardado {ruta} ({len(contenido)} caracteres)."

    return [buscar_documentos, buscar_cliente, ver_oportunidad, consultar_ventas, escribir_fichero]


# ---------------------------------------------------------------- 9 skills de relleno, 3 herramientas cada una

RELLENO = {
    "graficos": ("Genera gráficos de barras, líneas o tarta a partir de CSV del espacio del chat.",
                 ["grafico_barras", "grafico_lineas", "grafico_tarta"]),
    "excel-tablas": ("Crea y modifica hojas de Excel con tablas, fórmulas y formato condicional.",
                     ["crear_hoja", "anadir_tabla", "aplicar_formato"]),
    "traducir-documento": ("Traduce documentos Word o PDF conservando el formato.",
                           ["traducir_docx", "traducir_pdf", "glosario_terminos"]),
    "resumen-reunion": ("Resume la transcripción de una reunión en acuerdos, tareas y responsables.",
                        ["extraer_acuerdos", "extraer_tareas", "generar_acta"]),
    "comparar-contratos": ("Compara dos versiones de un contrato y señala cláusulas cambiadas y riesgos.",
                           ["comparar_versiones", "listar_clausulas", "marcar_riesgos"]),
    "presentacion": ("Genera presentaciones PowerPoint a partir de un guion en Markdown.",
                     ["crear_presentacion", "anadir_diapositiva", "aplicar_plantilla"]),
    "correo-cliente": ("Redacta correos a clientes con el tono y la firma de la empresa.",
                       ["redactar_correo", "adaptar_tono", "plantilla_correo"]),
    "analisis-ventas": ("Calcula variaciones, márgenes y previsiones a partir de las ventas del CRM.",
                        ["variacion_interanual", "margen_por_linea", "prevision_trimestre"]),
    "calendario-equipo": ("Consulta disponibilidad del equipo y propone huecos para reuniones.",
                          ["ver_disponibilidad", "proponer_huecos", "resumen_semana"]),
}


def tools_relleno(skill: str) -> dict:
    desc, nombres = RELLENO[skill]
    return {"formato": 1, "librerias": [], "herramientas": [{
        "nombre": n, "script": f"scripts/{n}.py", "etiqueta": n.replace("_", " "),
        "descripcion": f"{desc} Esta herramienta ({n.replace('_', ' ')}) trabaja sobre ficheros del espacio del chat "
                       f"y devuelve la ruta del resultado y un resumen de lo hecho, sin modificar los originales.",
        "entrada": {"type": "object", "required": ["origen"], "additionalProperties": False, "properties": {
            "origen": {"type": "string", "description": "Ruta del fichero de entrada, relativa al espacio del chat"},
            "destino": {"type": "string", "description": "Ruta del resultado; por defecto, en salida/ con el mismo nombre"},
            "opciones": {"type": "object", "description": "Opciones específicas de la operación",
                         "properties": {"idioma": {"type": "string", "enum": ["es", "en", "fr", "de"]},
                                        "detalle": {"type": "string", "enum": ["breve", "normal", "completo"]}}},
            "columnas": {"type": "array", "items": {"type": "string"}, "description": "Columnas o campos a usar"},
        }}} for n in nombres]}


TODAS_LAS_DE_SKILLS = {h["nombre"] for s in RELLENO for h in tools_relleno(s)["herramientas"]} | {"generar_pdf"}


# ---------------------------------------------------------------- el kernel, en pequeño

class Kernel:
    """Lo mínimo del kernel para esta fila: skills como instancias Skill, registro al activar y al montar."""

    def __init__(self, espacio: Path, modo: str = "progresivo", cache: bool = True, sesion=None,
                 registrar_al_montar: bool = True, validar: bool = True):
        self.espacio, self.modo = espacio, modo
        carpeta = SKILLS_DIR / "informe-pdf" / "3"
        doc, errores = ty.validar_version(carpeta, "informe-pdf", PAQUETES_IMAGEN)
        assert not errores, errores
        self.versiones = {"informe-pdf": (carpeta, doc)} | {s: (None, tools_relleno(s)) for s in RELLENO}
        skills = [Skill.from_content((carpeta / "SKILL.md").read_text())] + [
            Skill(name=s, description=d, instructions=f"# {s}\n\nUsa sus herramientas: {', '.join(n)}.")
            for s, (d, n) in RELLENO.items()]
        self.registradas: dict[str, ty.HerramientaDeScript] = {}
        self.registros = 0
        self.validar = validar
        self.plugin = AgentSkills(skills=skills)
        self.modelo = Medido(
            model_id=MODELO, boto_session=boto3.Session(region_name=REGION), max_tokens=2000, temperature=0.2,
            boto_client_config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, read_timeout=120),
            **({"cache_config": CacheConfig(strategy="auto", tools_ttl=True)} if cache else {}))
        self.agente = Agent(model=self.modelo, system_prompt=INSTRUCCIONES, tools=herramientas_hub(espacio),
                            plugins=[self.plugin], callback_handler=None, session_manager=sesion,
                            tool_executor=SequentialToolExecutor())
        self.agente.hooks.add_callback(AfterToolCallEvent, self._al_terminar_herramienta)
        if modo == "todo":
            for s in self.versiones:
                self.registrar(s)
        elif registrar_al_montar:
            for s in self.plugin.get_activated_skills(self.agente):
                self.registrar(s)

    def _herramienta(self, skill, h):
        carpeta, _ = self.versiones[skill]
        if carpeta is None:  # relleno: un script que no hace nada
            carpeta = Path(tempfile.mkdtemp())
            (carpeta / "scripts").mkdir()
            (carpeta / h["script"]).write_text("import json; print(json.dumps({'ok': True}))\n")
        return ty.HerramientaDeScript(skill, carpeta, h, self.espacio, validar=self.validar)

    def registrar(self, skill: str):
        for h in self.versiones[skill][1]["herramientas"]:
            if h["nombre"] in self.registradas:
                continue
            herramienta = self._herramienta(skill, h)
            self.agente.tool_registry.register_tool(herramienta)
            self.registradas[h["nombre"]] = herramienta
            self.registros += 1

    def _al_terminar_herramienta(self, ev: AfterToolCallEvent):
        if ev.tool_use["name"] != "skills" or isinstance(ev.result, Exception):
            return
        nombre = (ev.tool_use.get("input") or {}).get("skill_name")
        if ev.result.get("status") == "success" and nombre in self.versiones:
            self.registrar(nombre)

    def llamadas_a(self, nombre):
        return [c for m in self.agente.messages for c in m["content"]
                if "toolUse" in c and c["toolUse"]["name"] == nombre]


def pdf_valido(ruta: Path) -> bool:
    datos = ruta.read_bytes()
    if not datos.startswith(b"%PDF-") or not datos.rstrip().endswith(b"%%EOF"):
        return False
    xref = int(re.search(rb"startxref\n(\d+)", datos).group(1))
    offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", datos[xref:])]
    return all(re.match(rb"\d+ 0 obj", datos[o:o + 12]) for o in offsets)


# ---------------------------------------------------------------- 1. panel

def paso_1():
    print("\n== 1. Panel: esquema, validación y revisión ==")
    v3, v4 = SKILLS_DIR / "informe-pdf" / "3", SKILLS_DIR / "informe-pdf" / "4"
    doc3, e3 = ty.validar_version(v3, "informe-pdf", PAQUETES_IMAGEN)
    anota("1.1 informe-pdf@3 válida", not e3, f"hash {ty.hash_version(v3)[:16]}…" if not e3 else "; ".join(e3))

    def caso(nombre, cambia, espera):
        d = Path(tempfile.mkdtemp()) / "informe-pdf"
        shutil.copytree(v3, d)
        cambia(d)
        _, errs = ty.validar_version(d, "informe-pdf", PAQUETES_IMAGEN)
        ok = any(espera in e for e in errs)
        anota(f"1.2 rechaza: {nombre}", ok, "; ".join(errs)[:220])

    def yaml_sub(a, b):
        return lambda d: (d / "tools.yaml").write_text((d / "tools.yaml").read_text().replace(a, b, 1))

    caso("nombre con guion", yaml_sub("nombre: generar_pdf", "nombre: generar-pdf"), "does not match")
    caso("campo desconocido", yaml_sub("escribe_ficheros:", "escribe_fichero:"), "Additional properties")
    caso("sin formato", yaml_sub("formato: 1\n", ""), "'formato' is a required property")
    caso("nombre reservado", yaml_sub("nombre: generar_pdf", "nombre: skills"), "reservado")
    caso("script que no existe", yaml_sub("scripts/generar_pdf.py", "scripts/no_esta.py"), "no existe")
    caso("script enlazado fuera de scripts/",
         lambda d: ((d / "scripts" / "fuera.py").symlink_to("/etc/hosts"),
                    yaml_sub("scripts/generar_pdf.py", "scripts/fuera.py")(d)), "sale de scripts/")
    caso("entrada que no es un esquema JSON", yaml_sub("titulo: { type: string", "titulo: { type: texto"),
         "no es un esquema JSON válido")
    caso("resumen con un campo que no está en la salida", yaml_sub("({paginas} págs.)", "({hojas} hojas)"),
         "no están en la salida")
    caso("import sin declarar",
         lambda d: (d / "scripts" / "generar_pdf.py").write_text("import requests\n" + (d / "scripts" / "generar_pdf.py").read_text()),
         "importa 'requests'")
    caso("SKILL.md con otro nombre",
         lambda d: (d / "SKILL.md").write_text((d / "SKILL.md").read_text().replace("name: informe-pdf", "name: informe")),
         "se esperaba 'informe-pdf'")

    _, e4 = ty.validar_version(v4, "informe-pdf", PAQUETES_IMAGEN)
    anota("1.3 informe-pdf@4 rechazada por una librería que no está en la imagen", e4 == [
        "librería 'weasyprint' no está en la imagen del kernel"], "; ".join(e4))
    dif = ty.diferencia(v3, v4)
    for l in dif:
        info("revisión 3 → 4:", l)
    anota("1.3 la revisión enseña herramienta nueva, parámetro nuevo y librería nueva",
          any("herramienta añadida: contar_paginas" in l for l in dif)
          and any("parámetro nuevo orientacion" in l for l in dif) and any("librería nueva: weasyprint" in l for l in dif))

    otra = {"formato": 1, "herramientas": [dict(doc3["herramientas"][0])]}
    e_ag = ty.validar_agente({"informe-pdf": doc3, "otra-pdf": otra}, {"buscar_documentos"})
    anota("1.4 al publicar un agente: dos skills con la misma herramienta", any("otra-pdf: generar_pdf ya lo da informe-pdf" in e for e in e_ag), "; ".join(e_ag))
    e_hub = ty.validar_agente({"informe-pdf": doc3}, {"generar_pdf"})
    anota("1.4 al publicar un agente: herramienta de skill con el nombre de una del hub", any("ya lo da hub" in e for e in e_hub), "; ".join(e_hub))
    anota("1.4 informe-pdf + 9 skills de relleno + hub sin choques",
          not ty.validar_agente({"informe-pdf": doc3} | {s: tools_relleno(s) for s in RELLENO},
                                {"buscar_documentos", "buscar_cliente", "ver_oportunidad", "consultar_ventas", "escribir_fichero"}))


# ---------------------------------------------------------------- 2. tokens de la primera llamada

def paso_2():
    print("\n== 2. Tokens de la primera llamada: carga progresiva frente a todo registrado ==")
    medidas = {}
    for modo in ("progresivo", "todo"):
        k = Kernel(Path(tempfile.mkdtemp()), modo=modo, cache=False)
        k.agente("Hola. En una frase: ¿qué tipo de cosas puedes hacer?")
        l = k.modelo.llamadas[0]
        medidas[modo] = (uso_total(l["uso"]), len(l["herramientas"]))
        info(f"{modo}: {len(l['herramientas'])} herramientas, {uso_total(l['uso'])} tokens de entrada")
    (p, np_), (t, nt) = medidas["progresivo"], medidas["todo"]
    por = (t - p) / (nt - np_)
    anota("2.1 la carga progresiva ahorra tokens en cada llamada", t > p,
          f"{t - p} tokens menos por llamada ({(t - p) / t:.0%}); {por:.0f} tokens por herramienta de skill")
    return medidas


# ---------------------------------------------------------------- 3. la tarea, con carga progresiva

TAREA = ("Prepárame un informe en PDF con las ventas del tercer trimestre: Norte 120.000 €, Sur 95.000 €, "
         "Este 70.500 €. Título: «Ventas T3». Incluye un párrafo de conclusión breve.")


def paso_3(snap_dir: Path):
    print("\n== 3. La tarea con carga progresiva y caché de prompts ==")
    espacio = Path(tempfile.mkdtemp())
    sesion = SnapshotSessionManager("chat-16", storage=LocalFileStorage(str(snap_dir)), save_latest_on="message")
    k = Kernel(espacio, modo="progresivo", sesion=sesion)
    r = k.agente(TAREA)
    ll = k.modelo.llamadas
    tabla(ll)
    act = k.llamadas_a("skills")
    anota("3.1 Claude activa la skill", any(c["toolUse"]["input"].get("skill_name") == "informe-pdf" for c in act),
          f"{len(act)} activación(es)")
    i_act = next((i for i, l in enumerate(ll) if i + 1 < len(ll) and "generar_pdf" in ll[i + 1]["herramientas"]
                  and "generar_pdf" not in l["herramientas"]), None)
    anota("3.2 generar_pdf no está antes de activar y sí en la llamada siguiente", i_act is not None,
          f"aparece en la llamada {i_act + 2}" if i_act is not None else "")
    h = k.registradas.get("generar_pdf")
    exitos = [c for c in (h.llamadas if h else []) if "salida" in c]
    anota("3.3 Claude llama a generar_pdf y el script responde", bool(exitos),
          json.dumps(exitos[-1]["entrada"], ensure_ascii=False) if exitos else str(h and h.llamadas))
    if exitos:
        s = exitos[-1]["salida"]
        pdf = espacio / s["ruta"]
        anota("3.4 el PDF está en salida/ y es un PDF válido", pdf.is_file() and pdf_valido(pdf),
              f"{s['ruta']}, {s['paginas']} pág., {s['bytes']} B")
        anota("3.5 el resumen para la app sale de tools.yaml", exitos[-1]["resumen"] == f"PDF {s['nombre_fichero']} ({s['paginas']} págs.)",
              exitos[-1]["resumen"])
        anota("3.6 el script no ve variables AWS_*", s["variables_aws"] == 0,
              f"el kernel tiene {sum(k.startswith('AWS_') for k in os.environ)}, el script {s['variables_aws']}")
        shutil.copy(pdf, Path(__file__).parent / "sonda_skills" / "ultimo_informe.pdf")
    info("respuesta:", str(r).strip().replace("\n", " ")[:300])

    # caché: la llamada tras activar
    if i_act is not None:
        antes, despues = ll[i_act]["uso"], ll[i_act + 1]["uso"]
        info(f"caché: llamada de la activación lee {antes.get('cacheReadInputTokens', 0)} / escribe {antes.get('cacheWriteInputTokens', 0)}; "
             f"la siguiente lee {despues.get('cacheReadInputTokens', 0)} / escribe {despues.get('cacheWriteInputTokens', 0)}")
        anota("3.7 la llamada siguiente a la activación vuelve a escribir la caché (cambió la lista de herramientas)",
              despues.get("cacheWriteInputTokens", 0) > 0,
              f"escribe {despues.get('cacheWriteInputTokens', 0)}, lee {despues.get('cacheReadInputTokens', 0)}")
        if i_act + 2 < len(ll):
            d2 = ll[i_act + 2]["uso"]
            anota("3.8 y la de después ya lee de la caché", d2.get("cacheReadInputTokens", 0) > 0,
                  f"lee {d2.get('cacheReadInputTokens', 0)}")

    n0 = len(ll)  # llamadas del primer mensaje
    k.agente("Activa otra vez la skill informe-pdf con la herramienta skills (aunque ya la tengas) y genera otro PDF "
             "solo con la zona Norte, título «Norte T3», en trabajo/norte.md.")
    tabla(ll, n0)
    reactivado = len(k.llamadas_a("skills")) > len(act)
    nombres = ll[-1]["herramientas"]
    anota("3.9 volver a activar no registra dos veces ni da error", k.registros == 1 and nombres.count("generar_pdf") == 1,
          f"skills llamada otra vez: {reactivado}; registros {k.registros}; generar_pdf {nombres.count('generar_pdf')} vez en la lista")
    anota("3.10 la skill activada queda en el estado del agente (va al snapshot)",
          k.plugin.get_activated_skills(k.agente) == ["informe-pdf"], str(k.plugin.get_activated_skills(k.agente)))
    return k, n0


# ---------------------------------------------------------------- 4. la tarea, con todo registrado

def paso_4(k3: Kernel, n3: int):
    print("\n== 4. La misma tarea con todas las herramientas desde el principio ==")
    espacio = Path(tempfile.mkdtemp())
    k = Kernel(espacio, modo="todo")
    k.agente(TAREA)
    tabla(k.modelo.llamadas)
    h = k.registradas["generar_pdf"]
    anota("4.1 también genera el PDF", any("salida" in c for c in h.llamadas),
          f"activa la skill igualmente (para leer SKILL.md): {bool(k.llamadas_a('skills'))}")
    primero3 = k3.modelo.llamadas[:n3]  # solo el primer mensaje del paso 3, la misma tarea
    t3, t4 = sum(uso_total(l["uso"]) for l in primero3), sum(uso_total(l["uso"]) for l in k.modelo.llamadas)
    c3, c4 = coste(primero3), coste(k.modelo.llamadas)
    info(f"progresivo: {len(primero3)} llamadas, {t3} tokens de entrada, ${c3:.4f} · "
         f"todo: {len(k.modelo.llamadas)} llamadas, {t4} tokens de entrada, ${c4:.4f}")
    anota("4.2 comparación de la tarea completa (con caché)", True,
          f"progresivo {len(primero3)} llamadas / {t3} tok / ${c3:.4f}; todo {len(k.modelo.llamadas)} llamadas / {t4} tok / ${c4:.4f}")


# ---------------------------------------------------------------- 5. montar desde el snapshot

def paso_5(snap_dir: Path):
    print("\n== 5. Montar el Agent desde el snapshot (microVM nueva) ==")
    copia = Path(tempfile.mkdtemp()) / "snap"
    shutil.copytree(snap_dir, copia)
    for registrar, d, etiqueta in ((True, snap_dir, "5.1 registrando al montar"), (False, copia, "5.2 sin registrar al montar")):
        espacio = Path(tempfile.mkdtemp())
        (espacio / "trabajo").mkdir()
        sesion = SnapshotSessionManager("chat-16", storage=LocalFileStorage(str(d)), save_latest_on="message")
        k = Kernel(espacio, modo="progresivo", sesion=sesion, registrar_al_montar=registrar)
        activadas = k.plugin.get_activated_skills(k.agente)
        n_msgs = len(k.agente.messages)
        antes = len(k.llamadas_a("skills"))
        k.agente("Genera ahora otro PDF solo con la zona Sur, título «Sur T3», en trabajo/sur.md.")
        tabla(k.modelo.llamadas)
        primera = k.modelo.llamadas[0]["herramientas"]
        reactiva = len(k.llamadas_a("skills")) > antes
        h = k.registradas.get("generar_pdf")
        ok_pdf = bool(h and any("salida" in c for c in h.llamadas))
        errores_tool = [c["toolResult"]["content"][0].get("text", "")[:120] for m in k.agente.messages[n_msgs:]
                        for c in m["content"] if "toolResult" in c and c["toolResult"].get("status") == "error"]
        detalle = (f"restaura {n_msgs} mensajes y activadas={activadas}; generar_pdf en la 1.ª llamada: {'generar_pdf' in primera}; "
                   f"vuelve a activar: {reactiva}; PDF: {ok_pdf}; errores de herramienta: {errores_tool}")
        if registrar:
            anota(etiqueta + ": usa generar_pdf sin volver a activar", "generar_pdf" in primera and ok_pdf and not reactiva, detalle)
        else:
            anota(etiqueta + ": qué hace Claude", True, detalle)


# ---------------------------------------------------------------- 6. validación de la entrada

def paso_6():
    print("\n== 6. Validación de la entrada de una AgentTool propia ==")
    for validar in (False, True):
        k = Kernel(Path(tempfile.mkdtemp()), modo="progresivo", validar=validar)
        k.registrar("informe-pdf")
        r = k.agente.tool.generar_pdf(titulo=5, record_direct_tool_call=False)
        h = k.registradas["generar_pdf"]
        texto = r["content"][0]["text"][:160]
        if not validar:
            anota("6.1 Strands no valida la entrada contra el esquema: el script recibe titulo=5 y sin origen",
                  h.llamadas and h.llamadas[0]["entrada"] == {"titulo": 5} and r["status"] == "error", texto)
        else:
            anota("6.2 el kernel la rechaza antes de lanzar el script", r["status"] == "error" and "Argumentos no válidos" in texto, texto)


def main():
    print(f"Sonda 16 · {MODELO} en {REGION}")
    paso_1()
    paso_2()
    snap = Path(tempfile.mkdtemp()) / "snap"
    paso_4(*paso_3(snap))
    paso_5(snap)
    paso_6()
    print(f"\n{sum(ok for _, ok, _ in resultados)} de {len(resultados)} bien")
    for n, ok, d in resultados:
        if not ok:
            print("FALLA", n, d)


if __name__ == "__main__":
    main()
