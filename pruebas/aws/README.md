# Sondas en AWS (nivel 2)

Comprueban en una cuenta de pruebas lo que en `docs/arquitectura-agentes.md` sigue en "Por verificar antes de construir". Cada sonda es independiente: crea lo mínimo, mide, y lo borra al acabar aunque falle. Todo lo que crean lleva el prefijo `sonda-n2-` y la etiqueta `sonda=nivel2`, para encontrar restos si algo se interrumpe.

## Antes de empezar

- Credenciales de la cuenta de pruebas en el entorno (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` y, si son temporales, `AWS_SESSION_TOKEN`) o en un perfil (`AWS_PROFILE`), con permisos amplios en esa cuenta: las sondas crean roles de IAM, VPC, instancias EC2, balanceadores, PrivateLink, API Gateway, AgentCore, ECR, Route 53 Resolver DNS Firewall, KMS y Scheduler.
- Región: `us-east-1` por defecto; se cambia con `SONDA_REGION`. Si la organización restringe regiones con una SCP (en `us-east-1` solo deja servicios globales como IAM y STS), usa la región donde se permite todo, p. ej. `SONDA_REGION=eu-north-1`.
- Para la sonda de Bedrock, acceso habilitado en la cuenta al Claude más barato del catálogo (Haiku), por su perfil de la región (`us.` o `eu.`), y cuota de tokens por minuto mayor que 0. No hace falta ningún modelo de OpenAI: `bedrock-mantle` está fuera del diseño por ahora.
- Las sondas de AgentCore (5 y 6) necesitan boto3 >= 1.43 (la primera con `platformVersion`), que ya no admite Python 3.9: usan su propio entorno con Python 3.12. La 6 necesita además Docker (arm64).
- Ninguna usa CDK: son scripts con boto3, para no depender de `cdk bootstrap`.

```bash
python3 -m venv .venv && .venv/bin/pip install boto3
SONDA_REGION=eu-north-1 .venv/bin/python sonda_iam_datos.py
```

```bash
uv venv --python 3.12 .venv312 && uv pip install --python .venv312/bin/python boto3 strands-agents==1.63.0
SONDA_REGION=eu-north-1 .venv312/bin/python sonda_agentcore.py
```

Si una sonda de AgentCore deja una VPC sin borrar (las interfaces de red de AgentCore tardan en soltarse), se limpia luego con `.venv312/bin/python sonda_agentcore.py --borrar-restos`.

## Sondas, en orden

| # | Sonda | Qué resuelve de "Por verificar" | Coste aproximado | Estado |
|---|---|---|---|---|
| 1 | `sonda_iam_datos.py` | Ejecución de código local (la parte de IAM): etiquetas de sesión y `${aws:PrincipalTag/...}`, `LeadingKeys` en transacciones, S3 por prefijo y `ListBucket`, `If-Match` con dos escritores, `SessionTokenUtilization` | Céntimos, minutos | Hecha (1-11-2026, eu-north-1): 15 de 15; `SessionTokenUtilization` no vino en la respuesta |
| 2 | `sonda_scheduler_fifo.py` | Tareas programadas: Scheduler hacia la cola FIFO cifrada con KMS, permisos del rol del horario, `run_id` por disparo con `<aws.scheduler.execution-id>`, cola de fallidos | Céntimos (la clave de KMS queda con borrado programado a 7 días) | Hecha (1-11-2026, eu-north-1): 7 de 7 |
| 3 | `sonda_bedrock.py` | Inferencia directa y Claude por perfil: política por ARN de perfil y `foundation-model` (permite uno, deniega otro), uso con caché en streaming, perfiles disponibles en la cuenta, cuánto tarda la denegación por `aws:SourceIdentity`. El VPC endpoint se mide dentro de la VPC con la sonda 6 | Céntimos de tokens con Haiku | Escrita; desbloqueada (Haiku 4.5 con acceso desde el 1-11-2026, `eu.anthropic.claude-haiku-4-5-20251001-v1:0`), pendiente de ejecutar con `SONDA_MODELO` apuntando a él. La caché en streaming ya la prueba la sonda 7 |
| 4 | `sonda_sse_idmz.py` | Entrada por la iDMZ: API Gateway REST con streaming → VPC Link → NLB (con `--privatelink`: → PrivateLink → NLB interno) → servidor SSE de juguete; cuándo corta con y sin latido, y si el límite es 15 o 5 minutos | Céntimos; unos 35 minutos | Hecha (1-11-2026, eu-north-1), con y sin PrivateLink: sin datos corta a los ~5 min, con latido a los 15 min exactos |
| 5 | `sonda_agentcore.py` | Runtime V2 en modo VPC sin salida (código en zip), arranque en frío en VPC, instantánea compartida, variables de entorno de más de 2,5 KB, inactividad de 60 s, `StopRuntimeSession`; Code Interpreter en su VPC sin DNS, librerías, `writeFiles`/`readFiles` | Unos pocos dólares | Hecha (1-11-2026, eu-north-1) |
| 6 | `sonda_runtime_imagen.py` | Runtime V2 con imagen real (Strands) por los endpoints de ECR sin NAT, arranque en frío con la imagen, DNS Firewall en la VPC de agentes, ejecución ocupada hasta `maxLifetime` (55 min) | Unos pocos dólares; algo más de una hora | Hecha (1-11-2026, eu-north-1): 7 de 7; corte a los 3.307 s con `maxLifetime` = 3.300 |
| 7 | `sonda_coste_bedrock.py` | Coste de Bedrock sin fiarse del kernel: registro de invocaciones solo de metadatos, identidad y tokens por llamada (Converse, ConverseStream, caché), `requestMetadata`, latencia hasta FilterLogEvents y hasta una Lambda suscrita. Guarda y restaura la configuración del registro (es de toda la cuenta y región) | Céntimos (Haiku 4.5); unos 2 minutos | Hecha (1-11-2026, eu-north-1): 14 de 16; llega solo con metadatos, tokens iguales al `usage` (caché incluida), `identity.arn` con el RoleSessionName y `requestMetadata` presente; no salen ni el SourceIdentity ni las etiquetas de sesión; Lambda a 4-9 s |
| 7b | `sonda_runid_bedrock.py` (+ `runid_coste.py`) | El `run_id` con el usuario dentro como RoleSessionName (`{oid 32 hex}.{sufijo 26 base32}`, 59 caracteres; sufijo ULID en un chat, base32 de SHA-256 en una tarea): STS lo acepta con SourceIdentity y etiquetas, tope de 64, `identity.arn` exacto en el registro para Converse y ConverseStream, función de parseo de la Lambda de coste (registros reales y casos malos), Deny por `aws:SourceIdentity` con este nombre, latencia hasta FilterLogEvents | Céntimos (Haiku 4.5); unos 2 minutos | Hecha (1-11-2026, eu-north-1): 17 de 17; STS no altera el nombre (mayúsculas y punto intactos), 64 aceptado y 65 rechazado (`ValidationError`), el parseo saca rol, oid y run_id de los 3 registros y descarta los 11 casos malos, Deny a los 11 s, registro visible en FilterLogEvents a 7,5-11,4 s |
| 8 | `sonda_credenciales_vivas.py` | Kernel que reutiliza el `Agent` entre mensajes: el `BedrockModel` (boto3) usa en cada petición las credenciales STS del mensaje sin reconstruirse (rol A `msg-1` bien, rol B `msg-2` sin Bedrock da `AccessDeniedException`, A' `msg-3` bien; `GetCallerIdentity` del mismo session cambia de ARN), en ConverseStream, Converse y en un subagente con su propio `BedrockModel`; cambiar a mitad de un ConverseStream no corta la respuesta en curso. Necesita `strands-agents==1.63.0` en `.venv312` | Céntimos (Haiku 4.5); menos de un minuto | Hecha (1-11-2026, eu-north-1): 16 de 16; una llamada fallida deja el mensaje del usuario suelto en `agent.messages` |

Fuera de esta cuenta quedan Entra (refresh token del OBO, *overage* de grupos), la iDMZ entre cuentas, Akamai y Netskope.
