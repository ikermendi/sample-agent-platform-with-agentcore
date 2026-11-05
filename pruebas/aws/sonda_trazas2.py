"""Sonda 15 del nivel 2: lo que la sonda 14 dejó abierto en las trazas de punta a punta.

  D. Trazado del runtime de AgentCore activado con la entrega de CloudWatch Logs de tipo TRACES hacia
     X-Ray (lo que hace `CfnRuntimeTraces.toXRay()` en CDK). Un tramo de "api" enviado por OTLP es la raíz;
     el runtime se invoca con `X-Amzn-Trace-Id` y `traceparent` que cuelgan de él. ¿Se guarda el tramo de
     AgentCore del que cuelga `POST /invocations` del contenedor?, ¿cómo se llama?, ¿cuelga del tramo de
     "api"?, ¿en qué grupo de logs queda?
  E. Permiso de IAM mínimo para el endpoint OTLP `xray.{región}.amazonaws.com/v1/traces`: roles con una
     sola acción cada uno (ninguna, `xray:PutSpans`, `xray:PutTraceSegments`, `PutSpans` +
     `PutSpansForIndexing`), asumidos desde aquí, envían un tramo cada uno.
  F. La vista de la consola: la API de recuperación de trazas de X-Ray (`StartTraceRetrieval` +
     `ListRetrievedTraces`, la que usa la consola con Transaction Search) ¿devuelve en una sola traza los
     tramos de `aws/spans` (el de "api") y los del grupo de logs del runtime (el contenedor)?

Activa Transaction Search en la cuenta y región y deja el destino como estaba al acabar, como la sonda
14. Todo lo demás con el prefijo "sonda-n2-<sufijo>" y la etiqueta sonda=nivel2, y se borra al acabar
aunque algo falle. Necesita Docker (arm64) y boto3 >= 1.43. Unos 20-30 minutos; céntimos. Uso:

    SONDA_REGION=eu-north-1 AWS_PROFILE=sonda .venv312/bin/python sonda_trazas2.py

SONDA_SIN_IAM=1 se salta la parte E.
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

import sonda_agentcore as base
import sonda_trazas as s14

REGION, NOMBRE, TAGS = base.REGION, base.NOMBRE, base.TAGS
creado, anota, codigo, intenta = base.creado, base.anota, base.codigo, base.intenta
iam, sts, logs, xray, dp, ctl = base.iam, base.sts, s14.logs, s14.xray, s14.dp, base.ctl
id_xray, cab_xray, cab_w3c, tramo, otlp = s14.id_xray, s14.cab_xray, s14.cab_w3c, s14.tramo, s14.otlp
TRAZAS = s14.TRAZAS
ESPERADO = {}  # nombre de la traza → {"api": span_id, "agentcore": span_id del padre que ve el contenedor}
VARIANTES = {  # nombre → acciones de la política del rol
    "ninguna": [],
    "PutSpans": ["xray:PutSpans"],
    "PutTraceSegments": ["xray:PutTraceSegments"],
    "PutSpans+ForIndexing": ["xray:PutSpans", "xray:PutSpansForIndexing"],
}


def envia_otlp(cuerpo, credenciales):
    """POST de OTLP JSON firmado con SigV4 (servicio xray) al endpoint público de la región."""
    url = f"https://xray.{REGION}.amazonaws.com/v1/traces"
    datos = json.dumps(cuerpo).encode()
    req = AWSRequest(method="POST", url=url, data=datos, headers={"Content-Type": "application/json"})
    SigV4Auth(credenciales, "xray", REGION).add_auth(req)
    r = urllib.request.Request(url, data=datos, headers=dict(req.headers), method="POST")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read()[:400].decode("latin1")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode("latin1")


# ---------------------------------------------------------------- D. trazado del runtime
def activar_trazado_runtime(cuenta):
    arn = creado["runtime_arn"]
    fuente = logs.put_delivery_source(name=NOMBRE + "-traces", resourceArn=arn, logType="TRACES",
                                      tags=base.TAGS_DICT)["deliverySource"]
    creado["entrega_fuente"] = fuente["name"]
    destino = logs.put_delivery_destination(name=NOMBRE + "-xray", deliveryDestinationType="XRAY",
                                            tags=base.TAGS_DICT)["deliveryDestination"]
    creado["entrega_destino"] = destino["name"]
    entrega = logs.create_delivery(deliverySourceName=fuente["name"], deliveryDestinationArn=destino["arn"],
                                   tags=base.TAGS_DICT)["delivery"]
    creado["entrega"] = entrega["id"]
    anota("D0. El runtime acepta la entrega TRACES hacia X-Ray (vended logs)", True,
          f"fuente {fuente.get('service')}/{fuente.get('logType')} · entrega {entrega['id']}")


def invoca_con_api(nombre):
    """Tramo raíz de "api" por OTLP y una invocación del runtime que cuelga de él."""
    t, p_api = id_xray(), secrets.token_hex(8)
    TRAZAS[nombre] = t
    ahora = time.time()
    st, cuerpo = envia_otlp(otlp([dict(tramo(t, p_api, "sonda-api POST /mensajes", ahora - 0.2, ahora + 2), kind=2)],
                                 servicio="sonda-api"), base.ses.get_credentials())
    if st != 200:
        print(f"     tramo de api: {st} {cuerpo}", flush=True)
    sesion = creado.setdefault("sesion", base.sesion_nueva())
    r = dp.invoke_agent_runtime(agentRuntimeArn=creado["runtime_arn"], runtimeSessionId=sesion,
                                contentType="application/json", accept="application/json",
                                payload=json.dumps({"run_id": nombre}).encode(),
                                traceId=cab_xray(t, p_api), traceParent=cab_w3c(t, p_api))
    c = json.loads(r["response"].read())
    ESPERADO[nombre] = {"api": p_api, "agentcore": c.get("padre_actual"), "contenedor": c.get("tramo_actual")}
    print(f"     {nombre}: traza {t} · api {p_api} · el contenedor ({c.get('nombre_actual')} {c.get('tramo_actual')}) "
          f"cuelga de {c.get('padre_actual')}", flush=True)
    anota(f"D1. {nombre}: el contenedor continúa la traza", c.get("trace_actual") == t, c.get("trace_actual") or "")


def flush_kernel():
    try:
        dp.invoke_agent_runtime(agentRuntimeArn=creado["runtime_arn"], runtimeSessionId=creado["sesion"],
                                contentType="application/json", accept="application/json",
                                payload=json.dumps({"flush": True}).encode())["response"].read()
    except ClientError as e:
        print("     flush falló:", codigo(e), flush=True)


# ---------------------------------------------------------------- E. IAM mínimo
def crear_roles_iam(cuenta):
    confianza = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{cuenta}:root"}, "Action": "sts:AssumeRole"}]}
    for var, acciones in VARIANTES.items():
        rol = f"{NOMBRE}-o-{len(creado.setdefault('roles_iam', []))}"
        iam.create_role(RoleName=rol, AssumeRolePolicyDocument=json.dumps(confianza), Tags=TAGS)
        creado["roles_iam"].append(rol)
        if acciones:
            iam.put_role_policy(RoleName=rol, PolicyName="otlp", PolicyDocument=json.dumps({
                "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": acciones, "Resource": "*"}]}))
        creado.setdefault("rol_de", {})[var] = rol


def probar_iam(cuenta):
    time.sleep(20)  # que los roles y sus políticas se propaguen
    resultados = {}
    for var, rol in creado["rol_de"].items():
        cred = None
        for _ in range(8):
            try:
                c = sts.assume_role(RoleArn=f"arn:aws:iam::{cuenta}:role/{rol}", RoleSessionName="sonda")["Credentials"]
                cred = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
                break
            except ClientError:
                time.sleep(8)
        if not cred:
            anota(f"E. {var}: asumir el rol", False, rol)
            continue
        t = id_xray()
        TRAZAS[f"E_{var}"] = t
        ahora = time.time()
        st, cuerpo = envia_otlp(otlp([tramo(t, secrets.token_hex(8), f"sonda-iam-{var}", ahora - 0.2, ahora)],
                                     servicio="sonda-iam"), cred)
        resultados[var] = st
        print(f"     IAM {var}: {st} {cuerpo[:300]}", flush=True)
        anota(f"E. Con {var or 'ninguna'}: {'acepta' if st == 200 else 'rechaza'} el tramo", True, f"{st} {cuerpo[:200]}")
    creado["iam_resultados"] = resultados
    anota("E1. Sin permisos de X-Ray el endpoint rechaza (la prueba discrimina)", resultados.get("ninguna") in (401, 403),
          str(resultados.get("ninguna")))


# ---------------------------------------------------------------- búsqueda y F. vista de la consola
def buscar_tramos():
    grupos = ["aws/spans"]
    for rid in creado.get("runtimes", []):
        grupos += [g["logGroupName"] for g in
                   logs.describe_log_groups(logGroupNamePrefix=f"/aws/bedrock-agentcore/runtimes/{rid}").get("logGroups", [])]
    grupos = [g for g in grupos if logs.describe_log_groups(logGroupNamePrefix=g).get("logGroups")]
    print("buscando tramos en", grupos, flush=True)
    lista = ", ".join(f"'{i}'" for i in list(TRAZAS.values()))
    q = (f"fields @log, @logStream, traceId, spanId, parentSpanId, name, kind, resource.attributes.service.name as servicio "
         f"| filter traceId in [{lista}] | sort startTimeUnixNano asc | limit 300")
    filas = []
    for _ in range(18):  # hasta 9 minutos
        try:
            qid = logs.start_query(logGroupNames=grupos, startTime=int(time.time() - 7200), endTime=int(time.time() + 300),
                                   queryString=q)["queryId"]
        except ClientError as e:  # el grupo del runtime puede aparecer en el listado antes de poder consultarse
            print("     consulta:", codigo(e), str(e)[:160], flush=True)
            fallos = creado.setdefault("fallos_consulta", 0) + 1
            creado["fallos_consulta"] = fallos
            if fallos >= 4:  # se busca sin él: que falten los tramos del contenedor también es un resultado
                grupos = [g for g in grupos if g == "aws/spans"]
            time.sleep(30)
            continue
        while (r := logs.get_query_results(queryId=qid))["status"] not in ("Complete", "Failed", "Cancelled", "Timeout"):
            time.sleep(2)
        filas = [{c["field"]: c["value"] for c in fila} for fila in r.get("results", [])]
        ac = {f.get("spanId") for f in filas}
        if all(e["agentcore"] in ac for e in ESPERADO.values()) and any(f.get("name") == "sonda-kernel-tarde" for f in filas):
            break
        time.sleep(30)
    print("     tramos encontrados:", flush=True)
    for f in filas:
        quien = next((k for k, v in TRAZAS.items() if v == f.get("traceId")), "?")
        print(f"       [{quien}] {f.get('name')} · {f.get('spanId')} ← {f.get('parentSpanId')} · servicio {f.get('servicio')} · "
              f"{(f.get('@log') or '').split(':')[-1]} / {f.get('@logStream')}", flush=True)
    for nombre, e in ESPERADO.items():
        de = [f for f in filas if f.get("traceId") == TRAZAS[nombre]]
        ac = [f for f in de if f.get("spanId") == e["agentcore"]]
        anota(f"D2. {nombre}: se guarda el tramo de AgentCore del que cuelga el contenedor", bool(ac),
              f"{ac[0].get('name')} · servicio {ac[0].get('servicio')} · {(ac[0].get('@log') or '').split(':')[-1]} / "
              f"{ac[0].get('@logStream')}" if ac else f"no aparece {e['agentcore']}")
        if ac:
            anota(f"D3. {nombre}: el tramo de AgentCore cuelga del tramo de api", ac[0].get("parentSpanId") == e["api"],
                  f"padre {ac[0].get('parentSpanId')} (api {e['api']})")
        anota(f"D4. {nombre}: el tramo de api está en aws/spans", any(f.get("spanId") == e["api"] for f in de), "")
    for var in VARIANTES:
        k = f"E_{var}"
        if k in TRAZAS:
            hay = any(f.get("traceId") == TRAZAS[k] for f in filas)
            anota(f"E2. Con {var}: el tramo {'se guarda' if hay else 'no se guarda'}", True,
                  f"HTTP {creado.get('iam_resultados', {}).get(var)}")
    return filas


def vista_consola(filas):
    for nombre, e in ESPERADO.items():
        t = TRAZAS[nombre]
        xid = f"1-{t[:8]}-{t[8:]}"
        try:
            tok = xray.start_trace_retrieval(TraceIds=[xid], StartTime=time.time() - 7200,
                                             EndTime=time.time() + 300)["RetrievalToken"]
            r = {}
            for _ in range(30):
                r = xray.list_retrieved_traces(RetrievalToken=tok, TraceFormat="OTEL")
                if r.get("RetrievalStatus") in ("COMPLETE", "FAILED", "CANCELLED", "TIMEOUT"):
                    break
                time.sleep(4)
            tramos = []
            for traza in r.get("Traces", []):
                for sp in traza.get("Spans", []):
                    doc = sp.get("Document")
                    d = json.loads(doc) if isinstance(doc, str) else (doc or {})
                    tramos.append((d.get("name"), d.get("spanId") or sp.get("Id")))
            nombres = sorted({n or "?" for n, _ in tramos})
            print(f"     consola ({nombre}): {r.get('RetrievalStatus')} · {len(tramos)} tramos · {nombres}", flush=True)
            ids = {i for _, i in tramos}
            anota(f"F1. {nombre}: la recuperación de trazas devuelve el tramo de api (aws/spans)", e["api"] in ids, "")
            anota(f"F2. {nombre}: y los del contenedor (grupo del runtime) en la misma traza",
                  e["contenedor"] in ids or "sonda-kernel-ejecucion" in nombres, ", ".join(nombres)[:300])
            anota(f"F3. {nombre}: y el tramo de AgentCore", e["agentcore"] in ids, "")
        except ClientError as ex:
            anota(f"F. {nombre}: recuperación de trazas", False, f"{codigo(ex)} {str(ex)[:200]}")
        try:
            b = xray.batch_get_traces(TraceIds=[xid])
            segs = [json.loads(s["Document"]).get("name") for tr in b.get("Traces", []) for s in tr.get("Segments", [])]
            print(f"     BatchGetTraces ({nombre}): {len(segs)} segmentos {sorted(set(segs))}", flush=True)
        except ClientError as ex:
            print("     BatchGetTraces:", codigo(ex), flush=True)


# ---------------------------------------------------------------- borrado
def borrar():
    if "entrega" in creado:
        intenta("entrega", lambda: logs.delete_delivery(id=creado["entrega"]))
    if "entrega_fuente" in creado:
        intenta("fuente", lambda: logs.delete_delivery_source(name=creado["entrega_fuente"]))
    if "entrega_destino" in creado:
        intenta("destino", lambda: logs.delete_delivery_destination(name=creado["entrega_destino"]))
    for rol in creado.get("roles_iam", []):
        for p in iam.list_role_policies(RoleName=rol).get("PolicyNames", []):
            intenta("política", lambda p=p, rol=rol: iam.delete_role_policy(RoleName=rol, PolicyName=p))
        intenta("rol", lambda rol=rol: iam.delete_role(RoleName=rol))
    s14.borrar()


if __name__ == "__main__":
    try:
        cuenta = sts.get_caller_identity()["Account"]
        print("cuenta", cuenta, "·", REGION, "·", NOMBRE, flush=True)
        errores = []

        def en_hilo(fn, *a):
            try:
                fn(*a)
            except Exception as e:
                errores.append(f"{fn.__name__}: {type(e).__name__}: {str(e)[:300]}")

        while xray.get_trace_segment_destination()["Status"] == "PENDING":  # p. ej., la restauración de otra sonda
            print("destino de X-Ray en PENDING; espero", flush=True)
            time.sleep(30)
        hilos = [threading.Thread(target=en_hilo, args=a) for a in
                 ((s14.activar_transaction_search, cuenta), (s14.crear_runtime, cuenta), (crear_roles_iam, cuenta))
                 if a[0] is not crear_roles_iam or os.environ.get("SONDA_SIN_IAM") != "1"]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        for e in errores:
            anota("montaje", False, e)
        if "runtime_arn" in creado:
            try:
                activar_trazado_runtime(cuenta)
            except ClientError as e:
                anota("D0. El runtime acepta la entrega TRACES hacia X-Ray (vended logs)", False, f"{codigo(e)} {str(e)[:300]}")
            time.sleep(60)
            invoca_con_api("D_trazado_1")
        if "rol_de" in creado:
            probar_iam(cuenta)
        else:
            time.sleep(120)
        if "runtime_arn" in creado:
            invoca_con_api("D_trazado_2")  # unos minutos después de activar, por si la entrega tarda
            time.sleep(25)
            flush_kernel()
        filas = buscar_tramos()
        vista_consola(filas)
    finally:
        borrar()
    fallos = [r for r in base.resultados if not r[1]]
    print(f"\n{len(base.resultados) - len(fallos)} de {len(base.resultados)} comprobaciones como espera el diseño")
    sys.exit(1 if fallos else 0)
