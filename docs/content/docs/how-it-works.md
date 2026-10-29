---
title: How It Works
weight: 2
next: architecture
prev: business-context
---

## Overview

AWS ClinicalCompass uses an agentic AI architecture to process clinical queries through multiple stages: understanding the clinical context, retrieving relevant guidelines from multiple sources, resolving conflicts between them, and presenting a cited response.

## Query Processing

1. **Patient and guideline selection** - The clinician selects a patient from the Patient Selector (populated from DynamoDB via REST API) and optionally filters guideline sources via the Guideline Selector.

2. **Clinical context understanding** - The agent interprets the clinical question, identifying the condition, patient context, and type of guidance needed (diagnostic, treatment, referral, prescribing).

3. **Source identification** - Based on the query and selected guidelines, the agent determines which knowledge bases are relevant (e.g., NICE for national guidance, local trust formulary for prescribing decisions).

4. **Parallel retrieval** - Three specialised agents execute simultaneously: Patient Context Agent queries EHR data, Global Guidelines Agent consults international sources, Local Regulatory Agent checks jurisdiction-specific protocols.

5. **Conflict detection and resolution** - The supervisor identifies disagreements between sources and applies precedence rules (local > national > international) to resolve them.

6. **Response generation** - The supervisor produces a structured response with clear citations, confidence indicators, safety alerts, alternatives ranked by safety profile, and links to source documents.

## Key Features

### Guideline hierarchy awareness

ClinicalCompass understands that clinical guidelines operate in layers. National guidance provides the baseline, but local trust policies, formulary decisions, and regional pathways may modify recommendations. The system surfaces all relevant layers.

### Conflict resolution

When guidelines from different sources provide different recommendations, ClinicalCompass identifies the conflict and presents both positions with context on which takes precedence in the local setting.

### Citation and traceability

Every statement in a response links back to a specific section of a source document. Clinicians can click through to read the full guideline context.
