---
title: Agents
weight: 6
prev: development
next: observability
---

ClinicalCompass uses a supervisor pattern where a central orchestrator coordinates three specialised agents that run in parallel. Each agent has a single responsibility and queries its own knowledge base.

## Agent Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      Supervisor Agent                            │
│  (Claude Sonnet - Orchestration & Conflict Resolution)          │
└────────┬────────────────────────────────────────────────────────┘
         │
         ├──────────────────┬──────────────────┐
         ▼                  ▼                  ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Patient Context│  │    Global       │  │    Local        │
│     Agent       │  │  Guidelines     │  │  Regulatory     │
│                 │  │    Agent        │  │    Agent        │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│    EHR KB       │  │ Guidelines KB   │  │ Regulatory KB   │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

## Supervisor Agent

The orchestrator that manages the entire clinical decision workflow.

**Responsibilities:**
1. Parse clinical queries and extract entities (patient demographics, symptoms, intended action)
2. Coordinate parallel agent execution
3. Detect conflicts between guideline recommendations
4. Apply precedence rules (local > national > international)
5. Generate safety alerts and alternatives
6. Produce a final recommendation with citations

**Tools:**
| Tool | Purpose |
|------|---------|
| `detect_conflicts` | Identifies disagreements between agent responses |
| `apply_precedence_rules` | Resolves conflicts using local > national > international hierarchy |
| `generate_safety_alerts` | Analyses contraindications and risks |
| `search_alternatives` | Finds clinically equivalent safer options |
| `synthesize_recommendation` | Produces the final cited recommendation |

**Key behaviours:**
- No intermediate narration between tool calls — only the final recommendation is surfaced
- Multi-turn conversations supported via session memory
- Patient data cached within sessions to avoid redundant EHR queries
- All overridden recommendations documented for audit trail

## Patient Context Agent

Retrieves patient-specific data from the EHR knowledge base.

**Tool:** `query_ehr(patient_id, query_context)`

**Knowledge Base:** EHR KB
- Patient demographics
- Lab results (eGFR, creatinine, etc.)
- Current medications
- Allergies
- Active conditions

**Example query:**
```python
query_ehr(
    patient_id="patient_001",
    query_context="kidney function for contrast imaging"
)
```

**Returns:** Structured JSON with relevant patient data, labs, medications, allergies, and conditions.

## Global Guidelines Agent

Consults international clinical guideline knowledge bases.

**Tool:** `query_guidelines(clinical_scenario, guideline_sources)`

**Knowledge Base:** Global Guidelines KB
- ACR (American College of Radiology)
- ESUR (European Society of Urogenital Radiology)
- NICE (National Institute for Health and Care Excellence)
- WHO guidelines

**Example query:**
```python
query_guidelines(
    clinical_scenario="MRI with contrast for patient with reduced kidney function",
    guideline_sources=["ACR", "ESUR", "NICE"]
)
```

**Returns:** JSON with source, recommendation text, evidence level, confidence score, and citation.

## Local Regulatory Agent

Checks national and local regulatory protocols that may override international guidelines.

**Tool:** `query_regulatory_protocols(clinical_scenario, jurisdiction, protocol_sources)`

**Knowledge Base:** Local Regulatory KB
- HAS France (Haute Autorité de Santé) protocols
- NHS local trust policies
- Regional SOPs (Standard Operating Procedures)
- Formulary restrictions

**Example query:**
```python
query_regulatory_protocols(
    clinical_scenario="MRI with contrast for patient with reduced kidney function",
    jurisdiction="France",
    protocol_sources=["HAS"]
)
```

**Returns:** JSON with source, requirement text, mandatory/recommended flag, confidence score, and citation.

## Conflict Resolution

When agents return contradictory recommendations, the supervisor applies precedence rules:

```
Local Regulatory > National > International
```

**Example conflict:**
- ACR (International): "Gadolinium contrast can be used with caution for eGFR 30-44"
- HAS France (Local): "Gadolinium contrast should be avoided for eGFR <45"
- **Resolution:** HAS France takes precedence — local protocols are more conservative for patient safety and legal compliance

**Rationale for local precedence:**
- Legal compliance in local jurisdiction
- Patient safety (local protocols are often more conservative)
- Regulatory requirements that clinicians must follow

## Knowledge Bases

All three KBs use the same configuration:
- **Embedding model:** amazon.titan-embed-text-v2:0
- **Chunking:** 512 tokens, 20% overlap
- **Vector store:** OpenSearch Serverless
- **Retrieval:** Semantic search via Bedrock Knowledge Bases

### Data sources

```
knowledge-bases/data/
├── ehr/            # Patient records
├── guidelines/     # International clinical guidelines
└── regulatory/     # Local regulatory protocols
```

## Session Management

- Multi-turn conversations use `session_id` for context continuity
- Patient data is cached in AgentCore short-term memory within a session
- Follow-up questions reference previous context without re-querying
- Session timeout: 30 minutes of inactivity

## Performance

- Full multi-agent workflow: 30-40 seconds
- All three specialised agents execute in parallel
- Hot storage retrieval (decision traces): <200ms
- Individual KB query: 2-5 seconds
