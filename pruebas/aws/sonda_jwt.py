"""Sonda 17: el JWT de Entra verificado otra vez en `api` (fila "JWT in `api`" de "To verify").

Qué comprueba, sin AWS ni Entra (claves RSA propias y un JWKS servido en local, con la forma de los de Entra):

  1. Coste por petición de verificar la firma en `api` (RS256, clave de 2048 bits como las de Entra) con el
     JWKS ya en caché, con 0, 50 y 200 grupos en el token; y lo que cuesta un arranque sin caché (bajar el JWKS).
  2. Refresco del JWKS con `PyJWKClient`: cuántas veces lo baja con un `kid` conocido, tras caducar la caché, al
     rotar Entra la clave (token con un `kid` nuevo) y con tokens de `kid` inventado (¿cada uno es una bajada?).
  3. Lo que `api` debe rechazar: firma de otra clave, emisor, audiencia, caducado, `alg: none` y HS256 firmado
     con la clave pública (confusión de algoritmo).
  4. Tamaño del token frente al límite de cabeceras del API Gateway REST (20.480 bytes en un API regional, 8.000
     en uno privado): con cuántos grupos deja de caber, contando el resto de cabeceras de la app.
  5. *Overage*: con `_claim_names` en vez de `groups`, `api` va a Graph (simulado) una vez por usuario y caché.

Fuera queda lo que necesita el tenant: el permiso real de Graph de `api` y el usuario de prueba con 201 grupos.
Necesita PyJWT con cryptography (está en `.venv312`). Uso:

    .venv312/bin/python sonda_jwt.py
"""
from __future__ import annotations

import base64
import json
import statistics
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient

TENANT = str(uuid.uuid4())
EMISOR = f"https://login.microsoftonline.com/{TENANT}/v2.0"
AUDIENCIA = "api://plataforma-agentes"
LIMITE_APIGW = 20480  # bytes: todas las cabeceras (nombres, valores y saltos), API Gateway REST regional, fija
LIMITE_PRIVADA = 8000  # lo mismo en un API privado

resultados = []


def anota(nombre, ok, detalle=""):
    resultados.append((nombre, ok, detalle))
    print(("OK   " if ok else "FALLA"), nombre, ("· " + detalle) if detalle else "", flush=True)


def info(*a):
    print("     ", *a, flush=True)


# ---------------------------------------------------------------- claves y JWKS como los de Entra

def clave():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(priv, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    return {**d, "kid": kid, "use": "sig", "x5t": kid}


class JWKS:
    """Servidor local del JWKS que cuenta las bajadas."""

    def __init__(self):
        self.claves, self.bajadas = [], 0
        jw = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                jw.bajadas += 1
                cuerpo = json.dumps({"keys": jw.claves}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/discovery/v2.0/keys"


def claims(n_grupos, **extra):
    ahora = int(time.time())
    c = {
        "aud": AUDIENCIA, "iss": EMISOR, "iat": ahora, "nbf": ahora, "exp": ahora + 3600,
        "aio": base64.b64encode(uuid.uuid4().bytes * 4).decode(), "azp": str(uuid.uuid4()), "azpacr": "0",
        "name": "Usuaria de Prueba Con Nombre Largo", "oid": str(uuid.uuid4()),
        "preferred_username": "usuaria.de.prueba@empresa-ejemplo.com",
        "rh": "0.AXEA" + "x" * 60 + ".", "scp": "acceso_usuario", "sid": str(uuid.uuid4()),
        "sub": base64.urlsafe_b64encode(uuid.uuid4().bytes * 2).decode().rstrip("="), "tid": TENANT,
        "uti": "AbCdEfGhIjKlMnOpQrStUv", "ver": "2.0",
    }
    if n_grupos:
        c["groups"] = [str(uuid.uuid4()) for _ in range(n_grupos)]
    c.update(extra)
    return c


def firma(priv, kid, c, alg="RS256"):
    return jwt.encode(c, priv, algorithm=alg, headers={"kid": kid, "typ": "JWT"})


def verifica(token, cliente):
    """Lo que hace `api`: clave por `kid` del JWKS en caché, solo RS256, emisor, audiencia y caducidad."""
    k = cliente.get_signing_key_from_jwt(token)
    return jwt.decode(token, k.key, algorithms=["RS256"], audience=AUDIENCIA, issuer=EMISOR,
                      options={"require": ["exp", "iss", "aud", "oid"]})


def grupos(c, graph, cache):
    """Grupos del usuario: del claim, o de Graph (con caché por oid) si el token trae la marca de overage."""
    if "groups" in c:
        return c["groups"], "token"
    if "groups" in c.get("_claim_names", {}):
        if c["oid"] not in cache:
            cache[c["oid"]] = graph(c["oid"])
        return cache[c["oid"]], "graph"
    return [], "ninguno"


def percentiles(ms):
    ms = sorted(ms)
    return ms[len(ms) // 2], ms[int(len(ms) * 0.99)]


# ---------------------------------------------------------------- las pruebas

def p1_coste(jw, priv, kid):
    print("\n1. Coste de verificar en api")
    cliente = PyJWKClient(jw.url, cache_jwk_set=True, lifespan=3600)
    t = time.perf_counter()
    verifica(firma(priv, kid, claims(0)), cliente)
    frio = (time.perf_counter() - t) * 1000
    info(f"primera verificación (baja el JWKS de localhost): {frio:.2f} ms; contra Entra se suma su latencia")
    for n in (0, 50, 200):
        tok = firma(priv, kid, claims(n))
        ms = []
        for _ in range(3000):
            t = time.perf_counter()
            verifica(tok, cliente)
            ms.append((time.perf_counter() - t) * 1000)
        p50, p99 = percentiles(ms)
        anota(f"Verificación con JWKS en caché, {n} grupos, por debajo de 1 ms (P99)", p99 < 1,
              f"P50 {p50 * 1000:.0f} µs, P99 {p99 * 1000:.0f} µs, {len(tok)} B")


def p2_refresco(jw, priv, kid):
    print("\n2. Refresco del JWKS")
    jw.bajadas = 0
    cliente = PyJWKClient(jw.url, cache_jwk_set=True, lifespan=2)
    tok = firma(priv, kid, claims(0))
    for _ in range(100):
        verifica(tok, cliente)
    anota("100 verificaciones con kid conocido: una sola bajada", jw.bajadas == 1, f"{jw.bajadas} bajadas")

    time.sleep(2.2)
    verifica(tok, cliente)
    anota("Al caducar la caché (lifespan), vuelve a bajar", jw.bajadas == 2, f"{jw.bajadas} bajadas")

    # PyJWT >= 2.10 limita los refrescos por kid desconocido (cooldown_duration, 30 s por defecto, contado desde
    # cualquier bajada correcta); aquí 3 s para no esperar
    jw.bajadas = 0
    cliente = PyJWKClient(jw.url, cache_jwk_set=True, lifespan=3600, cooldown_duration=3)
    verifica(tok, cliente)
    nueva, kid2 = clave(), "kid-rotada"
    jw.claves.append(jwk(nueva, kid2))
    tok2 = firma(nueva, kid2, claims(0))
    try:
        verifica(tok2, cliente)
        dentro = "aceptado"
    except jwt.PyJWKClientError:
        dentro = "rechazado"
    anota("Rotación dentro del cooldown: el kid nuevo se rechaza sin bajar el JWKS",
          dentro == "rechazado" and jw.bajadas == 1, f"{dentro}, {jw.bajadas} bajada(s)")
    time.sleep(3.2)
    verifica(tok2, cliente)
    anota("Pasado el cooldown: el kid nuevo fuerza una bajada y se acepta", jw.bajadas == 2,
          f"{jw.bajadas} bajadas")

    antes = jw.bajadas
    falsos = 0
    for i in range(20):
        try:
            verifica(firma(priv, f"kid-inventado-{i}", claims(0)), cliente)
        except jwt.PyJWKClientError:
            falsos += 1
    n = jw.bajadas - antes
    anota("20 tokens con kid inventado seguidos: rechazados", falsos == 20, f"{falsos} rechazados")
    anota("…sin bajar el JWKS en cada uno (el cooldown los frena)", n <= 1, f"{n} bajada(s) para 20 tokens")


def p3_rechazos(jw, priv, kid):
    print("\n3. Lo que api rechaza")
    cliente = PyJWKClient(jw.url, cache_jwk_set=True, lifespan=3600)
    pub_pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)
    casos = {
        "firma de otra clave con el mismo kid": firma(clave(), kid, claims(0)),
        "otro emisor": firma(priv, kid, claims(0, iss="https://login.microsoftonline.com/otro/v2.0")),
        "otra audiencia": firma(priv, kid, claims(0, aud="api://otra")),
        "caducado": firma(priv, kid, claims(0, exp=int(time.time()) - 600)),
        "sin oid": firma(priv, kid, {k: v for k, v in claims(0).items() if k != "oid"}),
        "alg none": jwt.encode(claims(0), None, algorithm="none", headers={"kid": kid}),
    }
    # HS256 con la clave pública como secreto: PyJWT no deja firmarlo, se arma a mano
    cab = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "kid": kid}).encode()).rstrip(b"=")
    cue = base64.urlsafe_b64encode(json.dumps(claims(0)).encode()).rstrip(b"=")
    import hashlib
    import hmac
    sig = base64.urlsafe_b64encode(hmac.new(pub_pem, cab + b"." + cue, hashlib.sha256).digest()).rstrip(b"=")
    casos["HS256 con la clave pública"] = (cab + b"." + cue + b"." + sig).decode()
    for nombre, tok in casos.items():
        try:
            verifica(tok, cliente)
            anota(f"Rechaza: {nombre}", False, "aceptado")
        except jwt.PyJWTError as e:
            anota(f"Rechaza: {nombre}", True, type(e).__name__)


def p4_tamano(priv, kid):
    print("\n4. Tamaño frente al límite del API Gateway REST")
    # resto de la petición de la app: línea, Host, traceparent, tracestate, Content-Type, Accept, User-Agent…
    resto = len(
        "Host: abcdefghij.execute-api.eu-north-1.amazonaws.com\r\n"
        "traceparent: 00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01\r\n"
        "Content-Type: application/json\r\nAccept: text/event-stream\r\nContent-Length: 512\r\n"
        "User-Agent: Expo/1017699 CFNetwork/1568.200.51 Darwin/24.1.0\r\nAccept-Language: es-ES,es;q=0.9\r\n"
        "Accept-Encoding: gzip, deflate, br\r\n") + len("Authorization: Bearer \r\n")
    info(f"resto de las cabeceras, sin el token: {resto} B")
    privada = None
    for n in range(0, 201, 10):
        tam = len(firma(priv, kid, claims(n)))
        if tam + resto > LIMITE_PRIVADA and privada is None:
            privada = n
        if n in (0, 50, 100, 150, 200) or n == privada:
            info(f"{n:3d} grupos: token {tam} B, cabeceras {tam + resto} B")
    tam = len(firma(priv, kid, claims(200))) + resto
    anota("200 grupos (lo máximo sin overage) caben en las cabeceras de un API regional", tam <= LIMITE_APIGW,
          f"{tam} de {LIMITE_APIGW} B")
    info(f"en un API privado (8.000 B) dejaría de caber hacia {privada} grupos")


def p5_overage(priv, kid, jw):
    print("\n5. Overage")
    cliente = PyJWKClient(jw.url, cache_jwk_set=True, lifespan=3600)
    llamadas = []

    def graph(oid):
        llamadas.append(oid)
        return [str(uuid.uuid4()) for _ in range(250)]

    cache = {}
    c = claims(0, _claim_names={"groups": "src1"},
               _claim_sources={"src1": {"endpoint": "https://graph.microsoft.com/v1.0/users/x/getMemberObjects"}})
    tok = firma(priv, kid, c)
    for _ in range(5):
        g, origen = grupos(verifica(tok, cliente), graph, cache)
    anota("Token con _claim_names: grupos de Graph, una llamada para 5 peticiones", origen == "graph"
          and len(g) == 250 and len(llamadas) == 1, f"{origen}, {len(g)} grupos, {len(llamadas)} llamada(s)")
    g, origen = grupos(verifica(firma(priv, kid, claims(3)), cliente), graph, cache)
    anota("Token con groups: no llama a Graph", origen == "token" and len(llamadas) == 1, f"{len(g)} grupos")
    g, origen = grupos(verifica(firma(priv, kid, claims(0)), cliente), graph, cache)
    anota("Sin groups ni marca: sin grupos y sin Graph", origen == "ninguno" and len(llamadas) == 1)


def main():
    jw = JWKS()
    priv, kid = clave(), "kid-entra-1"
    jw.claves.append(jwk(priv, kid))
    p1_coste(jw, priv, kid)
    p2_refresco(jw, priv, kid)
    p3_rechazos(jw, priv, kid)
    p4_tamano(priv, kid)
    p5_overage(priv, kid, jw)
    ok = sum(1 for _, b, _ in resultados if b)
    print(f"\n{ok} de {len(resultados)}")
    jw.srv.shutdown()


if __name__ == "__main__":
    main()
