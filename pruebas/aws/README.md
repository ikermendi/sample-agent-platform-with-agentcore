# Sondas en AWS (nivel 2)

Comprueban en una cuenta de pruebas lo que en `docs/arquitectura-agentes.md` sigue en "Por verificar antes de construir". Cada sonda es independiente: crea lo mínimo, mide, y lo borra al acabar aunque falle. Todo lo que crean lleva el prefijo `sonda-n2-` y la etiqueta `sonda=nivel2`, para encontrar restos si algo se interrumpe.

## Antes de empezar

- Credenciales de la cuenta de pruebas en el entorno (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` y, si son temporales, `AWS_SESSION_TOKEN`), con permisos amplios en esa cuenta: las sondas crean roles de IAM, VPC, balanceadores, API Gateway, ECS, AgentCore, KMS y Scheduler.
- Región: `us-east-1` por defecto; se cambia con `SONDA_REGION`. Si la organización restringe regiones con una SCP (en `us-east-1` solo deja servicios globales como IAM y STS), usa la región donde se permite todo, p. ej. `SONDA_REGION=eu-north-1`.
- Para la sonda de Bedrock, acceso habilitado en la cuenta al Claude más barato del catálogo (Haiku), por su perfil `us.`. No hace falta ningún modelo de OpenAI: `bedrock-mantle` está fuera del diseño por ahora.
- Para las sondas con CDK, la cuenta tiene que admitir `cdk bootstrap`.

```bash
python3 -m venv .venv && .venv/bin/pip install boto3
.venv/bin/python sonda_iam_datos.py
```

## Sondas, en orden

| # | Sonda | Qué resuelve de "Por verificar" | Coste aproximado | Estado |
|---|---|---|---|---|
| 1 | `sonda_iam_datos.py` | Ejecución de código local (la parte de IAM): etiquetas de sesión y `${aws:PrincipalTag/...}`, `LeadingKeys` en transacciones, S3 por prefijo y `ListBucket`, `If-Match` con dos escritores, `SessionTokenUtilization` | Céntimos, minutos | Hecha (1-11-2026, eu-north-1): 15 de 15; `SessionTokenUtilization` no vino en la respuesta |
| 2 | Cola programada | Tareas programadas: Scheduler hacia la cola FIFO cifrada con KMS, permisos del rol del horario | ~1 $ (clave de KMS, borrado programado a 7 días) | Pendiente |
| 3 | Bedrock | Inferencia directa y Claude por perfil: el kernel llama a Claude por el VPC endpoint con las credenciales de la sesión, política por ARN de perfil y `foundation-model` (permite uno, deniega otro), uso con caché en streaming, perfiles disponibles en la cuenta (`ListInferenceProfiles`, gratis), cuánto tarda la denegación por `aws:SourceIdentity` | Céntimos de tokens con Haiku | Pendiente |
| 4 | SSE por la iDMZ (CDK) | Entrada por la iDMZ: API Gateway REST con streaming → VPC Link → NLB → PrivateLink → NLB interno → servidor SSE de juguete; cuándo corta con y sin latido, y si el límite es 15 o 5 minutos | ~1 $ por hora encendida; una o dos horas | Pendiente |
| 5 | AgentCore (CDK) | Runtime V2 en modo VPC sin salida (con código en zip, sin Docker), arranque en frío en VPC, inactividad de 60 s, `StopRuntimeSession`, 409; Code Interpreter en su VPC sin DNS, tamaños de `writeFiles` | Unos pocos dólares | Pendiente |

Fuera de esta cuenta quedan Entra (refresh token del OBO, *overage* de grupos), la iDMZ entre cuentas, Akamai y Netskope. La imagen real del kernel por ECR necesita construir la imagen (aquí no hay Docker): se hará con CodeBuild dentro de la sonda 5 si hace falta.
