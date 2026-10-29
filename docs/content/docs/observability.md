---
title: Observability
weight: 7
prev: agents
---

ClinicalCompass uses AWS Distro for OpenTelemetry (ADOT) for tracing, with custom clinical spans for healthcare-specific events.

## Architecture

```
┌─────────────────────────────────┐
│       Supervisor Agent          │
│  Custom Clinical Spans via      │
│  clinical_observability.py      │
└───────────────┬─────────────────┘
                │
                ▼
┌─────────────────────────────────┐
│  AWS Distro for OpenTelemetry   │
│  (Automatic instrumentation)    │
└───────────────┬─────────────────┘
                │
                ▼
┌─────────────────────────────────┐
│      Amazon CloudWatch          │
│  Logs • Traces • Metrics        │
└───────────────┬─────────────────┘
                │
                ▼
┌─────────────────────────────────┐
│  GenAI Observability Dashboard  │
│  Sessions • Agents • Traces     │
└─────────────────────────────────┘
```

## What Gets Instrumented

AgentCore provides automatic observability:
- All agent invocations are traced
- Tool calls logged with inputs/outputs
- Errors and exceptions captured
- Session context preserved across invocations

Custom clinical spans add healthcare-specific signals:
- **Conflict detection** - when guidelines disagree
- **Precedence rule application** - how conflicts are resolved
- **Recommendation generation** - final clinical output
- **Agent coordination** - per-agent timing and success

## Enabling Observability

### AgentCore (Production)

Observability is configured in `.bedrock_agentcore.yaml`:

```yaml
observability:
  enabled: true
```

Custom clinical spans require an additional environment variable:

```python
# In backend/app.py
import os
os.environ['AGENT_OBSERVABILITY_ENABLED'] = 'true'
```

### Local Development

```bash
export AGENT_OBSERVABILITY_ENABLED=true
export OTEL_PYTHON_DISTRO=aws_distro
export OTEL_PYTHON_CONFIGURATOR=aws_configurator
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_RESOURCE_ATTRIBUTES=service.name=clinical-compass
```

## Viewing Logs

### CloudWatch Logs (CLI)

```bash
# View recent application logs
aws logs tail \
  /aws/bedrock-agentcore/runtimes/clinicalcompass-DEFAULT \
  --since 10m --region us-east-1

# Follow logs in real-time
aws logs tail \
  /aws/bedrock-agentcore/runtimes/clinicalcompass-DEFAULT \
  --follow --region us-east-1
```

### GenAI Observability Dashboard

1. Open CloudWatch Console
2. Navigate to GenAI Observability → Bedrock AgentCore → Agents
3. Select `clinicalcompass`
4. View sessions, traces, and metrics

### CloudWatch Logs Insights Queries

**Find all sessions:**
```
fields @timestamp, @message
| filter @message like /session/
| sort @timestamp desc
| limit 100
```

**Find custom clinical spans:**
```
fields @timestamp, @message
| filter @message like /clinical\./
| sort @timestamp desc
```

**Find errors:**
```
fields @timestamp, @message
| filter @message like /ERROR/ or @message like /Exception/
| sort @timestamp desc
```

**Track a specific session:**
```
fields @timestamp, @message
| filter @message like /<SESSION_ID>/
| sort @timestamp asc
```

## Custom Clinical Spans

The `backend/utils/clinical_observability.py` module provides span helpers:

```python
from clinical_observability import (
    record_conflict_detection,
    record_precedence_rule_application,
    record_recommendation_generation,
    record_agent_coordination,
    clinical_span,
)

# Conflict detection
record_conflict_detection(session_id, conflicts, patient_id)

# Precedence rule application
record_precedence_rule_application(
    session_id=session_id,
    conflict="Contrast use for eGFR 32",
    chosen_guideline="HAS France",
    overridden_guideline="ACR International",
    rationale="Local regulatory requirements take precedence",
    patient_id=patient_id
)

# Recommendation generation
record_recommendation_generation(session_id, recommendation, patient_id)

# Agent coordination timing
record_agent_coordination(
    session_id=session_id,
    agent_name="patient_context",
    duration_ms=1500,
    success=True,
    patient_id=patient_id
)

# Custom spans
with clinical_span("custom.operation", session_id=session_id) as span:
    span.set_attribute("custom.attribute", "value")
```

## Dual Storage Strategy

Clinical decision traces use two storage tiers:

| Tier | Technology | Latency | Retention | Use Case |
|------|-----------|---------|-----------|----------|
| Hot | AgentCore Memory | <100ms | 24-48h | UI "View Full Reasoning" button |
| Cold | CloudWatch Logs (OTEL) | 5-10s | 90 days+ | Audit, compliance, forensics |

The hot tier stores a structured JSON decision trace written at the end of each recommendation. The cold tier receives complete OTEL spans automatically.

## Troubleshooting

### Dashboard not showing data

1. **Enable CloudWatch Transaction Search** - AWS Console → CloudWatch → Settings → Enable Transaction Search
2. **X-Ray permissions** are included in the CDK stack: the AgentCore role can write traces (`xray:PutTraceSegments`, `PutTelemetryRecords`, `GetSamplingRules`, `GetSamplingTargets`) and `bedrock-agentcore` metrics. If you still see `Failed to export span batch code: 403`, check that Transaction Search (step 1) is enabled.
3. Run a test query and wait 1-2 minutes

### Logging scripts

```bash
# AgentCore runtime logs (runtime ID is read from SSM, so it always matches the deployed runtime)
python deployment/scripts/check_agentcore_logs.py --minutes 30

# Lambda logs, e.g. the WebSocket query handler
aws logs tail /aws/lambda/clinicalcompass-ws-query --since 30m --follow --region us-east-1
```

### Common log messages

| Message | Meaning |
|---------|---------|
| `Created session: <id>` | New session started |
| `Stored session data` | Memory write successful |
| `Streaming Supervisor Agent response` | Agent invocation in progress |
| `AGENT_OBSERVABILITY_ENABLED: true` | Custom spans active |
| `Failed to export span batch code: 403` | Trace export rejected: CloudWatch Transaction Search not enabled, or X-Ray permissions missing from the AgentCore role |
