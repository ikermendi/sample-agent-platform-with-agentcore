"""El run_id lleva el usuario dentro y va de RoleSessionName; esto es lo que haría la Lambda de coste.

run_id = "{oid}.{sufijo}": el oid de Entra (GUID) en hexadecimal minúsculas sin guiones (32), un punto y
un sufijo de 26 caracteres en base32: en un chat, un ULID (Crockford, mayúsculas); en una tarea, los 26
primeros de base32 RFC 4648 de SHA-256("tarea|hora programada"). 59 caracteres, dentro del tope de 64 de
RoleSessionName y solo con caracteres de [\\w+=,.@-].
"""
import base64
import hashlib
import os
import re
import time
import uuid

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# El sufijo puede ser Crockford (sin I, L, O, U) o RFC 4648 (A-Z y 2-7): la unión es [0-9A-Z].
_ARN = re.compile(r"^arn:aws[\w-]*:sts::(\d{12}):assumed-role/([\w+=,.@-]{1,64})/([\w+=,.@-]{1,64})$", re.ASCII)
_RUN_ID = re.compile(r"^([0-9a-f]{32})\.([0-9A-Z]{26})$")


def ulid(ms=None):
    """ULID: 48 bits de milisegundos y 80 aleatorios, en Crockford base32 (26 caracteres, mayúsculas)."""
    n = ((int(time.time() * 1000) if ms is None else ms) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def sufijo_tarea(tarea, hora):
    """26 primeros caracteres de base32 (RFC 4648) de SHA-256(tarea|hora): sin relleno, A-Z y 2-7."""
    return base64.b32encode(hashlib.sha256(f"{tarea}|{hora}".encode()).digest()).decode()[:26]


def run_id(oid, sufijo):
    return f"{uuid.UUID(oid).hex}.{sufijo}"


def parsear_arn(arn, roles=None):
    """identity.arn del registro de invocaciones → {cuenta, rol, oid (GUID con guiones), run_id} o None.

    Devuelve None si no es una sesión de rol asumido, si el rol no está en `roles` (cuando se da) o si
    el nombre de sesión no tiene el formato del run_id.
    """
    m = _ARN.match(arn or "")
    if not m:
        return None
    cuenta, rol, sesion = m.groups()
    if roles is not None and rol not in roles:
        return None
    r = _RUN_ID.match(sesion)
    if not r:
        return None
    return {"cuenta": cuenta, "rol": rol, "oid": str(uuid.UUID(hex=r.group(1))), "run_id": sesion}
