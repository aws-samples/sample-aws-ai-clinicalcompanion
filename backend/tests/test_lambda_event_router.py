"""
Unit tests for the Lambda Event Router (query.py).

Tests AG-UI event detection, forwarding, and error handling.
Only AG-UI events are forwarded to WebSocket clients (legacy messages removed).

Feature: agui-protocol-integration
Requirements: 6.1, 6.2, 6.3, 9.1, 9.3, 7.4, 10.1, 10.4
"""

import json
import sys
import os
import time
import uuid
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from botocore.exceptions import ClientError

# Add lambda directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda', 'websocket'))


# ─── Test is_agui_event detection ────────────────────────────────────────────


class TestIsAguiEvent:
    """Tests for AG-UI event detection logic."""

    def setup_method(self):
        """Import the module under test with mocked environment."""
        # Set required env vars before importing
        env_vars = {
            'CONNECTIONS_TABLE_NAME': 'test-connections',
            'AWS_BEDROCK_REGION': 'us-east-1',
            'AGENT_ARN': 'arn:aws:bedrock:us-east-1:123456789:agent/test-agent',
        }
        with patch.dict(os.environ, env_vars):
            with patch('boto3.resource'), patch('boto3.client'):
                # Remove cached module if present
                if 'query' in sys.modules:
                    del sys.modules['query']
                import query
                self.query = query

    def test_detects_run_started(self):
        """RUN_STARTED event is correctly identified."""
        event = {"type": "RUN_STARTED", "run_id": "abc", "thread_id": "t1", "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_detects_run_finished(self):
        """RUN_FINISHED event is correctly identified."""
        event = {"type": "RUN_FINISHED", "run_id": "abc", "status": "completed", "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_detects_step_started(self):
        """STEP_STARTED event is correctly identified."""
        event = {"type": "STEP_STARTED", "step_name": "Parse query", "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_detects_tool_call_start(self):
        """TOOL_CALL_START event is correctly identified."""
        event = {"type": "TOOL_CALL_START", "tool_call_id": "tc-1", "tool_call_name": "retrieve", "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_detects_text_message_content(self):
        """TEXT_MESSAGE_CONTENT event is correctly identified."""
        event = {"type": "TEXT_MESSAGE_CONTENT", "message_id": "m1", "delta": "hello", "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_detects_reasoning_start(self):
        """REASONING_START event is correctly identified."""
        event = {"type": "REASONING_START", "message_id": "r1", "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_detects_state_snapshot(self):
        """STATE_SNAPSHOT event is correctly identified."""
        event = {"type": "STATE_SNAPSHOT", "snapshot": {"session_id": "s1"}, "timestamp": 123}
        assert self.query.is_agui_event(event) is True

    def test_rejects_legacy_chunk(self):
        """Legacy chunk message is NOT identified as AG-UI."""
        event = {"type": "chunk", "data": "hello", "chunkNumber": 1}
        assert self.query.is_agui_event(event) is False

    def test_rejects_legacy_progress(self):
        """Legacy progress message is NOT identified as AG-UI."""
        event = {"type": "progress", "step": "parsing"}
        assert self.query.is_agui_event(event) is False

    def test_rejects_legacy_complete(self):
        """Legacy complete message is NOT identified as AG-UI."""
        event = {"type": "complete", "sessionId": "s1"}
        assert self.query.is_agui_event(event) is False

    def test_rejects_unknown_type(self):
        """Unknown type is NOT identified as AG-UI."""
        event = {"type": "UNKNOWN_EVENT", "data": "test"}
        assert self.query.is_agui_event(event) is False

    def test_rejects_no_type_field(self):
        """Dict without type field is NOT identified as AG-UI."""
        event = {"event": {"contentBlockDelta": {"delta": {"text": "hi"}}}}
        assert self.query.is_agui_event(event) is False

    def test_rejects_non_dict(self):
        """Non-dict input returns False."""
        assert self.query.is_agui_event("not a dict") is False
        assert self.query.is_agui_event(None) is False
        assert self.query.is_agui_event(42) is False

    def test_all_agui_types_recognized(self):
        """All defined AG-UI event types are recognized."""
        expected_types = {
            "RUN_STARTED", "RUN_FINISHED", "RUN_ERROR",
            "STEP_STARTED", "STEP_FINISHED",
            "TOOL_CALL_START", "TOOL_CALL_END",
            "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
            "REASONING_START", "REASONING_MESSAGE_CONTENT", "REASONING_END",
            "STATE_SNAPSHOT", "STATE_DELTA",
        }
        for event_type in expected_types:
            event = {"type": event_type, "timestamp": 123}
            assert self.query.is_agui_event(event) is True, f"{event_type} not recognized"


# ─── Test make_agui_run_finished_error ───────────────────────────────────────


class TestMakeAguiRunFinishedError:
    """Tests for the AG-UI error event factory."""

    def setup_method(self):
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

    def test_creates_valid_run_finished_error(self):
        """Creates a valid RUN_FINISHED event with error status."""
        event = self.query.make_agui_run_finished_error("run-123", "Something failed")

        assert event["type"] == "RUN_FINISHED"
        assert event["run_id"] == "run-123"
        assert event["thread_id"] == "run-123"
        assert event["status"] == "error"
        assert event["error"] == "Something failed"
        assert "timestamp" in event
        assert isinstance(event["timestamp"], int)

    def test_is_recognized_as_agui_event(self):
        """The generated error event is recognized by is_agui_event."""
        event = self.query.make_agui_run_finished_error("run-456", "Timeout")
        assert self.query.is_agui_event(event) is True

    def test_is_json_serializable(self):
        """The generated error event can be serialized to JSON."""
        event = self.query.make_agui_run_finished_error("run-789", "Error msg")
        serialized = json.dumps(event)
        deserialized = json.loads(serialized)
        assert deserialized["type"] == "RUN_FINISHED"
        assert deserialized["status"] == "error"


# ─── Test AGUI_EVENT_TYPES completeness ──────────────────────────────────────


class TestAguiEventTypes:
    """Tests for the AGUI_EVENT_TYPES set."""

    def setup_method(self):
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

    def test_contains_all_lifecycle_events(self):
        """AGUI_EVENT_TYPES includes all run lifecycle events."""
        assert "RUN_STARTED" in self.query.AGUI_EVENT_TYPES
        assert "RUN_FINISHED" in self.query.AGUI_EVENT_TYPES
        assert "RUN_ERROR" in self.query.AGUI_EVENT_TYPES

    def test_contains_all_step_events(self):
        """AGUI_EVENT_TYPES includes all step events."""
        assert "STEP_STARTED" in self.query.AGUI_EVENT_TYPES
        assert "STEP_FINISHED" in self.query.AGUI_EVENT_TYPES

    def test_contains_all_tool_events(self):
        """AGUI_EVENT_TYPES includes all tool call events."""
        assert "TOOL_CALL_START" in self.query.AGUI_EVENT_TYPES
        assert "TOOL_CALL_END" in self.query.AGUI_EVENT_TYPES

    def test_contains_all_text_events(self):
        """AGUI_EVENT_TYPES includes all text message events."""
        assert "TEXT_MESSAGE_START" in self.query.AGUI_EVENT_TYPES
        assert "TEXT_MESSAGE_CONTENT" in self.query.AGUI_EVENT_TYPES
        assert "TEXT_MESSAGE_END" in self.query.AGUI_EVENT_TYPES

    def test_contains_all_reasoning_events(self):
        """AGUI_EVENT_TYPES includes all reasoning events."""
        assert "REASONING_START" in self.query.AGUI_EVENT_TYPES
        assert "REASONING_MESSAGE_CONTENT" in self.query.AGUI_EVENT_TYPES
        assert "REASONING_END" in self.query.AGUI_EVENT_TYPES

    def test_contains_all_state_events(self):
        """AGUI_EVENT_TYPES includes all state events."""
        assert "STATE_SNAPSHOT" in self.query.AGUI_EVENT_TYPES
        assert "STATE_DELTA" in self.query.AGUI_EVENT_TYPES

    def test_does_not_contain_legacy_types(self):
        """AGUI_EVENT_TYPES does NOT include legacy message types."""
        assert "chunk" not in self.query.AGUI_EVENT_TYPES
        assert "progress" not in self.query.AGUI_EVENT_TYPES
        assert "complete" not in self.query.AGUI_EVENT_TYPES
        assert "error" not in self.query.AGUI_EVENT_TYPES
        assert "progress_step" not in self.query.AGUI_EVENT_TYPES


# ─── Test send_message_safe ──────────────────────────────────────────────────


class TestSendMessageSafe:
    """Tests for the safe message sending function."""

    def setup_method(self):
        env_vars = {
            'CONNECTIONS_TABLE_NAME': 'test-connections',
            'AWS_BEDROCK_REGION': 'us-east-1',
            'AGENT_ARN': 'arn:aws:bedrock:us-east-1:123456789:agent/test-agent',
        }
        with patch.dict(os.environ, env_vars):
            with patch('boto3.resource') as mock_resource, patch('boto3.client'):
                if 'query' in sys.modules:
                    del sys.modules['query']
                import query
                self.query = query
                self.mock_table = mock_resource.return_value.Table.return_value

    def test_returns_true_on_success(self):
        """Returns True when message is sent successfully."""
        mock_apigw = MagicMock()
        result = self.query.send_message_safe(mock_apigw, "conn-1", {"type": "test"})
        assert result is True
        mock_apigw.post_to_connection.assert_called_once()

    def test_returns_false_on_gone_exception(self):
        """Returns False when client is disconnected (GoneException)."""
        mock_apigw = MagicMock()
        # Create a real exception class and assign it to the mock's exceptions
        GoneException = type('GoneException', (Exception,), {})
        mock_apigw.exceptions.GoneException = GoneException
        mock_apigw.post_to_connection.side_effect = GoneException("Gone")
        result = self.query.send_message_safe(mock_apigw, "conn-1", {"type": "test"})
        assert result is False

    def test_returns_true_on_other_exception(self):
        """Returns True on transient failures (to continue processing)."""
        mock_apigw = MagicMock()
        mock_apigw.exceptions.GoneException = type('GoneException', (Exception,), {})
        mock_apigw.post_to_connection.side_effect = RuntimeError("Network error")
        result = self.query.send_message_safe(mock_apigw, "conn-1", {"type": "test"})
        assert result is True

    def test_serializes_message_as_json(self):
        """Message is serialized as JSON bytes."""
        mock_apigw = MagicMock()
        mock_apigw.exceptions.GoneException = type('GoneException', (Exception,), {})
        message = {"type": "RUN_STARTED", "run_id": "r1", "timestamp": 123}
        self.query.send_message_safe(mock_apigw, "conn-1", message)

        call_kwargs = mock_apigw.post_to_connection.call_args[1]
        assert call_kwargs['ConnectionId'] == "conn-1"
        sent_data = json.loads(call_kwargs['Data'].decode('utf-8'))
        assert sent_data == message


# ─── Test Lambda Handler: AG-UI Event Forwarding ─────────────────────────────


class _LambdaHandlerTestBase:
    """
    Base class for lambda_handler tests.

    Provides common setup: mocked environment, boto3 clients, and helper
    methods for constructing API Gateway WebSocket events and streaming
    responses.
    """

    ENV_VARS = {
        'CONNECTIONS_TABLE_NAME': 'test-connections',
        'AWS_BEDROCK_REGION': 'us-east-1',
        'AGENT_ARN': 'arn:aws:bedrock:us-east-1:123456789:agent/test-agent',
    }

    def setup_method(self):
        """Set up mocks and import query module fresh."""
        self._patches = []

        env_patch = patch.dict(os.environ, self.ENV_VARS)
        env_patch.start()
        self._patches.append(env_patch)

        self.mock_boto3_resource = patch('boto3.resource').start()
        self._patches.append(patch('boto3.resource'))

        self.mock_boto3_client = patch('boto3.client').start()
        self._patches.append(patch('boto3.client'))

        # Mock DynamoDB table
        self.mock_table = MagicMock()
        self.mock_boto3_resource.return_value.Table.return_value = self.mock_table
        self.mock_table.get_item.return_value = {'Item': {}}

        # Remove cached module to get fresh import
        if 'query' in sys.modules:
            del sys.modules['query']
        import query
        self.query = query

        # Mock the module-level table reference
        self.query.table = self.mock_table

        # Mock API Gateway Management client
        self.mock_apigw = MagicMock()
        GoneException = type('GoneException', (Exception,), {})
        self.mock_apigw.exceptions.GoneException = GoneException
        self.GoneException = GoneException

        # Patch boto3.client to return our mock for apigatewaymanagementapi
        def client_factory(service_name, **kwargs):
            if service_name == 'apigatewaymanagementapi':
                return self.mock_apigw
            if service_name == 'bedrock-agentcore':
                return self.mock_agentcore
            return MagicMock()

        self.mock_agentcore = MagicMock()
        self.mock_boto3_client.side_effect = client_factory

        # Patch module-level agentcore_client
        self.query.agentcore_client = self.mock_agentcore

    def teardown_method(self):
        """Stop all patches."""
        patch.stopall()
        if 'query' in sys.modules:
            del sys.modules['query']

    def _make_ws_event(self, query_text="What is the dosage?", patient_id="patient-123",
                       session_id="session-abc"):
        """Create a mock API Gateway WebSocket event."""
        return {
            'requestContext': {
                'connectionId': 'conn-test-001',
                'domainName': 'ws.example.com',
                'stage': 'prod',
            },
            'body': json.dumps({
                'query': query_text,
                'patientId': patient_id,
                'sessionId': session_id,
            }),
        }

    def _make_streaming_response(self, lines: list[str], content_type='text/event-stream'):
        """
        Create a mock AgentCore streaming response.

        Args:
            lines: List of strings to yield as stream lines (will be encoded to bytes).
            content_type: Response content type.

        Returns:
            dict mimicking the AgentCore invoke_agent_runtime response.
        """
        class MockStream:
            def __init__(self, data_lines):
                self._lines = data_lines

            def iter_lines(self, chunk_size=None):
                for line in self._lines:
                    yield line.encode('utf-8') if isinstance(line, str) else line

        return {
            'response': MockStream(lines),
            'contentType': content_type,
        }

    def _get_sent_messages(self) -> list[dict]:
        """Extract all messages sent via post_to_connection."""
        messages = []
        for call in self.mock_apigw.post_to_connection.call_args_list:
            data = call[1]['Data'] if 'Data' in call[1] else call[0][0]
            if isinstance(data, bytes):
                data = data.decode('utf-8')
            messages.append(json.loads(data))
        return messages


class TestLambdaHandlerAguiForwarding(_LambdaHandlerTestBase):
    """
    Tests that AG-UI events are forwarded without transformation.

    Validates: Requirements 6.1, 6.2
    """

    def test_forwards_run_started_without_transformation(self):
        """AG-UI RUN_STARTED event is forwarded exactly as received."""
        agui_event = {
            "type": "RUN_STARTED",
            "run_id": "run-001",
            "thread_id": "thread-001",
            "timestamp": 1700000000000,
        }
        stream_line = json.dumps(agui_event)
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([stream_line])

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        # Find the AG-UI event in sent messages (there may also be legacy messages)
        agui_sent = [m for m in sent if m.get('type') == 'RUN_STARTED']
        assert len(agui_sent) == 1
        assert agui_sent[0] == agui_event

    def test_forwards_step_events_without_transformation(self):
        """AG-UI STEP_STARTED and STEP_FINISHED events are forwarded exactly."""
        step_started = {
            "type": "STEP_STARTED",
            "step_name": "Parse clinical query",
            "timestamp": 1700000001000,
        }
        step_finished = {
            "type": "STEP_FINISHED",
            "step_name": "Parse clinical query",
            "status": "completed",
            "timestamp": 1700000002000,
        }
        lines = [json.dumps(step_started), json.dumps(step_finished)]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        step_msgs = [m for m in sent if m.get('type') in ('STEP_STARTED', 'STEP_FINISHED')]
        assert len(step_msgs) == 2
        assert step_msgs[0] == step_started
        assert step_msgs[1] == step_finished

    def test_forwards_tool_call_events_without_transformation(self):
        """AG-UI TOOL_CALL_START and TOOL_CALL_END events are forwarded exactly."""
        tool_start = {
            "type": "TOOL_CALL_START",
            "tool_call_id": "tc-001",
            "tool_name": "retrieve_global_guidelines",
            "agent_name": "global_guidelines_agent",
            "args": {"query": "aspirin dosage"},
            "timestamp": 1700000003000,
        }
        tool_end = {
            "type": "TOOL_CALL_END",
            "tool_call_id": "tc-001",
            "tool_name": "retrieve_global_guidelines",
            "status": "completed",
            "result_summary": "Found 3 guidelines",
            "timestamp": 1700000004000,
        }
        lines = [json.dumps(tool_start), json.dumps(tool_end)]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        tool_msgs = [m for m in sent if m.get('type') in ('TOOL_CALL_START', 'TOOL_CALL_END')]
        assert len(tool_msgs) == 2
        assert tool_msgs[0] == tool_start
        assert tool_msgs[1] == tool_end

    def test_forwards_text_message_events_without_transformation(self):
        """AG-UI TEXT_MESSAGE_* events are forwarded exactly."""
        text_start = {"type": "TEXT_MESSAGE_START", "message_id": "msg-1", "timestamp": 100}
        text_content = {"type": "TEXT_MESSAGE_CONTENT", "message_id": "msg-1", "delta": "Take 500mg", "timestamp": 101}
        text_end = {"type": "TEXT_MESSAGE_END", "message_id": "msg-1", "timestamp": 102}
        lines = [json.dumps(e) for e in [text_start, text_content, text_end]]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        text_msgs = [m for m in sent if 'TEXT_MESSAGE' in m.get('type', '')]
        assert len(text_msgs) == 3
        assert text_msgs[0] == text_start
        assert text_msgs[1] == text_content
        assert text_msgs[2] == text_end

    def test_forwards_reasoning_events_without_transformation(self):
        """AG-UI REASONING_* events are forwarded exactly."""
        reasoning_start = {"type": "REASONING_START", "timestamp": 200}
        reasoning_content = {"type": "REASONING_MESSAGE_CONTENT", "message_id": "r1", "delta": "Considering...", "timestamp": 201}
        reasoning_end = {"type": "REASONING_END", "timestamp": 202}
        lines = [json.dumps(e) for e in [reasoning_start, reasoning_content, reasoning_end]]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        reasoning_msgs = [m for m in sent if 'REASONING' in m.get('type', '')]
        assert len(reasoning_msgs) == 3
        assert reasoning_msgs[0] == reasoning_start
        assert reasoning_msgs[1] == reasoning_content
        assert reasoning_msgs[2] == reasoning_end

    def test_forwards_state_snapshot_without_transformation(self):
        """AG-UI STATE_SNAPSHOT event is forwarded exactly."""
        state_event = {
            "type": "STATE_SNAPSHOT",
            "session_id": "sess-abc",
            "turn_count": 3,
            "patient_id": "patient-123",
            "timestamp": 300,
        }
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([json.dumps(state_event)])

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        state_msgs = [m for m in sent if m.get('type') == 'STATE_SNAPSHOT']
        assert len(state_msgs) == 1
        assert state_msgs[0] == state_event

    def test_strips_sse_data_prefix_before_forwarding(self):
        """SSE 'data: ' prefix is stripped before parsing AG-UI events."""
        agui_event = {"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 100}
        stream_line = f"data: {json.dumps(agui_event)}"
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([stream_line])

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') == 'RUN_STARTED']
        assert len(agui_sent) == 1
        assert agui_sent[0] == agui_event

    def test_multiple_agui_events_forwarded_in_order(self):
        """Multiple AG-UI events are forwarded in the order received."""
        events = [
            {"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1},
            {"type": "STEP_STARTED", "step_name": "Parse", "timestamp": 2},
            {"type": "STEP_FINISHED", "step_name": "Parse", "status": "completed", "timestamp": 3},
            {"type": "TEXT_MESSAGE_START", "message_id": "m1", "timestamp": 4},
            {"type": "TEXT_MESSAGE_CONTENT", "message_id": "m1", "delta": "Result", "timestamp": 5},
            {"type": "TEXT_MESSAGE_END", "message_id": "m1", "timestamp": 6},
            {"type": "RUN_FINISHED", "run_id": "r1", "thread_id": "t1", "status": "completed", "timestamp": 7},
        ]
        lines = [json.dumps(e) for e in events]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') in self.query.AGUI_EVENT_TYPES]
        # Verify order matches
        assert [m['type'] for m in agui_sent] == [e['type'] for e in events]

    def test_non_agui_json_not_forwarded_as_agui(self):
        """Non-AG-UI-only streams fall back to a synthetic AG-UI error run."""
        non_agui = {"event": {"contentBlockDelta": {"delta": {"text": "hello"}}}}
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([json.dumps(non_agui)])

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        sent_types = [m.get('type') for m in sent]
        assert sent_types == [
            'RUN_STARTED',
            'TEXT_MESSAGE_START',
            'TEXT_MESSAGE_CONTENT',
            'TEXT_MESSAGE_END',
            'RUN_FINISHED',
        ]

    def test_empty_stream_emits_fallback_bookends(self):
        """Empty AG-UI stream emits fallback lifecycle events for UI recovery."""
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([])

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        sent_types = [m.get('type') for m in sent]
        assert sent_types == [
            'RUN_STARTED',
            'TEXT_MESSAGE_START',
            'TEXT_MESSAGE_CONTENT',
            'TEXT_MESSAGE_END',
            'RUN_FINISHED',
        ]
        assert sent[-1]['status'] == 'error'
        assert 'empty event stream' in sent[-1]['error']

    def test_stored_session_id_reused_for_runtime_session(self):
        """Follow-up turns reuse the stored session ID for AgentCore runtimeSessionId."""
        self.mock_table.get_item.return_value = {
            'Item': {
                'connectionId': 'conn-test-001',
                'sessionId': 'session-continuity-001',
                'patientId': '001',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([])

        self.query.lambda_handler(
            self._make_ws_event(session_id=None), MagicMock()
        )

        call_kwargs = self.mock_agentcore.invoke_agent_runtime.call_args[1]
        payload = json.loads(call_kwargs['payload'].decode('utf-8'))
        assert call_kwargs['runtimeSessionId'] == 'session-continuity-001'
        assert payload['SessionId'] == 'session-continuity-001'


# ─── Test Lambda Handler: AgentCore Error Handling ───────────────────────────


class TestLambdaHandlerAgentCoreError(_LambdaHandlerTestBase):
    """
    Tests error handling when AgentCore invocation fails.

    Validates: Requirements 9.1
    """

    def test_client_error_emits_run_finished_error(self):
        """ClientError from AgentCore emits RUN_FINISHED with error status."""
        error_response = {
            'Error': {
                'Code': 'ServiceUnavailableException',
                'Message': 'Service is temporarily unavailable',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 500
        sent = self._get_sent_messages()
        # Should have an AG-UI RUN_FINISHED error event
        run_finished = [m for m in sent if m.get('type') == 'RUN_FINISHED']
        assert len(run_finished) == 1
        assert run_finished[0]['status'] == 'error'
        assert 'Service is temporarily unavailable' in run_finished[0]['error']

    def test_client_error_only_sends_agui_error(self):
        """ClientError sends only AG-UI RUN_FINISHED error (no legacy error message)."""
        error_response = {
            'Error': {
                'Code': 'ThrottlingException',
                'Message': 'Rate exceeded',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        # Only AG-UI RUN_FINISHED error event should be sent, no legacy 'error' type
        legacy_errors = [m for m in sent if m.get('type') == 'error']
        assert len(legacy_errors) == 0
        run_finished = [m for m in sent if m.get('type') == 'RUN_FINISHED']
        assert len(run_finished) == 1
        assert run_finished[0]['status'] == 'error'

    def test_generic_exception_emits_run_finished_error(self):
        """Generic exception emits RUN_FINISHED with error status."""
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            RuntimeError("Unexpected internal error")

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 500
        sent = self._get_sent_messages()
        run_finished = [m for m in sent if m.get('type') == 'RUN_FINISHED']
        assert len(run_finished) == 1
        assert run_finished[0]['status'] == 'error'
        assert 'Unexpected internal error' in run_finished[0]['error']

    def test_client_error_run_finished_has_valid_structure(self):
        """RUN_FINISHED error event has all required fields."""
        error_response = {
            'Error': {'Code': 'AccessDeniedException', 'Message': 'Not authorized'}
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        run_finished = [m for m in sent if m.get('type') == 'RUN_FINISHED'][0]
        # Verify required fields
        assert 'run_id' in run_finished
        assert 'thread_id' in run_finished
        assert 'status' in run_finished
        assert 'timestamp' in run_finished
        assert isinstance(run_finished['timestamp'], int)

    def test_missing_query_returns_400(self):
        """Missing query in body returns 400 with AG-UI error event."""
        event = {
            'requestContext': {
                'connectionId': 'conn-test-001',
                'domainName': 'ws.example.com',
                'stage': 'prod',
            },
            'body': json.dumps({'patientId': 'p1'}),  # No 'query' field
        }

        result = self.query.lambda_handler(event, MagicMock())

        assert result['statusCode'] == 400
        self.mock_agentcore.invoke_agent_runtime.assert_not_called()
        # Should send AG-UI RUN_FINISHED error event
        sent = self._get_sent_messages()
        run_finished = [m for m in sent if m.get('type') == 'RUN_FINISHED']
        assert len(run_finished) == 1
        assert run_finished[0]['status'] == 'error'
        assert 'No query provided' in run_finished[0]['error']


# ─── Test Lambda Handler: GoneException Handling ─────────────────────────────


class TestLambdaHandlerGoneException(_LambdaHandlerTestBase):
    """
    Tests GoneException handling (stale connection cleanup).

    Validates: Requirements 9.3
    """

    def test_gone_exception_stops_stream_processing(self):
        """GoneException during forwarding stops further stream processing."""
        events = [
            {"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1},
            {"type": "STEP_STARTED", "step_name": "Parse", "timestamp": 2},
            {"type": "STEP_FINISHED", "step_name": "Parse", "status": "completed", "timestamp": 3},
        ]
        lines = [json.dumps(e) for e in events]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        # First call succeeds, second call raises GoneException
        call_count = [0]

        def post_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise self.GoneException("Connection gone")

        self.mock_apigw.post_to_connection.side_effect = post_side_effect

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        # Handler should still return 200 (stream processed, client just disconnected)
        assert result['statusCode'] == 200
        # Should have attempted cleanup
        self.mock_table.delete_item.assert_called()

    def test_gone_exception_cleans_up_dynamodb(self):
        """GoneException triggers DynamoDB connection record deletion."""
        agui_event = {"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1}
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([json.dumps(agui_event)])

        self.mock_apigw.post_to_connection.side_effect = self.GoneException("Gone")

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        # Verify DynamoDB cleanup was attempted
        self.mock_table.delete_item.assert_called_with(Key={'connectionId': 'conn-test-001'})

    def test_gone_exception_does_not_send_further_events(self):
        """After GoneException, no further events are sent to the client."""
        events = [
            {"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1},
            {"type": "STEP_STARTED", "step_name": "Step 1", "timestamp": 2},
            {"type": "STEP_FINISHED", "step_name": "Step 1", "status": "completed", "timestamp": 3},
            {"type": "RUN_FINISHED", "run_id": "r1", "status": "completed", "timestamp": 4},
        ]
        lines = [json.dumps(e) for e in events]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        # First call raises GoneException immediately
        self.mock_apigw.post_to_connection.side_effect = self.GoneException("Gone")

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        # Only 1 call should have been attempted (the first one that failed)
        assert self.mock_apigw.post_to_connection.call_count == 1


# ─── Test Lambda Handler: JSON Parse Error Handling ──────────────────────────


class TestLambdaHandlerJsonParseErrors(_LambdaHandlerTestBase):
    """
    Tests JSON parse error handling on malformed stream lines.

    Validates: Requirements 6.1 (graceful handling of non-JSON lines)
    """

    def test_malformed_json_skipped_gracefully(self):
        """Malformed JSON lines are skipped without crashing."""
        lines = [
            "this is not json at all",
            json.dumps({"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1}),
            "{broken json: [",
            json.dumps({"type": "RUN_FINISHED", "run_id": "r1", "status": "completed", "timestamp": 2}),
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') in self.query.AGUI_EVENT_TYPES]
        # Both valid AG-UI events should have been forwarded
        assert len(agui_sent) == 2
        assert agui_sent[0]['type'] == 'RUN_STARTED'
        assert agui_sent[1]['type'] == 'RUN_FINISHED'

    def test_empty_lines_skipped(self):
        """Empty lines in the stream are skipped."""
        lines = [
            "",
            json.dumps({"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1}),
            "",
            "",
            json.dumps({"type": "RUN_FINISHED", "run_id": "r1", "status": "completed", "timestamp": 2}),
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') in self.query.AGUI_EVENT_TYPES]
        assert len(agui_sent) == 2

    def test_partial_json_does_not_crash(self):
        """Partial/truncated JSON does not crash the handler."""
        lines = [
            '{"type": "RUN_STARTED", "run_id": "r1"',  # Truncated — missing closing brace
            json.dumps({"type": "RUN_FINISHED", "run_id": "r1", "status": "completed", "timestamp": 2}),
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') in self.query.AGUI_EVENT_TYPES]
        # Only the valid RUN_FINISHED should be forwarded
        assert len(agui_sent) == 1
        assert agui_sent[0]['type'] == 'RUN_FINISHED'

    def test_binary_garbage_does_not_crash(self):
        """Binary/garbage data in stream does not crash the handler."""
        lines = [
            "\x00\x01\x02\xff",
            json.dumps({"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1}),
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200

    def test_mixed_valid_and_invalid_lines(self):
        """Handler processes valid events even when interspersed with invalid lines."""
        lines = [
            "garbage line 1",
            json.dumps({"type": "RUN_STARTED", "run_id": "r1", "thread_id": "t1", "timestamp": 1}),
            "not json {{{",
            json.dumps({"type": "STEP_STARTED", "step_name": "Parse", "timestamp": 2}),
            "another bad line",
            json.dumps({"type": "STEP_FINISHED", "step_name": "Parse", "status": "completed", "timestamp": 3}),
            '{"incomplete": true',
            json.dumps({"type": "RUN_FINISHED", "run_id": "r1", "status": "completed", "timestamp": 4}),
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') in self.query.AGUI_EVENT_TYPES]
        assert len(agui_sent) == 4
        assert [m['type'] for m in agui_sent] == [
            'RUN_STARTED', 'STEP_STARTED', 'STEP_FINISHED', 'RUN_FINISHED'
        ]

    def test_sse_prefix_on_malformed_json_handled(self):
        """SSE prefix on malformed JSON is handled gracefully."""
        lines = [
            "data: not valid json",
            f"data: {json.dumps({'type': 'RUN_STARTED', 'run_id': 'r1', 'thread_id': 't1', 'timestamp': 1})}",
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200
        sent = self._get_sent_messages()
        agui_sent = [m for m in sent if m.get('type') == 'RUN_STARTED']
        assert len(agui_sent) == 1
