---
title: Business Context
weight: 1
next: how-it-works
prev: /docs
---

## What are Clinical Guidelines?

Clinical guidelines are evidence-based recommendations that help clinicians make decisions about patient care in specific clinical circumstances. They are produced by national bodies (e.g., NICE in England/Wales), professional societies, and individual healthcare organisations.

## The Challenge

Healthcare professionals face significant challenges when navigating clinical guidelines:

* **Layered complexity**: Guidelines exist at multiple levels - national (NICE, SIGN, BNF), regional (NHS Trust/ICB), and local (formulary, commercial agreements). A single clinical question may require consulting guidance from several sources.
* **Volume**: NICE alone publishes over 2,000 guidelines, quality standards, and technology appraisals. Each NHS Trust adds local policies on top.
* **Geographic variation**: Clinical pathways vary by region. The same condition may be managed differently depending on local funding decisions, available services, and formulary restrictions.
* **Time pressure**: Clinicians need answers at the point of care, often during consultations, with limited time to search across multiple sources.
* **Currency**: Guidelines are regularly updated. Keeping track of changes across multiple sources is a significant administrative burden.

Current processes rely on clinicians manually searching guideline databases, reading lengthy PDF documents, and cross-referencing multiple sources.

## Our Solution

AWS ClinicalCompass addresses these challenges through:

### Agentic guideline navigation

An AI agent that understands the structure and hierarchy of clinical guidelines, retrieving from the relevant sources based on the clinical question.

### Multi-source aggregation

Retrieves and reasons across national guidelines, local trust policies, and formulary data to provide a unified, contextualised answer.

### Evidence-based citations

Every response includes citations to source documents, enabling clinicians to verify recommendations and maintain clinical accountability.

### Natural language interaction

Clinicians can ask questions in plain clinical language rather than constructing keyword searches across multiple databases.

### Human-in-the-loop design

ClinicalCompass assists clinical decision-making - it does not replace it. All outputs are clearly presented as guideline summaries requiring clinical judgement.

## Benefits

* **Time savings**: Reduces the effort of searching across multiple guideline databases to a single natural language query.
* **Consistency**: Ensures clinicians access the most current, locally-relevant guidance regardless of which source it originates from.
* **Completeness**: Surfaces all relevant layers of guidance (national, regional, local) rather than relying on a single source.
* **Workforce productivity**: Frees clinical time for direct patient care rather than administrative searching.

## Getting Started

Ready to deploy ClinicalCompass in your environment? Follow our [installation guide](../installation/) to get started.
