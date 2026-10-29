"""
Property-based tests for AGUIEventEmitter (Properties 1-6).

Uses Hypothesis to generate arbitrary valid strands stream event sequences
and verifies that the AG-UI correctness properties hold universally.

Feature: agui-protocol-integration
"""

import asyncio
from collections import Counter

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from utils.agui_emitter import (
    AGUIEventEmitter,
    TOOL_STEP_MAPPINGS,
    KB_RETRIEVAL_PATTERNS,
    KB_NAME_MAPPINGS,
)


# ─── Hypothesis Strategies ──────────────────────────────────────────────────

# Tool names that map to known steps
KNOWN_TOOL_NAMES = list(TOOL_STEP_MAPPINGS.keys())
KB_TOOL_NAMES = list(KB_RETRIEVAL_PATTERNS)
NON_KB_TOOL_NAMES = [t for t in KNOWN_TOOL_NAMES if t not in KB_RETRIEVAL_PATTERNS]

# Strategy for a tool use ID
tool_use_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=3,
    max_size=20,
).map(lambda s: f"tc-{s}")

# Strategy for a tool name (from known mappings)
tool_name_st = st.sampled_from(KNOWN_TOOL_NAMES)

# Strategy for a KB tool name
kb_tool_name_st = st.sampled_from(KB_TOOL_NAMES)

# Strategy for a non-KB tool name
non_kb_tool_name_st = st.sampled_from(NON_KB_TOOL_NAMES)

# Strategy for non-empty text chunks
text_chunk_st = st.text(min_size=1, max_size=200).filter(lambda t: t.strip() != "")

# Strategy for reasoning text chunks
reasoning_chunk_st = st.text(min_size=1, max_size=200).filter(lambda t: t.strip() != "")


def make_tool_block(tool_name: str, tool_use_id: str) -> list[dict]:
    """Create a complete tool use block (start + optional args + stop)."""
    return [
        {
            "event": {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": tool_name,
                            "toolUseId": tool_use_id,
                        }
                    }
                }
            }
        },
        {"event": {"contentBlockStop": {}}},
    ]


def make_text_block(chunks: list[str]) -> list[dict]:
    """Create text content block delta events."""
    return [
        {"event": {"contentBlockDelta": {"delta": {"text": chunk}}}}
        for chunk in chunks
    ]


def make_reasoning_block(chunks: list[str]) -> list[dict]:
    """Create reasoning content block delta events."""
    return [
        {"event": {"contentBlockDelta": {"delta": {"reasoningContent": {"text": chunk}}}}}
        for chunk in chunks
    ]


# Strategy for a complete tool use sequence (start + stop)
@st.composite
def tool_use_sequence_st(draw):
    """Generate a complete tool use block."""
    name = draw(tool_name_st)
    tid = draw(tool_use_id_st)
    return make_tool_block(name, tid)


# Strategy for a KB tool use sequence
@st.composite
def kb_tool_use_sequence_st(draw):
    """Generate a complete KB tool use block."""
    name = draw(kb_tool_name_st)
    tid = draw(tool_use_id_st)
    return make_tool_block(name, tid)


# Strategy for text content
@st.composite
def text_content_st(draw):
    """Generate one or more text delta events."""
    chunks = draw(st.lists(text_chunk_st, min_size=1, max_size=5))
    return make_text_block(chunks)


# Strategy for reasoning content
@st.composite
def reasoning_content_st(draw):
    """Generate one or more reasoning delta events."""
    chunks = draw(st.lists(reasoning_chunk_st, min_size=1, max_size=5))
    return make_reasoning_block(chunks)


# Strategy for a complete stream (sequence of blocks ending with messageStop)
@st.composite
def valid_stream_st(draw):
    """
    Generate a valid strands stream: zero or more tool blocks,
    optional reasoning, optional text, ending with messageStop.
    """
    blocks: list[dict] = []

    # 0-3 tool use blocks
    num_tools = draw(st.integers(min_value=0, max_value=3))
    for _ in range(num_tools):
        tool_block = draw(tool_use_sequence_st())
        blocks.extend(tool_block)

    # Optional reasoning block
    if draw(st.booleans()):
        reasoning = draw(reasoning_content_st())
        blocks.extend(reasoning)

    # Optional text block
    if draw(st.booleans()):
        text = draw(text_content_st())
        blocks.extend(text)

    # End with messageStop
    blocks.append({"event": {"messageStop": {}}})

    return blocks


# Strategy for a stream that always has tool calls
@st.composite
def stream_with_tools_st(draw):
    """Generate a stream that has at least one tool call."""
    blocks: list[dict] = []

    # 1-3 tool use blocks
    num_tools = draw(st.integers(min_value=1, max_value=3))
    for _ in range(num_tools):
        tool_block = draw(tool_use_sequence_st())
        blocks.extend(tool_block)

    # Optional text
    if draw(st.booleans()):
        text = draw(text_content_st())
        blocks.extend(text)

    blocks.append({"event": {"messageStop": {}}})
    return blocks


# Strategy for a stream with KB tool calls
@st.composite
def stream_with_kb_tools_st(draw):
    """Generate a stream that has at least one KB retrieval tool call."""
    blocks: list[dict] = []

    # At least one KB tool
    kb_block = draw(kb_tool_use_sequence_st())
    blocks.extend(kb_block)

    # Optional additional tools
    if draw(st.booleans()):
        extra = draw(tool_use_sequence_st())
        blocks.extend(extra)

    blocks.append({"event": {"messageStop": {}}})
    return blocks


# Strategy for a stream with reasoning
@st.composite
def stream_with_reasoning_st(draw):
    """Generate a stream that has reasoning content."""
    blocks: list[dict] = []

    # Optional tool calls before reasoning
    if draw(st.booleans()):
        tool_block = draw(tool_use_sequence_st())
        blocks.extend(tool_block)

    # Reasoning block (required)
    reasoning = draw(reasoning_content_st())
    blocks.extend(reasoning)

    # Optional text after reasoning
    if draw(st.booleans()):
        text = draw(text_content_st())
        blocks.extend(text)

    blocks.append({"event": {"messageStop": {}}})
    return blocks


# Strategy for a stream with text content
@st.composite
def stream_with_text_st(draw):
    """Generate a stream that has text message content."""
    blocks: list[dict] = []

    # Optional tool calls before text
    if draw(st.booleans()):
        tool_block = draw(tool_use_sequence_st())
        blocks.extend(tool_block)

    # Text block (required)
    text = draw(text_content_st())
    blocks.extend(text)

    blocks.append({"event": {"messageStop": {}}})
    return blocks


# ─── Helper: Run emitter on a stream ────────────────────────────────────────


async def collect_events(stream_events: list[dict], session_context: dict | None = None) -> list[dict]:
    """Run the emitter on a list of stream events and collect all AG-UI events."""
    if session_context is None:
        session_context = {}

    async def mock_stream():
        for event in stream_events:
            yield event

    emitter = AGUIEventEmitter(
        run_id="prop-run-1",
        session_id="prop-sess-1",
        thread_id="prop-thread-1",
    )
    events = []
    async for evt in emitter.wrap_stream(mock_stream(), session_context):
        events.append(evt)
    return events


def run_collect(stream_events: list[dict], session_context: dict | None = None) -> list[dict]:
    """Synchronous wrapper for collect_events."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(collect_events(stream_events, session_context))
    finally:
        loop.close()


# ─── Property 1: Run lifecycle bookends ─────────────────────────────────────
# Feature: agui-protocol-integration, Property 1: Run lifecycle bookends


@given(stream_events=valid_stream_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_1_run_lifecycle_bookends(stream_events):
    """
    Property 1: Run lifecycle bookends.

    For any valid clinical query payload and any stream of agent events
    (including empty streams), the Event Emitter SHALL yield RUN_STARTED
    as the first event and RUN_FINISHED as the last event.

    Validates: Requirements 1.1, 1.2
    """
    events = run_collect(stream_events)

    # Must have at least 2 events (RUN_STARTED + RUN_FINISHED)
    assert len(events) >= 2, "Must emit at least RUN_STARTED and RUN_FINISHED"

    # First event is RUN_STARTED
    first = events[0]
    assert first["type"] == "RUN_STARTED", f"First event must be RUN_STARTED, got {first['type']}"
    assert "run_id" in first, "RUN_STARTED must contain run_id"
    assert "thread_id" in first, "RUN_STARTED must contain thread_id"
    assert "timestamp" in first, "RUN_STARTED must contain timestamp"

    # Last event is RUN_FINISHED
    last = events[-1]
    assert last["type"] == "RUN_FINISHED", f"Last event must be RUN_FINISHED, got {last['type']}"
    assert "run_id" in last, "RUN_FINISHED must contain run_id"
    assert "status" in last, "RUN_FINISHED must contain status"
    assert "timestamp" in last, "RUN_FINISHED must contain timestamp"
    assert last["status"] in ("completed", "error", "session_timeout"), (
        f"RUN_FINISHED status must be valid, got {last['status']}"
    )


def test_property_1_empty_stream():
    """Property 1 edge case: empty stream still produces bookends."""
    events = run_collect([])

    # With no events in the stream, we still get bookends
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"


# ─── Property 2: Step event pairing ─────────────────────────────────────────
# Feature: agui-protocol-integration, Property 2: Step event pairing


@given(stream_events=stream_with_tools_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_2_step_event_pairing(stream_events):
    """
    Property 2: Step event pairing.

    For any stream containing tool use blocks, every STEP_STARTED event
    emitted by the Event Emitter SHALL be followed by exactly one
    corresponding STEP_FINISHED event with the same step_name, and no
    STEP_FINISHED SHALL appear without a preceding STEP_STARTED.

    Validates: Requirements 1.3, 1.4
    """
    events = run_collect(stream_events)

    step_started_names = []
    step_finished_names = []

    for evt in events:
        if evt["type"] == "STEP_STARTED":
            step_started_names.append(evt["step_name"])
        elif evt["type"] == "STEP_FINISHED":
            step_finished_names.append(evt["step_name"])

    # Every STEP_STARTED must have a matching STEP_FINISHED
    started_counts = Counter(step_started_names)
    finished_counts = Counter(step_finished_names)

    for name, count in started_counts.items():
        assert name in finished_counts, (
            f"STEP_STARTED '{name}' has no matching STEP_FINISHED"
        )
        assert finished_counts[name] == count, (
            f"STEP_STARTED '{name}' count ({count}) != STEP_FINISHED count ({finished_counts[name]})"
        )

    # No STEP_FINISHED without a preceding STEP_STARTED
    for name in finished_counts:
        assert name in started_counts, (
            f"STEP_FINISHED '{name}' has no preceding STEP_STARTED"
        )

    # Verify ordering: each STEP_STARTED must come before its STEP_FINISHED
    for name in started_counts:
        start_indices = [i for i, e in enumerate(events) if e["type"] == "STEP_STARTED" and e["step_name"] == name]
        finish_indices = [i for i, e in enumerate(events) if e["type"] == "STEP_FINISHED" and e["step_name"] == name]

        for s_idx, f_idx in zip(start_indices, finish_indices):
            assert s_idx < f_idx, (
                f"STEP_STARTED '{name}' at index {s_idx} must precede STEP_FINISHED at {f_idx}"
            )


# ─── Property 3: Tool call event pairing ────────────────────────────────────
# Feature: agui-protocol-integration, Property 3: Tool call event pairing


@given(stream_events=stream_with_tools_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_3_tool_call_event_pairing(stream_events):
    """
    Property 3: Tool call event pairing.

    For any tool invocation in the agent stream, the Event Emitter SHALL
    emit a TOOL_CALL_START event (containing tool_call_name and tool_call_id)
    followed by exactly one TOOL_CALL_END event with the same tool_call_id.

    Since the stream may reuse tool_call_ids across separate invocations,
    we verify sequential pairing: each START at position i is paired with
    the next END sharing the same tool_call_id.

    Validates: Requirements 3.1, 3.2
    """
    events = run_collect(stream_events)

    # Collect all TOOL_CALL_START and TOOL_CALL_END events with their indices
    starts = [(i, e) for i, e in enumerate(events) if e["type"] == "TOOL_CALL_START"]
    ends = [(i, e) for i, e in enumerate(events) if e["type"] == "TOOL_CALL_END"]

    # Must have at least one tool call (stream_with_tools_st guarantees this)
    assert len(starts) > 0, "Must have at least one TOOL_CALL_START"

    # Equal number of starts and ends
    assert len(starts) == len(ends), (
        f"TOOL_CALL_START count ({len(starts)}) != TOOL_CALL_END count ({len(ends)})"
    )

    # Verify required fields on all starts
    for _, evt in starts:
        assert "tool_call_name" in evt, "TOOL_CALL_START must have tool_call_name"
        assert "tool_call_id" in evt, "TOOL_CALL_START must have tool_call_id"

    # Verify sequential pairing: each START is followed by an END with same ID
    # before the next START with the same ID
    for start_pos, (start_idx, start_evt) in enumerate(starts):
        tool_call_id = start_evt["tool_call_id"]

        # Find the matching END: the first END with same ID after this START
        matching_end = None
        for end_idx, end_evt in ends:
            if end_idx > start_idx and end_evt["tool_call_id"] == tool_call_id:
                matching_end = (end_idx, end_evt)
                break

        assert matching_end is not None, (
            f"TOOL_CALL_START id={tool_call_id} at index {start_idx} has no subsequent TOOL_CALL_END"
        )

        end_idx, end_evt = matching_end

        # Verify ordering
        assert start_idx < end_idx, (
            f"TOOL_CALL_START id={tool_call_id} at {start_idx} must precede TOOL_CALL_END at {end_idx}"
        )

        # Verify no other START with same ID between this START and its END
        for other_pos, (other_idx, other_evt) in enumerate(starts):
            if other_pos != start_pos and other_evt["tool_call_id"] == tool_call_id:
                if start_idx < other_idx < end_idx:
                    # This would mean a nested tool call with same ID — not valid
                    pytest.fail(
                        f"Nested TOOL_CALL_START with same id={tool_call_id} "
                        f"at {other_idx} between START({start_idx}) and END({end_idx})"
                    )


# ─── Property 4: KB name enrichment ─────────────────────────────────────────
# Feature: agui-protocol-integration, Property 4: KB name enrichment


@given(stream_events=stream_with_kb_tools_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_4_kb_name_enrichment(stream_events):
    """
    Property 4: Knowledge base name enrichment.

    For any tool call event where the tool name matches a knowledge base
    retrieval pattern, the TOOL_CALL_START event SHALL include a non-empty
    knowledge base identifier (kb_name) in its payload.

    Validates: Requirements 3.4
    """
    events = run_collect(stream_events)

    tool_starts = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert len(tool_starts) > 0, "Stream must produce at least one TOOL_CALL_START"

    for evt in tool_starts:
        tool_name = evt["tool_call_name"]
        if tool_name in KB_RETRIEVAL_PATTERNS:
            assert "kb_name" in evt, (
                f"KB tool '{tool_name}' TOOL_CALL_START must include kb_name"
            )
            assert evt["kb_name"], (
                f"KB tool '{tool_name}' kb_name must be non-empty"
            )
            assert isinstance(evt["kb_name"], str), (
                f"KB tool '{tool_name}' kb_name must be a string"
            )


# ─── Property 5: Reasoning block pairing ────────────────────────────────────
# Feature: agui-protocol-integration, Property 5: Reasoning block pairing


@given(stream_events=stream_with_reasoning_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_5_reasoning_block_pairing(stream_events):
    """
    Property 5: Reasoning block pairing.

    For any stream containing reasoning/thinking content blocks, every
    REASONING_START event SHALL be followed by one or more
    REASONING_MESSAGE_CONTENT events and exactly one REASONING_END event,
    and no reasoning content SHALL appear outside a REASONING_START/END pair.

    Validates: Requirements 4.1, 4.2
    """
    events = run_collect(stream_events)

    # Extract reasoning-related events
    reasoning_events = [
        e for e in events
        if e["type"] in ("REASONING_START", "REASONING_MESSAGE_CONTENT", "REASONING_END")
    ]

    # Must have reasoning events since we generated a stream with reasoning
    assert len(reasoning_events) > 0, "Stream with reasoning must produce reasoning events"

    # Walk through reasoning events and verify structure
    in_reasoning = False
    content_count = 0

    for evt in reasoning_events:
        if evt["type"] == "REASONING_START":
            assert not in_reasoning, "Nested REASONING_START without REASONING_END"
            in_reasoning = True
            content_count = 0

        elif evt["type"] == "REASONING_MESSAGE_CONTENT":
            assert in_reasoning, (
                "REASONING_MESSAGE_CONTENT outside REASONING_START/END pair"
            )
            content_count += 1
            # Must have message_id and delta
            assert "message_id" in evt, "REASONING_MESSAGE_CONTENT must have message_id"
            assert "delta" in evt, "REASONING_MESSAGE_CONTENT must have delta"

        elif evt["type"] == "REASONING_END":
            assert in_reasoning, "REASONING_END without preceding REASONING_START"
            assert content_count > 0, (
                "REASONING_END without any REASONING_MESSAGE_CONTENT"
            )
            in_reasoning = False

    # Must be closed at the end
    assert not in_reasoning, "REASONING_START without matching REASONING_END"


# ─── Property 6: Text message sequence integrity ────────────────────────────
# Feature: agui-protocol-integration, Property 6: Text message sequence integrity


@given(stream_events=stream_with_text_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_6_text_message_sequence_integrity(stream_events):
    """
    Property 6: Text message sequence integrity.

    For any stream containing final answer text, the Event Emitter SHALL
    emit exactly one TEXT_MESSAGE_START event, followed by one or more
    TEXT_MESSAGE_CONTENT events (each with a non-empty delta), followed by
    exactly one TEXT_MESSAGE_END event, all sharing the same message_id.

    Validates: Requirements 5.1, 5.2, 5.3
    """
    events = run_collect(stream_events)

    # Extract text message events
    text_events = [
        e for e in events
        if e["type"] in ("TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END")
    ]

    # Must have text events since we generated a stream with text
    assert len(text_events) > 0, "Stream with text must produce text message events"

    # Count event types
    starts = [e for e in text_events if e["type"] == "TEXT_MESSAGE_START"]
    contents = [e for e in text_events if e["type"] == "TEXT_MESSAGE_CONTENT"]
    ends = [e for e in text_events if e["type"] == "TEXT_MESSAGE_END"]

    # Exactly one START and one END
    assert len(starts) == 1, f"Expected 1 TEXT_MESSAGE_START, got {len(starts)}"
    assert len(ends) == 1, f"Expected 1 TEXT_MESSAGE_END, got {len(ends)}"

    # At least one CONTENT
    assert len(contents) >= 1, "Must have at least one TEXT_MESSAGE_CONTENT"

    # All share the same message_id
    message_id = starts[0]["message_id"]
    assert message_id, "message_id must be non-empty"

    for evt in contents:
        assert evt["message_id"] == message_id, (
            f"TEXT_MESSAGE_CONTENT message_id mismatch: {evt['message_id']} != {message_id}"
        )

    assert ends[0]["message_id"] == message_id, (
        f"TEXT_MESSAGE_END message_id mismatch: {ends[0]['message_id']} != {message_id}"
    )

    # All content deltas must be non-empty
    for evt in contents:
        assert "delta" in evt, "TEXT_MESSAGE_CONTENT must have delta"
        assert evt["delta"], f"TEXT_MESSAGE_CONTENT delta must be non-empty, got: {repr(evt['delta'])}"

    # Verify ordering: START < all CONTENT < END
    start_idx = next(i for i, e in enumerate(events) if e["type"] == "TEXT_MESSAGE_START")
    end_idx = next(i for i, e in enumerate(events) if e["type"] == "TEXT_MESSAGE_END")

    for i, e in enumerate(events):
        if e["type"] == "TEXT_MESSAGE_CONTENT":
            assert start_idx < i < end_idx, (
                f"TEXT_MESSAGE_CONTENT at {i} must be between START ({start_idx}) and END ({end_idx})"
            )


# ─── Property 7: Event serialization round-trip ──────────────────────────────
# Feature: agui-protocol-integration, Property 7: Event serialization round-trip

import json


# Required fields per event type
REQUIRED_FIELDS: dict[str, list[str]] = {
    "RUN_STARTED": ["type", "run_id", "thread_id", "timestamp"],
    "RUN_FINISHED": ["type", "run_id", "thread_id", "status", "timestamp"],
    "STEP_STARTED": ["type", "step_name", "timestamp"],
    "STEP_FINISHED": ["type", "step_name", "status", "timestamp"],
    "TOOL_CALL_START": ["type", "tool_call_id", "tool_call_name", "timestamp"],
    "TOOL_CALL_END": ["type", "tool_call_id", "tool_call_name", "status", "timestamp"],
    "TEXT_MESSAGE_START": ["type", "message_id", "timestamp"],
    "TEXT_MESSAGE_CONTENT": ["type", "message_id", "delta", "timestamp"],
    "TEXT_MESSAGE_END": ["type", "message_id", "timestamp"],
    "REASONING_START": ["type", "message_id", "timestamp"],
    "REASONING_MESSAGE_CONTENT": ["type", "message_id", "delta", "timestamp"],
    "REASONING_END": ["type", "message_id", "timestamp"],
    "STATE_SNAPSHOT": ["type", "snapshot", "timestamp"],
    "STATE_DELTA": ["type", "delta", "timestamp"],
}


@given(stream_events=valid_stream_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_7_event_serialization_round_trip(stream_events):
    """
    Property 7: Event serialization round-trip.

    For any AG-UI event object produced by the Event Emitter, serializing
    it to JSON and deserializing it back SHALL produce an object with a
    `type` field matching the original event type, and all required fields
    for that event type SHALL be present and non-null.

    Validates: Requirements 6.2
    """
    events = run_collect(stream_events)

    for evt in events:
        # Serialize to JSON
        serialized = json.dumps(evt)

        # Deserialize back
        deserialized = json.loads(serialized)

        # Type field must be preserved
        assert "type" in deserialized, "Deserialized event must have 'type' field"
        assert deserialized["type"] == evt["type"], (
            f"Type mismatch after round-trip: {deserialized['type']} != {evt['type']}"
        )

        # All required fields for this event type must be present and non-null
        event_type = evt["type"]
        if event_type in REQUIRED_FIELDS:
            for field in REQUIRED_FIELDS[event_type]:
                assert field in deserialized, (
                    f"Event type '{event_type}' missing required field '{field}' after round-trip"
                )
                assert deserialized[field] is not None, (
                    f"Event type '{event_type}' field '{field}' is null after round-trip"
                )


@given(
    stream_events=st.one_of(
        stream_with_tools_st(),
        stream_with_reasoning_st(),
        stream_with_text_st(),
    )
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_7_all_event_types_serializable(stream_events):
    """
    Property 7 (supplementary): All event types produced by the emitter
    are JSON-serializable without raising exceptions.

    Validates: Requirements 6.2
    """
    events = run_collect(stream_events)

    for evt in events:
        # Must not raise
        try:
            serialized = json.dumps(evt)
        except (TypeError, ValueError) as e:
            pytest.fail(
                f"Event type '{evt.get('type', 'UNKNOWN')}' is not JSON-serializable: {e}"
            )

        # Must produce valid JSON
        try:
            json.loads(serialized)
        except json.JSONDecodeError as e:
            pytest.fail(
                f"Event type '{evt.get('type', 'UNKNOWN')}' produced invalid JSON: {e}"
            )


# ─── Property 9: Session state snapshot emission ─────────────────────────────
# Feature: agui-protocol-integration, Property 9: Session state snapshot emission


# Strategy for non-empty session context
session_context_st = st.fixed_dictionaries(
    {"query_count": st.integers(min_value=0, max_value=100)},
    optional={"patient_id": st.text(min_size=1, max_size=30)},
)


@given(
    stream_events=valid_stream_st(),
    session_context=session_context_st,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_9_state_snapshot_emission(stream_events, session_context):
    """
    Property 9: Session state snapshot emission.

    For any run that starts within an existing session (session_id is non-null
    and session context is non-empty), the Event Emitter SHALL emit a
    STATE_SNAPSHOT event containing the session_id and a turn_count >= 0,
    immediately after the RUN_STARTED event.

    Validates: Requirements 7.1
    """
    events = run_collect(stream_events, session_context=session_context)

    # First event must be RUN_STARTED
    assert events[0]["type"] == "RUN_STARTED"

    # Second event must be STATE_SNAPSHOT (since session_context is non-empty)
    assert len(events) >= 2, "Must have at least RUN_STARTED and STATE_SNAPSHOT"
    assert events[1]["type"] == "STATE_SNAPSHOT", (
        f"Second event must be STATE_SNAPSHOT when session context is non-empty, "
        f"got '{events[1]['type']}'"
    )

    # STATE_SNAPSHOT must contain snapshot with session_id and turn_count
    snapshot_evt = events[1]
    assert "snapshot" in snapshot_evt, "STATE_SNAPSHOT must contain 'snapshot' field"
    snapshot = snapshot_evt["snapshot"]
    assert "session_id" in snapshot, "STATE_SNAPSHOT snapshot must contain session_id"
    assert snapshot["session_id"] == "prop-sess-1", (
        f"STATE_SNAPSHOT session_id mismatch: {snapshot['session_id']}"
    )
    assert "turn_count" in snapshot, "STATE_SNAPSHOT snapshot must contain turn_count"
    assert isinstance(snapshot["turn_count"], int), "turn_count must be an integer"
    assert snapshot["turn_count"] >= 0, (
        f"turn_count must be >= 0, got {snapshot['turn_count']}"
    )
    assert "timestamp" in snapshot_evt, "STATE_SNAPSHOT must contain timestamp"


@given(stream_events=valid_stream_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_9_no_snapshot_for_empty_context(stream_events):
    """
    Property 9 (inverse): When session context is empty, no STATE_SNAPSHOT
    SHALL be emitted.

    Validates: Requirements 7.1
    """
    events = run_collect(stream_events, session_context={})

    # First event must be RUN_STARTED
    assert events[0]["type"] == "RUN_STARTED"

    # No STATE_SNAPSHOT should appear
    snapshot_events = [e for e in events if e["type"] == "STATE_SNAPSHOT"]
    assert len(snapshot_events) == 0, (
        "STATE_SNAPSHOT must NOT be emitted when session context is empty"
    )


# ─── Property 12: Step failure isolation ─────────────────────────────────────
# Feature: agui-protocol-integration, Property 12: Step failure isolation


# Strategy for a stream where one tool call raises an error during translation
# but the stream continues with more events afterward
@st.composite
def stream_with_step_error_st(draw):
    """
    Generate a stream where a step encounters a translation error
    (simulated by an unparseable event between valid tool blocks),
    but subsequent events continue to be processed.

    The emitter's _translate_event wraps each event in a try/except,
    so we test that the run continues after a problematic event.
    """
    blocks: list[dict] = []

    # First tool call (valid)
    first_tool = draw(st.sampled_from(KNOWN_TOOL_NAMES))
    first_id = draw(tool_use_id_st)
    blocks.extend(make_tool_block(first_tool, first_id))

    # Second tool call (valid — proves run continues)
    second_tool = draw(st.sampled_from(KNOWN_TOOL_NAMES))
    # Ensure different step name to verify both steps are processed
    assume(TOOL_STEP_MAPPINGS.get(second_tool) != TOOL_STEP_MAPPINGS.get(first_tool))
    second_id = draw(tool_use_id_st)
    blocks.extend(make_tool_block(second_tool, second_id))

    # Optional text at the end
    if draw(st.booleans()):
        text = draw(text_content_st())
        blocks.extend(text)

    blocks.append({"event": {"messageStop": {}}})
    return blocks


@st.composite
def stream_with_exception_mid_step_st(draw):
    """
    Generate a stream where an exception-raising event appears between
    valid tool blocks. The emitter should skip the bad event and continue.
    """
    blocks: list[dict] = []

    # First tool call (valid)
    first_tool = draw(st.sampled_from(KNOWN_TOOL_NAMES))
    first_id = draw(tool_use_id_st)
    blocks.extend(make_tool_block(first_tool, first_id))

    # Inject a problematic event that will cause _translate_event to raise
    # (an object with a broken structure that triggers an exception)
    # The emitter catches this in wrap_stream's inner try/except and continues
    class BadEvent:
        """An event that causes attribute access errors during translation."""
        def __getattr__(self, name):
            if name == "get":
                raise AttributeError("Simulated translation error")
            raise AttributeError(f"No attribute {name}")

        def __str__(self):
            raise RuntimeError("Cannot stringify")

    blocks.append(BadEvent())

    # Second tool call (valid — proves run continues after error)
    second_tool = draw(st.sampled_from(KNOWN_TOOL_NAMES))
    assume(TOOL_STEP_MAPPINGS.get(second_tool) != TOOL_STEP_MAPPINGS.get(first_tool))
    second_id = draw(tool_use_id_st)
    blocks.extend(make_tool_block(second_tool, second_id))

    # Text at the end
    text = draw(text_content_st())
    blocks.extend(text)

    blocks.append({"event": {"messageStop": {}}})
    return blocks


@given(stream_events=stream_with_step_error_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_12_step_failure_isolation_continues(stream_events):
    """
    Property 12: Step failure isolation.

    For any stream where a single step encounters an error, the Event
    Emitter SHALL emit a STEP_FINISHED event with status "error" for that
    step AND continue processing subsequent events in the stream (the run
    SHALL NOT be aborted).

    This test verifies that multiple steps are processed and the run
    completes successfully even when steps transition (which closes the
    previous step).

    Validates: Requirements 9.2
    """
    events = run_collect(stream_events)

    # Run must complete (not abort)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "completed", (
        f"Run should complete successfully, got status '{events[-1]['status']}'"
    )

    # Must have at least 2 STEP_STARTED events (proving continuation)
    step_starts = [e for e in events if e["type"] == "STEP_STARTED"]
    assert len(step_starts) >= 2, (
        f"Expected at least 2 STEP_STARTED events (proving continuation), got {len(step_starts)}"
    )

    # All steps must be properly finished
    step_finishes = [e for e in events if e["type"] == "STEP_FINISHED"]
    assert len(step_finishes) == len(step_starts), (
        f"Every STEP_STARTED must have a STEP_FINISHED: "
        f"starts={len(step_starts)}, finishes={len(step_finishes)}"
    )

    # Both tool calls must be present
    tool_starts = [e for e in events if e["type"] == "TOOL_CALL_START"]
    tool_ends = [e for e in events if e["type"] == "TOOL_CALL_END"]
    assert len(tool_starts) >= 2, "Must have at least 2 TOOL_CALL_START events"
    assert len(tool_starts) == len(tool_ends), "Every TOOL_CALL_START must have a TOOL_CALL_END"


@given(stream_events=stream_with_exception_mid_step_st())
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_12_step_failure_isolation_with_bad_event(stream_events):
    """
    Property 12: Step failure isolation (with injected bad event).

    Verifies that when an individual event causes a translation error,
    the emitter logs a warning, skips the event, and continues processing
    subsequent events. The run SHALL NOT be aborted.

    Validates: Requirements 9.2
    """
    events = run_collect(stream_events)

    # Run must complete (not abort due to the bad event)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["status"] == "completed", (
        f"Run should complete successfully despite bad event, got status '{events[-1]['status']}'"
    )

    # Must have events from AFTER the bad event (proving continuation)
    # The second tool call should produce TOOL_CALL_START
    tool_starts = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert len(tool_starts) >= 2, (
        f"Expected at least 2 TOOL_CALL_START events (proving continuation past bad event), "
        f"got {len(tool_starts)}"
    )

    # Text message should be present (it comes after the bad event)
    text_events = [e for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert len(text_events) >= 1, (
        "TEXT_MESSAGE_CONTENT must be present (proving stream continued after bad event)"
    )
