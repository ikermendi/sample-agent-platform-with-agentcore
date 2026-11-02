"""Saltar el backfill del stash al restaurar un chat: el ContextManager, en la primera llamada tras montar el Agent,
lee del stash cada bloque de los mensajes restaurados (un GET por bloque en S3) para guardar los que falten. Con el
stash en almacenamiento duradero ya están todos, así que el kernel lo salta poniendo _backfill_done (privado)."""
import json, re, tempfile
from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from fake import FakeModel, texto, herramienta


@tool
def consultar_ventas(zona: str) -> str:
    """Devuelve las ventas de una zona."""
    return "\n".join(f"Pedido {i} de la {zona}: {i * 7} euros." for i in range(600))  # ~25 KB, se recorta


class Contado(LocalFileStorage):
    """LocalFileStorage que apunta las lecturas y escrituras del stash."""

    def __init__(self, d):
        super().__init__(d)
        self.lecturas, self.escrituras = [], []

    async def read(self, key):
        if "/context/" in f"/{key}":
            self.lecturas.append(key)
        return await super().read(key)

    async def write(self, key, data):
        if "/context/" in f"/{key}":
            self.escrituras.append(key)
        return await super().write(key, data)


class GuionVivo(FakeModel):
    """Como FakeModel, pero un elemento del guion puede ser una función que recibe los mensajes."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.guion and callable(self.guion[0]):
            self.guion[0] = self.guion[0](messages)
        async for ev in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield ev


def pedir_lo_recortado(messages):
    ref = re.search(r"\[ref: ([^\]]+)\]", json.dumps(messages, ensure_ascii=False)).group(1)
    return herramienta("retrieve_context", {"reference": ref})


def saltar_backfill(agente):
    """Lo que haría el kernel tras montar el Agent de un chat que ya existía."""
    # Strands 1.63.0 hace, en la primera llamada tras restaurar, un backfill del stash: lee cada bloque de los
    # mensajes restaurados para guardar los que falten (un GET por bloque, 4,4 s con 50 turnos en la sonda 19).
    # Con el stash en S3 ya están todos, guardados en su turno; el snapshot solo apunta a él ("location":
    # "external"). _backfill_done es privado: la versión de Strands está fijada y t_backfill.py falla si deja de
    # existir o de tener efecto. Lo que se pierde: si la escritura de un bloque en el stash falló en su turno, ya
    # no se repara al restaurar, y ese bloque no se podrá recuperar con retrieve_context si después se recorta.
    if agente.messages and agente.context_manager is not None:
        agente.context_manager._backfill_done = True


def montar(almac, modelo, *, saltar):
    a = Agent(model=modelo, agent_id="principal", tools=[consultar_ventas], callback_handler=None,
              context_manager="auto", storage=almac,
              session_manager=SnapshotSessionManager("chat1", storage=almac, save_latest_on="message"))
    if saltar:
        saltar_backfill(a)
    return a


def chat_de_tres_turnos(d):
    almac = Contado(d)
    guion = []
    for z in ("norte", "sur", "este"):
        guion += [herramienta("consultar_ventas", {"zona": z}), texto(f"Ya tengo la zona {z}.")]
    a = montar(almac, FakeModel(guion), saltar=True)
    for z in ("norte", "sur", "este"):
        a(f"Dame las ventas de la zona {z}.")
    return a


def test_sin_backfill_no_lee_el_stash_y_retrieve_context_sigue_funcionando():
    d = tempfile.mkdtemp()
    chat_de_tres_turnos(d)

    # Nueva microVM: se monta desde el snapshot y se salta el backfill
    almac = Contado(d)
    m = GuionVivo([pedir_lo_recortado, texto("El pedido 599 de la norte es de 4193 euros.")])
    a = montar(almac, m, saltar=True)
    assert a.messages, "el chat no se ha restaurado"
    assert a.context_manager._backfill_done is True
    a("¿Cuánto es el último pedido de la zona norte?")

    recuperado = json.dumps(m.llamadas[-1]["mensajes"], ensure_ascii=False)
    print("lecturas del stash:", len(almac.lecturas), "| escrituras:", len(almac.escrituras),
          "| ¿recupera el pedido 599?", "Pedido 599 de la norte" in recuperado)
    assert len(almac.lecturas) == 1          # solo la de retrieve_context
    assert "Pedido 599 de la norte" in recuperado
    assert almac.escrituras                  # los mensajes nuevos se siguen guardando en el stash


def test_con_backfill_lee_un_bloque_por_mensaje_restaurado():
    d = tempfile.mkdtemp()
    chat_de_tres_turnos(d)

    almac = Contado(d)
    a = montar(almac, FakeModel([texto("Aquí estoy.")]), saltar=False)
    n = len(a.messages)
    a("¿Sigues ahí?")
    print("mensajes restaurados:", n, "| lecturas del stash en la primera llamada:", len(almac.lecturas))
    # 3 turnos × (pregunta + respuesta final) + la pregunta nueva; los toolUse y los resultados recortados se saltan
    assert len(almac.lecturas) == 7


def test_el_atributo_privado_sigue_existiendo():
    # Si una versión nueva de Strands lo renombra o lo quita, poner _backfill_done ya no haría nada
    a = Agent(model=FakeModel([]), callback_handler=None, context_manager="auto",
              storage=LocalFileStorage(tempfile.mkdtemp()))
    assert a.context_manager._backfill_done is False
    assert callable(a.context_manager._backfill_stash)


def test_subagente_restaurado_tambien_lo_salta():
    # Cada subagente as_tool(preserve_context=True) se restaura de su propio snapshot con su propio ContextManager:
    # el kernel lo marca en el principal y en cada subagente
    d = tempfile.mkdtemp()

    def montar_con_revisor(almac, m_pri, m_rev, saltar):
        revisor = Agent(model=m_rev, agent_id="revisor", name="revisor", description="Revisa ventas.",
                        tools=[consultar_ventas], callback_handler=None, context_manager="auto", storage=almac,
                        session_manager=SnapshotSessionManager("chat1", storage=almac, save_latest_on="message"))
        principal = Agent(model=m_pri, agent_id="principal", tools=[revisor.as_tool(preserve_context=True)],
                          callback_handler=None, context_manager="auto", storage=almac,
                          session_manager=SnapshotSessionManager("chat1", storage=almac, save_latest_on="message"))
        if saltar:
            for a in (principal, revisor):
                saltar_backfill(a)
        return principal, revisor

    p, _ = montar_con_revisor(Contado(d), FakeModel([herramienta("revisor", {"input": "revisa la norte"}), texto("ok")]),
                              FakeModel([herramienta("consultar_ventas", {"zona": "norte"}), texto("revisada")]), True)
    p("revisa la zona norte")

    for saltar, esperadas in ((False, None), (True, 0)):
        almac = Contado(d)
        p, r = montar_con_revisor(almac, FakeModel([herramienta("revisor", {"input": "¿y ahora?"}), texto("ok")]),
                                  FakeModel([texto("sigue igual")]), saltar)
        assert p.messages and r.messages
        p("¿y ahora?")
        print("saltando:", saltar, "| lecturas del stash:", len(almac.lecturas))
        if esperadas is None:
            assert len(almac.lecturas) > 0
        else:
            assert len(almac.lecturas) == esperadas
