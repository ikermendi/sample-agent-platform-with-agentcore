"""Modelo falso para Strands: devuelve respuestas preparadas y apunta lo que recibe."""
import json
import uuid
from typing import Any

from strands.models.model import Model


def texto(t):
    return {"text": t}


def herramienta(nombre, entrada=None, id=None):
    return {"tool": nombre, "input": entrada or {}, "id": id or f"tu_{uuid.uuid4().hex[:8]}"}


class FakeModel(Model):
    """Cada elemento del guion es una respuesta: un dict texto(...) / herramienta(...),
    o una lista de ellos (varias herramientas en el mismo mensaje)."""

    def __init__(self, guion, context_window_limit=200_000):
        self.guion = list(guion)
        self.llamadas = []  # (system_prompt, mensajes, nombres de herramientas)
        self.config = {"model_id": "fake", "context_window_limit": context_window_limit}

    # --- interfaz de Model que usa el Agent ---
    def update_config(self, **kw):
        self.config.update(kw)

    def get_config(self):
        return self.config

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.llamadas.append({
            "system_prompt": system_prompt,
            "mensajes": json.loads(json.dumps(messages, default=str)),
            "herramientas": [t["name"] for t in (tool_specs or [])],
        })
        if not self.guion:
            resp = [texto("fin del guion")]
        else:
            resp = self.guion.pop(0)
        if isinstance(resp, Exception):  # el guion también puede fallar
            raise resp
        if isinstance(resp, dict):
            resp = [resp]
        yield {"messageStart": {"role": "assistant"}}
        hay_herramienta = False
        for b in resp:
            if "text" in b:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": b["text"]}}}
                yield {"contentBlockStop": {}}
            else:
                hay_herramienta = True
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": b["id"], "name": b["tool"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(b["input"])}}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if hay_herramienta else "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110},
                            "metrics": {"latencyMs": 1}}}

    async def structured_output(self, *a, **k):  # no se usa
        raise NotImplementedError
