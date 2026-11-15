"""Encryption for stored source credentials (Fernet: AES-128-CBC + HMAC-SHA256).

Credentials are encrypted before they reach the database and decrypted only when an adapter
needs them to connect. Several keys are supported for rotation: the first encrypts, all of
them decrypt, so ``QP_FERNET_KEY=new,old`` keeps old rows readable while re-encrypting.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CredentialCipher:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("at least one Fernet key is required")
        self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            # Never include the token or plaintext in the message.
            raise ValueError(
                "Stored credential can't be decrypted: QP_FERNET_KEY doesn't include the key "
                "it was encrypted with"
            ) from exc

    def rotate(self, token: str) -> str:
        """Re-encrypt with the current (first) key."""
        return self._fernet.rotate(token.encode()).decode()
