"""
WebSocket Query Handler
Processes clinical queries and streams AgentCore responses in real-time.

This module serves as the Event Router in the AG-UI protocol integration:
- Receives AG-UI events from the AgentCore streaming response
- Forwards them as JSON WebSocket messages to connected frontend clients
- Preserves event ordering from AgentCore to frontend
"""

import boto3
import os
import json
import uuid
import time
import logging
import sys
from botocore.config import Config
from botocore.exceptions import ClientError

# Add backend directory to path for imports
sys.path.insert(0, '/opt/python')  # Lambda layer path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from input_validator import validate_query, sanitize_query
from rate_limiter import check_rate_limit

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['CONNECTIONS_TABLE_NAME'])
# The agent stream can go quiet for well over a minute while sub-agents work (and longer
# on a cold start), so the 60s botocore default read timeout would cut the response off.
# 300s matches the agents' own Bedrock read timeout; the Lambda-level guard below
# (LAMBDA_TIMEOUT_THRESHOLD_S) still bounds the whole invocation.
AGENTCORE_READ_TIMEOUT_S = 300
agentcore_client = boto3.client(
    'bedrock-agentcore',
    region_name=os.environ['AWS_BEDROCK_REGION'],
    config=Config(read_timeout=AGENTCORE_READ_TIMEOUT_S, connect_timeout=10),
)
ssm_client = boto3.client('ssm', region_name=os.environ['AWS_BEDROCK_REGION'])

# AgentCore configuration
AGENT_ARN = os.environ.get('AGENT_ARN', '')
AGENT_ARN_SSM_PARAM = os.environ.get('AGENT_ARN_SSM_PARAM', '')

# Bedrock Guardrails configuration (optional — set when guardrail is deployed)
GUARDRAIL_ID = os.environ.get('BEDROCK_GUARDRAIL_ID', '')
GUARDRAIL_VERSION = os.environ.get('BEDROCK_GUARDRAIL_VERSION', '')

# Safe fallback message when Bedrock Guardrails block a response
GUARDRAIL_FALLBACK_MESSAGE = (
    "I'm unable to provide a response to that query. "
    "Please rephrase your question about clinical guidelines."
)

# Lambda timeout threshold (seconds) — emit RUN_FINISHED if approaching
LAMBDA_TIMEOUT_THRESHOLD_S = 850

# ─── AG-UI Event Types ───────────────────────────────────────────────────────
# All known AG-UI event type strings. Used to detect AG-UI events in the
# AgentCore streaming response and forward them to the frontend.
AGUI_EVENT_TYPES: set[str] = {
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
}


def get_agent_runtime_arn() -> str:
    """
    Resolve the AgentCore runtime ARN from env or SSM.

    CDK passes the SSM parameter name so the runtime can be created after stack
    deploy. Resolve lazily at invocation time instead of failing during import.
    """
    global AGENT_ARN

    if AGENT_ARN:
        return AGENT_ARN

    if not AGENT_ARN_SSM_PARAM:
        raise ValueError("AGENT_ARN and AGENT_ARN_SSM_PARAM are not configured")

    response = ssm_client.get_parameter(Name=AGENT_ARN_SSM_PARAM)
    AGENT_ARN = response['Parameter']['Value']

    if not AGENT_ARN or AGENT_ARN == 'PLACEHOLDER':
        raise ValueError(
            f"Agent runtime ARN is not ready in SSM parameter {AGENT_ARN_SSM_PARAM}"
        )

    return AGENT_ARN


def is_agui_event(data: dict) -> bool:
    """
    Check if a parsed JSON dict is an AG-UI event.

    AG-UI events are identified by having a 'type' field whose value
    matches one of the known AG-UI event type strings.

    Args:
        data: Parsed JSON dictionary.

    Returns:
        True if this is an AG-UI event, False otherwise.
    """
    return isinstance(data, dict) and data.get('type') in AGUI_EVENT_TYPES


def send_message(apigw_management, connection_id: str, message: dict):
    """
    Send message to WebSocket client.

    Args:
        apigw_management: API Gateway Management API client
        connection_id: WebSocket connection ID
        message: Message dictionary to send

    Raises:
        GoneException is handled internally (connection cleanup).
        Other post_to_connection failures are logged and re-raised.
    """
    try:
        apigw_management.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(message).encode('utf-8')
        )
    except apigw_management.exceptions.GoneException:
        # Connection closed, clean up DynamoDB record
        try:
            table.delete_item(Key={'connectionId': connection_id})
            logger.info(f"Cleaned up stale connection: {connection_id}")
        except Exception as e:
            logger.warning(f"Failed to clean up connection: {e}")
        raise  # Re-raise so caller knows client is gone
    except Exception as e:
        logger.error(f"Error sending message to {connection_id}: {e}")
        raise


def send_message_safe(apigw_management, connection_id: str, message: dict) -> bool:
    """
    Send message to WebSocket client with graceful error handling.

    Unlike send_message(), this function catches all exceptions and returns
    a boolean indicating success. Use this when processing a stream where
    a single send failure should not abort the entire run.

    Args:
        apigw_management: API Gateway Management API client
        connection_id: WebSocket connection ID
        message: Message dictionary to send

    Returns:
        True if message was sent successfully, False otherwise.
        Returns False with special handling for GoneException (client disconnected).
    """
    try:
        apigw_management.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(message).encode('utf-8')
        )
        return True
    except apigw_management.exceptions.GoneException:
        # Connection closed, clean up DynamoDB record
        try:
            table.delete_item(Key={'connectionId': connection_id})
            logger.info(f"Cleaned up stale connection (GoneException): {connection_id}")
        except Exception as e:
            logger.warning(f"Failed to clean up connection: {e}")
        return False
    except Exception as e:
        logger.error(f"Error sending message to {connection_id}: {e}")
        return True  # Return True to continue processing (transient failure)


def get_session(connection_id: str) -> dict:
    """
    Retrieve session from DynamoDB

    Args:
        connection_id: WebSocket connection ID

    Returns:
        dict: Session data or empty dict
    """
    try:
        response = table.get_item(Key={'connectionId': connection_id})
        return response.get('Item', {})
    except Exception as e:
        logger.error(f"Failed to retrieve session: {e}")
        return {}


def update_session(connection_id: str, updates: dict):
    """
    Update session in DynamoDB

    Args:
        connection_id: WebSocket connection ID
        updates: Dictionary of fields to update
    """
    try:
        update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in updates.keys())
        expr_attr_names = {f'#{k}': k for k in updates.keys()}
        expr_attr_values = {f':{k}': v for k, v in updates.items()}

        table.update_item(
            Key={'connectionId': connection_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values
        )
    except Exception as e:
        logger.error(f"Failed to update session: {e}")


def make_agui_run_finished_error(run_id: str, error_message: str) -> dict:
    """
    Create a RUN_FINISHED event with error status.

    Used when AgentCore invocation fails or Lambda timeout approaches.

    Args:
        run_id: The run ID for this invocation.
        error_message: Human-readable error description.

    Returns:
        dict: AG-UI RUN_FINISHED event with status "error".
    """
    return {
        "type": "RUN_FINISHED",
        "run_id": run_id,
        "thread_id": run_id,
        "status": "error",
        "error": error_message,
        "timestamp": int(time.time() * 1000),
    }


def make_agui_run_started(run_id: str) -> dict:
    """Create a minimal RUN_STARTED event for fallback flows."""
    return {
        "type": "RUN_STARTED",
        "run_id": run_id,
        "thread_id": run_id,
        "timestamp": int(time.time() * 1000),
    }


def emit_empty_stream_fallback(
    apigw_management,
    connection_id: str,
    run_id: str,
    error_message: str,
):
    """
    Emit a synthetic AG-UI run so the frontend can recover from empty streams.

    This preserves the bookend contract expected by the frontend and tests,
    and provides a visible message instead of leaving the UI disabled forever.
    """
    message_id = f"fallback-{run_id}"
    now = int(time.time() * 1000)
    fallback_events = [
        make_agui_run_started(run_id),
        {
            "type": "TEXT_MESSAGE_START",
            "message_id": message_id,
            "role": "assistant",
            "timestamp": now + 1,
        },
        {
            "type": "TEXT_MESSAGE_CONTENT",
            "message_id": message_id,
            "delta": (
                "I couldn't complete that follow-up because the backend returned "
                "an empty event stream. Please try the question again."
            ),
            "timestamp": now + 2,
        },
        {
            "type": "TEXT_MESSAGE_END",
            "message_id": message_id,
            "timestamp": now + 3,
        },
        make_agui_run_finished_error(run_id, error_message),
    ]

    for event in fallback_events:
        if not send_message_safe(apigw_management, connection_id, event):
            break


def _is_guardrail_blocked(parsed: dict) -> bool:
    """Check if an AG-UI event indicates a Bedrock Guardrails intervention.

    Guardrails may intervene by:
    - Adding a 'guardrail' action type in the event
    - Setting an event status to 'blocked' or 'GUARDRAIL_INTERVENED'
    - Including guardrail metadata in TEXT_MESSAGE_CONTENT events

    Args:
        parsed: A parsed AG-UI event dictionary.

    Returns:
        True if the event indicates guardrail blocking, False otherwise.
    """
    # Check for guardrail action type
    if parsed.get('action') == 'guardrail':
        return True

    # Check for blocked/intervened status
    status = str(parsed.get('status', '')).upper()
    if status in ('BLOCKED', 'GUARDRAIL_INTERVENED'):
        return True

    # Check for guardrail trace in metadata
    trace = parsed.get('trace', {})
    if isinstance(trace, dict):
        guardrail_trace = trace.get('guardrail', {})
        if guardrail_trace.get('action') in ('BLOCKED', 'INTERVENED'):
            return True

    # Check for amazonBedrockGuardrailAction in the event
    if parsed.get('amazonBedrockGuardrailAction') in ('BLOCKED', 'INTERVENED'):
        return True

    return False


# Patient index table for fetching patient context (Requirement 9.4)
PATIENT_INDEX_TABLE_NAME = os.environ.get('PATIENT_INDEX_TABLE_NAME', '')


def _fetch_patient_context(patient_id: str) -> dict | None:
    """Fetch patient record from DynamoDB patient index.

    Retrieves the full patient record so it can be included in the
    agent invocation payload. This enables the agent to have patient
    context without a separate KB retrieval step.

    Args:
        patient_id: The patient identifier to look up.

    Returns:
        Patient record dict, or None if not found or table not configured.
    """
    if not PATIENT_INDEX_TABLE_NAME:
        logger.warning("PATIENT_INDEX_TABLE_NAME not configured, skipping patient context fetch")
        return None

    try:
        patient_table = dynamodb.Table(PATIENT_INDEX_TABLE_NAME)
        response = patient_table.get_item(Key={'patient_id': patient_id})
        item = response.get('Item')
        if item:
            logger.info(f"Fetched patient context for {patient_id}")
            # Convert DynamoDB types to plain Python types for JSON serialization
            return json.loads(json.dumps(item, default=str))
        else:
            logger.warning(f"Patient {patient_id} not found in index table")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch patient context for {patient_id}: {e}")
        return None


def lambda_handler(event, context):
    """
    Handle clinical query and stream AgentCore response.

    This is the Event Router: it invokes AgentCore, parses the streaming
    response, detects AG-UI events, and forwards them as JSON WebSocket
    messages. Only AG-UI events are forwarded to clients.

    Args:
        event: API Gateway WebSocket event
        context: Lambda context

    Returns:
        dict: Status code 200 on success, 500 on error
    """
    connection_id = event['requestContext']['connectionId']
    domain_name = event['requestContext']['domainName']
    stage = event['requestContext']['stage']

    # Initialize API Gateway Management API client
    apigw_management = boto3.client('apigatewaymanagementapi',
                                   endpoint_url=f'https://{domain_name}/{stage}')

    # Generate a run_id for AG-UI error events (used if invocation fails before stream)
    run_id = str(uuid.uuid4())

    # Track whether client is still connected
    client_connected = True

    try:
        # Parse message
        body = json.loads(event['body'])
        query = body.get('query')
        patient_id = body.get('patientId')
        session_id = body.get('sessionId')
        language_code = body.get('languageCode', 'en-GB')
        selected_guidelines = body.get('selectedGuidelines')  # Requirement 3.4

        if not query:
            error_event = make_agui_run_finished_error(
                run_id=run_id,
                error_message="No query provided"
            )
            send_message_safe(apigw_management, connection_id, error_event)
            return {'statusCode': 400}

        # ─── Input Validation (Requirement 2.1, 2.2, 2.3) ────────────
        validation_result = validate_query(query, user_identity=connection_id)
        if not validation_result.is_valid:
            error_event = make_agui_run_finished_error(
                run_id=run_id,
                error_message=validation_result.error_message or "Query validation failed"
            )
            send_message_safe(apigw_management, connection_id, error_event)
            return {'statusCode': 400}

        # Sanitize the query for safe forwarding (Requirement 2.3)
        query = sanitize_query(query)

        # ─── Rate Limiting (Requirement 3.1, 3.3) ────────────────────
        # Key the limiter on the authenticated Cognito identity (captured at
        # $connect) so a client cannot reset its window by reconnecting. Fall
        # back to connectionId only if the identity was not recorded.
        session = get_session(connection_id)
        rate_limit_identity = session.get('userSub') or connection_id
        rate_result = check_rate_limit(user_id=rate_limit_identity)
        if not rate_result.allowed:
            error_event = make_agui_run_finished_error(
                run_id=run_id,
                error_message=(
                    f"Rate limit exceeded. Please wait {rate_result.retry_after} seconds "
                    "before sending another query."
                )
            )
            send_message_safe(apigw_management, connection_id, error_event)
            return {'statusCode': 429}

        logger.info(f"Processing query from {connection_id}: {query[:50]}...")

        # 'session' was already retrieved above for the rate-limit identity.
        # Use stored session ID if not provided
        if not session_id and session.get('sessionId'):
            session_id = session['sessionId']

        # Use stored patient ID if not provided
        if not patient_id and session.get('patientId'):
            patient_id = session['patientId']

        # Fetch patient record from DynamoDB for agent context (Requirement 9.4)
        patient_context = None
        if patient_id:
            patient_context = _fetch_patient_context(patient_id)

        # Prepare AgentCore payload — include session_id for AG-UI state continuity
        runtime_session_id = session_id
        if not runtime_session_id:
            if patient_id and len(patient_id) >= 33:
                runtime_session_id = patient_id
            else:
                runtime_session_id = str(uuid.uuid4())

        agent_payload = {
            'Prompt': query,
            'ReferralId': patient_id,
            'SessionId': runtime_session_id,
            'LanguageCode': language_code
        }

        # Include selected guidelines in payload (Requirement 3.4)
        if selected_guidelines:
            agent_payload['SelectedGuidelines'] = selected_guidelines

        # Include patient context in payload for agent use
        if patient_context:
            agent_payload['PatientContext'] = patient_context

        if not patient_id:
            patient_id = runtime_session_id

        logger.info(f"Invoking AgentCore with session: {runtime_session_id}")

        # Invoke AgentCore Runtime with streaming
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=get_agent_runtime_arn(),
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(agent_payload).encode('utf-8')
        )

        # Stream AG-UI events to client
        agui_event_count = 0
        content_type = response.get('contentType', '')
        invocation_start_time = time.time()
        guardrail_blocked = False

        logger.info(f"AgentCore response content type: {content_type}")

        if 'text/event-stream' in content_type:
            # Handle streaming response — forward AG-UI events only
            for line in response['response'].iter_lines(chunk_size=1024):
                # Check for Lambda timeout approaching
                elapsed = time.time() - invocation_start_time
                if elapsed > LAMBDA_TIMEOUT_THRESHOLD_S:
                    logger.warning(
                        f"Lambda timeout approaching ({elapsed:.0f}s elapsed). "
                        f"Emitting RUN_FINISHED error."
                    )
                    timeout_event = make_agui_run_finished_error(
                        run_id=run_id,
                        error_message="Processing timeout — the query took too long to complete."
                    )
                    send_message_safe(apigw_management, connection_id, timeout_event)
                    break

                if not client_connected:
                    logger.info("Client disconnected, stopping stream processing.")
                    break

                if line:
                    line_str = line.decode('utf-8')

                    # Remove SSE prefix if present
                    if line_str.startswith('data: '):
                        line_str = line_str[6:]

                    # ─── AG-UI Event Detection and Forwarding ─────────────
                    # Parse as JSON and check if it's an AG-UI event.
                    # AG-UI events are forwarded directly as WebSocket messages.
                    try:
                        parsed = json.loads(line_str)
                        if is_agui_event(parsed):
                            # ─── Guardrail Intervention Detection (Req 2.5) ───
                            if _is_guardrail_blocked(parsed):
                                guardrail_blocked = True
                                logger.warning(
                                    f"Bedrock Guardrails blocked response for "
                                    f"connection {connection_id}"
                                )
                                # Send safe fallback message instead of blocked content
                                fallback_event = {
                                    "type": "TEXT_MESSAGE_CONTENT",
                                    "run_id": run_id,
                                    "content": GUARDRAIL_FALLBACK_MESSAGE,
                                    "timestamp": int(time.time() * 1000),
                                }
                                send_message_safe(
                                    apigw_management, connection_id, fallback_event
                                )
                                # Send RUN_FINISHED to close out the run
                                finished_event = {
                                    "type": "RUN_FINISHED",
                                    "run_id": run_id,
                                    "thread_id": run_id,
                                    "status": "complete",
                                    "timestamp": int(time.time() * 1000),
                                }
                                send_message_safe(
                                    apigw_management, connection_id, finished_event
                                )
                                break

                            # Forward AG-UI event directly to frontend
                            success = send_message_safe(
                                apigw_management, connection_id, parsed
                            )
                            if not success:
                                # GoneException — client disconnected
                                client_connected = False
                                logger.info(
                                    "Client disconnected during AG-UI event forwarding."
                                )
                                break
                            agui_event_count += 1

                            # Extract run_id from RUN_STARTED for error events
                            if parsed.get('type') == 'RUN_STARTED' and 'run_id' in parsed:
                                run_id = parsed['run_id']
                            elif parsed.get('type') == 'STATE_SNAPSHOT':
                                snapshot = parsed.get('snapshot', {})
                                session_id = (
                                    snapshot.get('session_id')
                                    or parsed.get('session_id')
                                    or session_id
                                )
                    except json.JSONDecodeError:
                        # Not JSON — skip
                        pass

        if agui_event_count == 0 and client_connected:
            logger.warning(
                "AgentCore returned no AG-UI events; emitting synthetic fallback run"
            )
            emit_empty_stream_fallback(
                apigw_management,
                connection_id,
                run_id,
                "AgentCore returned an empty event stream",
            )

        logger.info(
            f"AgentCore processing complete: {agui_event_count} AG-UI events forwarded"
            f"{' (guardrail blocked)' if guardrail_blocked else ''}"
        )

        # Update session in DynamoDB
        update_session(connection_id, {
            'sessionId': session_id or runtime_session_id,
            'patientId': patient_id,
            'lastActiveAt': int(time.time())
        })

        return {'statusCode': 200}

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']

        logger.error(f"AgentCore error: {error_code} - {error_msg}")

        # Emit AG-UI RUN_FINISHED with error status
        agui_error_event = make_agui_run_finished_error(
            run_id=run_id,
            error_message=f"AgentCore error: {error_msg}"
        )
        send_message_safe(apigw_management, connection_id, agui_error_event)

        return {'statusCode': 500}

    except Exception as e:
        logger.error(f"Query handler error: {e}", exc_info=True)

        # Emit AG-UI RUN_FINISHED with error status
        agui_error_event = make_agui_run_finished_error(
            run_id=run_id,
            error_message=str(e)
        )
        send_message_safe(apigw_management, connection_id, agui_error_event)

        return {'statusCode': 500}
