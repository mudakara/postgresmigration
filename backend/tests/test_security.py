import pytest
from cryptography.fernet import Fernet

from app.security import CredentialCipher, CredentialError


def test_round_trip():
    cipher = CredentialCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("s3cr3t#;pass word")
    assert "s3cr3t" not in token
    assert cipher.decrypt(token) == "s3cr3t#;pass word"


def test_malformed_key_is_rejected_with_a_clear_message():
    with pytest.raises(CredentialError, match="CREDENTIALS_KEY"):
        CredentialCipher("not-a-key")


def test_token_from_another_key_cannot_be_decrypted():
    token = CredentialCipher(Fernet.generate_key().decode()).encrypt("pw")
    with pytest.raises(CredentialError, match="changed"):
        CredentialCipher(Fernet.generate_key().decode()).decrypt(token)
