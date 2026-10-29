"""
Clinical Observability Module - OpenTelemetry instrumentation for clinical decision support

This module provides helper functions for adding custom OTEL spans to track clinical
decision-making events like conflict detection, precedence rule application, and
recommendation generation.

Uses AWS Distro for OpenTelemetry (ADOT) for automatic instrumentation and CloudWatch export.
"""

import os
from typing import Dict, Any, Optional
from contextlib import contextmanager

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    print("Warning: OpenTelemetry not available. Observability disabled.")


def is_observability_enabled() -> bool:
    """
    Check if observability is enabled
    
    For AgentCore-hosted agents:
    - AgentCore automatically configures OTEL when observability is enabled in console
    - Custom spans require AGENT_OBSERVABILITY_ENABLED=true environment variable
    
    Returns True if AGENT_OBSERVABILITY_ENABLED is set to 'true' AND OTEL packages are available
    """
    return os.getenv('AGENT_OBSERVABILITY_ENABLED', 'false').lower() == 'true' and OTEL_AVAILABLE


def get_tracer():
    """Get the OpenTelemetry tracer for clinical compass"""
    if not OTEL_AVAILABLE:
        return None
    return trace.get_tracer("clinical-compass", "1.0.0")


@contextmanager
def clinical_span(
    name: str,
    session_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None
):
    """
    Context manager for creating clinical decision support spans
    
    Args:
        name: Name of the span (e.g., "conflict_detection", "precedence_rule_application")
        session_id: Clinical session identifier
        patient_id: Patient identifier (anonymized)
        attributes: Additional span attributes
    
    Example:
        with clinical_span("conflict_detection", session_id="session-123") as span:
            # Detect conflicts
            span.set_attribute("conflict.count", 2)
            span.set_attribute("conflict.severity", "high")
    """
    if not is_observability_enabled():
        # No-op context manager when observability is disabled
        class NoOpSpan:
            def set_attribute(self, key, value):
                pass
            def set_status(self, status):
                pass
            def record_exception(self, exception):
                pass
        yield NoOpSpan()
        return
    
    tracer = get_tracer()
    if not tracer:
        yield NoOpSpan()
        return
    
    with tracer.start_as_current_span(name) as span:
        # Add standard clinical context attributes
        if session_id:
            span.set_attribute("session.id", session_id)
        if patient_id:
            span.set_attribute("patient.id", patient_id)
        
        # Add custom attributes
        if attributes:
            for key, value in attributes.items():
                # Convert complex types to strings
                if isinstance(value, (dict, list)):
                    import json
                    value = json.dumps(value)
                span.set_attribute(key, value)
        
        try:
            yield span
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise


def record_conflict_detection(
    session_id: str,
    conflicts: list,
    patient_id: Optional[str] = None
):
    """
    Record a conflict detection event
    
    Args:
        session_id: Clinical session identifier
        conflicts: List of detected conflicts
        patient_id: Patient identifier (anonymized)
    """
    with clinical_span(
        "clinical.conflict_detection",
        session_id=session_id,
        patient_id=patient_id,
        attributes={
            "conflict.count": len(conflicts),
            "conflict.details": conflicts
        }
    ) as span:
        for i, conflict in enumerate(conflicts):
            span.set_attribute(f"conflict.{i}.issue", conflict.get("issue", ""))
            span.set_attribute(f"conflict.{i}.severity", conflict.get("severity", ""))


def record_precedence_rule_application(
    session_id: str,
    conflict: str,
    chosen_guideline: str,
    overridden_guideline: str,
    rationale: str,
    patient_id: Optional[str] = None
):
    """
    Record a precedence rule application event
    
    Args:
        session_id: Clinical session identifier
        conflict: Description of the conflict
        chosen_guideline: Guideline that was chosen
        overridden_guideline: Guideline that was overridden
        rationale: Rationale for the decision
        patient_id: Patient identifier (anonymized)
    """
    with clinical_span(
        "clinical.precedence_rule_application",
        session_id=session_id,
        patient_id=patient_id,
        attributes={
            "precedence.conflict": conflict,
            "precedence.chosen": chosen_guideline,
            "precedence.overridden": overridden_guideline,
            "precedence.rationale": rationale
        }
    ):
        pass  # Span attributes already set


def record_recommendation_generation(
    session_id: str,
    recommendation: Dict[str, Any],
    patient_id: Optional[str] = None
):
    """
    Record a recommendation generation event
    
    Args:
        session_id: Clinical session identifier
        recommendation: Generated recommendation
        patient_id: Patient identifier (anonymized)
    """
    with clinical_span(
        "clinical.recommendation_generation",
        session_id=session_id,
        patient_id=patient_id,
        attributes={
            "recommendation.action": recommendation.get("recommended_action", ""),
            "recommendation.confidence": recommendation.get("confidence_score", 0.0),
            "recommendation.has_safety_alerts": len(recommendation.get("safety_alerts", [])) > 0,
            "recommendation.has_alternatives": len(recommendation.get("alternatives", [])) > 0,
            "recommendation.citations_count": len(recommendation.get("citations", []))
        }
    ):
        pass  # Span attributes already set


def record_agent_coordination(
    session_id: str,
    agent_name: str,
    duration_ms: int,
    success: bool,
    patient_id: Optional[str] = None
):
    """
    Record an agent coordination event
    
    Args:
        session_id: Clinical session identifier
        agent_name: Name of the agent
        duration_ms: Duration in milliseconds
        success: Whether the agent succeeded
        patient_id: Patient identifier (anonymized)
    """
    with clinical_span(
        f"clinical.agent.{agent_name}",
        session_id=session_id,
        patient_id=patient_id,
        attributes={
            "agent.name": agent_name,
            "agent.duration_ms": duration_ms,
            "agent.success": success
        }
    ) as span:
        if not success:
            span.set_status(Status(StatusCode.ERROR, f"{agent_name} failed"))


# Environment variable configuration helper
def get_otel_config() -> Dict[str, str]:
    """
    Get OpenTelemetry configuration from environment variables
    
    Returns:
        Dictionary of OTEL configuration
    """
    return {
        "AGENT_OBSERVABILITY_ENABLED": os.getenv("AGENT_OBSERVABILITY_ENABLED", "false"),
        "OTEL_PYTHON_DISTRO": os.getenv("OTEL_PYTHON_DISTRO", "aws_distro"),
        "OTEL_PYTHON_CONFIGURATOR": os.getenv("OTEL_PYTHON_CONFIGURATOR", "aws_configurator"),
        "OTEL_EXPORTER_OTLP_PROTOCOL": os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
        "OTEL_RESOURCE_ATTRIBUTES": os.getenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=clinical-compass"),
    }


def print_otel_config():
    """Print current OTEL configuration for debugging"""
    config = get_otel_config()
    print("OpenTelemetry Configuration:")
    for key, value in config.items():
        print(f"  {key}: {value}")
    print(f"  OTEL Available: {OTEL_AVAILABLE}")
    print(f"  Observability Enabled: {is_observability_enabled()}")
    
    # Debug AgentCore detection
    print(f"  AWS_EXECUTION_ENV: {os.getenv('AWS_EXECUTION_ENV', 'not set')}")
    print(f"  BEDROCK_AGENTCORE_RUNTIME: {os.getenv('BEDROCK_AGENTCORE_RUNTIME', 'not set')}")
    print(f"  In AgentCore: {os.getenv('AWS_EXECUTION_ENV', '').startswith('AWS_Bedrock_AgentCore') or 'bedrock-agentcore' in os.getenv('AWS_EXECUTION_ENV', '').lower()}")
    
    # Debug Memory ID
    print(f"  STM_MEMORY_ID: {os.getenv('STM_MEMORY_ID', 'not set')}")
