from types import SimpleNamespace

from app.models import SaveMode
from app.services.training import (
    extract_training_input_text,
    list_few_shot_examples,
    redact_training_payload,
)


class _DummyScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _DummyDB:
    def __init__(self, events):
        self._events = list(events)

    def scalars(self, *_args, **_kwargs):
        return _DummyScalarResult(self._events)


def _event(input_json, output_text, edited_ideal_output=None, is_few_shot=True):
    return SimpleNamespace(
        input_json=input_json,
        output_text=output_text,
        edited_ideal_output=edited_ideal_output,
        is_few_shot=is_few_shot,
        save_mode=SaveMode.full,
    )


def test_extract_training_input_text_prefers_input() -> None:
    event = _event({"input": "hello", "messages": [{"role": "user", "content": "ignored"}]}, "out")
    assert extract_training_input_text(event) == "hello"


def test_extract_training_input_text_uses_messages_when_input_missing() -> None:
    event = _event({"messages": [{"role": "user", "content": "How are you?"}]}, "I am good.")
    assert extract_training_input_text(event) == "user: How are you?"


def test_list_few_shot_examples_returns_clean_pairs_in_order() -> None:
    db = _DummyDB(
        [
            _event({"input": ""}, "A3"),
            _event({"input": "Q2"}, "A2", edited_ideal_output="A2_edited"),
            _event({"input": "Q1"}, "A1"),
        ]
    )
    examples = list_few_shot_examples(db, tenant_id="tenant_1", endpoint_id="ep_1", limit=5)
    assert examples == [("Q1", "A1"), ("Q2", "A2_edited")]


def test_redact_training_payload_masks_common_pii_patterns() -> None:
    input_json = {
        "input": "Email me at alice@example.com and call +1 (415) 555-0199",
        "metadata": {"card": "4242 4242 4242 4242"},
    }
    output_text = "Reach support@example.org or 555-333-1111"

    redacted_input, redacted_output = redact_training_payload(input_json, output_text)

    assert redacted_input["input"] == "Email me at [REDACTED_EMAIL] and call [REDACTED_PHONE]"
    assert redacted_input["metadata"]["card"] == "[REDACTED_CARD]"
    assert redacted_output == "Reach [REDACTED_EMAIL] or [REDACTED_PHONE]"
