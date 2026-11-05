"""Carreras entre el kernel, el watchdog, launch y close sobre DynamoDB Local.

Las escrituras son las del diseño ("Who writes what in RUN#{run_id} and CTL#{run_id}"), con transacciones
reales. Cada prueba fija un orden concreto; la última lanza los dos escritores a la vez en hilos. Lo que
debe cumplirse siempre: un solo end, status = ended si y solo si hay end, y close nunca libera el run
siguiente. Necesita Docker: arranca amazon/dynamodb-local en un puerto libre.
"""
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

T = "actividad"


def s(v):
    return {"S": v}


@pytest.fixture(scope="module")
def ddb():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        puerto = so.getsockname()[1]
    nombre = f"ddb-local-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", nombre, "-p", f"127.0.0.1:{puerto}:8000",
                    "amazon/dynamodb-local", "-jar", "DynamoDBLocal.jar", "-inMemory", "-sharedDb"],
                   check=True, capture_output=True)
    c = boto3.client("dynamodb", endpoint_url=f"http://127.0.0.1:{puerto}", region_name="eu-north-1",
                     aws_access_key_id="x", aws_secret_access_key="x",
                     config=Config(max_pool_connections=40, retries={"max_attempts": 1}))
    try:
        for _ in range(60):
            try:
                c.list_tables()
                break
            except Exception:
                time.sleep(0.5)
        c.create_table(TableName=T, BillingMode="PAY_PER_REQUEST",
                       AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                             {"AttributeName": "sk", "AttributeType": "S"}],
                       KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                  {"AttributeName": "sk", "KeyType": "RANGE"}])
        yield c
    finally:
        subprocess.run(["docker", "rm", "-f", nombre], capture_output=True)


class Mundo:
    """Un chat y sus runs. Cada método es una escritura del diseño; False si perdió su condición."""

    def __init__(self, c):
        self.c, self.chat = c, f"CHAT#{uuid.uuid4().hex}"

    def _ok(self, f):
        try:
            f()
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ConditionalCheckFailedException", "TransactionCanceledException"):
                return False
            raise

    def nuevo(self):
        self.c.put_item(TableName=T, Item={"pk": s(self.chat), "sk": s("CHAT")})
        return self

    def lanza(self):
        run = uuid.uuid4().hex
        hecho = self._ok(lambda: self.c.transact_write_items(TransactItems=[
            {"Put": {"TableName": T, "Item": {"pk": s(f"RUN#{run}"), "sk": s("RUN"), "status": s("pending"),
                                              "hb": {"N": "0"}},
                     "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": T, "Item": {"pk": s(f"CTL#{run}"), "sk": s("CTL"), "origin": s("chat")}}},
            {"Update": {"TableName": T, "Key": {"pk": s(self.chat), "sk": s("CHAT")},
                        "UpdateExpression": "SET active_run = :r",
                        "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(active_run)",
                        "ExpressionAttributeValues": {":r": s(run)}}}]))
        return run if hecho else None

    def deshaz(self, run):
        """launch falló: el run deja de existir, solo si sigue pending y active_run es suyo."""
        return self._ok(lambda: self.c.transact_write_items(TransactItems=[
            {"Delete": {"TableName": T, "Key": {"pk": s(f"RUN#{run}"), "sk": s("RUN")},
                        "ConditionExpression": "#s = :p", "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":p": s("pending")}}},
            {"Delete": {"TableName": T, "Key": {"pk": s(f"CTL#{run}"), "sk": s("CTL")}}},
            {"Update": {"TableName": T, "Key": {"pk": s(self.chat), "sk": s("CHAT")},
                        "UpdateExpression": "REMOVE active_run",
                        "ConditionExpression": "active_run = :r",
                        "ExpressionAttributeValues": {":r": s(run)}}}]))

    def arranca(self, run):
        return self._ok(lambda: self.c.update_item(
            TableName=T, Key={"pk": s(f"RUN#{run}"), "sk": s("RUN")}, UpdateExpression="SET #s = :r",
            ConditionExpression="#s = :p", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":r": s("running"), ":p": s("pending")}))

    def latido(self, run):
        return self._ok(lambda: self.c.update_item(
            TableName=T, Key={"pk": s(f"RUN#{run}"), "sk": s("RUN")}, UpdateExpression="ADD hb :u",
            ConditionExpression="#s = :r", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":u": {"N": "1"}, ":r": s("running")}))

    def termina(self, run, visto, reason, closed_by):
        """status = ended + end en una transacción: kernel (desde running), watchdog o borrado (lo que vio)."""
        return self._ok(lambda: self.c.transact_write_items(TransactItems=[
            {"Update": {"TableName": T, "Key": {"pk": s(f"RUN#{run}"), "sk": s("RUN")},
                        "UpdateExpression": "SET #s = :e, reason = :m, closed_by = :q",
                        "ConditionExpression": "#s = :v", "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":e": s("ended"), ":m": s(reason), ":q": s(closed_by),
                                                      ":v": s(visto)}}},
            {"Put": {"TableName": T, "Item": {"pk": s(f"RUN#{run}"), "sk": s("EVT#999999"), "reason": s(reason)},
                     "ConditionExpression": "attribute_not_exists(pk)"}}]))

    def cierra(self, run):
        """close(run_id): cada paso por separado y repetible."""
        gano = self._ok(lambda: self.c.put_item(
            TableName=T, Item={"pk": s(self.chat), "sk": s(f"MSG#{run}")},
            ConditionExpression="attribute_not_exists(pk)"))
        self._ok(lambda: self.c.update_item(
            TableName=T, Key={"pk": s(self.chat), "sk": s("CHAT")}, UpdateExpression="REMOVE active_run",
            ConditionExpression="active_run = :r", ExpressionAttributeValues={":r": s(run)}))
        return gano

    # lecturas
    def run(self, run):
        return self.c.get_item(TableName=T, Key={"pk": s(f"RUN#{run}"), "sk": s("RUN")}, ConsistentRead=True).get("Item")

    def estado(self, run):
        i = self.run(run)
        return i and i["status"]["S"]

    def fin(self, run):
        return self.c.get_item(TableName=T, Key={"pk": s(f"RUN#{run}"), "sk": s("EVT#999999")},
                               ConsistentRead=True).get("Item")

    def activo(self):
        i = self.c.get_item(TableName=T, Key={"pk": s(self.chat), "sk": s("CHAT")}, ConsistentRead=True)["Item"]
        return i.get("active_run", {}).get("S")

    def coherente(self, run):
        assert (self.estado(run) == "ended") == (self.fin(run) is not None)


class Watchdog:
    """Una pasada por un run muerto: Stop, ~3 s y transacción final, con la AgentCore que se le indique."""

    def __init__(self, m, stop="ok", antes_de_escribir=None, muere=False):
        self.m, self.stop, self.antes, self.muere = m, stop, antes_de_escribir, muere

    def pasada(self, run):
        visto = self.m.estado(run)
        if visto not in ("pending", "running"):
            return "nada"
        if self.stop == "409":
            return "reintenta"  # no escribe nada: lo reevalúa en la pasada siguiente
        if self.antes:
            self.antes()  # lo que pasa durante la espera de ~3 s
        if self.muere:
            return "muerta"
        return "escrito" if self.m.termina(run, visto, "interrupted", "watchdog") else "perdio"


@pytest.fixture
def m(ddb):
    return Mundo(ddb).nuevo()


def corriendo(m):
    r = m.lanza()
    assert m.arranca(r)
    return r


def test_el_kernel_acaba_durante_la_espera_y_gana(m):
    r = corriendo(m)
    w = Watchdog(m, antes_de_escribir=lambda: m.termina(r, "running", "ok", "kernel"))
    assert w.pasada(r) == "perdio"
    assert m.fin(r)["reason"]["S"] == "ok"
    assert m.run(r)["closed_by"]["S"] == "kernel"
    m.coherente(r)


def test_un_latido_tardio_no_salva_al_run(m):
    r = corriendo(m)
    assert Watchdog(m, antes_de_escribir=lambda: m.latido(r)).pasada(r) == "escrito"
    assert m.fin(r)["reason"]["S"] == "interrupted"
    assert not m.latido(r)  # el kernel que vuelve ve la condición fallida y para
    m.coherente(r)


def test_el_kernel_que_vuelve_no_escribe_un_segundo_end(m):
    r = corriendo(m)
    assert Watchdog(m).pasada(r) == "escrito"
    assert not m.termina(r, "running", "ok", "kernel")
    assert m.fin(r)["reason"]["S"] == "interrupted"
    m.coherente(r)


def test_409_del_stop_no_escribe_y_la_pasada_siguiente_acaba(m):
    r = corriendo(m)
    assert Watchdog(m, stop="409").pasada(r) == "reintenta"
    assert m.estado(r) == "running" and m.fin(r) is None
    assert m.activo() == r  # el chat sigue ocupado
    assert Watchdog(m).pasada(r) == "escrito"
    m.coherente(r)


def test_lambda_muerta_entre_el_stop_y_la_escritura(m):
    r = corriendo(m)
    assert Watchdog(m, muere=True).pasada(r) == "muerta"
    assert m.estado(r) == "running" and m.fin(r) is None
    assert Watchdog(m).pasada(r) == "escrito"  # el Stop repetido da 404, que vale
    m.coherente(r)


def test_pending_que_pasa_a_running_durante_la_espera(m):
    r = m.lanza()
    w = Watchdog(m, antes_de_escribir=lambda: m.arranca(r))
    assert w.pasada(r) == "perdio"
    assert m.estado(r) == "running" and m.fin(r) is None
    assert Watchdog(m).pasada(r) == "escrito"  # sin heartbeat, la pasada siguiente lo cierra
    m.coherente(r)


def test_liberacion_temprana_el_mensaje_perdido_lo_cierra_el_watchdog(m):
    """La carrera aceptada: el kernel acaba en la espera y el siguiente cae en la microVM moribunda."""
    r1 = corriendo(m)
    siguiente = []

    def acaba_y_lanza():
        assert m.termina(r1, "running", "ok", "kernel")
        m.cierra(r1)
        siguiente.append(m.lanza())  # esta invocación se pierde: nadie la pasa a running

    assert Watchdog(m, antes_de_escribir=acaba_y_lanza).pasada(r1) == "perdio"
    r2 = siguiente[0]
    assert m.activo() == r2 and m.estado(r2) == "pending"
    assert Watchdog(m).pasada(r2) == "escrito"
    assert m.fin(r2)["reason"]["S"] == "interrupted"
    m.cierra(r2)
    assert m.activo() is None


def test_close_tardio_no_libera_el_run_siguiente(m):
    r1 = corriendo(m)
    m.termina(r1, "running", "ok", "kernel")
    assert m.cierra(r1)
    r2 = corriendo(m)
    assert not m.cierra(r1)  # el repetido no gana el mensaje
    assert m.activo() == r2


def test_deshacer_launch(m):
    r = m.lanza()
    assert m.deshaz(r)
    assert m.run(r) is None and m.activo() is None and m.fin(r) is None


def test_deshacer_pierde_si_el_kernel_ya_arranco(m):
    r = m.lanza()
    assert m.arranca(r)  # la invocación en duda sí llegó
    assert not m.deshaz(r)
    assert m.estado(r) == "running" and m.activo() == r


def test_deshacer_pierde_si_el_watchdog_ya_cerro(m):
    r = m.lanza()
    assert Watchdog(m).pasada(r) == "escrito"
    assert not m.deshaz(r)
    m.coherente(r)


def test_arranque_repetido_tras_deshacer_no_resucita_el_run(m):
    r = m.lanza()
    m.deshaz(r)
    assert not m.arranca(r)  # invocación que llega tarde: el run ya no existe
    assert m.run(r) is None


def test_reintento_con_el_mismo_id_tras_deshacer(m):
    """La condición de arrancar no crea nada, y una invocación tardía del mismo run_id entra una vez."""
    r = m.lanza()
    assert m.deshaz(r)
    m.c.transact_write_items(TransactItems=[
        {"Put": {"TableName": T, "Item": {"pk": s(f"RUN#{r}"), "sk": s("RUN"), "status": s("pending")},
                 "ConditionExpression": "attribute_not_exists(pk)"}},
        {"Update": {"TableName": T, "Key": {"pk": s(m.chat), "sk": s("CHAT")},
                    "UpdateExpression": "SET active_run = :r", "ConditionExpression": "attribute_not_exists(active_run)",
                    "ExpressionAttributeValues": {":r": s(r)}}}])
    assert m.arranca(r)
    assert not m.arranca(r)


def test_hilos_kernel_contra_watchdog_contra_borrado(ddb):
    """Tres escritores a la vez sobre 150 runs, y close repetido por dos llamadores."""
    resultados = []

    def uno(i):
        m = Mundo(ddb).nuevo()
        r = corriendo(m)
        with ThreadPoolExecutor(3) as ex:
            f = [ex.submit(m.termina, r, "running", "ok", "kernel"),
                 ex.submit(m.termina, r, "running", "interrupted", "watchdog"),
                 ex.submit(m.termina, r, "running", "cancelled", "chat_deletion")]
            ganan = sum(x.result() for x in f)
        with ThreadPoolExecutor(2) as ex:
            cierres = sum(ex.map(lambda _: m.cierra(r), range(2)))
        m.coherente(r)
        return ganan, cierres, m.run(r)["closed_by"]["S"], m.fin(r)["reason"]["S"], m.activo()

    with ThreadPoolExecutor(8) as ex:
        resultados = list(ex.map(uno, range(150)))
    assert all(g == 1 and c == 1 and a is None for g, c, _, _, a in resultados)
    par = {"kernel": "ok", "watchdog": "interrupted", "chat_deletion": "cancelled"}
    assert all(par[q] == why for _, _, q, why, _ in resultados)  # closed_by y reason son del mismo ganador
    quien = {}
    for _, _, q, _, _ in resultados:
        quien[q] = quien.get(q, 0) + 1
    print(f"\n     150 runs, ganador: {quien}")
