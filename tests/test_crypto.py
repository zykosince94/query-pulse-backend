import pytest
from cryptography.fernet import Fernet

from query_pulse.crypto import CredentialCipher
from tests.conftest import make_settings


def test_round_trip() -> None:
    cipher = CredentialCipher([Fernet.generate_key().decode()])
    token = cipher.encrypt("s3cret")
    assert token != "s3cret"
    assert cipher.decrypt(token) == "s3cret"


def test_rotation_old_key_still_decrypts_new_key_encrypts() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token_from_old = CredentialCipher([old]).encrypt("s3cret")

    rotated = CredentialCipher([new, old])
    assert rotated.decrypt(token_from_old) == "s3cret"
    re_encrypted = rotated.rotate(token_from_old)
    assert CredentialCipher([new]).decrypt(re_encrypted) == "s3cret"


def test_wrong_key_fails_without_revealing_anything() -> None:
    token = CredentialCipher([Fernet.generate_key().decode()]).encrypt("s3cret")
    with pytest.raises(ValueError, match="QP_FERNET_KEY") as excinfo:
        CredentialCipher([Fernet.generate_key().decode()]).decrypt(token)
    assert token not in str(excinfo.value)
    assert "s3cret" not in str(excinfo.value)


def test_settings_accept_several_comma_separated_keys() -> None:
    keys = [Fernet.generate_key().decode() for _ in range(2)]
    from pydantic import SecretStr

    settings = make_settings("sqlite+aiosqlite:///:memory:", fernet_key=SecretStr(" , ".join(keys)))
    assert settings.fernet_keys == keys


def test_settings_reject_an_invalid_key_in_the_list() -> None:
    from pydantic import SecretStr, ValidationError

    good = Fernet.generate_key().decode()
    with pytest.raises(ValidationError, match="Fernet"):
        make_settings("sqlite+aiosqlite:///:memory:", fernet_key=SecretStr(f"{good},nope"))
