"""Exercise the preview SDK's typed fields, including done-only delivery."""

import pytest

sdk = pytest.importorskip("agent_api_sdk")
SessionTurnOutputTextDeltaEvent = sdk.SessionTurnOutputTextDeltaEvent
SessionTurnOutputTextDoneEvent = sdk.SessionTurnOutputTextDoneEvent
TextOutput = pytest.importorskip("modal_agents.output").TextOutput


def event(kind, text, index=0):
    fields = {
        "event_id": "evt_1",
        "session_id": "sess_test",
        "item_id": "msg_1",
        "output_index": index,
        "content_index": 0,
    }
    if kind == "delta":
        return SessionTurnOutputTextDeltaEvent(
            type="session.turn.output_text.delta", delta=text, **fields
        )
    return SessionTurnOutputTextDoneEvent(type="session.turn.output_text.done", text=text, **fields)


def test_final_snapshot_without_deltas_is_captured():
    output = TextOutput()
    output.add(event("done", "The whole joke"))
    assert output.text == "The whole joke"


def test_final_snapshot_replaces_partial_deltas_without_duplication():
    output = TextOutput()
    output.add(event("delta", "The "))
    output.add(event("delta", "joke"))
    output.add(event("done", "The joke ends here."))
    output.add(event("done", "The joke ends here."))
    output.add(event("delta", "late replay"))
    assert output.text == "The joke ends here."


def test_multiple_parts_are_returned_in_output_order():
    output = TextOutput()
    output.add(event("done", "second", index=1))
    output.add(event("delta", "first ", index=0))
    assert output.text == "first second"
