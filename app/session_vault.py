"""Encrypted storage for an authorized AppFolio browser session."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken


def _fernet(data_key: str) -> Fernet:
    if len(data_key) < 24:
        raise ValueError("BRIDGE_DATA_KEY must be at least 24 characters long")
    key = base64.urlsafe_b64encode(hashlib.sha256(data_key.encode()).digest())
    return Fernet(key)


def save_session(path: Path, state: dict, data_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _fernet(data_key).encrypt(json.dumps(state).encode())
    path.write_bytes(encrypted)


def load_session(path: Path, data_key: str) -> dict:
    try:
        return json.loads(_fernet(data_key).decrypt(path.read_bytes()))
    except InvalidToken as exc:
        raise ValueError(
            "The session cannot be decrypted with this key. Enter the exact key used when the session was saved; "
            "if it is unavailable, create a new session with a new key."
        ) from exc


def encrypt_json(value: object, data_key: str) -> str:
    return "fernet:" + _fernet(data_key).encrypt(json.dumps(value).encode()).decode()


def decrypt_json(value: str, data_key: str):
    if not value.startswith("fernet:"):
        return json.loads(value)
    return json.loads(_fernet(data_key).decrypt(value.removeprefix("fernet:").encode()))


def encrypt_bytes(value: bytes, data_key: str) -> bytes:
    """Encrypt arbitrary private data and fail closed when no valid key exists."""
    return _fernet(data_key).encrypt(value)


def decrypt_bytes(value: bytes, data_key: str) -> bytes:
    """Decrypt arbitrary private data previously encrypted by ``encrypt_bytes``."""
    try:
        return _fernet(data_key).decrypt(value)
    except InvalidToken as exc:
        raise ValueError("The encrypted data cannot be decrypted with this key.") from exc
