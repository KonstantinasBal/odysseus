"""Regression: stream_agent_loop surfaces *why* a guard ended the turn.

Two internal guards used to stop the agent in ways that looked like a clean
completion or a vague blocked message:

  * the loop-breaker stall detector -> now emits `loop_breaker_triggered`
  * the intent-without-action nudge cap -> now emits `intent_nudge_exhausted`

These tests run the real loop body against a fake LLM stream (no model calls,
no sleeps) and assert the structured stop event is emitted.
"""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds, relevant_tools={"bash"}):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools=relevant_tools,
    )
    return _types(_collect(gen))


def test_emits_loop_breaker_triggered_on_repeated_no_progress(monkeypatch):
    _patch_common(monkeypatch)
    # Same exact tool call every round, no answer text -> stuck-round streak
    # trips the loop-breaker once the cap is reached.
    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=8)
    lb = [e for e in events if e.get("type") == "loop_breaker_triggered"]
    assert lb, events
    e = lb[0]
    assert e["reason"]
    assert e["max_stuck_rounds"] == 4
    assert e["stuck_rounds"] >= 4
    assert "message" in e


def test_no_loop_breaker_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    events = _run_loop(monkeypatch, "All done, here is your answer.", max_rounds=8)
    assert not any(e.get("type") == "loop_breaker_triggered" for e in events), events


def test_emits_intent_nudge_exhausted_when_cap_reached(monkeypatch):
    _patch_common(monkeypatch)
    # The model keeps announcing an action with no tool call. After the nudge
    # cap is spent, the turn ends with an explicit intent_nudge_exhausted event.
    events = _run_loop(monkeypatch, "Let me check the logs now", max_rounds=5)
    inx = [e for e in events if e.get("type") == "intent_nudge_exhausted"]
    assert inx, events
    e = inx[0]
    assert e["max_nudges"] == 2
    assert e["nudges"] >= 2
    assert "message" in e


def test_no_intent_nudge_exhausted_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    events = _run_loop(monkeypatch, "Here is the complete answer to your question.", max_rounds=5)
    assert not any(e.get("type") == "intent_nudge_exhausted" for e in events), events


def test_redacts_sensitive_tool_output_before_surfacing():
    text = al._redact_sensitive_text(
        "password: private-value\n"
        "api_key=private-key\n"
        "Authorization: Bearer private-token\n"
        "normal output"
    )

    assert "private-value" not in text
    assert "private-key" not in text
    assert "private-token" not in text
    assert "password: [redacted]" in text
    assert "api_key=[redacted]" in text
    assert "Authorization: Bearer [redacted]" in text
    assert "normal output" in text
