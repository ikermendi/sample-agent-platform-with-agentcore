"""El plan en el chat: update_plan valida la lista entera, deja un evento `plan` sin tool_start ni tool_end,
devuelve el error al modelo si la lista no vale y sigue registrada y en la conversación tras el snapshot."""
import json
import tempfile

from strands import Agent, tool
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage

from fake import FakeModel, texto, herramienta
from plan import Plan


@tool
def buscar(consulta: str) -> str:
    """Busca en los documentos."""
    return f"resultados de {consulta}"


def pasos(*estados):
    return {"steps": [{"text": f"Paso {i + 1}", "status": s} for i, s in enumerate(estados)]}


def agente(guion, eventos, **kw):
    plan = Plan(eventos)
    m = FakeModel(guion)
    a = Agent(model=m, tools=[plan.herramienta, buscar], callback_handler=None, **kw)
    plan.registrar_hooks(a)
    return a, m


def resultados(m, llamada):
    return [b["toolResult"] for msg in m.llamadas[llamada]["mensajes"] for b in msg["content"] if "toolResult" in b]


def test_evento_plan_sin_tarjeta_de_herramienta():
    eventos = []
    a, m = agente([herramienta("update_plan", pasos("in_progress", "pending", "pending")),
                   herramienta("buscar", {"consulta": "ventas"}),
                   herramienta("update_plan", pasos("completed", "in_progress", "pending")),
                   texto("hecho")], eventos)
    a("haz el informe")
    tipos = [(e["type"], e.get("name")) for e in eventos]
    print("eventos:", tipos)
    assert tipos == [("plan", None), ("tool_start", "buscar"), ("tool_end", "buscar"), ("plan", None)]
    assert [p["status"] for p in eventos[-1]["steps"]] == ["completed", "in_progress", "pending"]
    assert resultados(m, 1)[0]["status"] == "success" and resultados(m, 1)[0]["content"][0]["text"] == "ok"
    spec = a.tool_registry.get_all_tools_config()["update_plan"]
    print("tamaño de la definición de update_plan:", len(json.dumps(spec)), "caracteres")


def test_cerrar_marca_el_paso_en_curso_y_deja_lo_pendiente():
    eventos = []
    plan = Plan(eventos)
    plan.cerrar()
    assert eventos == []  # sin plan no escribe nada
    eventos.append({"type": "plan", "steps": pasos("completed", "in_progress", "pending")["steps"]})
    plan.cerrar()
    assert [p["status"] for p in eventos[-1]["steps"]] == ["completed", "completed", "pending"]
    n = len(eventos)
    plan.cerrar()
    assert len(eventos) == n  # sin paso en curso no escribe otro


def test_lista_mala_vuelve_como_error_sin_evento():
    malas = {
        "dos en curso": pasos("in_progress", "in_progress"),
        "13 pasos": pasos(*["pending"] * 13),
        "vacía": {"steps": []},
        "estado inventado": pasos("done"),
        "texto largo": {"steps": [{"text": "x" * 101, "status": "pending"}]},
        "sin steps": {},
    }
    for nombre, entrada in malas.items():
        eventos = []
        a, m = agente([herramienta("update_plan", entrada), texto("vale")], eventos)
        a("haz algo")
        r = resultados(m, 1)[0]
        print(f"{nombre}: {r['status']} · {r['content'][0]['text'][:90]!r}")
        assert r["status"] == "error", nombre
        assert eventos == [], nombre


def test_tras_el_snapshot_sigue_registrada_y_en_la_conversacion():
    d = tempfile.mkdtemp()
    sm = lambda: SnapshotSessionManager("chat-plan", storage=LocalFileStorage(d), save_latest_on="message")
    a1, _ = agente([herramienta("update_plan", pasos("completed", "completed", "completed")), texto("listo")],
                   [], session_manager=sm())
    a1("haz el informe")
    eventos = []
    a2, m2 = agente([texto("sigo")], eventos, session_manager=sm())
    a2("y ahora otro")
    print("herramientas:", m2.llamadas[0]["herramientas"], "| mensajes restaurados:", len(m2.llamadas[0]["mensajes"]))
    assert "update_plan" in m2.llamadas[0]["herramientas"]
    assert any("update_plan" in str(msg) for msg in m2.llamadas[0]["mensajes"])
    assert eventos == []  # el plan es de la ejecución: la nueva no emite nada si el modelo no lo vuelve a mandar
