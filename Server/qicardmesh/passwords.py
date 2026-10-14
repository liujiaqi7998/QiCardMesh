"""Client-side password digest contract for password based API calls."""

import hashlib
from pydantic import ValidationError

from .schemas import CLIENT_PASSWORD_ADAPTER


def hash_password_for_api(password: str) -> str:
    """Reference algorithm for clients: SHA-256 over the UTF-8 password."""
    return "sha256$" + hashlib.sha256(password.encode("utf-8")).hexdigest()


def is_client_password_hash(value) -> bool:
    try:
        CLIENT_PASSWORD_ADAPTER.validate_python(value)
    except ValidationError:
        return False
    return True
