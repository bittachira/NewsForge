"""Security primitives (authz / secret management §39).

- Password hashing with PBKDF2-HMAC-SHA256 (stdlib + cryptography, no secrets hardcoded).
- Secure token generation.
- Role-based access control (RBAC) with a small permission model.

Never place API keys or credentials in code — they live only in environment variables.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _secrets
from enum import Enum


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex). PBKDF2-HMAC-SHA256, 200k iterations."""
    if salt is None:
        salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return hashlib.hexdigest(dk).encode("utf-8"), salt


def verify_password(password: str, stored_hash: bytes, salt_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    candidate = hashlib.hexdigest(dk).encode("utf-8")
    return hmac.compare_digest(candidate, stored_hash)


def generate_token(nbytes: int = 32) -> str:
    """Cryptographically secure random token (session / API key material)."""
    return _secrets.token_urlsafe(nbytes)


class Role(str, Enum):
    """RBAC roles with a minimal permission model (§39)."""

    ADMIN = "admin"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


# Roles that may approve/publish content; viewers cannot.
APPROVERS = {Role.ADMIN, Role.EDITOR, Role.REVIEWER}


def is_approver(role: str | Role) -> bool:
    return role in APPROVERS
