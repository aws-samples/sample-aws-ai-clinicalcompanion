"""
AG-UI Event Emitter for ClinicalCompass

Wraps the strands-agents async stream and translates stream events into
AG-UI protocol events. This is the Event Emitter component described in
the AG-UI Protocol Integration design.

The emitter intercepts supervisor_agent.stream_async() output and produces
standardized AG-UI events that flow through the Lambda Event Router to the
frontend Event Consumer.
"""

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from ag_ui.core import (
    EventType,
    RunStartedEvent,
    RunFinishedEvent,
    RunErrorEvent,
    StepStartedEvent,
    StepFinishedEvent,
    TextMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    ToolCallStartEvent,
    ToolCallEndEvent,
    StateSnapshotEvent,
    StateDeltaEvent,
    ReasoningStartEvent,
    ReasoningMessageContentEvent,
    ReasoningEndEvent,
)

logger = logging.getLogger(__name__)

# Tool name to human-readable step name mappings
TOOL_STEP_MAPPINGS: dict[str, str] = {
    "parse_clinical_query": "Parse clinical query",
    "coordinate_agents": "Coordinate specialized agents",
    "retrieve_patient_context": "Retrieve patient context",
    "query_patient_ehr": "Retrieve patient context",
    "retrieve_global_guidelines": "Retrieve global guidelines",
    "retrieve_local_regulatory": "Retrieve local regulatory protocols",
    "detect_conflicts": "Detect guideline conflicts",
    "apply_precedence_rules": "Apply precedence rules",
    "generate_reasoning_narrative": "Generate reasoning narrative",
    "synthesize_recommendation": "Synthesize final recommendation",
    "generate_safety_alerts": "Generate safety alerts",
    "search_alternatives": "Search alternatives",
}

# Tools that are knowledge base retrievals
KB_RETRIEVAL_PATTERNS: set[str] = {
    "retrieve_patient_context",
    "retrieve_global_guidelines",
    "retrieve_local_regulatory",
    "query_patient_ehr",
}

# KB name mappings for enrichment
KB_NAME_MAPPINGS: dict[str, str] = {
    "retrieve_patient_context": "Patient EHR",
    "query_patient_ehr": "Patient EHR",
    "retrieve_global_guidelines": "Global Clinical Guidelines (ACR, ESUR, NICE, WHO)",
    "retrieve_local_regulatory": "Local Regulatory Protocols (HAS France)",
}


def _timestamp_ms() -> int:
    """Return current time as Unix milliseconds."""
    return int(time.time() * 1000)


class AGUIEventEmitter:
    """
    Wraps a strands-agents async stream and yields AG-UI event dicts.

    The emitter translates raw strands stream events (contentBlockStart,
    contentBlockDelta, contentBlockStop, messageStart, messageStop) into
    AG-UI protocol events (RUN_STARTED, STEP_STARTED, TOOL_CALL_START,
    TEXT_MESSAGE_CONTENT, REASONING_MESSAGE_CONTENT, etc.).

    Events are yielded as JSON-serializable dicts ready for WebSocket
    transmission through the Lambda Event Router.
    """

    def __init__(self, run_id: str, session_id: str, thread_id: str):
        """
        Initialize the AG-UI Event Emitter.

        Args:
            run_id: Unique identifier for this run (UUID).
            session_id: Session identifier for multi-turn context.
            thread_id: Thread identifier (typically same as session_id).
        """
        self.run_id = run_id
        self.session_id = session_id
        self.thread_id = thread_id

        # Internal state tracking
        self._step_counter = 0
        self._current_step: str | None = None
        self._current_tool_call_id: str | None = None
        self._current_tool_name: str | None = None
        self._in_text_message = False
        self._in_reasoning = False
        self._synthesize_result: dict | None = None  # Captures citations from synthesize_recommendation
        self._text_message_id: str | None = None
        self._reasoning_message_id: str | None = None
        self._tool_args_buffer: str = ""

    async def wrap_stream(
        self, stream: AsyncIterator, session_context: dict[str, Any]
    ) -> AsyncIterator[dict]:
        """
        Async generator that consumes strands stream events and yields
        AG-UI event dicts.

        Args:
            stream: The async iterator from supervisor_agent.stream_async().
            session_context: Session context dict for STATE_SNAPSHOT emission.

        Yields:
            dict: AG-UI event dicts ready for JSON serialization.
        """
        # Emit RUN_STARTED as the first event
        yield self._make_run_started()

        # Emit STATE_SNAPSHOT if session context is non-empty
        if session_context:
            yield self._make_state_snapshot(session_context)

        try:
            async for event in stream:
                try:
                    for agui_event in self._translate_event(event):
                        yield agui_event
                except Exception as e:
                    logger.warning(
                        f"Error translating stream event: {e}. Skipping."
                    )
                    continue

            # Close any open blocks before finishing
            for evt in self._close_open_blocks():
                yield evt

            # Read synthesis result from supervisor agent (citations, guidelines, safety checks)
            try:
                from agents.supervisor_agent import get_last_synthesis_result
                self._synthesize_result = get_last_synthesis_result()
                if self._synthesize_result:
                    logger.info(f"[AGUIEmitter] Read synthesis result: {len(self._synthesize_result.get('citations', []))} citations")
                else:
                    logger.warning("[AGUIEmitter] get_last_synthesis_result() returned None — citations will not be emitted")
            except Exception as e:
                logger.warning(f"Could not read synthesis result: {e}")
                self._synthesize_result = None

            # Emit final STATE_SNAPSHOT with citations, guidelines_used, safety_checks
            # from the synthesize_recommendation tool result
            try:
                if self._synthesize_result:
                    final_snapshot = {
                        "type": EventType.STATE_SNAPSHOT.value,
                        "snapshot": {
                            "session_id": self.session_id,
                        },
                        "citations": self._synthesize_result.get("citations", []),
                        "guidelines_used": self._synthesize_result.get("guidelines_used", []),
                        "safety_checks": self._synthesize_result.get("safety_checks", []),
                        "timestamp": _timestamp_ms(),
                    }
                    yield final_snapshot
            except Exception as e:
                logger.warning(f"Could not emit final STATE_SNAPSHOT with citations: {e}")

            # Emit RUN_FINISHED as the last event (also includes citations)
            yield self._make_run_finished(status="completed")

        except Exception as e:
            logger.error(f"Stream exception: {e}")
            # Close any open blocks
            for evt in self._close_open_blocks():
                yield evt
            # Emit RUN_FINISHED with error status
            yield self._make_run_finished(status="error", error=str(e))

    def _translate_event(self, raw_event: Any) -> list[dict]:
        """
        Map a single strands stream event to zero or more AG-UI events.

        Strands stream events have the structure:
        - {"event": {"contentBlockStart": {"start": {"toolUse": {...}}}}}
        - {"event": {"contentBlockDelta": {"delta": {"text": "..."}}}}
        - {"event": {"contentBlockDelta": {"delta": {"toolUse": {"input": "..."}}}}}
        - {"event": {"contentBlockStop": ...}}
        - {"event": {"messageStart": ...}}
        - {"event": {"messageStop": ...}}
        - {"event": {"metadata": ...}}

        Or the event may be a plain string (text chunk from the stream).

        Args:
            raw_event: A single event from the strands stream.

        Returns:
            List of AG-UI event dicts (may be empty).
        """
        events: list[dict] = []

        # Handle string events (direct text output from agent)
        if isinstance(raw_event, str):
            events.extend(self._handle_text_chunk(raw_event))
            return events

        # Handle dict-like events
        if hasattr(raw_event, "get"):
            event_data = raw_event
        elif hasattr(raw_event, "__dict__"):
            event_data = raw_event.__dict__ if not hasattr(raw_event, "get") else raw_event
        else:
            # Try converting to string and treating as text
            text = str(raw_event)
            if text:
                events.extend(self._handle_text_chunk(text))
            return events

        # Parse strands event structure
        inner = event_data.get("event", event_data) if isinstance(event_data, dict) else {}

        if not isinstance(inner, dict):
            # Treat as text
            text = str(raw_event)
            if text:
                events.extend(self._handle_text_chunk(text))
            return events

        # Content block start (tool use or text)
        if "contentBlockStart" in inner:
            events.extend(self._handle_content_block_start(inner["contentBlockStart"]))

        # Content block delta (streaming content)
        elif "contentBlockDelta" in inner:
            events.extend(self._handle_content_block_delta(inner["contentBlockDelta"]))

        # Content block stop
        elif "contentBlockStop" in inner:
            events.extend(self._handle_content_block_stop(inner["contentBlockStop"]))

        # Message start
        elif "messageStart" in inner:
            events.extend(self._handle_message_start(inner["messageStart"]))

        # Message stop
        elif "messageStop" in inner:
            events.extend(self._handle_message_stop(inner["messageStop"]))

        # Metadata — skip
        elif "metadata" in inner:
            pass

        return events

    def _handle_content_block_start(self, block: dict) -> list[dict]:
        """Handle contentBlockStart event."""
        events: list[dict] = []
        start = block.get("start", {})

        if "toolUse" in start:
            tool_use = start["toolUse"]
            tool_name = tool_use.get("name", tool_use.get("toolName", ""))

            if not tool_name:
                tool_name = "unknown"
                logger.warning("Tool call block missing tool name, using 'unknown'")

            tool_call_id = tool_use.get("toolUseId", str(uuid.uuid4()))

            # Determine step name from tool
            step_name = TOOL_STEP_MAPPINGS.get(tool_name, tool_name.replace("_", " ").title())

            # Emit STEP_STARTED if this is a new step
            if self._current_step != step_name:
                # Close previous step if open
                if self._current_step is not None:
                    events.append(self._make_step_finished(self._current_step, status="completed"))
                    # Emit STATE_DELTA for streaming partial results (Requirement 7.6)
                    # When a step completes, emit progress so the UI can update incrementally
                    events.append(self._make_state_delta({
                        "completed_step": self._current_step,
                        "step_number": self._step_counter,
                        "total_steps_so_far": self._step_counter,
                    }))

                self._current_step = step_name
                self._step_counter += 1
                events.append(self._make_step_started(step_name))

            # Emit TOOL_CALL_START
            self._current_tool_call_id = tool_call_id
            self._current_tool_name = tool_name
            self._tool_args_buffer = ""

            tool_event = self._make_tool_call_start(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
            events.append(tool_event)

        return events

    def _handle_content_block_delta(self, block: dict) -> list[dict]:
        """Handle contentBlockDelta event."""
        events: list[dict] = []
        delta = block.get("delta", {})

        # Text delta
        if "text" in delta:
            text = delta["text"]
            if text:
                events.extend(self._handle_text_chunk(text))

        # Tool use input delta (buffered, not emitted as AG-UI event)
        elif "toolUse" in delta:
            tool_input = delta["toolUse"].get("input", "")
            self._tool_args_buffer += tool_input

        # Reasoning/thinking delta
        elif "reasoningContent" in delta:
            reasoning_text = delta["reasoningContent"].get("text", "")
            if reasoning_text:
                events.extend(self._handle_reasoning_chunk(reasoning_text))

        return events

    def _handle_content_block_stop(self, block: dict) -> list[dict]:
        """Handle contentBlockStop event."""
        events: list[dict] = []

        # Close tool call if one is active
        if self._current_tool_call_id:
            end_event = self._make_tool_call_end(
                tool_call_id=self._current_tool_call_id,
                tool_name=self._current_tool_name or "unknown",
                status="completed",
            )
            # Include tool args for trail/audit visibility
            if self._tool_args_buffer:
                try:
                    end_event["tool_args"] = self._tool_args_buffer
                except Exception:
                    pass
            events.append(end_event)
            
            # Emit STATE_DELTA for streaming partial results (Requirement 7.6)
            # When coordinate_agents completes, emit agent completion data
            # so the UI can render partial results as each sub-agent finishes
            if self._current_tool_name == "coordinate_agents":
                events.append(self._make_state_delta({
                    "agents_coordinated": True,
                    "step": "coordinate_agents",
                    "status": "completed",
                }))
            
            self._current_tool_call_id = None
            self._current_tool_name = None
            self._tool_args_buffer = ""

        return events

    def _handle_message_start(self, msg: dict) -> list[dict]:
        """Handle messageStart event — detect reasoning vs answer phase."""
        # messageStart may indicate a new message from the model
        # We use this to detect transitions but don't emit AG-UI events directly
        return []

    def _handle_message_stop(self, msg: dict) -> list[dict]:
        """Handle messageStop event — close open text/reasoning blocks."""
        events: list[dict] = []

        # Close reasoning if open
        if self._in_reasoning:
            events.extend(self._close_reasoning())

        # Close text message if open
        if self._in_text_message:
            events.extend(self._close_text_message())

        # Close current step if open
        if self._current_step:
            events.append(self._make_step_finished(self._current_step, status="completed"))
            self._current_step = None

        return events

    def _handle_text_chunk(self, text: str) -> list[dict]:
        """Handle a text chunk — emit as TEXT_MESSAGE_CONTENT."""
        events: list[dict] = []

        if not text:
            return events

        # Close reasoning if we were in it (transition to answer)
        if self._in_reasoning:
            events.extend(self._close_reasoning())

        # Start text message if not already started
        if not self._in_text_message:
            self._text_message_id = str(uuid.uuid4())
            self._in_text_message = True
            events.append(self._make_text_message_start(self._text_message_id))

        # Emit content chunk
        events.append(self._make_text_message_content(self._text_message_id, text))

        return events

    def _handle_reasoning_chunk(self, text: str) -> list[dict]:
        """Handle a reasoning/thinking chunk."""
        events: list[dict] = []

        if not text:
            return events

        # Start reasoning if not already started
        if not self._in_reasoning:
            self._reasoning_message_id = str(uuid.uuid4())
            self._in_reasoning = True
            events.append(self._make_reasoning_start(self._reasoning_message_id))

        # Emit reasoning content
        events.append(self._make_reasoning_content(self._reasoning_message_id, text))

        return events

    def _close_reasoning(self) -> list[dict]:
        """Close an open reasoning block."""
        events: list[dict] = []
        if self._in_reasoning:
            events.append(self._make_reasoning_end(self._reasoning_message_id))
            self._in_reasoning = False
            self._reasoning_message_id = None
        return events

    def _close_text_message(self) -> list[dict]:
        """Close an open text message block."""
        events: list[dict] = []
        if self._in_text_message:
            events.append(self._make_text_message_end(self._text_message_id))
            self._in_text_message = False
            self._text_message_id = None
        return events

    def _close_open_blocks(self) -> list[dict]:
        """Close all open blocks before run finishes."""
        events: list[dict] = []

        # Close tool call if open
        if self._current_tool_call_id:
            events.append(self._make_tool_call_end(
                tool_call_id=self._current_tool_call_id,
                tool_name=self._current_tool_name or "unknown",
                status="completed",
            ))
            self._current_tool_call_id = None
            self._current_tool_name = None

        # Close reasoning if open
        events.extend(self._close_reasoning())

        # Close text message if open
        events.extend(self._close_text_message())

        # Close current step if open
        if self._current_step:
            events.append(self._make_step_finished(self._current_step, status="completed"))
            self._current_step = None

        return events

    # ─── Event Factory Methods ───────────────────────────────────────────

    def _make_run_started(self) -> dict:
        """Create RUN_STARTED event dict."""
        return {
            "type": EventType.RUN_STARTED.value,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "timestamp": _timestamp_ms(),
        }

    def _make_run_finished(self, status: str = "completed", error: str | None = None) -> dict:
        """Create RUN_FINISHED event dict. Includes citations if available."""
        event = {
            "type": EventType.RUN_FINISHED.value,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "status": status,
            "timestamp": _timestamp_ms(),
        }
        if error:
            event["error"] = error
        
        # Include structured data from synthesize_recommendation if available
        if self._synthesize_result:
            if self._synthesize_result.get("citations"):
                event["citations"] = self._synthesize_result["citations"]
            if self._synthesize_result.get("guidelines_used"):
                event["guidelines_used"] = self._synthesize_result["guidelines_used"]
            if self._synthesize_result.get("safety_checks"):
                event["safety_checks"] = self._synthesize_result["safety_checks"]
        
        return event

    def _make_state_snapshot(self, session_context: dict[str, Any]) -> dict:
        """Create STATE_SNAPSHOT event dict."""
        return {
            "type": EventType.STATE_SNAPSHOT.value,
            "snapshot": {
                "session_id": self.session_id,
                "turn_count": session_context.get("query_count", 0),
                "patient_id": session_context.get("patient_id"),
            },
            "timestamp": _timestamp_ms(),
        }

    def _make_state_delta(self, delta: dict) -> dict:
        """Create STATE_DELTA event dict."""
        return {
            "type": EventType.STATE_DELTA.value,
            "delta": delta,
            "timestamp": _timestamp_ms(),
        }

    def _make_step_started(self, step_name: str) -> dict:
        """Create STEP_STARTED event dict."""
        return {
            "type": EventType.STEP_STARTED.value,
            "step_name": step_name,
            "timestamp": _timestamp_ms(),
        }

    def _make_step_finished(self, step_name: str, status: str = "completed", error: str | None = None) -> dict:
        """Create STEP_FINISHED event dict."""
        event = {
            "type": EventType.STEP_FINISHED.value,
            "step_name": step_name,
            "status": status,
            "timestamp": _timestamp_ms(),
        }
        if error:
            event["error"] = error
        return event

    def _make_tool_call_start(self, tool_call_id: str, tool_name: str) -> dict:
        """Create TOOL_CALL_START event dict."""
        event = {
            "type": EventType.TOOL_CALL_START.value,
            "tool_call_id": tool_call_id,
            "tool_call_name": tool_name,
            "timestamp": _timestamp_ms(),
        }

        # Enrich KB retrieval tool calls with KB name
        if tool_name in KB_RETRIEVAL_PATTERNS:
            kb_name = KB_NAME_MAPPINGS.get(tool_name, "")
            if kb_name:
                event["kb_name"] = kb_name

        return event

    def _make_tool_call_end(
        self, tool_call_id: str, tool_name: str, status: str = "completed", error: str | None = None
    ) -> dict:
        """Create TOOL_CALL_END event dict."""
        event = {
            "type": EventType.TOOL_CALL_END.value,
            "tool_call_id": tool_call_id,
            "tool_call_name": tool_name,
            "status": status,
            "timestamp": _timestamp_ms(),
        }
        if error:
            event["error"] = error
        return event

    def _make_text_message_start(self, message_id: str) -> dict:
        """Create TEXT_MESSAGE_START event dict."""
        return {
            "type": EventType.TEXT_MESSAGE_START.value,
            "message_id": message_id,
            "role": "assistant",
            "timestamp": _timestamp_ms(),
        }

    def _make_text_message_content(self, message_id: str, delta: str) -> dict:
        """Create TEXT_MESSAGE_CONTENT event dict."""
        return {
            "type": EventType.TEXT_MESSAGE_CONTENT.value,
            "message_id": message_id,
            "delta": delta,
            "timestamp": _timestamp_ms(),
        }

    def _make_text_message_end(self, message_id: str) -> dict:
        """Create TEXT_MESSAGE_END event dict."""
        return {
            "type": EventType.TEXT_MESSAGE_END.value,
            "message_id": message_id,
            "timestamp": _timestamp_ms(),
        }

    def _make_reasoning_start(self, message_id: str) -> dict:
        """Create REASONING_START event dict."""
        return {
            "type": EventType.REASONING_START.value,
            "message_id": message_id,
            "timestamp": _timestamp_ms(),
        }

    def _make_reasoning_content(self, message_id: str, delta: str) -> dict:
        """Create REASONING_MESSAGE_CONTENT event dict."""
        return {
            "type": EventType.REASONING_MESSAGE_CONTENT.value,
            "message_id": message_id,
            "delta": delta,
            "timestamp": _timestamp_ms(),
        }

    def _make_reasoning_end(self, message_id: str) -> dict:
        """Create REASONING_END event dict."""
        return {
            "type": EventType.REASONING_END.value,
            "message_id": message_id,
            "timestamp": _timestamp_ms(),
        }
