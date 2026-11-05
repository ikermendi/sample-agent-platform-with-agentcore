"""El Agent del chat se monta una vez y se reutiliza en los mensajes siguientes de la misma microVM.

Cada mensaje trae credenciales STS nuevas y un token nuevo para el MCP Hub. El kernel no reconstruye nada:
cambia el valor vigente en dos objetos que leen el cliente de Bedrock (al firmar cada petición) y el
cliente MCP (al mandar cada petición HTTP). Aquí se comprueba sin AWS: el BedrockModel es real (boto3),
pero un evento `before-send` de botocore contesta en lugar de Bedrock y apunta con qué clave se firmó
cada petición. El servidor MCP es local (`mcp_cabeceras.py`) y apunta la cabecera Authorization de cada
petición HTTP. La parte con Bedrock de verdad (AccessDenied al cambiar de rol, streaming) está en
`pruebas/aws/sonda_credenciales_vivas.py`.
"""
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

import boto3
import pytest
import botocore.session
import httpx
from botocore.awsrequest import AWSResponse
from botocore.credentials import CredentialProvider, CredentialResolver, Credentials, ReadOnlyCredentials
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.session.snapshot_session_manager import SnapshotSessionManager
from strands.storage.local_file_storage import LocalFileStorage
from strands.tools.executors import SequentialToolExecutor
from strands.tools.mcp import MCPClient

AQUI = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- lo que iría al kernel
class CredencialesDelMensaje(Credentials):
    """Credenciales que el kernel cambia en cada mensaje. botocore llama a get_frozen_credentials()
    al firmar cada petición, así que cada petición usa las vigentes (sin caducidad ni refresco)."""

    method = "kernel"

    def __init__(self):  # sin super().__init__: los campos salen de la copia vigente
        self._v = None

    def actualizar(self, c):  # c = respuesta["Credentials"] de AssumeRole
        self._v = ReadOnlyCredentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"], None)

    def get_frozen_credentials(self):
        return self._v

    access_key = property(lambda s: s._v.access_key)
    secret_key = property(lambda s: s._v.secret_key)
    token = property(lambda s: s._v.token)
    account_id = property(lambda s: s._v.account_id)


def sesion_boto(credenciales, region):
    """boto3.Session cuya cadena de credenciales solo tiene las del mensaje."""
    class Proveedor(CredentialProvider):
        METHOD = "kernel"

        def load(self):
            return credenciales
    bs = botocore.session.Session()
    bs.register_component("credential_provider", CredentialResolver([Proveedor()]))
    return boto3.Session(botocore_session=bs, region_name=region)


class TokenDelMensaje(httpx.Auth):
    """El cliente MCP llama a auth_flow en cada petición HTTP: pone el token vigente."""

    def __init__(self):
        self.valor = None

    def actualizar(self, t):
        self.valor = t

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.valor}"
        yield request


# ---------------------------------------------------------------- Bedrock de mentira (Converse)
class _Crudo:
    def __init__(self, b):
        self.b = b

    def stream(self, **kw):
        yield self.b


def bedrock_falso(modelo, guion, firmas, quien):
    """Contesta las Converse de este BedrockModel con el guion y apunta la clave con que se firmó cada una."""
    def antes_de_enviar(request, **kw):
        clave = re.search(r"Credential=([^/]+)/", request.headers["Authorization"].decode()).group(1)
        firmas.append((quien, clave, request.headers.get("X-Amz-Security-Token", b"").decode()))
        paso = guion.pop(0)
        if "tool" in paso:
            contenido = [{"toolUse": {"toolUseId": f"tu_{len(firmas)}", "name": paso["tool"], "input": paso["input"]}}]
            parada = "tool_use"
        else:
            contenido, parada = [{"text": paso["text"]}], "end_turn"
        cuerpo = {"output": {"message": {"role": "assistant", "content": contenido}}, "stopReason": parada,
                  "usage": {"inputTokens": 10, "outputTokens": 1, "totalTokens": 11}, "metrics": {"latencyMs": 1}}
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, _Crudo(json.dumps(cuerpo).encode()))
    modelo.client.meta.events.register("before-send.bedrock-runtime.Converse", antes_de_enviar)


def sts_falsas(n):
    return {"AccessKeyId": f"ASIAMSG{n}", "SecretAccessKey": f"secreto{n}", "SessionToken": f"sesion-msg-{n}"}


# ---------------------------------------------------------------- servidor MCP local
class ServidorMCP:
    def __init__(self, legado=False):
        self.legado = legado

    def __enter__(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.puerto = s.getsockname()[1]; s.close()
        self.registro = tempfile.mktemp(suffix=".jsonl")
        open(self.registro, "w").close()
        self.p = subprocess.Popen([sys.executable, os.path.join(AQUI, "mcp_cabeceras.py"), str(self.puerto)],
                                  env={**os.environ, "MCP_REGISTRO": self.registro, "MCP_LEGADO": "1" if self.legado else ""})
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.puerto), 0.1).close(); break
            except OSError:
                time.sleep(0.1)
        self.url = f"http://127.0.0.1:{self.puerto}/mcp"
        return self

    def lineas(self):
        with open(self.registro) as f:
            return [json.loads(x) for x in f if x.strip()]

    def __exit__(self, *a):
        self.p.terminate(); self.p.wait()


class AlmacenConEscrituras(LocalFileStorage):
    def __init__(self, d, escrituras):
        super().__init__(d); self.escrituras = escrituras

    async def write(self, key, data):
        self.escrituras.append((key, data))
        await super().write(key, data)


# ---------------------------------------------------------------- pruebas
@pytest.mark.parametrize("legado", [False, True], ids=["mcp-2026-07-28", "mcp-initialize"])
def test_principal_y_subagente_usan_credenciales_y_token_del_mensaje_sin_reconstruir(legado):
    credenciales, token = CredencialesDelMensaje(), TokenDelMensaje()
    firmas, escrituras = [], []
    guion_p, guion_s = [], []
    with ServidorMCP(legado) as srv:
        cliente = MCPClient(url=srv.url, auth_provider=token)
        agente = None
        for n in (1, 2, 3):
            # ---- la regla del kernel, por mensaje
            credenciales.actualizar(sts_falsas(n))
            token.actualizar(f"tok-msg-{n}")
            if agente is None:
                ses = sesion_boto(credenciales, "eu-north-1")
                m_sub = BedrockModel(model_id="eu.x", boto_session=ses, streaming=False)
                bedrock_falso(m_sub, guion_s, firmas, "subagente")
                sub = Agent(model=m_sub, name="revisor", description="Revisa", tools=[cliente], callback_handler=None)
                m_p = BedrockModel(model_id="eu.x", boto_session=ses, streaming=False)
                bedrock_falso(m_p, guion_p, firmas, "principal")
                d = tempfile.mkdtemp()
                agente = Agent(model=m_p, tools=[cliente, sub.as_tool(preserve_context=True)], callback_handler=None,
                               tool_executor=SequentialToolExecutor(),
                               session_manager=SnapshotSessionManager("chat1", storage=AlmacenConEscrituras(d, escrituras),
                                                                      save_latest_on="message"))
                ids = (id(agente), id(m_p), id(m_sub), id(sub), id(cliente))
            guion_p += [{"tool": "resumir", "input": {"texto": f"t{n}"}},
                        {"tool": "revisor", "input": {"input": f"revisa {n}"}}, {"text": f"respuesta {n}"}]
            guion_s += [{"tool": "buscar", "input": {"q": f"q{n}"}}, {"text": f"revisado {n}"}]
            antes_f, antes_m, antes_e = len(firmas), len(srv.lineas()), len(escrituras)
            r = agente(f"mensaje {n}")
            assert str(r).strip() == f"respuesta {n}"
            # ---- Bedrock: todas las peticiones del mensaje, del principal y del subagente, con las claves n
            del_mensaje = firmas[antes_f:]
            print(f"msg {n} Bedrock:", del_mensaje)
            assert {q for q, _, _ in del_mensaje} == {"principal", "subagente"}
            assert all(c == f"ASIAMSG{n}" and t == f"sesion-msg-{n}" for _, c, t in del_mensaje)
            # ---- MCP: todas las POST del mensaje (tools/call del principal y del subagente) con el token n
            mcp_msg = srv.lineas()[antes_m:]
            print(f"msg {n} MCP:", [(x["http"], x["rpc"], x["auth"], (x["sesion"] or "")[:8]) for x in mcp_msg])
            llamadas = [x for x in mcp_msg if x["rpc"] == "tools/call"]
            assert len(llamadas) == 2
            assert all(x["auth"] == f"Bearer tok-msg-{n}" for x in mcp_msg if x["http"] == "POST")
            # ---- snapshot: se escribe tras cada mensaje añadido, también en el 2.º y 3.º agent(...)
            nuevas = [k for k, _ in escrituras[antes_e:] if k.endswith("snapshot_latest.json")]
            print(f"msg {n} snapshot_latest escrito {len(nuevas)} veces")
            assert len(nuevas) >= 5  # usuario, toolUse, toolResult, toolUse, toolResult, respuesta
            assert f"respuesta {n}" in escrituras[-1][1].decode()
        assert (id(agente), id(m_p), id(m_sub), id(sub), id(cliente)) == ids
        todo = srv.lineas()
        conexion = [x["rpc"] for x in todo if x["rpc"] in ("server/discover", "initialize")]
        sesiones = {x["sesion"] for x in todo if x["sesion"]}
        gets = [x for x in todo if x["http"] == "GET"]
        print("conexión:", conexion, "| sesiones MCP (Mcp-Session-Id) distintas:", len(sesiones),
              "| GET (stream del servidor) y su token:", [x["auth"] for x in gets])
        # Se conecta una sola vez para los tres mensajes (y para principal y subagente)
        if legado:
            assert conexion == ["server/discover", "initialize"] and len(sesiones) == 1
        else:
            assert conexion == ["server/discover"] and len(sesiones) == 0
        cliente_vivo = cliente._background_thread is not None and cliente._background_thread.is_alive()
        print("MCPClient abierto tras los tres mensajes:", cliente_vivo, "| consumidores:", len(cliente._consumers))
        assert cliente_vivo
        agente.cleanup(); sub.cleanup()
        print("tras cleanup() de los dos agentes, MCPClient abierto:", cliente._background_thread is not None)
        assert cliente._background_thread is None
        borrados = [x for x in srv.lineas() if x["http"] == "DELETE"]
        print("DELETE de la sesión al cerrar:", len(borrados), [x["auth"] for x in borrados])


def test_mcpclient_abierto_a_mano_entre_invocaciones():
    """Alternativa sin el ciclo de vida gestionado: start() una vez, list_tools_sync(), stop() al final."""
    from fake import FakeModel, herramienta, texto
    token = TokenDelMensaje()
    with ServidorMCP() as srv:
        cliente = MCPClient(url=srv.url, auth_provider=token)
        token.actualizar("tok-msg-1")
        cliente.start()
        m = FakeModel([herramienta("buscar", {"q": "a"}), texto("uno"), herramienta("buscar", {"q": "b"}), texto("dos")])
        a = Agent(model=m, tools=cliente.list_tools_sync(), callback_handler=None)
        a("mensaje 1")
        token.actualizar("tok-msg-2")
        a("mensaje 2")
        cliente.stop(None, None, None)
        todo = srv.lineas()
        print([(x["http"], x["rpc"], x["auth"]) for x in todo])
        llamadas = [x["auth"] for x in todo if x["rpc"] == "tools/call"]
        assert llamadas == ["Bearer tok-msg-1", "Bearer tok-msg-2"]
        assert len([x for x in todo if x["rpc"] in ("server/discover", "initialize")]) == 1
