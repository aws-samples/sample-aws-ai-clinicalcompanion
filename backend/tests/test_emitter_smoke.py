"""Smoke test for AGUIEventEmitter — verifies basic event emission."""
import json
import pytest
from utils.agui_emitter import AGUIEventEmitter


async def mock_stream():
    """Simulate a strands-agents stream with tool call and text output."""
    yield {
        "event": {
            "contentBlockStart": {
                "start": {
                    "toolUse": {
                        "name": "parse_clinical_query",
                        "toolUseId": "tc-001",
                    }
                }
            }
        }
    }
    yield {
        "event": {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": '{"query": "test"}'}}
            }
        }
    }
    yield {"event": {"contentBlockStop": {}}}
    yield {"event": {"contentBlockDelta": {"delta": {"text": "Here is my "}}}}
    yield {
        "event": {"contentBlockDelta": {"delta": {"text": "recommendation."}}}
    }
    yield {"event": {"messageStop": {}}}


async def test_basic_event_emission():
    """Test that a basic stream produces correct AG-UI event sequence."""
    emitter = AGUIEventEmitter(
        run_id="run-1", session_id="sess-1", thread_id="thread-1"
    )
    session_ctx = {"query_count": 2, "patient_id": "patient-001"}
    events = []
    async for evt in emitter.wrap_stream(mock_stream(), session_ctx):
        events.append(evt)

    # Verify bookends
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "completed"

    # Verify STATE_SNAPSHOT
    assert events[1]["type"] == "STATE_SNAPSHOT"
    assert events[1]["snapshot"]["session_id"] == "sess-1"
    assert events[1]["snapshot"]["turn_count"] == 2

    # Verify step and tool events present
    types = [e["type"] for e in events]
    assert "STEP_STARTED" in types
    assert "TOOL_CALL_START" in types
    assert "TOOL_CALL_END" in types
    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "TEXT_MESSAGE_END" in types
    assert "STEP_FINISHED" in types


async def test_all_events_json_serializable():
    """Test that all emitted events are JSON-serializable."""
    emitter = AGUIEventEmitter(
        run_id="run-2", session_id="sess-2", thread_id="thread-2"
    )
    async for evt in emitter.wrap_stream(mock_stream(), {"query_count": 0}):
        serialized = json.dumps(evt)
        assert isinstance(serialized, str)
        # Round-trip
        deserialized = json.loads(serialized)
        assert deserialized["type"] == evt["type"]


async def test_run_started_fields():
    """Test RUN_STARTED event contains required fields."""
    emitter = AGUIEventEmitter(
        run_id="run-3", session_id="sess-3", thread_id="thread-3"
    )
    events = []
    async for evt in emitter.wrap_stream(mock_stream(), {}):
        events.append(evt)

    run_started = events[0]
    assert run_started["type"] == "RUN_STARTED"
    assert run_started["run_id"] == "run-3"
    assert run_started["thread_id"] == "thread-3"
    assert "timestamp" in run_started
    assert isinstance(run_started["timestamp"], int)


async def test_tool_call_kb_enrichment():
    """Test that KB retrieval tool calls include kb_name."""

    async def kb_stream():
        yield {
            "event": {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": "retrieve_patient_context",
                            "toolUseId": "tc-kb",
                        }
                    }
                }
            }
        }
        yield {"event": {"contentBlockStop": {}}}
        yield {"event": {"messageStop": {}}}

    emitter = AGUIEventEmitter(
        run_id="run-4", session_id="sess-4", thread_id="thread-4"
    )
    events = []
    async for evt in emitter.wrap_stream(kb_stream(), {}):
        events.append(evt)

    tool_starts = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert len(tool_starts) == 1
    assert tool_starts[0]["kb_name"] == "Patient EHR"
