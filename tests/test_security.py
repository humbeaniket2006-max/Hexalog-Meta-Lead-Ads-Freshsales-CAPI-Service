import hashlib
import hmac

from pydantic import SecretStr

from app.security import verify_meta_signature, verify_shared_secret


def test_meta_signature_valid() -> None:
    body = b'{"object":"page"}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, f"sha256={digest}", SecretStr("secret"))


def test_meta_signature_invalid() -> None:
    assert not verify_meta_signature(b"{}", "sha256=bad", SecretStr("secret"))
    assert not verify_meta_signature(b"{}", None, SecretStr("secret"))
    assert not verify_meta_signature(b"{}", "sha1=bad", SecretStr("secret"))


def test_shared_secret() -> None:
    assert verify_shared_secret("abc", SecretStr("abc"))
    assert not verify_shared_secret("abc", SecretStr("def"))
