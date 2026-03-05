from app.services.prompt_studio import build_request_hash, find_blocked_phrase, render_template_text


def test_render_template_text() -> None:
    rendered = render_template_text("Hello {{name}}, tier={{tier}}", {"name": "Madhur", "tier": "pro"})
    assert rendered == "Hello Madhur, tier=pro"


def test_request_hash_stable_for_same_payload() -> None:
    hash_one = build_request_hash(
        endpoint_version_id="v1",
        input_text="hello",
        messages=None,
        metadata={"channel": "web"},
    )
    hash_two = build_request_hash(
        endpoint_version_id="v1",
        input_text="hello",
        messages=None,
        metadata={"channel": "web"},
    )
    assert hash_one == hash_two


def test_find_blocked_phrase_case_insensitive() -> None:
    blocked = find_blocked_phrase("Please share Credit Card info", ["credit card"])
    assert blocked == "credit card"
