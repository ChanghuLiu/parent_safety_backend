import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("PURCHASE_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("PURCHASE_TOKEN_ENCRYPTION_KEY is required for purchase verification")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("PURCHASE_TOKEN_ENCRYPTION_KEY is invalid") from exc


def encrypt_purchase_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_purchase_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise RuntimeError("purchase token decryption failed") from exc
