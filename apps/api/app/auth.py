import base64
import hashlib
import hmac
import json
import os
import time
from uuid import uuid4


TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 14)))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$" + b64url(salt) + "$" + b64url(digest)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            b64url_decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(expected, b64url_decode(digest))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
        "jti": str(uuid4()),
    }
    encoded_payload = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = sign(encoded_payload)
    return f"{encoded_payload}.{signature}"


def verify_access_token(token: str) -> str | None:
    try:
        encoded_payload, signature = token.split(".", 1)
    except ValueError:
        return None

    if not hmac.compare_digest(signature, sign(encoded_payload)):
        return None

    try:
        payload = json.loads(b64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError):
        return None

    if int(payload.get("exp", 0)) < int(time.time()):
        return None

    user_id = payload.get("sub")
    return str(user_id) if user_id else None


def sign(value: str) -> str:
    digest = hmac.new(auth_secret(), value.encode("utf-8"), hashlib.sha256).digest()
    return b64url(digest)


def auth_secret() -> bytes:
    return os.getenv("AUTH_SECRET", "local-dev-auth-secret-change-me").encode("utf-8")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
