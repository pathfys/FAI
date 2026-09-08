import base64
import os

from cryptography.fernet import Fernet

import config

_key: bytes | None = None


def _get_key() -> bytes:
    global _key
    if _key is not None:
        return _key
    raw = config.ENCRYPTION_KEY
    if raw:
        _key = raw.encode() if isinstance(raw, str) else raw
    else:
        _key = Fernet.generate_key()
        print(
            f"WARNING: No ENCRYPTION_KEY set. Generated ephemeral key: {_key.decode()}"
        )
        print("Set ENCRYPTION_KEY in .env to persist encrypted data across restarts.")
    return _key


def encrypt(plaintext: str) -> str:
    f = Fernet(_get_key())
    return base64.urlsafe_b64encode(f.encrypt(plaintext.encode())).decode()


def decrypt(ciphertext: str) -> str:
    f = Fernet(_get_key())
    return f.decrypt(base64.urlsafe_b64decode(ciphertext.encode())).decode()
