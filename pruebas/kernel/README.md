# Pruebas del kernel sin AWS

Comprueban, ejecutando Strands de verdad, lo que el documento de arquitectura (`docs/arquitectura-agentes.md`, sección "Verificado antes de construir") afirma sobre su comportamiento. No llaman a Bedrock ni a ningún servicio de AWS: usan un modelo falso (`fake.py`) que devuelve respuestas preparadas y apunta lo que recibe, y servidores MCP mínimos y locales (`mcp_eco.py` por stdio; `mcp_cabeceras.py` por Streamable HTTP, que apunta la cabecera `Authorization` y la sesión de cada petición).

Están fijadas a las versiones que se han leído (Strands 1.63.0, SDK de AgentCore 1.25.2). Al subir de versión, se repiten: si alguna falla, el documento ha dejado de ser cierto.

```bash
cd pruebas/kernel
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q -s -p no:warnings
```

`test_limite_de_procesos_solo_vale_con_otro_usuario` solo comprueba la diferencia entre root y otro usuario si se ejecuta como root (y necesita el usuario `nobody`).

| Fichero | Qué comprueba |
|---|---|
| `t_basico.py` | El snapshot restaura el prompt de sistema guardado y hay que volver a fijarlo; `BeforeModelCallEvent.cancel` termina con `end_turn` sin llamar al modelo; registrar dos veces una herramienta (falla con `AgentTool` propias, no con `@tool`); `as_tool` exige `preserve_context=True` con snapshot. |
| `t_subagentes.py` | Ejecutor concurrente por defecto y secuencial con `SequentialToolExecutor`; un subagente consume los `Limits` del principal y sus llamadas al modelo no pasan por los hooks del principal; sus eventos llegan como `AgentAsToolStreamEvent`; un hook de orden 0 ve la llamada antes que el `ask` de `HumanInTheLoop`, que solo recibe texto; herramientas registradas a mitad de ejecución. |
| `t_contexto_sandbox.py` | Modo automático del contexto con stash en almacenamiento duradero (recorte, `retrieve_context`, snapshot pequeño, una entrada por bloque); `Graph` rechaza nodos con snapshot; la shell por defecto hereda el entorno y acepta el timeout del modelo; prototipo del `Sandbox` propio (entorno limpio, memoria, timeout fijo), el límite de procesos, que como root no se aplica, y que otro usuario no lee el entorno ni la memoria del kernel. |
| `t_mcp_meta.py` | El `approval_id` llega al servidor MCP en `_meta` solo en la llamada marcada, con un diccionario `meta` del `MCPClient` que rellena un hook. |
| `t_reutilizar.py` | El `Agent` del chat montado una vez y reutilizado en tres mensajes, cada uno con credenciales y token nuevos, sin reconstruir nada: todas las peticiones a Bedrock (principal y subagente `as_tool(preserve_context=True)`, `BedrockModel` real con la respuesta simulada en `before-send`) van firmadas con las credenciales del mensaje, leídas de un `Credentials` propio en la cadena del `boto3.Session`; todas las peticiones al MCP (principal y subagente) llevan el token del mensaje, puesto por un `httpx.Auth` propio (`auth_provider` del `MCPClient`); el `MCPClient` se conecta una vez y sigue abierto entre mensajes (con el protocolo 2026-07-28 sin sesión; con `initialize`, la misma `Mcp-Session-Id`, pero el stream GET se queda con el token del primer mensaje); el `SnapshotSessionManager` escribe tras cada mensaje también en el 2.º y 3.º `agent(...)`. Hecha (1-11-2026): 3 de 3, con mcp 2.3.0. Con Bedrock de verdad: `pruebas/aws/sonda_credenciales_vivas.py`. |
| `t_trazas_mcp.py` (+ `mcp_trazas.py`, `hub_trazas.py`, `trazas_fichero.py`) | Trazas a través del hub: kernel (Strands + `MCPClient` por Streamable HTTP, con los propagadores de AgentCore `baggage,xray,tracecontext`) → hub como el de la sonda 13 (proxy HTTP que lee el cuerpo) → MCP con el SDK 2.x. El `_meta` del kernel lleva el tramo `MCP send tools/call` del SDK de MCP (que cuelga del `execute_tool` de Strands y pisa lo que Strands inyecta), con `traceparent` y también `X-Amzn-Trace-Id`; el servidor MCP abre su tramo SERVER desde `_meta` sin configurar nada. Si el hub reenvía los mismos bytes, el MCP cuelga del kernel y el hub queda como hermano; si quita las claves de contexto (`traceparent`, `tracestate`, `baggage`, `X-Amzn-Trace-Id`) y escribe las de su tramo, el MCP cuelga del hub. Hecha (2-11-2026): 2 de 2, con mcp 2.3.0. Necesita `opentelemetry-propagator-aws-xray`. |
