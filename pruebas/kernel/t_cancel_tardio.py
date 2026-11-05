"""Un cancel que llega justo al acabar un run no debe cancelar el run siguiente del Agent reutilizado.

El hilo de heartbeat ve el flag de cancelación cada 30 s, sin relación con el fin de la invocación. Con
agent.cancel() la señal es del Agent y solo se limpia al acabar una invocación: si cae después, se queda
puesta y corta el run siguiente. Con un cancel_signal propio de cada run, la señal muere con su run.
"""
import random
import threading
import time

from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor

from fake import FakeModel, herramienta, texto


def agente(guion, **kw):
    return Agent(model=FakeModel(guion), callback_handler=None, **kw)


def test_agent_cancel_tardio_corta_el_run_siguiente():
    a = agente([texto("uno"), texto("dos")])
    assert a("primero").stop_reason == "end_turn"
    a.cancel()  # el heartbeat lo ve cuando la invocación ya ha acabado
    r = a("segundo")
    assert r.stop_reason == "cancelled"
    assert "dos" not in str(a.messages)  # el primer punto de control va dentro del stream: abre la llamada y la corta


def test_cancel_signal_tardio_no_toca_el_run_siguiente():
    a = agente([texto("uno"), texto("dos")])
    s1 = threading.Event()
    assert a("primero", cancel_signal=s1).stop_reason == "end_turn"
    s1.set()
    r = a("segundo", cancel_signal=threading.Event())
    assert r.stop_reason == "end_turn"
    assert len(a.model.llamadas) == 2


def test_cancel_signal_para_el_run_en_curso():
    senal = threading.Event()

    @tool
    def corta() -> str:
        """Simula que el heartbeat ve el flag durante la herramienta."""
        senal.set()
        return "hecho"

    a = agente([herramienta("corta"), texto("no debe llegar")], tools=[corta])
    r = a("adelante", cancel_signal=senal)
    assert r.stop_reason == "cancelled"
    assert len(a.model.llamadas) == 1
    # el Agent sirve para el run siguiente
    a.model.guion = [texto("sigue")]
    assert a("otro", cancel_signal=threading.Event()).stop_reason == "end_turn"


def test_cancel_signal_ya_puesto_abre_la_llamada_al_modelo():
    a = agente([texto("no debe llegar")])
    senal = threading.Event()
    senal.set()  # cancel en pending
    assert a("adelante", cancel_signal=senal).stop_reason == "cancelled"
    assert "no debe llegar" not in str(a.messages)
    assert len(a.model.llamadas) == 1  # pero la llamada se abre: el kernel debe mirar el flag antes de llamar al Agent


def test_cancel_signal_para_al_subagente():
    senal = threading.Event()

    @tool
    def paso() -> str:
        """Un paso lento del subagente: en él cae la señal, y Strands la refleja cada 50 ms."""
        senal.set()
        time.sleep(0.2)
        return "hecho"

    sub = agente([herramienta("paso") for _ in range(6)] + [texto("el subagente no debe acabar")], tools=[paso],
                 name="revisor", description="Revisa", tool_executor=SequentialToolExecutor())
    a = agente([herramienta("revisor", {"input": "mira"}), texto("el principal no debe seguir")],
               tools=[sub.as_tool(preserve_context=True)])
    r = a("adelante", cancel_signal=senal)
    assert r.stop_reason == "cancelled"
    assert "no debe acabar" not in str(sub.messages)
    assert len(sub.model.llamadas) <= 2
    assert len(a.model.llamadas) == 1


def carrera(cancelar, senal_por_run, vueltas=300):
    """Lanza el cancel desde otro hilo alrededor del fin de cada run; cuenta los runs siguientes cortados."""
    a = agente([])
    cortados = 0
    for _ in range(vueltas):
        a.model.guion = [texto("uno")]
        s = threading.Event() if senal_por_run else None
        h = threading.Thread(target=lambda: (time.sleep(random.uniform(0, 0.0015)), cancelar(a, s)))
        h.start()
        a("primero", **({"cancel_signal": s} if s else {}))
        h.join()
        a.model.guion = [texto("dos")]
        r = a("segundo", **({"cancel_signal": threading.Event()} if senal_por_run else {}))
        cortados += r.stop_reason == "cancelled"
    return cortados


def test_carrera_con_hilos():
    random.seed(7)
    con_agent = carrera(lambda a, s: a.cancel(), False)
    con_senal = carrera(lambda a, s: s.set(), True)
    print(f"\n     runs siguientes cortados, de 300: agent.cancel()={con_agent}, cancel_signal={con_senal}")
    assert con_agent > 0
    assert con_senal == 0
