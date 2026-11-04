"""Prototipo de los mensajes durante un run: un hook del kernel recoge antes de cada llamada al modelo lo que el
usuario ha enviado y lo pone en la conversación, junto al resultado de la herramienta, sin añadir otro mensaje
del usuario. Lo usan t_mensajes_run.py (con el modelo falso) y pruebas/aws/sonda_mensajes_run.py (con Bedrock)."""
from strands.hooks import BeforeModelCallEvent


class Buzon:
    """`pendientes` hace de CTL#{run_id} / MSG#: lo escribe api y el kernel solo lo lee, así que lo ya entregado se
    recuerda en memoria. `eventos` es lo que el kernel escribiría en RUN#{run_id}."""

    def __init__(self, eventos):
        self.pendientes = []  # (message_id, texto), en orden de ULID
        self.entregados = set()
        self.eventos = eventos

    def recoger(self, ev: BeforeModelCallEvent):
        nuevos = [(i, t) for i, t in self.pendientes if i not in self.entregados]
        if not nuevos:
            return
        ultimo = ev.agent.messages[-1]
        assert ultimo["role"] == "user"  # antes de llamar al modelo, siempre: el mensaje o los resultados
        for i, t in nuevos:
            ultimo["content"].append({"text": t})
            self.entregados.add(i)
            self.eventos.append({"type": "user_message", "message_id": i, "text": t})

    def registrar(self, agente):
        agente.hooks.add_callback(BeforeModelCallEvent, self.recoger)
