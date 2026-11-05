"""Prototipo de la herramienta update_plan del kernel: el modelo manda la lista entera de pasos y la herramienta
la valida y deja un evento `plan`; los hooks de eventos no emiten tool_start ni tool_end para ella.
Lo usan t_plan.py (con el modelo falso) y pruebas/aws/sonda_plan.py (con Bedrock)."""
from typing import Literal

from pydantic import BaseModel, Field
from strands import tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

MAX_PASOS = 12
MAX_TEXTO = 100

DESCRIPCION = f"""Muestra al usuario el plan de la tarea como una lista de pasos que se va marcando.

Úsala solo en tareas de tres pasos o más (varias búsquedas, cálculos o ficheros); nunca para una pregunta que se
responde en uno o dos pasos.
- Al empezar la tarea, antes de cualquier otra herramienta, manda la lista con todos los pasos en `pending` y el
  primero en `in_progress`.
- No gastes una respuesta solo en esta herramienta: llámala en la misma respuesta que la herramienta del paso
  siguiente, antes que ella, marcando `completed` lo terminado y `in_progress` el paso que empieza.
- Al empezar el último paso, ponlo `in_progress` como los demás; no hace falta marcarlo `completed`: eso se hace
  solo al terminar.
- Si el plan cambia, manda la lista nueva entera.
Manda siempre la lista entera (como mucho {MAX_PASOS} pasos de {MAX_TEXTO} caracteres), con un paso
`in_progress` como mucho. La lista solo informa al usuario: no hace nada por sí misma."""


class Paso(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXTO, description="Qué se hace en el paso, en pocas palabras")
    status: Literal["pending", "in_progress", "completed"]


class Plan:
    """Una por ejecución: `eventos` es lo que el kernel escribiría en RUN#{run_id}."""

    def __init__(self, eventos):
        self.eventos = eventos

        @tool(name="update_plan", description=DESCRIPCION)
        def update_plan(steps: list[Paso]) -> str:
            """steps: la lista entera de pasos, en orden."""
            if not 1 <= len(steps) <= MAX_PASOS:
                raise ValueError(f"El plan tiene que tener entre 1 y {MAX_PASOS} pasos; tiene {len(steps)}.")
            pasos = [p if isinstance(p, dict) else p.model_dump() for p in steps]
            if sum(p["status"] == "in_progress" for p in pasos) > 1:
                raise ValueError("Como mucho un paso puede estar in_progress.")
            self.eventos.append({"type": "plan", "steps": pasos})
            return "ok"

        self.herramienta = update_plan

    def cerrar(self):
        """Al terminar con reason = ok: un último `plan` con el paso en curso `completed`, sin llamar al modelo."""
        planes = [e for e in self.eventos if e["type"] == "plan"]
        if planes and any(p["status"] == "in_progress" for p in planes[-1]["steps"]):
            pasos = [{**p, "status": "completed" if p["status"] == "in_progress" else p["status"]}
                     for p in planes[-1]["steps"]]
            self.eventos.append({"type": "plan", "steps": pasos})

    def registrar_hooks(self, agente):
        """Los hooks de eventos del kernel (aquí, solo tool_start y tool_end), sin tarjeta para update_plan."""

        def inicio(ev: BeforeToolCallEvent):
            if ev.tool_use["name"] != "update_plan":
                self.eventos.append({"type": "tool_start", "name": ev.tool_use["name"]})

        def fin(ev: AfterToolCallEvent):
            if ev.tool_use["name"] != "update_plan":
                self.eventos.append({"type": "tool_end", "name": ev.tool_use["name"], "status": ev.result["status"]})

        agente.hooks.add_callback(BeforeToolCallEvent, inicio)
        agente.hooks.add_callback(AfterToolCallEvent, fin)
