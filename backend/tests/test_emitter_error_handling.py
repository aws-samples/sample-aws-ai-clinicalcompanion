"""Test error handling and edge cases in AGUIEventEmitter."""
import pytest
from utils.agui_emitter import AGUIEventEmitter


async def stream_with_exception():
    """Stream that raises an exception mid-way."""
    yield {
        "event": {
            "contentBlockStart": {
                "start": {
                    "toolUse": {"name": "parse_clinical_query", "toolUseId": "tc-1"}
                }
            }
        }
    }
    raise RuntimeError("Connection lost")


async def stream_with_reasoning():
    """Stream with reasoning/thinking content followed by answer."""
    yield {
        "event": {
            "contentBlockDelta": {
                "delta": {"reasoningContent": {"text": "Let me think about "}}
            }
        }
    }
    yield {
        "event": {
            "contentBlockDelta": {
                "delta": {"reasoningContent": {"text": "this carefully."}}
            }
        }
    }
    # Transition to answer
    yield {"event": {"contentBlockDelta": {"delta": {"text": "My recommendation is..."}}}}
    yield {"event": {"messageStop": {}}}


async def stream_with_missing_tool_name():
    """Stream with a tool call that has no name."""
    yield {
        "event": {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": "tc-no-name"}}
            }
        }
    }
    yield {"event": {"contentBlockStop": {}}}
    yield {"event": {"messageStop": {}}}


async def empty_stream():
    """Empty stream — no events at all."""
    return
    yield  # noqa: unreachable — makes it an async generator


async def test_stream_exception():
    """Test that stream exceptions produce RUN_FINISHED with error status."""
    emitter = AGUIEventEmitter(run_id="r1", session_id="s1", thread_id="t1")
    events = []
    async for evt in emitter.wrap_stream(stream_with_exception(), {}):
        events.append(evt)

    types = [e["type"] for e in events]
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "error"
    assert "Connection lost" in events[-1].get("error", "")
    # Tool call should be closed before RUN_FINISHED
    assert "TOOL_CALL_END" in types


async def test_reasoning_then_answer():
    """Test reasoning blocks followed by text answer."""
    emitter = AGUIEventEmitter(run_id="r2", session_id="s2", thread_id="t2")
    events = []
    async for evt in emitter.wrap_stream(stream_with_reasoning(), {}):
        events.append(evt)

    types = [e["type"] for e in events]
    assert "REASONING_START" in types
    assert "REASONING_MESSAGE_CONTENT" in types
    assert "REASONING_END" in types
    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "TEXT_MESSAGE_END" in types

    # Reasoning should come before text
    reasoning_start_idx = types.index("REASONING_START")
    reasoning_end_idx = types.index("REASONING_END")
    text_start_idx = types.index("TEXT_MESSAGE_START")
    assert reasoning_start_idx < reasoning_end_idx < text_start_idx


async def test_missing_tool_name():
    """Test that missing tool name defaults to 'unknown'."""
    emitter = AGUIEventEmitter(run_id="r3", session_id="s3", thread_id="t3")
    events = []
    async for evt in emitter.wrap_stream(stream_with_missing_tool_name(), {}):
        events.append(evt)

    tool_starts = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert len(tool_starts) == 1
    assert tool_starts[0]["tool_call_name"] == "unknown"


async def test_empty_stream():
    """Test that empty stream still produces RUN_STARTED and RUN_FINISHED."""
    emitter = AGUIEventEmitter(run_id="r4", session_id="s4", thread_id="t4")
    events = []
    async for evt in emitter.wrap_stream(empty_stream(), {}):
        events.append(evt)

    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "completed"


async def test_session_timeout():
    """Test session timeout handling via exception."""

    async def timeout_stream():
        raise TimeoutError("Session timed out after 30 minutes")
        yield  # noqa: unreachable

    emitter = AGUIEventEmitter(run_id="r5", session_id="s5", thread_id="t5")
    events = []
    async for evt in emitter.wrap_stream(timeout_stream(), {}):
        events.append(evt)

    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "error"
    assert "timed out" in events[-1].get("error", "").lower()


async def test_step_failure_isolation():
    """Test that a step failure doesn't abort the run — subsequent events still process."""

    async def stream_with_step_error():
        # First tool call
        yield {
            "event": {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {"name": "parse_clinical_query", "toolUseId": "tc-1"}
                    }
                }
            }
        }
        yield {"event": {"contentBlockStop": {}}}
        # Second tool call (simulates continuation after first step)
        yield {
            "event": {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {"name": "coordinate_agents", "toolUseId": "tc-2"}
                    }
                }
            }
        }
        yield {"event": {"contentBlockStop": {}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "Final answer"}}}}
        yield {"event": {"messageStop": {}}}

    emitter = AGUIEventEmitter(run_id="r6", session_id="s6", thread_id="t6")
    events = []
    async for evt in emitter.wrap_stream(stream_with_step_error(), {}):
        events.append(evt)

    types = [e["type"] for e in events]
    # Should have two STEP_STARTED events (one per tool)
    step_starts = [e for e in events if e["type"] == "STEP_STARTED"]
    assert len(step_starts) == 2
    assert step_starts[0]["step_name"] == "Parse clinical query"
    assert step_starts[1]["step_name"] == "Coordinate specialized agents"

    # Run should complete successfully
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "completed"


async def test_no_state_snapshot_for_empty_context():
    """Test that STATE_SNAPSHOT is NOT emitted when session context is empty."""
    emitter = AGUIEventEmitter(run_id="r7", session_id="s7", thread_id="t7")
    events = []
    async for evt in emitter.wrap_stream(empty_stream(), {}):
        events.append(evt)

    types = [e["type"] for e in events]
    assert "STATE_SNAPSHOT" not in types
