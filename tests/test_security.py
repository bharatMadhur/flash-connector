from app.core.security import generate_api_key, generate_portal_token, verify_api_key, verify_portal_token



def test_api_key_hash_and_verify() -> None:
    raw, _, salt, digest = generate_api_key()
    assert raw.startswith("fc_")
    assert verify_api_key(raw, salt, digest)
    assert not verify_api_key(raw + "x", salt, digest)


def test_portal_token_hash_and_verify() -> None:
    raw, prefix, digest = generate_portal_token()
    assert raw.startswith("pl_")
    assert raw.startswith(prefix)
    assert verify_portal_token(raw, digest)
    assert not verify_portal_token(raw + "x", digest)
