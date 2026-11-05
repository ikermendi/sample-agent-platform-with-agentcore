"""Token de ejecución: JWT ES256 firmado con una clave asimétrica de KMS (ECC_NIST_P256).

`api` firma con kms:Sign (aquí, la sonda); el hub y /internal/obo verifican en memoria con la clave pública
que leen una vez con kms:GetPublicKey. KMS devuelve la firma ECDSA en DER y JWS la quiere como r||s.
"""
import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def deb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def firmar(claims, kid, firma_der):
    """firma_der(bytes) -> firma ECDSA en DER (kms.sign o una clave local)."""
    cab = b64u(json.dumps({"alg": "ES256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
    cuerpo = b64u(json.dumps(claims, separators=(",", ":")).encode())
    r, s = utils.decode_dss_signature(firma_der(f"{cab}.{cuerpo}".encode()))
    return f"{cab}.{cuerpo}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def firmador_kms(kms, key_id):
    return lambda m: kms.sign(KeyId=key_id, Message=m, MessageType="RAW", SigningAlgorithm="ECDSA_SHA_256")["Signature"]


def firmador_local(clave_privada):
    return lambda m: clave_privada.sign(m, ec.ECDSA(hashes.SHA256()))


def clave_publica_kms(kms, key_id):
    return serialization.load_der_public_key(kms.get_public_key(KeyId=key_id)["PublicKey"])


class TokenInvalido(Exception):
    pass


def verificar(token, clave_publica, kid, ahora=None):
    if not token:
        raise TokenInvalido("sin token")
    try:
        cab_b, cuerpo_b, firma_b = token.split(".")
        cab = json.loads(deb64u(cab_b))
        firma = deb64u(firma_b)
    except Exception:
        raise TokenInvalido("formato")
    if cab.get("alg") != "ES256" or cab.get("kid") != kid:
        raise TokenInvalido("alg o kid")
    if len(firma) != 64:
        raise TokenInvalido("firma")
    der = utils.encode_dss_signature(int.from_bytes(firma[:32], "big"), int.from_bytes(firma[32:], "big"))
    try:
        clave_publica.verify(der, f"{cab_b}.{cuerpo_b}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise TokenInvalido("firma")
    c = json.loads(deb64u(cuerpo_b))
    if c.get("iss") != "api" or c.get("aud") != "mcp-hub":
        raise TokenInvalido("iss o aud")
    if not isinstance(c.get("exp"), (int, float)) or c["exp"] <= (ahora or time.time()):
        raise TokenInvalido("caducado")
    if not isinstance(c.get("allow"), list):
        raise TokenInvalido("sin allow")
    return c
