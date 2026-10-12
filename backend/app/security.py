from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CredentialError(Exception):
    pass


class CredentialCipher:
    """Symmetric encryption for database passwords stored in the app DB."""

    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise CredentialError(
                "CREDENTIALS_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise CredentialError(
                "Stored credential cannot be decrypted; was CREDENTIALS_KEY changed?"
            ) from exc


@lru_cache
def get_cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().credentials_key)
