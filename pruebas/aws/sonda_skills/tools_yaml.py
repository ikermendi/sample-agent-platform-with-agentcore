"""tools.yaml: lo que comprobaría el panel al subir una versión de skill y lo que haría el kernel al activarla.

En la plataforma, el panel es TypeScript y valida con el mismo esquema JSON (`esquema_tools.json`); aquí va en
Python para que la sonda lo ejecute entero. La parte del kernel (`HerramientaDeScript`) sí es la que iría al kernel.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import string
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from strands.types.tools import AgentTool

AQUI = Path(__file__).parent
ESQUEMA = json.loads((AQUI / "esquema_tools.json").read_text())
RESERVADOS = {"skills", "recordar", "retrieve_context"}  # herramientas del propio kernel


# ---------------------------------------------------------------- panel: validar una versión

def hash_version(carpeta: Path) -> str:
    """sha256 de la lista ordenada (ruta relativa, sha256 del contenido): lo que guarda DynamoDB y comprueba el kernel."""
    h = hashlib.sha256()
    for f in sorted(p for p in carpeta.rglob("*") if p.is_file()):
        h.update(f.relative_to(carpeta).as_posix().encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def _frontmatter(texto: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", texto, re.S)
    return (yaml.safe_load(m.group(1)) or {}) if m else {}


def _importa(script: Path) -> set[str]:
    arbol = ast.parse(script.read_text())
    mods = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    return mods


def _campos(plantilla: str) -> set[str]:
    return {c for _, c, _, _ in string.Formatter().parse(plantilla) if c}


def validar_version(carpeta: Path, skill_id: str, paquetes_imagen: set[str]) -> tuple[dict | None, list[str]]:
    """Devuelve (tools.yaml leído, errores). Sin errores, la versión se puede aprobar."""
    errores: list[str] = []
    carpeta = carpeta.resolve()
    md = carpeta / "SKILL.md"
    if not md.is_file():
        return None, ["falta SKILL.md"]
    cab = _frontmatter(md.read_text())
    if cab.get("name") != skill_id:
        errores.append(f"SKILL.md: name = {cab.get('name')!r}, se esperaba {skill_id!r}")
    if not cab.get("description"):
        errores.append("SKILL.md: falta description")
    try:
        doc = yaml.safe_load((carpeta / "tools.yaml").read_text())
    except FileNotFoundError:
        return None, errores + ["falta tools.yaml"]
    except yaml.YAMLError as e:
        return None, errores + [f"tools.yaml no es YAML válido: {e}"]
    for e in sorted(Draft202012Validator(ESQUEMA).iter_errors(doc), key=lambda e: list(e.path)):
        errores.append(f"tools.yaml {'/'.join(map(str, e.path)) or '(raíz)'}: {e.message}")
    if errores:
        return doc, errores

    declaradas = set(doc.get("librerias", []))
    for falta in sorted(declaradas - paquetes_imagen):
        errores.append(f"librería {falta!r} no está en la imagen del kernel")
    vistos: set[str] = set()
    for h in doc["herramientas"]:
        n = h["nombre"]
        if n in vistos:
            errores.append(f"{n}: nombre repetido")
        vistos.add(n)
        if n in RESERVADOS:
            errores.append(f"{n}: nombre reservado del kernel")
        for campo in ("entrada", "salida"):
            if campo in h:
                try:
                    Draft202012Validator.check_schema(h[campo])
                except Exception as e:  # SchemaError
                    errores.append(f"{n}.{campo}: no es un esquema JSON válido: {e.message}")
        ruta = (carpeta / h["script"]).resolve()
        if not ruta.is_relative_to(carpeta / "scripts"):
            errores.append(f"{n}: el script sale de scripts/ ({h['script']})")
        elif not ruta.is_file():
            errores.append(f"{n}: no existe {h['script']}")
        else:
            ajenas = _importa(ruta) - set(sys.stdlib_module_names) - declaradas
            for m in sorted(ajenas):
                errores.append(f"{n}: {h['script']} importa {m!r} sin declararlo en librerias")
        if "resumen" in h:
            campos = _campos(h["resumen"])
            props = set(h.get("salida", {}).get("properties", {}))
            if campos and "salida" not in h:
                errores.append(f"{n}: el resumen usa {sorted(campos)} pero no hay salida declarada")
            elif campos - props:
                errores.append(f"{n}: el resumen usa campos que no están en la salida: {sorted(campos - props)}")
    return doc, errores


def validar_agente(skills: dict[str, dict], herramientas_hub: set[str]) -> list[str]:
    """Al publicar una versión de agente: ningún nombre de herramienta repetido entre sus skills, el hub y el kernel."""
    errores, dueno = [], {n: "hub" for n in herramientas_hub} | {n: "kernel" for n in RESERVADOS}
    for skill, doc in skills.items():
        for h in doc["herramientas"]:
            n = h["nombre"]
            if n in dueno:
                errores.append(f"{skill}: {n} ya lo da {dueno[n]}")
            else:
                dueno[n] = skill
    return errores


# ---------------------------------------------------------------- panel: qué enseña la revisión

def _props(esq: dict) -> dict:
    return esq.get("properties", {}) if esq else {}


def diferencia(ant: Path, nueva: Path) -> list[str]:
    """Lo que enseña la revisión de una versión frente a la anterior."""
    lineas = []
    fa = {p.relative_to(ant).as_posix(): p.read_bytes() for p in ant.rglob("*") if p.is_file()}
    fn = {p.relative_to(nueva).as_posix(): p.read_bytes() for p in nueva.rglob("*") if p.is_file()}
    for f in sorted(fa.keys() | fn.keys()):
        if f not in fa:
            lineas.append(f"fichero nuevo: {f}")
        elif f not in fn:
            lineas.append(f"fichero quitado: {f}")
        elif fa[f] != fn[f]:
            lineas.append(f"fichero cambiado: {f}")
    da, dn = (yaml.safe_load((c / "tools.yaml").read_text()) for c in (ant, nueva))
    ha, hn = ({h["nombre"]: h for h in d["herramientas"]} for d in (da, dn))
    for n in sorted(hn.keys() - ha.keys()):
        h = hn[n]
        lineas.append(f"herramienta añadida: {n} (escribe_ficheros={h.get('escribe_ficheros', False)}, "
                      f"confirmar={h.get('confirmar', False)})")
    for n in sorted(ha.keys() - hn.keys()):
        lineas.append(f"herramienta quitada: {n}")
    for n in sorted(ha.keys() & hn.keys()):
        a, b = ha[n], hn[n]
        pa, pb = _props(a["entrada"]), _props(b["entrada"])
        for p in sorted(pb.keys() - pa.keys()):
            lineas.append(f"{n}: parámetro nuevo {p} ({pb[p].get('type')})")
        for p in sorted(pa.keys() - pb.keys()):
            lineas.append(f"{n}: parámetro quitado {p}")
        for p in sorted(pa.keys() & pb.keys()):
            if pa[p] != pb[p]:
                lineas.append(f"{n}: parámetro {p} cambia {pa[p]} → {pb[p]}")
        ra, rb = set(a["entrada"].get("required", [])), set(b["entrada"].get("required", []))
        if ra != rb:
            lineas.append(f"{n}: obligatorios {sorted(ra)} → {sorted(rb)}")
        for campo in ("script", "descripcion", "escribe_ficheros", "confirmar", "tiempo_max_s", "etiqueta", "resumen"):
            if a.get(campo) != b.get(campo):
                lineas.append(f"{n}: {campo} {a.get(campo)!r} → {b.get(campo)!r}")
    la, lb = set(da.get("librerias", [])), set(dn.get("librerias", []))
    for l in sorted(lb - la):
        lineas.append(f"librería nueva: {l}")
    for l in sorted(la - lb):
        lineas.append(f"librería quitada: {l}")
    return lineas


# ---------------------------------------------------------------- kernel: el script como herramienta

class HerramientaDeScript(AgentTool):
    """Un script de tools.yaml como herramienta tipada de Strands.

    Strands no valida la entrada de una AgentTool propia contra su esquema (solo el nombre:
    strands/tools/_validator.py), así que la valida aquí. El script corre en otro proceso con un entorno
    limpio (sin variables AWS_*), con el espacio del chat como directorio de trabajo; recibe los argumentos
    en stdin como JSON y escribe su resultado en stdout como JSON.
    """

    def __init__(self, skill: str, carpeta: Path, h: dict, espacio: Path, validar: bool = True):
        super().__init__()
        self.skill, self.carpeta, self.h, self.espacio, self.validar = skill, carpeta, h, espacio, validar
        self.llamadas: list[dict] = []

    @property
    def tool_name(self) -> str:
        return self.h["nombre"]

    @property
    def tool_spec(self) -> dict:
        return {"name": self.h["nombre"], "description": self.h["descripcion"], "inputSchema": {"json": self.h["entrada"]}}

    @property
    def tool_type(self) -> str:
        return "skill_script"

    def resumen(self, salida: dict) -> str | None:
        plantilla = self.h.get("resumen")
        return plantilla.format(**salida) if plantilla else None

    async def stream(self, tool_use, invocation_state, **kwargs):
        args = tool_use.get("input") or {}
        reg = {"entrada": args}
        self.llamadas.append(reg)

        def error(msg):
            reg["error"] = msg
            return {"toolUseId": tool_use["toolUseId"], "status": "error", "content": [{"text": msg}]}

        if self.validar:
            errs = [f"{'/'.join(map(str, e.path)) or 'entrada'}: {e.message}"
                    for e in Draft202012Validator(self.h["entrada"]).iter_errors(args)]
            if errs:
                yield error("Argumentos no válidos: " + "; ".join(errs))
                return
        entorno = {"PATH": "/usr/bin:/bin", "HOME": str(self.espacio), "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(self.carpeta / self.h["script"]), cwd=self.espacio, env=entorno,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(args).encode()), self.h.get("tiempo_max_s", 120))
        except asyncio.TimeoutError:
            proc.kill()
            yield error(f"El script tardó más de {self.h.get('tiempo_max_s', 120)} s")
            return
        if proc.returncode != 0:
            yield error(f"El script falló ({proc.returncode}): {err.decode()[-500:]}")
            return
        try:
            salida = json.loads(out)
        except json.JSONDecodeError:
            yield error("El script no devolvió JSON")
            return
        if "salida" in self.h:
            errs = [e.message for e in Draft202012Validator(self.h["salida"]).iter_errors(salida)]
            if errs:
                yield error("Salida del script no válida: " + "; ".join(errs))
                return
        reg["salida"] = salida
        reg["resumen"] = self.resumen(salida)
        yield {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": json.dumps(salida, ensure_ascii=False)}]}
