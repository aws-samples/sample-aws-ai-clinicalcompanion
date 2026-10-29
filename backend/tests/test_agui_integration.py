"""
Integration tests for end-to-end AG-UI flow.

Tests the complete pipeline: Mock AgentCore response → Lambda handler →
verify WebSocket messages match AG-UI format.

Feature: agui-protocol-integration
Requirements: 6.1, 6.2, 7.1, 9.1, 10.1
"""

import json
import os
import sys
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Add lambda directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda', 'websocket'))

# Legacy message types that must NOT appear after migration
LEGACY_MESSAGE_TYPES = {"chunk", "progress", "complete", "progress_step", "error"}


class _IntegrationTestBase:
    """
    Base class for AG-UI integration tests.

    Sets up the Lambda handler with mocked AWS services and provides
    helpers for constructing streaming responses and inspecting sent messages.
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
        self.mock_boto3_client = patch('boto3.client').start()

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

        # Mock AgentCore client
        self.mock_agentcore = MagicMock()

        def client_factory(service_name, **kwargs):
            if service_name == 'apigatewaymanagementapi':
                return self.mock_apigw
            if service_name == 'bedrock-agentcore':
                return self.mock_agentcore
            return MagicMock()

        self.mock_boto3_client.side_effect = client_factory
        self.query.agentcore_client = self.mock_agentcore

    def teardown_method(self):
        """Stop all patches."""
        patch.stopall()
        if 'query' in sys.modules:
            del sys.modules['query']

    def _make_ws_event(self, query_text="What is the recommended dosage?",
                       patient_id="patient-123", session_id="session-abc"):
        """Create a mock API Gateway WebSocket event."""
        return {
            'requestContext': {
                'connectionId': 'conn-integration-001',
                'domainName': 'ws.example.com',
                'stage': 'prod',
            },
            'body': json.dumps({
                'query': query_text,
                'patientId': patient_id,
                'sessionId': session_id,
            }),
        }

    def _make_streaming_response(self, lines: list[str],
                                 content_type='text/event-stream'):
        """
        Create a mock AgentCore streaming response.

        Args:
            lines: List of strings to yield as stream lines.
            content_type: Response content type.
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

    def _make_full_agui_stream(self, run_id="run-int-001",
                               thread_id="thread-int-001",
                               include_state_snapshot=False,
                               session_id="session-abc",
                               turn_count=0):
        """
        Create a complete AG-UI event stream simulating a full clinical query run.

        Returns list of JSON strings representing AG-UI events as they would
        come from AgentCore.
        """
        ts = int(time.time() * 1000)
        events = []

        # RUN_STARTED
        events.append({
            "type": "RUN_STARTED",
            "run_id": run_id,
            "thread_id": thread_id,
            "timestamp": ts,
        })

        # STATE_SNAPSHOT (emitted when session context exists)
        if include_state_snapshot:
            events.append({
                "type": "STATE_SNAPSHOT",
                "snapshot": {
                    "session_id": session_id,
                    "turn_count": turn_count,
                    "patient_id": "patient-123",
                },
                "timestamp": ts + 1,
            })

        # STEP_STARTED - Parse clinical query
        events.append({
            "type": "STEP_STARTED",
            "step_name": "Parse clinical query",
            "timestamp": ts + 10,
        })

        # TOOL_CALL_START
        events.append({
            "type": "TOOL_CALL_START",
            "tool_call_id": "tc-int-001",
            "tool_call_name": "retrieve_global_guidelines",
            "kb_name": "Global Clinical Guidelines (ACR, ESUR, NICE, WHO)",
            "timestamp": ts + 20,
        })

        # TOOL_CALL_END
        events.append({
            "type": "TOOL_CALL_END",
            "tool_call_id": "tc-int-001",
            "tool_call_name": "retrieve_global_guidelines",
            "status": "completed",
            "timestamp": ts + 30,
        })

        # STEP_FINISHED
        events.append({
            "type": "STEP_FINISHED",
            "step_name": "Parse clinical query",
            "status": "completed",
            "timestamp": ts + 40,
        })

        # TEXT_MESSAGE_START
        msg_id = str(uuid.uuid4())
        events.append({
            "type": "TEXT_MESSAGE_START",
            "message_id": msg_id,
            "role": "assistant",
            "timestamp": ts + 50,
        })

        # TEXT_MESSAGE_CONTENT
        events.append({
            "type": "TEXT_MESSAGE_CONTENT",
            "message_id": msg_id,
            "delta": "Based on the clinical guidelines, ",
            "timestamp": ts + 60,
        })
        events.append({
            "type": "TEXT_MESSAGE_CONTENT",
            "message_id": msg_id,
            "delta": "the recommended dosage is 500mg twice daily.",
            "timestamp": ts + 70,
        })

        # TEXT_MESSAGE_END
        events.append({
            "type": "TEXT_MESSAGE_END",
            "message_id": msg_id,
            "timestamp": ts + 80,
        })

        # RUN_FINISHED
        events.append({
            "type": "RUN_FINISHED",
            "run_id": run_id,
            "thread_id": thread_id,
            "status": "completed",
            "timestamp": ts + 90,
        })

        return [json.dumps(e) for e in events]


# ─── Integration Test 1: End-to-End AG-UI Flow ──────────────────────────────


class TestEndToEndAguiFlow(_IntegrationTestBase):
    """
    Integration test: Mock AgentCore response → Lambda handler →
    verify WebSocket messages match AG-UI format.

    Validates: Requirements 6.1, 6.2
    """

    def test_full_run_produces_correct_agui_sequence(self):
        """
        A complete AgentCore stream with AG-UI events flows through the
        Lambda handler and arrives at the WebSocket client in AG-UI format
        with correct event types and ordering.
        """
        run_id = "run-e2e-001"
        stream_lines = self._make_full_agui_stream(
            run_id=run_id,
            include_state_snapshot=True,
            turn_count=1,
        )
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 200

        sent = self._get_sent_messages()
        assert len(sent) > 0

        # Verify first event is RUN_STARTED
        assert sent[0]['type'] == 'RUN_STARTED'
        assert sent[0]['run_id'] == run_id

        # Verify last event is RUN_FINISHED
        assert sent[-1]['type'] == 'RUN_FINISHED'
        assert sent[-1]['status'] == 'completed'

        # Verify all events have 'type' field matching AG-UI types
        for msg in sent:
            assert 'type' in msg
            assert msg['type'] in self.query.AGUI_EVENT_TYPES

    def test_event_ordering_preserved_end_to_end(self):
        """
        Events arrive at the WebSocket client in the same order they
        were emitted by AgentCore.
        """
        stream_lines = self._make_full_agui_stream(run_id="run-order-001")
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        sent_types = [m['type'] for m in sent]

        # Expected order from _make_full_agui_stream
        expected_types = [
            "RUN_STARTED",
            "STEP_STARTED",
            "TOOL_CALL_START",
            "TOOL_CALL_END",
            "STEP_FINISHED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]
        assert sent_types == expected_types

    def test_all_event_fields_preserved_through_pipeline(self):
        """
        AG-UI event fields are preserved without transformation through
        the Lambda handler pipeline.
        """
        run_id = "run-fields-001"
        ts = 1700000000000
        agui_events = [
            {
                "type": "RUN_STARTED",
                "run_id": run_id,
                "thread_id": "thread-f1",
                "timestamp": ts,
            },
            {
                "type": "TOOL_CALL_START",
                "tool_call_id": "tc-f1",
                "tool_call_name": "retrieve_patient_context",
                "kb_name": "Patient EHR",
                "timestamp": ts + 10,
            },
            {
                "type": "TOOL_CALL_END",
                "tool_call_id": "tc-f1",
                "tool_call_name": "retrieve_patient_context",
                "status": "completed",
                "timestamp": ts + 20,
            },
            {
                "type": "RUN_FINISHED",
                "run_id": run_id,
                "thread_id": "thread-f1",
                "status": "completed",
                "timestamp": ts + 30,
            },
        ]
        stream_lines = [json.dumps(e) for e in agui_events]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        # Each sent message should exactly match the original event
        assert len(sent) == len(agui_events)
        for original, forwarded in zip(agui_events, sent):
            assert forwarded == original

    def test_sse_prefix_stripped_in_pipeline(self):
        """
        SSE 'data: ' prefix is correctly stripped before AG-UI event
        detection and forwarding.
        """
        agui_event = {
            "type": "RUN_STARTED",
            "run_id": "run-sse-001",
            "thread_id": "t-sse",
            "timestamp": 1700000000000,
        }
        # Simulate SSE-formatted stream line
        stream_line = f"data: {json.dumps(agui_event)}"
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response([stream_line])

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        assert len(sent) == 1
        assert sent[0] == agui_event

    def test_text_message_content_deltas_forwarded_intact(self):
        """
        TEXT_MESSAGE_CONTENT deltas are forwarded without modification,
        preserving the streaming text chunks.
        """
        msg_id = "msg-delta-001"
        ts = 1700000000000
        events = [
            {"type": "TEXT_MESSAGE_START", "message_id": msg_id, "role": "assistant", "timestamp": ts},
            {"type": "TEXT_MESSAGE_CONTENT", "message_id": msg_id, "delta": "First chunk. ", "timestamp": ts + 1},
            {"type": "TEXT_MESSAGE_CONTENT", "message_id": msg_id, "delta": "Second chunk.", "timestamp": ts + 2},
            {"type": "TEXT_MESSAGE_END", "message_id": msg_id, "timestamp": ts + 3},
        ]
        stream_lines = [json.dumps(e) for e in events]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        content_msgs = [m for m in sent if m['type'] == 'TEXT_MESSAGE_CONTENT']
        assert len(content_msgs) == 2
        assert content_msgs[0]['delta'] == "First chunk. "
        assert content_msgs[1]['delta'] == "Second chunk."


# ─── Integration Test 2: Session State Continuity ────────────────────────────


class TestSessionStateContinuity(_IntegrationTestBase):
    """
    Integration test: Two sequential runs with same session_id,
    verify STATE_SNAPSHOT on second run.

    Validates: Requirements 7.1
    """

    def test_state_snapshot_emitted_on_second_run(self):
        """
        When a second run starts within the same session, the stream
        includes a STATE_SNAPSHOT event with session context.
        """
        session_id = "session-continuity-001"

        # First run — no STATE_SNAPSHOT (first interaction, turn_count=0)
        first_run_lines = self._make_full_agui_stream(
            run_id="run-first",
            thread_id="thread-first",
            include_state_snapshot=False,
        )
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(first_run_lines)

        result1 = self.query.lambda_handler(
            self._make_ws_event(session_id=session_id), MagicMock()
        )
        assert result1['statusCode'] == 200

        first_sent = self._get_sent_messages()
        first_types = [m['type'] for m in first_sent]
        assert 'STATE_SNAPSHOT' not in first_types

        # Reset sent messages for second run
        self.mock_apigw.post_to_connection.reset_mock()

        # Second run — includes STATE_SNAPSHOT with turn_count > 0
        second_run_lines = self._make_full_agui_stream(
            run_id="run-second",
            thread_id="thread-second",
            include_state_snapshot=True,
            session_id=session_id,
            turn_count=1,
        )
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(second_run_lines)

        # Simulate DynamoDB returning session from first run
        self.mock_table.get_item.return_value = {
            'Item': {
                'connectionId': 'conn-integration-001',
                'sessionId': session_id,
                'patientId': 'patient-123',
            }
        }

        result2 = self.query.lambda_handler(
            self._make_ws_event(session_id=session_id), MagicMock()
        )
        assert result2['statusCode'] == 200

        second_sent = self._get_sent_messages()
        second_types = [m['type'] for m in second_sent]

        # STATE_SNAPSHOT should be present in second run
        assert 'STATE_SNAPSHOT' in second_types

        # STATE_SNAPSHOT should appear after RUN_STARTED
        run_started_idx = second_types.index('RUN_STARTED')
        state_snapshot_idx = second_types.index('STATE_SNAPSHOT')
        assert state_snapshot_idx == run_started_idx + 1

    def test_state_snapshot_contains_session_context(self):
        """
        STATE_SNAPSHOT event contains session_id and turn_count
        reflecting the session history.
        """
        session_id = "session-ctx-001"
        turn_count = 3

        stream_lines = self._make_full_agui_stream(
            run_id="run-ctx",
            include_state_snapshot=True,
            session_id=session_id,
            turn_count=turn_count,
        )
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(
            self._make_ws_event(session_id=session_id), MagicMock()
        )

        sent = self._get_sent_messages()
        snapshots = [m for m in sent if m['type'] == 'STATE_SNAPSHOT']
        assert len(snapshots) == 1

        snapshot = snapshots[0]
        assert snapshot['snapshot']['session_id'] == session_id
        assert snapshot['snapshot']['turn_count'] == turn_count
        assert snapshot['snapshot']['patient_id'] == 'patient-123'

    def test_session_id_passed_to_agentcore_payload(self):
        """
        The session_id from the frontend query is passed through to
        the AgentCore invocation payload.
        """
        session_id = "session-passthrough-001"
        stream_lines = self._make_full_agui_stream(run_id="run-pass")
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(
            self._make_ws_event(session_id=session_id), MagicMock()
        )

        # Verify AgentCore was called with the session_id in payload
        call_kwargs = self.mock_agentcore.invoke_agent_runtime.call_args[1]
        payload = json.loads(call_kwargs['payload'].decode('utf-8'))
        assert payload['SessionId'] == session_id


# ─── Integration Test 3: AgentCore Failure → RUN_FINISHED Error ──────────────


class TestAgentCoreFailureErrorPropagation(_IntegrationTestBase):
    """
    Integration test: Simulate AgentCore failure, verify frontend
    receives RUN_FINISHED with error status.

    Validates: Requirements 9.1
    """

    def test_client_error_produces_run_finished_error(self):
        """
        When AgentCore raises a ClientError, the Lambda handler emits
        a RUN_FINISHED event with status "error" to the frontend.
        """
        error_response = {
            'Error': {
                'Code': 'ServiceUnavailableException',
                'Message': 'AgentCore service is temporarily unavailable',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 500

        sent = self._get_sent_messages()
        assert len(sent) == 1

        error_event = sent[0]
        assert error_event['type'] == 'RUN_FINISHED'
        assert error_event['status'] == 'error'
        assert 'AgentCore service is temporarily unavailable' in error_event['error']

    def test_error_event_has_valid_agui_structure(self):
        """
        The error event emitted on AgentCore failure has all required
        AG-UI RUN_FINISHED fields.
        """
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
        error_event = sent[0]

        # Verify all required fields for RUN_FINISHED
        assert 'type' in error_event
        assert 'run_id' in error_event
        assert 'thread_id' in error_event
        assert 'status' in error_event
        assert 'timestamp' in error_event
        assert isinstance(error_event['timestamp'], int)
        assert error_event['type'] == 'RUN_FINISHED'

    def test_error_event_is_recognized_as_agui(self):
        """
        The error event is a valid AG-UI event (recognized by is_agui_event).
        """
        error_response = {
            'Error': {
                'Code': 'InternalServerError',
                'Message': 'Internal error',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        assert len(sent) == 1
        assert self.query.is_agui_event(sent[0]) is True

    def test_access_denied_error_propagates_message(self):
        """
        AccessDeniedException error message is included in the
        RUN_FINISHED error event for debugging.
        """
        error_response = {
            'Error': {
                'Code': 'AccessDeniedException',
                'Message': 'User is not authorized to invoke agent',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        error_event = sent[0]
        assert 'not authorized' in error_event['error'].lower()

    def test_generic_exception_produces_run_finished_error(self):
        """
        A generic (non-ClientError) exception also produces a
        RUN_FINISHED error event.
        """
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            RuntimeError("Unexpected connection reset")

        result = self.query.lambda_handler(self._make_ws_event(), MagicMock())

        assert result['statusCode'] == 500
        sent = self._get_sent_messages()
        assert len(sent) == 1
        assert sent[0]['type'] == 'RUN_FINISHED'
        assert sent[0]['status'] == 'error'
        assert 'Unexpected connection reset' in sent[0]['error']


# ─── Integration Test 4: No Legacy Message Types After Migration ─────────────


class TestNoLegacyMessagesAfterMigration(_IntegrationTestBase):
    """
    Integration test: Verify no legacy message types (chunk, progress,
    complete) appear in output after migration.

    Validates: Requirements 10.1
    """

    def test_full_run_contains_no_legacy_types(self):
        """
        A complete AG-UI run produces zero legacy message types in the
        WebSocket output.
        """
        stream_lines = self._make_full_agui_stream(
            run_id="run-legacy-check",
            include_state_snapshot=True,
            turn_count=2,
        )
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        sent_types = {m.get('type') for m in sent}

        # No legacy types should be present
        legacy_found = sent_types & LEGACY_MESSAGE_TYPES
        assert legacy_found == set(), (
            f"Legacy message types found in output: {legacy_found}"
        )

    def test_legacy_json_in_stream_not_forwarded(self):
        """
        If the AgentCore stream contains legacy-format JSON messages
        (e.g., {type: "chunk"}), they are NOT forwarded to the client.
        """
        legacy_messages = [
            json.dumps({"type": "chunk", "data": "some text", "chunkNumber": 1}),
            json.dumps({"type": "progress", "step": "parsing", "percent": 50}),
            json.dumps({"type": "complete", "sessionId": "s1"}),
        ]
        # Mix legacy with one valid AG-UI event
        agui_event = json.dumps({
            "type": "RUN_STARTED",
            "run_id": "run-mixed",
            "thread_id": "t-mixed",
            "timestamp": 1700000000000,
        })
        stream_lines = legacy_messages + [agui_event]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        sent_types = {m.get('type') for m in sent}

        # Only AG-UI events should be forwarded
        assert 'chunk' not in sent_types
        assert 'progress' not in sent_types
        assert 'complete' not in sent_types
        assert 'RUN_STARTED' in sent_types

    def test_only_agui_event_types_in_output(self):
        """
        Every message sent to the WebSocket client has a type that
        belongs to the AGUI_EVENT_TYPES set.
        """
        stream_lines = self._make_full_agui_stream(run_id="run-types-check")
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        for msg in sent:
            assert msg.get('type') in self.query.AGUI_EVENT_TYPES, (
                f"Non-AG-UI message type found: {msg.get('type')}"
            )

    def test_non_json_stream_lines_not_forwarded(self):
        """
        Non-JSON lines in the stream (e.g., raw text, empty lines)
        are silently skipped and not forwarded.
        """
        stream_lines = [
            "This is raw text, not JSON",
            "",
            "<!--PROGRESS:step1-->",  # Legacy progress marker
            json.dumps({
                "type": "RUN_STARTED",
                "run_id": "run-nonjson",
                "thread_id": "t-nonjson",
                "timestamp": 1700000000000,
            }),
        ]
        self.mock_agentcore.invoke_agent_runtime.return_value = \
            self._make_streaming_response(stream_lines)

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        # Only the valid AG-UI event should be forwarded
        assert len(sent) == 1
        assert sent[0]['type'] == 'RUN_STARTED'

    def test_error_path_produces_no_legacy_types(self):
        """
        Even on error, the Lambda handler emits only AG-UI events
        (no legacy {type: "error"} messages).
        """
        error_response = {
            'Error': {
                'Code': 'ServiceUnavailableException',
                'Message': 'Service down',
            }
        }
        self.mock_agentcore.invoke_agent_runtime.side_effect = \
            ClientError(error_response, 'InvokeAgentRuntime')

        self.query.lambda_handler(self._make_ws_event(), MagicMock())

        sent = self._get_sent_messages()
        sent_types = {m.get('type') for m in sent}

        # No legacy error type
        assert 'error' not in sent_types
        # Should use AG-UI RUN_FINISHED with error status instead
        assert 'RUN_FINISHED' in sent_types
