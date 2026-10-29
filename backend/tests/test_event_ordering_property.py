"""
Property-based test for Event Router event ordering preservation (Property 8).

Uses Hypothesis to generate arbitrary sequences of AG-UI events and verifies
that the Event Router forwards them in the same order received with no drops.

Feature: agui-protocol-integration, Property 8: Event ordering preservation
Validates: Requirements 6.3
"""

import json
import sys
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

# Add lambda directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda', 'websocket'))


# ─── Hypothesis Strategies ──────────────────────────────────────────────────

# All known AG-UI event types
AGUI_EVENT_TYPES = [
    "RUN_STARTED",
    "RUN_FINISHED",
    "RUN_ERROR",
    "STEP_STARTED",
    "STEP_FINISHED",
    "TOOL_CALL_START",
    "TOOL_CALL_END",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "REASONING_START",
    "REASONING_MESSAGE_CONTENT",
    "REASONING_END",
    "STATE_SNAPSHOT",
    "STATE_DELTA",
]

# Strategy for a unique ID string
id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=3,
    max_size=20,
)

# Strategy for a non-empty text string (for deltas, step names, etc.)
non_empty_text_st = st.text(min_size=1, max_size=100).filter(lambda t: t.strip() != "")

# Strategy for a timestamp (milliseconds)
timestamp_st = st.integers(min_value=1_000_000_000_000, max_value=9_999_999_999_999)


@st.composite
def agui_event_st(draw):
    """Generate a single valid AG-UI event with appropriate fields."""
    event_type = draw(st.sampled_from(AGUI_EVENT_TYPES))
    ts = draw(timestamp_st)

    if event_type == "RUN_STARTED":
        return {
            "type": event_type,
            "run_id": draw(id_st),
            "thread_id": draw(id_st),
            "timestamp": ts,
        }
    elif event_type == "RUN_FINISHED":
        return {
            "type": event_type,
            "run_id": draw(id_st),
            "thread_id": draw(id_st),
            "status": draw(st.sampled_from(["completed", "error", "session_timeout"])),
            "timestamp": ts,
        }
    elif event_type == "RUN_ERROR":
        return {
            "type": event_type,
            "run_id": draw(id_st),
            "message": draw(non_empty_text_st),
            "timestamp": ts,
        }
    elif event_type == "STEP_STARTED":
        return {
            "type": event_type,
            "step_name": draw(non_empty_text_st),
            "timestamp": ts,
        }
    elif event_type == "STEP_FINISHED":
        return {
            "type": event_type,
            "step_name": draw(non_empty_text_st),
            "status": draw(st.sampled_from(["completed", "error"])),
            "timestamp": ts,
        }
    elif event_type == "TOOL_CALL_START":
        return {
            "type": event_type,
            "tool_call_id": draw(id_st),
            "tool_call_name": draw(non_empty_text_st),
            "timestamp": ts,
        }
    elif event_type == "TOOL_CALL_END":
        return {
            "type": event_type,
            "tool_call_id": draw(id_st),
            "tool_call_name": draw(non_empty_text_st),
            "status": draw(st.sampled_from(["completed", "error"])),
            "timestamp": ts,
        }
    elif event_type == "TEXT_MESSAGE_START":
        return {
            "type": event_type,
            "message_id": draw(id_st),
            "timestamp": ts,
        }
    elif event_type == "TEXT_MESSAGE_CONTENT":
        return {
            "type": event_type,
            "message_id": draw(id_st),
            "delta": draw(non_empty_text_st),
            "timestamp": ts,
        }
    elif event_type == "TEXT_MESSAGE_END":
        return {
            "type": event_type,
            "message_id": draw(id_st),
            "timestamp": ts,
        }
    elif event_type == "REASONING_START":
        return {
            "type": event_type,
            "message_id": draw(id_st),
            "timestamp": ts,
        }
    elif event_type == "REASONING_MESSAGE_CONTENT":
        return {
            "type": event_type,
            "message_id": draw(id_st),
            "delta": draw(non_empty_text_st),
            "timestamp": ts,
        }
    elif event_type == "REASONING_END":
        return {
            "type": event_type,
            "message_id": draw(id_st),
            "timestamp": ts,
        }
    elif event_type == "STATE_SNAPSHOT":
        return {
            "type": event_type,
            "snapshot": {"session_id": draw(id_st), "turn_count": draw(st.integers(min_value=0, max_value=50))},
            "timestamp": ts,
        }
    elif event_type == "STATE_DELTA":
        return {
            "type": event_type,
            "delta": {"key": draw(non_empty_text_st)},
            "timestamp": ts,
        }
    else:
        # Fallback (should not reach here)
        return {"type": event_type, "timestamp": ts}


# Strategy for a sequence of AG-UI events (1 to 20 events)
agui_event_sequence_st = st.lists(agui_event_st(), min_size=1, max_size=20)


# Strategy for a mixed stream: AG-UI events interleaved with non-AG-UI lines
@st.composite
def mixed_stream_lines_st(draw):
    """
    Generate a list of SSE stream lines that includes AG-UI events
    interleaved with non-AG-UI content (legacy events, metadata, non-JSON).

    Returns:
        tuple: (lines, expected_agui_events)
            - lines: list of byte-encoded SSE lines as they'd come from AgentCore
            - expected_agui_events: list of AG-UI event dicts in order
    """
    agui_events = draw(st.lists(agui_event_st(), min_size=1, max_size=15))
    lines = []
    expected = []

    for evt in agui_events:
        # Optionally prepend non-AG-UI lines (noise)
        num_noise = draw(st.integers(min_value=0, max_value=3))
        for _ in range(num_noise):
            noise_type = draw(st.sampled_from(["legacy", "metadata", "non_json", "empty"]))
            if noise_type == "legacy":
                legacy = json.dumps({"type": "chunk", "data": "some text", "chunkNumber": 1})
                lines.append(legacy.encode('utf-8'))
            elif noise_type == "metadata":
                meta = json.dumps({"event": {"metadata": {"usage": {"inputTokens": 100}}}})
                lines.append(meta.encode('utf-8'))
            elif noise_type == "non_json":
                lines.append(b"this is not json at all")
            elif noise_type == "empty":
                lines.append(b"")

        # Add the AG-UI event line
        line_str = json.dumps(evt)
        # Optionally add SSE "data: " prefix
        if draw(st.booleans()):
            line_str = f"data: {line_str}"
        lines.append(line_str.encode('utf-8'))
        expected.append(evt)

    return lines, expected


# ─── Helper: Simulate Event Router forwarding logic ──────────────────────────


def simulate_event_router_forwarding(lines: list[bytes], query_module) -> list[dict]:
    """
    Simulate the Event Router's AG-UI event detection and forwarding logic.

    This replicates the core loop from query.py's lambda_handler that:
    1. Iterates over streaming response lines
    2. Strips SSE "data: " prefix
    3. Parses JSON
    4. Detects AG-UI events via is_agui_event()
    5. Forwards them via send_message_safe()

    Args:
        lines: List of byte-encoded stream lines (simulating response.iter_lines())
        query_module: The imported query module (for is_agui_event)

    Returns:
        List of AG-UI event dicts in the order they would be forwarded.
    """
    forwarded_events = []

    for line in lines:
        if not line:
            continue

        line_str = line.decode('utf-8')

        # Remove SSE prefix if present
        if line_str.startswith('data: '):
            line_str = line_str[6:]

        # Try to parse as JSON and check if it's an AG-UI event
        try:
            parsed = json.loads(line_str)
            if query_module.is_agui_event(parsed):
                forwarded_events.append(parsed)
        except json.JSONDecodeError:
            pass  # Skip non-JSON lines

    return forwarded_events


# ─── Fixture: Import query module ────────────────────────────────────────────


@pytest.fixture
def query_module():
    """Import the query module with mocked AWS dependencies."""
    env_vars = {
        'CONNECTIONS_TABLE_NAME': 'test-connections',
        'AWS_BEDROCK_REGION': 'us-east-1',
        'AGENT_ARN': 'arn:aws:bedrock:us-east-1:123456789:agent/test-agent',
    }
    with patch.dict(os.environ, env_vars):
        with patch('boto3.resource'), patch('boto3.client'):
            if 'query' in sys.modules:
                del sys.modules['query']
            import query
            return query


# ─── Property 8: Event ordering preservation ─────────────────────────────────
# Feature: agui-protocol-integration, Property 8: Event ordering preservation


class TestProperty8EventOrderingPreservation:
    """
    Property 8: Event ordering preservation.

    For any sequence of AG-UI events emitted by the Event Emitter, the Event
    Router SHALL forward them to the WebSocket client in the same order they
    were received, with no events dropped or reordered.

    Validates: Requirements 6.3
    """

    def setup_method(self):
        """Import the query module with mocked environment."""
        env_vars = {
            'CONNECTIONS_TABLE_NAME': 'test-connections',
            'AWS_BEDROCK_REGION': 'us-east-1',
            'AGENT_ARN': 'arn:aws:bedrock:us-east-1:123456789:agent/test-agent',
        }
        with patch.dict(os.environ, env_vars):
            with patch('boto3.resource'), patch('boto3.client'):
                if 'query' in sys.modules:
                    del sys.modules['query']
                import query
                self.query = query

    @given(events=agui_event_sequence_st)
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_pure_agui_stream_preserves_order(self, events):
        """
        Given a pure sequence of AG-UI events (no noise), the Event Router
        forwards all events in the exact same order with no drops.
        """
        # Encode events as SSE lines (simulating AgentCore response)
        lines = [json.dumps(evt).encode('utf-8') for evt in events]

        # Simulate the router's forwarding logic
        forwarded = simulate_event_router_forwarding(lines, self.query)

        # Same count (no drops)
        assert len(forwarded) == len(events), (
            f"Event count mismatch: sent {len(events)}, forwarded {len(forwarded)}. "
            f"Dropped {len(events) - len(forwarded)} events."
        )

        # Same order (no reordering)
        for i, (original, fwd) in enumerate(zip(events, forwarded)):
            assert original == fwd, (
                f"Event at position {i} differs.\n"
                f"  Original: {original}\n"
                f"  Forwarded: {fwd}"
            )

    @given(events=agui_event_sequence_st)
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_sse_prefixed_stream_preserves_order(self, events):
        """
        Given AG-UI events with SSE 'data: ' prefix, the Event Router
        correctly strips the prefix and forwards events in order.
        """
        # Encode with SSE prefix
        lines = [f"data: {json.dumps(evt)}".encode('utf-8') for evt in events]

        forwarded = simulate_event_router_forwarding(lines, self.query)

        assert len(forwarded) == len(events), (
            f"Event count mismatch with SSE prefix: sent {len(events)}, forwarded {len(forwarded)}"
        )

        for i, (original, fwd) in enumerate(zip(events, forwarded)):
            assert original == fwd, (
                f"Event at position {i} differs after SSE prefix stripping.\n"
                f"  Original: {original}\n"
                f"  Forwarded: {fwd}"
            )

    @given(data=mixed_stream_lines_st())
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_mixed_stream_preserves_agui_order_no_drops(self, data):
        """
        Given a mixed stream of AG-UI events interleaved with non-AG-UI
        content (legacy messages, metadata, non-JSON lines, empty lines),
        the Event Router:
        1. Forwards only AG-UI events
        2. Preserves their original order
        3. Drops no AG-UI events
        """
        lines, expected_events = data

        forwarded = simulate_event_router_forwarding(lines, self.query)

        # No AG-UI events dropped
        assert len(forwarded) == len(expected_events), (
            f"AG-UI event count mismatch in mixed stream: "
            f"expected {len(expected_events)}, forwarded {len(forwarded)}. "
            f"Total lines in stream: {len(lines)}"
        )

        # Order preserved
        for i, (expected, fwd) in enumerate(zip(expected_events, forwarded)):
            assert expected == fwd, (
                f"AG-UI event at position {i} differs in mixed stream.\n"
                f"  Expected: {expected}\n"
                f"  Forwarded: {fwd}"
            )

    @given(events=agui_event_sequence_st)
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_non_agui_events_not_forwarded(self, events):
        """
        Non-AG-UI events (legacy messages, metadata) are NOT forwarded
        by the Event Router — only AG-UI events pass through.
        """
        # Build a stream with only non-AG-UI content
        non_agui_lines = [
            json.dumps({"type": "chunk", "data": "text", "chunkNumber": 1}).encode('utf-8'),
            json.dumps({"type": "progress", "step": "parsing"}).encode('utf-8'),
            json.dumps({"type": "complete", "sessionId": "s1"}).encode('utf-8'),
            json.dumps({"event": {"metadata": {"usage": {}}}}).encode('utf-8'),
            b"plain text line",
            b"",
        ]

        forwarded = simulate_event_router_forwarding(non_agui_lines, self.query)

        assert len(forwarded) == 0, (
            f"Non-AG-UI lines should not be forwarded, but got {len(forwarded)} events"
        )

    @given(events=agui_event_sequence_st)
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_event_content_integrity(self, events):
        """
        The Event Router forwards AG-UI events without modifying their content.
        All fields in the original event are preserved exactly.
        """
        lines = [json.dumps(evt).encode('utf-8') for evt in events]
        forwarded = simulate_event_router_forwarding(lines, self.query)

        for i, (original, fwd) in enumerate(zip(events, forwarded)):
            # Verify all keys are preserved
            assert set(original.keys()) == set(fwd.keys()), (
                f"Event {i} key set changed.\n"
                f"  Original keys: {set(original.keys())}\n"
                f"  Forwarded keys: {set(fwd.keys())}"
            )
            # Verify all values are preserved
            for key in original:
                assert original[key] == fwd[key], (
                    f"Event {i} field '{key}' changed.\n"
                    f"  Original: {original[key]}\n"
                    f"  Forwarded: {fwd[key]}"
                )

    @given(
        events=st.lists(agui_event_st(), min_size=2, max_size=10),
        num_empty=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_empty_lines_between_events_do_not_cause_drops(self, events, num_empty):
        """
        Empty lines interspersed between AG-UI events do not cause
        any events to be dropped or reordered.
        """
        lines = []
        for evt in events:
            # Add empty lines before each event
            for _ in range(num_empty):
                lines.append(b"")
            lines.append(json.dumps(evt).encode('utf-8'))

        forwarded = simulate_event_router_forwarding(lines, self.query)

        assert len(forwarded) == len(events), (
            f"Empty lines caused event drops: expected {len(events)}, got {len(forwarded)}"
        )
        for i, (original, fwd) in enumerate(zip(events, forwarded)):
            assert original == fwd, f"Event {i} changed after empty line interleaving"

    @given(events=st.lists(agui_event_st(), min_size=2, max_size=10))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_malformed_json_between_events_do_not_cause_drops(self, events):
        """
        Malformed JSON lines between valid AG-UI events do not cause
        any events to be dropped or reordered.
        """
        lines = []
        for evt in events:
            # Add a malformed JSON line before each event
            lines.append(b"{broken json here")
            lines.append(json.dumps(evt).encode('utf-8'))

        forwarded = simulate_event_router_forwarding(lines, self.query)

        assert len(forwarded) == len(events), (
            f"Malformed JSON caused event drops: expected {len(events)}, got {len(forwarded)}"
        )
        for i, (original, fwd) in enumerate(zip(events, forwarded)):
            assert original == fwd, f"Event {i} changed after malformed JSON interleaving"
