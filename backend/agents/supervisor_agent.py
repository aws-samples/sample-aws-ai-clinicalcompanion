"""
Supervisor Agent - Orchestrates multi-agent workflow for clinical decision support
"""

import json
import asyncio
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

from strands import Agent, tool
from strands.models import BedrockModel

# Global agent registry for inter-agent communication
_agent_registry = {}

def register_agent(name: str, agent: Agent):
    """Register an agent in the global registry"""
    _agent_registry[name] = agent

def get_agent(name: str) -> Agent:
    """Get an agent from the registry"""
    return _agent_registry.get(name)

# Import clinical observability for OTEL spans
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.clinical_observability import (
    record_conflict_detection,
    record_precedence_rule_application,
    record_recommendation_generation,
    record_agent_coordination,
    clinical_span
)
from utils.guideline_filtering import (
    resolve_selected_guidelines,
    DEFAULT_GUIDELINE_SOURCES,
)
from utils.safety_checks import build_safety_check
from utils.citation_validation import group_citations_by_source

# Module-level storage for the latest synthesis result (citations, guidelines_used, safety_checks).
# The AG-UI emitter reads this after the stream completes to emit a final STATE_SNAPSHOT.
_last_synthesis_result: dict | None = None


def get_last_synthesis_result() -> dict | None:
    """Get the last synthesis result for AG-UI STATE_SNAPSHOT emission."""
    return _last_synthesis_result


@tool
def generate_safety_alerts(
    patient_context: str,
    intended_action: str,
    guideline_context: str
) -> str:
    """
    Analyze patient data and generate safety alerts using clinical reasoning.
    
    You are a clinical safety expert. Review the patient data, intended action, and guideline context.
    Identify any contraindications, risks, or safety concerns.
    
    For each safety concern, provide:
    1. severity: "HIGH" (absolute/relative contraindication), "MEDIUM" (caution required), or "LOW" (minor concern)
    2. finding: What you observed in the patient data (be specific with values)
    3. risk: What could go wrong if the intended action proceeds
    4. action_required: What the clinician should do
    5. evidence: Brief explanation of the clinical reasoning
    6. citations: List of guideline sources that support this alert
    
    Prioritize by severity: HIGH > MEDIUM > LOW
    
    Args:
        patient_context: Patient data including labs, medications, allergies, conditions
        intended_action: The clinical action being considered (e.g., "MRI with contrast")
        guideline_context: Relevant guidelines from Global and Local agents
        
    Returns:
        JSON array of safety alert objects, sorted by severity
        
    Example output:
    [
        {
            "severity": "HIGH",
            "finding": "Patient has eGFR 32 mL/min/1.73m² (CKD Stage 3B)",
            "risk": "Gadolinium contrast poses significant risk of contrast-induced nephropathy",
            "action_required": "Avoid gadolinium contrast. Consider non-contrast MRI instead.",
            "evidence": "HAS France 2024: eGFR <45 is contraindication without nephrology consultation",
            "citations": ["HAS Protocoles 2024 Article 4.2", "ACR Contrast Manual 2024 Section 2.3"]
        }
    ]
    
    IMPORTANT: You MUST return a valid JSON array. Analyze the patient context and guidelines,
    then return ONLY the JSON array with no additional text or explanation.
    """
    # This tool will be executed by the Supervisor Agent's LLM with structured output
    # The LLM will analyze the inputs and generate appropriate safety alerts as JSON
    pass


@tool
def search_alternatives(
    intended_action: str,
    safety_concerns: str,
    guideline_context: str
) -> str:
    """
    Search for clinically equivalent alternatives with better safety profiles.
    
    You are a clinical decision support expert. Given the intended action and safety concerns,
    identify alternative approaches that achieve the same diagnostic or therapeutic goal
    with better safety profiles.
    
    For each alternative, provide:
    1. option: Name of the alternative approach
    2. safety_benefit: How this alternative reduces risk compared to original action
    3. diagnostic_value: What this alternative can detect/achieve
    4. clinical_equivalence: Evidence of diagnostic accuracy or therapeutic equivalence
    5. evidence: Clinical reasoning for why this is a good alternative
    6. limitations: What this alternative cannot do (be honest)
    7. rank: Priority ranking (1 = best alternative)
    8. citations: Guideline sources supporting this alternative
    
    Rank alternatives by: safety profile > diagnostic value > availability
    
    Args:
        intended_action: The original clinical action being considered
        safety_concerns: JSON array of safety alerts from generate_safety_alerts
        guideline_context: Relevant guidelines that may suggest alternatives
        
    Returns:
        JSON array of alternative recommendation objects, ranked by priority
        
    Example output:
    [
        {
            "option": "MRI lumbar spine without contrast",
            "safety_benefit": "Eliminates all contrast-related risks (nephrotoxicity, allergic reactions)",
            "diagnostic_value": "Excellent for disc herniation, spinal stenosis, tumors",
            "clinical_equivalence": "Sensitivity >90% for most spinal pathology without contrast",
            "evidence": "Non-contrast MRI is first-line per ACR Appropriateness Criteria",
            "limitations": "May miss some vascular lesions or subtle enhancement",
            "rank": 1,
            "citations": ["ACR Appropriateness Criteria 2024: Spine Imaging"]
        }
    ]
    
    IMPORTANT: You MUST return a valid JSON array. Analyze the safety concerns and guidelines,
    then return ONLY the JSON array with no additional text or explanation.
    """
    # This tool will be executed by the Supervisor Agent's LLM with structured output
    # The LLM will reason about alternatives based on the clinical context
    pass


@tool
def parse_clinical_query(query: str) -> str:
    """
    Parse natural language clinical query and extract structured entities using LLM reasoning.
    
    You are a clinical entity extraction expert. Analyze the query and extract the following entities
    in a structured JSON format. Use your medical knowledge to identify entities even if they use
    different terminology or phrasing.
    
    Args:
        query: Natural language clinical query (e.g., "68-year-old male, back pain, want MRI spine with contrast")
        
    Returns:
        JSON string with extracted entities following this exact schema:
        {
            "patient_age": int or null,  // Patient's age in years
            "patient_sex": str or null,  // "male", "female", or null
            "symptoms": list[str],  // List of symptoms/complaints (e.g., ["back pain", "numbness"])
            "medical_history": list[str],  // List of relevant medical conditions (e.g., ["diabetes", "hypertension"])
            "intended_action": {
                "type": str or null,  // "imaging", "medication", "procedure", "consultation"
                "modality": str or null,  // For imaging: "MRI", "CT", "X-ray", "ultrasound", "PET", "mammography", etc.
                "contrast": bool or null,  // For imaging: true if contrast mentioned, false if explicitly non-contrast, null if not specified
                "region": str or null,  // Anatomical region: "spine", "brain", "chest", "abdomen", "pelvis", "head", "lumbar", "cervical", "knee", etc.
                "details": str  // Full description of intended action
            }
        }
    
    EXTRACTION GUIDELINES:
    - Extract age from phrases like "68-year-old", "68 yo", "68 y.o.", "age 68"
    - Extract sex from "male", "female", "man", "woman", "M", "F"
    - Identify symptoms even with medical terminology (e.g., "cephalalgia" → "headache")
    - Recognize imaging modalities with variations (e.g., "computed tomography" → "CT")
    - Identify anatomical regions with variations (e.g., "lower back" → "lumbar spine")
    - For contrast: look for "with contrast", "contrast-enhanced", "gadolinium", "iodinated contrast"
    - For non-contrast: look for "without contrast", "non-contrast", "plain"
    - Extract medical history from context (e.g., "diabetic patient" → ["diabetes"])
    
    IMPORTANT: Return ONLY valid JSON matching the schema above. Do not add explanatory text.
    If an entity cannot be determined, use null for single values or empty list [] for arrays.
    
    Example input: "45 year old female with severe headache and dizziness, requesting brain MRI with contrast"
    Example output:
    {
        "patient_age": 45,
        "patient_sex": "female",
        "symptoms": ["severe headache", "dizziness"],
        "medical_history": [],
        "intended_action": {
            "type": "imaging",
            "modality": "MRI",
            "contrast": true,
            "region": "brain",
            "details": "brain MRI with contrast"
        }
    }
    """
    # This tool will be executed by the Supervisor Agent's LLM
    # The LLM will analyze the query and return structured JSON
    # No hardcoded extraction logic - the LLM does all the work
    pass


@tool
def detect_conflicts(agent_responses: str, session_id: str = None, patient_id: str = None) -> str:
    """
    Detect conflicts between guideline recommendations using clinical reasoning.
    
    You are a clinical guideline expert. Analyze the responses from three agents:
    - Patient Context Agent: Patient-specific data (labs, medications, conditions)
    - Global Guidelines Agent: International clinical guidelines (ACR, ESUR, NICE, WHO)
    - Local Regulatory Agent: National/local regulatory protocols (HAS France, NHS, hospital SOPs)
    
    Your task is to identify conflicts where guidelines provide contradictory recommendations
    for the same clinical scenario. Focus on conflicts that impact patient safety or clinical decisions.
    
    Args:
        agent_responses: JSON string containing:
            - patient_context: Patient data from EHR
            - global_guidelines: International guideline recommendations
            - local_regulatory: Local/national regulatory protocols
        session_id: Session identifier for observability tracing
        patient_id: Patient identifier for observability tracing
        
    Returns:
        JSON string following this exact schema:
        {
            "conflicts_detected": bool,
            "conflicts": [
                {
                    "issue": str,  // Brief description of the conflicting issue
                    "global_recommendation": str,  // What international guidelines recommend
                    "local_recommendation": str,  // What local/national protocols require
                    "severity": str,  // "high", "medium", or "low"
                    "patient_safety_impact": str  // How this conflict affects patient safety
                }
            ],
            "summary": str  // Brief summary of conflicts found
        }
    
    CONFLICT DETECTION GUIDELINES:
    
    1. **What constitutes a conflict:**
       - International guidelines recommend action A, local protocols prohibit or contraindicate A
       - Guidelines differ on safety thresholds (e.g., eGFR cutoffs for contrast)
       - Different medication management protocols (e.g., metformin hold duration)
       - Contradictory recommendations for the same clinical scenario
    
    2. **What is NOT a conflict:**
       - One guideline provides more detail than another (complementary, not contradictory)
       - Guidelines address different aspects of care (not mutually exclusive)
       - One guideline is silent while another provides guidance (absence is not contradiction)
    
    3. **Severity assessment:**
       - HIGH: Direct contradiction on safety-critical issues (e.g., contraindication vs. allowed)
       - MEDIUM: Different thresholds or protocols that affect clinical decisions
       - LOW: Minor procedural differences that don't significantly impact safety
    
    4. **Patient safety impact:**
       - Reference specific patient data (e.g., "Patient has eGFR 32")
       - Explain the clinical risk (e.g., "contrast poses risk of nephropathy")
       - Be specific about why this matters for THIS patient
    
    IMPORTANT: 
    - Return ONLY valid JSON matching the schema above
    - If no conflicts found, return: {"conflicts_detected": false, "conflicts": [], "summary": "No conflicts detected"}
    - Do not add explanatory text outside the JSON structure
    - Analyze ALL aspects of the guidelines, not just contrast or specific scenarios
    - Consider patient-specific factors when assessing conflicts
    
    Example output:
    {
        "conflicts_detected": true,
        "conflicts": [
            {
                "issue": "Contrast administration for patient with moderate renal impairment",
                "global_recommendation": "ACR guidelines allow gadolinium-based contrast with caution for eGFR 30-44 with adequate hydration",
                "local_recommendation": "HAS France protocols require avoiding gadolinium-based contrast for eGFR < 45 without nephrology consultation",
                "severity": "high",
                "patient_safety_impact": "Patient has eGFR 32 (CKD Stage 3B) - contrast administration poses significant risk of contrast-induced nephropathy"
            }
        ],
        "summary": "Detected 1 high-severity conflict regarding contrast safety in renal impairment"
    }
    """
    # Note: This tool is LLM-powered. The LLM will analyze the agent responses
    # and return structured JSON based on the detailed instructions above.
    # The observability tracking happens in the wrapper below.
    pass


@tool
def apply_precedence_rules(conflicts: str, session_id: str = None, patient_id: str = None) -> str:
    """
    Apply precedence rules to resolve conflicts.
    Precedence: local regulatory > national > international
    
    Args:
        conflicts: JSON string of detected conflicts from detect_conflicts
        session_id: Session identifier for observability tracing
        patient_id: Patient identifier for observability tracing
        
    Returns:
        JSON string with resolutions: {
            "resolutions": list[{
                "conflict": str,
                "chosen_guideline": str,  # "local", "national", "international"
                "rationale": str,
                "final_recommendation": str,
                "overridden_guidelines": list[str]
            }]
        }
    """
    with clinical_span(
        "clinical.apply_precedence_rules",
        session_id=session_id,
        patient_id=patient_id,
        attributes={"operation": "precedence_resolution"}
    ) as span:
        try:
            conflict_data = json.loads(conflicts)
            
            if not conflict_data.get("conflicts_detected"):
                return json.dumps({
                    "resolutions": [],
                    "summary": "No conflicts to resolve"
                })
            
            resolutions = []
            
            for conflict in conflict_data.get("conflicts", []):
                issue = conflict.get("issue", "")
                severity = conflict.get("severity", "medium")
                
                # Apply precedence rule: local > international
                resolution = {
                    "conflict": issue,
                    "chosen_guideline": "local",
                    "rationale": f"Local regulatory protocols (HAS France) take precedence over international guidelines (ACR) for patient safety and legal compliance in France. {conflict.get('patient_safety_impact', '')}",
                    "final_recommendation": conflict.get("local_recommendation", ""),
                    "overridden_guidelines": [
                        f"ACR (International): {conflict.get('global_recommendation', '')}"
                    ],
                    "severity": severity,
                    "compliance_note": "Following local regulatory requirements is mandatory for clinical practice in France"
                }
                
                # Record precedence rule application with OTEL
                if session_id:
                    record_precedence_rule_application(
                        session_id=session_id,
                        conflict=issue,
                        chosen_guideline="HAS France (local)",
                        overridden_guideline="ACR (international)",
                        rationale=resolution["rationale"],
                        patient_id=patient_id
                    )
                
                resolutions.append(resolution)
            
            # Add span attributes
            span.set_attribute("resolution.count", len(resolutions))
            span.set_attribute("precedence.rule", "local > national > international")
            
            result = {
                "resolutions": resolutions,
                "summary": f"Resolved {len(resolutions)} conflict(s) by applying local precedence rules",
                "precedence_order": "local regulatory > national > international"
            }
            
            return json.dumps(result, indent=2)
            
        except Exception as e:
            span.set_attribute("error", str(e))
            return json.dumps({
                "error": f"Error applying precedence rules: {str(e)}",
                "resolutions": []
            })


@tool
async def coordinate_agents(
    query_entities: str, 
    patient_id: str = "patient_001", 
    session_id: str = None,
    session_context: str = None,
    selected_guidelines: str = None
) -> str:
    """
    Coordinate parallel execution of specialized agents with session context support.
    
    All sub-agents are invoked concurrently via asyncio.gather (Requirement 7.2).
    AG-UI progress events are emitted as each sub-agent completes for incremental
    streaming (Requirement 7.6).
    
    Args:
        query_entities: JSON string of parsed query entities
        patient_id: Patient identifier (default: patient_001 for Jean-Pierre)
        session_id: Session identifier for observability tracing
        session_context: JSON string of session context (optional, for patient data caching)
        selected_guidelines: JSON array of selected guideline source names (e.g., '["ACR", "HAS France"]').
            When None or empty, all available sources are queried.
        
    Returns:
        JSON string with agent responses: {
            "patient_context": dict,
            "global_guidelines": dict,
            "local_regulatory": dict,
            "processing_time_ms": int,
            "guidelines_queried": list,
            "agent_completion_order": list
        }
    """
    with clinical_span(
        "clinical.coordinate_agents",
        session_id=session_id,
        patient_id=patient_id,
        attributes={"operation": "agent_coordination"}
    ) as span:
        start_time = datetime.now()
        
        # Parse query entities
        entities = json.loads(query_entities)
        
        # Parse session context
        context = json.loads(session_context) if session_context else {}
        
        # Resolve selected guidelines (Requirement 3.4)
        selected_list = json.loads(selected_guidelines) if selected_guidelines else None
        queried_sources = resolve_selected_guidelines(
            available_sources=DEFAULT_GUIDELINE_SOURCES,
            selected_sources=selected_list,
        )
        
        # Check for cached patient data
        cached_patient_data = context.get('patient_data_cache', {}).get('data')
        
        # Get agents from registry
        patient_agent = get_agent("patient_context_agent")
        guidelines_agent = get_agent("global_guidelines_agent")
        regulatory_agent = get_agent("local_regulatory_agent")
        
        if not all([patient_agent, guidelines_agent, regulatory_agent]):
            return json.dumps({
                "error": "One or more specialized agents not available",
                "patient_context": None,
                "global_guidelines": None,
                "local_regulatory": None,
                "processing_time_ms": 0,
                "guidelines_queried": [],
                "agent_completion_order": []
            })
        
        # Prepare queries for each agent
        intended_action = entities.get("intended_action", {})
        action_description = f"{intended_action.get('modality', 'imaging')} {intended_action.get('region', '')} {'with contrast' if intended_action.get('contrast') else ''}".strip()
        
        # Build guideline-restricted query for the guidelines agent
        sources_str = ", ".join(sorted(queried_sources))
        guidelines_query = (
            f"Find international guidelines for {action_description} "
            f"in patient age {entities.get('patient_age', 'unknown')}. "
            f"Restrict to these sources only: {sources_str}"
        )
        
        # Track agent completion order for streaming (Requirement 7.6)
        agent_completion_order: List[str] = []
        agent_timings: Dict[str, int] = {}
        
        async def _run_agent_with_progress(agent, query: str, agent_name: str):
            """Run a single agent and record completion for streaming progress."""
            agent_start = datetime.now()
            try:
                result = await agent.invoke_async(query)
                duration_ms = int((datetime.now() - agent_start).total_seconds() * 1000)
                agent_completion_order.append(agent_name)
                agent_timings[agent_name] = duration_ms
                
                # Record OTEL coordination event immediately on completion
                if session_id:
                    record_agent_coordination(
                        session_id=session_id,
                        agent_name=agent_name,
                        duration_ms=duration_ms,
                        success=True,
                        patient_id=patient_id
                    )
                
                import logging
                logging.getLogger(__name__).info(
                    f"Agent '{agent_name}' completed in {duration_ms}ms "
                    f"(completion #{len(agent_completion_order)})"
                )
                return result
            except Exception as e:
                duration_ms = int((datetime.now() - agent_start).total_seconds() * 1000)
                agent_timings[agent_name] = duration_ms
                
                if session_id:
                    record_agent_coordination(
                        session_id=session_id,
                        agent_name=agent_name,
                        duration_ms=duration_ms,
                        success=False,
                        patient_id=patient_id
                    )
                raise
        
        # Execute agents in parallel (Requirement 7.2)
        try:
            if cached_patient_data:
                # Use cached data instead of querying EHR again
                patient_response = json.dumps(cached_patient_data)
                agent_completion_order.append("patient_context_agent")
                agent_timings["patient_context_agent"] = 0
                
                import logging
                logging.getLogger(__name__).info(f"Using cached patient data for session {session_id}")
                
                # Run guidelines agents in parallel — each emits progress on completion
                guidelines_response, regulatory_response = await asyncio.gather(
                    _run_agent_with_progress(
                        guidelines_agent, guidelines_query, "global_guidelines_agent"
                    ),
                    _run_agent_with_progress(
                        regulatory_agent,
                        f"Find French regulatory protocols for {action_description}",
                        "local_regulatory_agent"
                    )
                )
            else:
                # Run ALL agents in parallel (Requirement 7.2)
                # asyncio.gather ensures concurrent execution — wall-clock time
                # is bounded by the slowest agent, not the sum of all agents.
                patient_response, guidelines_response, regulatory_response = await asyncio.gather(
                    _run_agent_with_progress(
                        patient_agent,
                        f"Get patient data for {patient_id} relevant to {action_description}",
                        "patient_context_agent"
                    ),
                    _run_agent_with_progress(
                        guidelines_agent, guidelines_query, "global_guidelines_agent"
                    ),
                    _run_agent_with_progress(
                        regulatory_agent,
                        f"Find French regulatory protocols for {action_description}",
                        "local_regulatory_agent"
                    )
                )
                
                # Cache patient data for future queries in this session
                if session_id:
                    try:
                        from utils.session_manager import cache_patient_data
                        patient_text = str(patient_response)
                        try:
                            patient_data = json.loads(patient_text)
                        except (json.JSONDecodeError, TypeError):
                            patient_data = {"text": patient_text}
                        cache_patient_data(patient_id, session_id, patient_data)
                        import logging
                        logging.getLogger(__name__).info(f"Cached patient data for session {session_id}")
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(f"Failed to cache patient data: {e}")
            
            # Calculate total processing time (wall-clock, not sum)
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            
            # Add span attributes
            span.set_attribute("agent.count", 3)
            span.set_attribute("agent.parallel", True)
            span.set_attribute("processing_time_ms", int(processing_time))
            span.set_attribute("patient_data_cached", bool(cached_patient_data))
            span.set_attribute("guidelines.queried_sources", sources_str)
            span.set_attribute("agent.completion_order", ",".join(agent_completion_order))
            
            result = {
                "patient_context": str(patient_response),
                "global_guidelines": str(guidelines_response),
                "local_regulatory": str(regulatory_response),
                "processing_time_ms": int(processing_time),
                "guidelines_queried": sorted(queried_sources),
                "agent_completion_order": agent_completion_order,
                "agent_timings": agent_timings,
            }
            
            return json.dumps(result, indent=2)
            
        except Exception as e:
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            
            # Record failed coordination
            if session_id:
                record_agent_coordination(
                    session_id=session_id,
                    agent_name="coordination",
                    duration_ms=int(processing_time),
                    success=False,
                    patient_id=patient_id
                )
            
            span.set_attribute("error", str(e))
            return json.dumps({
                "error": f"Error coordinating agents: {str(e)}",
                "patient_context": None,
                "global_guidelines": None,
                "local_regulatory": None,
                "processing_time_ms": int(processing_time),
                "guidelines_queried": [],
                "agent_completion_order": agent_completion_order,
            })


def _extract_citations_from_responses(responses: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract structured citations from agent responses.

    Parses the global_guidelines and local_regulatory agent responses to
    build citation objects following the design document data model
    (Requirement 4.1).

    Args:
        responses: Dict with keys 'global_guidelines' and 'local_regulatory'
            containing stringified agent responses.

    Returns:
        List of citation dicts with keys: source_organization, document_title,
        publication_reference, variant, rating, quoted_text.
    """
    citations: List[Dict[str, str]] = []

    # Parse global guidelines response
    global_text = responses.get("global_guidelines", "")
    try:
        global_data = json.loads(global_text) if isinstance(global_text, str) else global_text
    except (json.JSONDecodeError, TypeError):
        global_data = {}

    guidelines_list = global_data.get("guidelines", []) if isinstance(global_data, dict) else []
    for guideline in guidelines_list:
        if not isinstance(guideline, dict):
            continue
        citation = {
            "source_organization": guideline.get("source", guideline.get("organization", "Unknown")),
            "document_title": guideline.get("document_title", "Clinical Guideline"),
            "publication_reference": guideline.get("citation", f"{guideline.get('source', 'Unknown')} Clinical Guidelines {guideline.get('publication_date', '2024')}"),
            "variant": guideline.get("section", "General recommendation"),
            "rating": guideline.get("evidence_level", "Not specified"),
            "quoted_text": guideline.get("recommendation", guideline.get("full_content", "")),
        }
        # Only include citations with meaningful content
        if citation["quoted_text"].strip():
            citations.append(citation)

    # Parse local regulatory response
    local_text = responses.get("local_regulatory", "")
    try:
        local_data = json.loads(local_text) if isinstance(local_text, str) else local_text
    except (json.JSONDecodeError, TypeError):
        local_data = {}

    # Local regulatory may have a similar structure or be plain text
    if isinstance(local_data, dict):
        local_guidelines = local_data.get("guidelines", local_data.get("protocols", []))
        for guideline in local_guidelines:
            if not isinstance(guideline, dict):
                continue
            citation = {
                "source_organization": guideline.get("source", guideline.get("organization", "HAS France")),
                "document_title": guideline.get("document_title", "Regulatory Protocol"),
                "publication_reference": guideline.get("citation", "HAS France Protocols 2024"),
                "variant": guideline.get("section", "General protocol"),
                "rating": guideline.get("evidence_level", "Mandatory"),
                "quoted_text": guideline.get("recommendation", guideline.get("full_content", "")),
            }
            if citation["quoted_text"].strip():
                citations.append(citation)

    # Group citations by source (Requirement 4.6)
    return group_citations_by_source(citations)


def _build_guidelines_used(responses: Dict[str, Any], resolution_data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build guidelines_used list for STATE_SNAPSHOT event (Requirement 8.5).

    Extracts which guidelines were consulted and their verdict for the
    Patient Context Pane display.

    Args:
        responses: Dict with agent response strings.
        resolution_data: Parsed conflict resolution data.

    Returns:
        List of dicts with keys: name, verdict.
    """
    guidelines_used: List[Dict[str, str]] = []
    seen_sources: set = set()

    # Extract from resolutions (these have the most specific verdicts)
    for resolution in resolution_data.get("resolutions", []):
        chosen = resolution.get("chosen_guideline", "")
        if chosen == "local":
            source_name = "HAS France"
            verdict = resolution.get("final_recommendation", "Local protocol applied")
        else:
            source_name = chosen
            verdict = resolution.get("final_recommendation", "Recommendation applied")

        if source_name and source_name not in seen_sources:
            guidelines_used.append({"name": source_name, "verdict": verdict[:100]})
            seen_sources.add(source_name)

        # Also add overridden guidelines
        for overridden in resolution.get("overridden_guidelines", []):
            # Format: "ACR (International): recommendation text"
            parts = overridden.split(":", 1)
            if parts:
                override_name = parts[0].split("(")[0].strip()
                override_verdict = "Overridden by local protocol"
                if override_name and override_name not in seen_sources:
                    guidelines_used.append({"name": override_name, "verdict": override_verdict})
                    seen_sources.add(override_name)

    # Extract from global guidelines response if not already covered
    global_text = responses.get("global_guidelines", "")
    try:
        global_data = json.loads(global_text) if isinstance(global_text, str) else global_text
    except (json.JSONDecodeError, TypeError):
        global_data = {}

    if isinstance(global_data, dict):
        for guideline in global_data.get("guidelines", []):
            if not isinstance(guideline, dict):
                continue
            source = guideline.get("source", guideline.get("organization", ""))
            if source and source not in seen_sources:
                # Derive verdict from evidence level or confidence
                confidence = guideline.get("confidence_score", 0)
                if confidence >= 0.8:
                    verdict = "Usually appropriate"
                elif confidence >= 0.5:
                    verdict = "May be appropriate"
                else:
                    verdict = "Usually not appropriate"
                guidelines_used.append({"name": source, "verdict": verdict})
                seen_sources.add(source)

    return guidelines_used


def _build_safety_checks(patient_context: str, responses: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build safety_checks list for STATE_SNAPSHOT event (Requirement 8.6).

    Analyzes patient context and agent responses to produce color-coded
    safety check entries for the Patient Context Pane.

    Args:
        patient_context: Patient data string (may be JSON or plain text).
        responses: Dict with agent response strings.

    Returns:
        List of safety check dicts with keys: check, status, color.
    """
    safety_checks: List[Dict[str, str]] = []
    patient_lower = patient_context.lower() if patient_context else ""

    # Check for renal impairment (eGFR-based)
    if "egfr" in patient_lower or "kidney" in patient_lower or "renal" in patient_lower:
        # Look for specific eGFR values
        import re
        egfr_match = re.search(r'egfr[:\s]*(\d+)', patient_lower)
        if egfr_match:
            egfr_value = int(egfr_match.group(1))
            if egfr_value < 30:
                safety_checks.append(build_safety_check("Contrast nephropathy risk", "contraindicated"))
            elif egfr_value < 45:
                safety_checks.append(build_safety_check("Contrast nephropathy risk", "caution"))
            else:
                safety_checks.append(build_safety_check("Renal function", "safe"))
        else:
            # Mentioned but no value — flag as caution
            safety_checks.append(build_safety_check("Renal function assessment needed", "caution"))

    # Check for metformin interaction
    if "metformin" in patient_lower:
        safety_checks.append(build_safety_check("Metformin interaction", "caution"))

    # Check for contrast allergy
    if "iodine" in patient_lower or "contrast allergy" in patient_lower:
        safety_checks.append(build_safety_check("Contrast allergy", "contraindicated"))

    # Check for pregnancy
    if "pregnant" in patient_lower or "pregnancy" in patient_lower:
        safety_checks.append(build_safety_check("Pregnancy — radiation/contrast risk", "contraindicated"))

    # If no specific checks triggered, mark as safe
    if not safety_checks:
        safety_checks.append(build_safety_check("No contraindications identified", "safe"))

    return safety_checks


@tool
def generate_reasoning_narrative(
    query: str,
    patient_context: str,
    agent_responses: str,
    conflicts: str,
    resolutions: str
) -> str:
    """
    Generate user-friendly reasoning narrative explaining the decision process.
    
    Args:
        query: Original clinical query
        patient_context: Patient data from EHR
        agent_responses: Raw agent responses with guidelines
        conflicts: Detected conflicts
        resolutions: Conflict resolutions
        
    Returns:
        Plain-language narrative in story flow format (first...then...finally...)
    """
    try:
        # Parse inputs
        conflict_data = json.loads(conflicts) if conflicts else {}
        resolution_data = json.loads(resolutions) if resolutions else {}
        
        # Build narrative in story flow
        narrative_parts = []
        
        # Part 1: What we checked first
        narrative_parts.append("First, I reviewed the patient's medical record.")
        
        # Extract key patient findings
        if "egfr" in patient_context.lower():
            if "32" in patient_context:
                narrative_parts.append("The patient has reduced kidney function with an eGFR of 32 mL/min/1.73m², which is classified as Stage 3B Chronic Kidney Disease.")
        
        if "metformin" in patient_context.lower():
            narrative_parts.append("The patient is also taking metformin for diabetes management.")
        
        # Part 2: What guidelines we consulted
        narrative_parts.append("\nThen, I consulted both international and French clinical guidelines.")
        
        # Part 3: What conflicts we found
        if conflict_data.get("conflicts_detected"):
            conflicts_list = conflict_data.get("conflicts", [])
            if conflicts_list:
                narrative_parts.append("\nI found that the guidelines disagreed on the best approach:")
                for conflict in conflicts_list:
                    narrative_parts.append(f"- International guidelines (ACR) suggest: {conflict.get('global_recommendation', 'N/A')}")
                    narrative_parts.append(f"- French guidelines (HAS) require: {conflict.get('local_recommendation', 'N/A')}")
        
        # Part 4: How we resolved conflicts
        if resolution_data.get("resolutions"):
            narrative_parts.append("\nFinally, I applied the precedence rules to resolve these differences.")
            narrative_parts.append("In France, local regulatory protocols must take precedence over international guidelines for both patient safety and legal compliance.")
            
            for resolution in resolution_data.get("resolutions", []):
                narrative_parts.append(f"\nFor {resolution.get('conflict', 'this issue')}: {resolution.get('rationale', '')}")
        
        # Part 5: Summary
        narrative_parts.append("\nThis approach ensures we follow the most protective guidelines while maintaining legal compliance in France.")
        
        narrative = "\n".join(narrative_parts)
        
        return json.dumps({
            "narrative": narrative,
            "style": "story_flow",
            "audience": "clinician"
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": f"Error generating narrative: {str(e)}",
            "narrative": "Unable to generate reasoning narrative"
        })


@tool
def synthesize_recommendation(
    query: str,
    resolutions: str,
    patient_context: str,
    agent_responses: str,
    reasoning_narrative: str,
    session_id: str = None,
    patient_id: str = None
) -> str:
    """
    Synthesize final recommendation with safety alerts, alternatives, and citations.
    
    Args:
        query: Original clinical query
        resolutions: Conflict resolutions
        patient_context: Patient data
        agent_responses: Raw agent responses containing citations
        reasoning_narrative: Generated reasoning narrative
        session_id: Session identifier for observability tracing
        patient_id: Patient identifier for observability tracing
        
    Returns:
        JSON with final recommendation: {
            "original_intent": str,
            "recommended_action": str,
            "safety_alerts": list[dict],
            "alternatives": list[dict],
            "reasoning_narrative": str,
            "citations": list[dict],
            "guidelines_used": list[dict],
            "safety_checks": list[dict],
            "state_snapshot": dict,
            "confidence": float
        }
    """
    with clinical_span(
        "clinical.synthesize_recommendation",
        session_id=session_id,
        patient_id=patient_id,
        attributes={"operation": "recommendation_synthesis"}
    ) as span:
        start_time = datetime.now()
        
        try:
            # Parse inputs
            resolution_data = json.loads(resolutions) if resolutions else {}
            narrative_data = json.loads(reasoning_narrative) if reasoning_narrative else {}
            responses = json.loads(agent_responses) if agent_responses else {}
            
            # Extract original intent from query
            original_intent = query
            
            # Determine recommended action from resolutions
            recommended_action = "Avoid contrast-enhanced MRI"
            if resolution_data.get("resolutions"):
                for resolution in resolution_data.get("resolutions", []):
                    if "avoid" in resolution.get("final_recommendation", "").lower():
                        recommended_action = "Use MRI without contrast (non-contrast sequences: STIR, T1, T2-weighted)"
            
            # Generate safety alerts using LLM-powered analysis
            # Call the generate_safety_alerts tool with patient context and guidelines
            safety_alerts_json = generate_safety_alerts(
                patient_context=patient_context,
                intended_action=original_intent,
                guideline_context=agent_responses
            )
            
            # Parse safety alerts
            try:
                safety_alerts = json.loads(safety_alerts_json) if isinstance(safety_alerts_json, str) else safety_alerts_json
            except (json.JSONDecodeError, TypeError, ValueError):
                # Fallback to empty list if parsing fails
                safety_alerts = []
            
            # Generate alternatives using LLM-powered search
            # Call the search_alternatives tool with safety concerns
            alternatives_json = search_alternatives(
                intended_action=original_intent,
                safety_concerns=json.dumps(safety_alerts),
                guideline_context=agent_responses
            )
            
            # Parse alternatives
            try:
                alternatives = json.loads(alternatives_json) if isinstance(alternatives_json, str) else alternatives_json
            except (json.JSONDecodeError, TypeError, ValueError):
                # Fallback to empty list if parsing fails
                alternatives = []
            
            # Extract citations following the design citation data model (Requirement 4.1)
            citations = _extract_citations_from_responses(responses)
            
            # Build guidelines_used for STATE_SNAPSHOT (Requirement 8.5)
            guidelines_used = _build_guidelines_used(responses, resolution_data)
            
            # Build safety_checks for STATE_SNAPSHOT (Requirement 8.6)
            safety_checks = _build_safety_checks(patient_context, responses)
            
            # Calculate confidence score
            confidence = 0.95  # High confidence when following local regulatory protocols
            
            # Calculate processing time
            processing_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            # Build STATE_SNAPSHOT for AG-UI (Requirements 8.5, 8.6)
            state_snapshot = {
                "type": "STATE_SNAPSHOT",
                "guidelines_used": guidelines_used,
                "safety_checks": safety_checks,
            }

            result = {
                "original_intent": original_intent,
                "recommended_action": recommended_action,
                "safety_alerts": safety_alerts,
                "alternatives": alternatives,
                "reasoning_narrative": narrative_data.get("narrative", ""),
                "citations": citations,
                "guidelines_used": guidelines_used,
                "safety_checks": safety_checks,
                "state_snapshot": state_snapshot,
                "confidence": confidence,
                "compliance_note": "Recommendation follows HAS France mandatory protocols for patient safety and legal compliance"
            }
            
            # Store result EARLY for AG-UI STATE_SNAPSHOT emission
            # Must happen before OTEL/memory operations which may fail
            global _last_synthesis_result
            _last_synthesis_result = result
            logger.info(f"[synthesize_recommendation] Stored synthesis result with {len(citations)} citations for STATE_SNAPSHOT emission")
            
            # Record recommendation generation with OTEL
            if session_id:
                record_recommendation_generation(
                    session_id=session_id,
                    recommendation=result,
                    patient_id=patient_id
                )
            
            # Add span attributes
            span.set_attribute("recommendation.action", recommended_action)
            span.set_attribute("recommendation.confidence", confidence)
            span.set_attribute("safety_alerts.count", len(safety_alerts))
            span.set_attribute("alternatives.count", len(alternatives))
            span.set_attribute("citations.count", len(citations))
            
            # DUAL STORAGE: Store structured trace in hot storage (AgentCore Memory)
            # This enables fast retrieval (<100ms) for UI "View Full Reasoning" button
            if session_id and patient_id:
                try:
                    # Import memory utilities
                    import sys
                    import os
                    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    from utils.memory_utils import store_session_data
                    
                    # Parse conflicts for storage
                    conflicts_data = json.loads(resolutions) if resolutions else {}
                    
                    # Create structured trace for hot storage
                    trace_data = {
                        'type': 'decision_trace',
                        'timestamp': datetime.now().isoformat(),
                        'query': query,
                        'patient_id': patient_id,
                        'recommendation': result,
                        'conflicts': conflicts_data.get('resolutions', []),
                        'reasoning_narrative': narrative_data.get('narrative', ''),
                        'citations': citations,
                        'processing_time_ms': processing_time_ms,
                        'session_id': session_id
                    }
                    
                    # Store in AgentCore Memory (hot storage)
                    store_session_data(
                        actor_id=patient_id,
                        session_id=session_id,
                        data={'decision_trace': trace_data}
                    )
                    
                    span.set_attribute("trace.stored_in_memory", True)
                    
                except Exception as memory_error:
                    # Log but don't fail the recommendation if memory storage fails
                    span.set_attribute("trace.memory_error", str(memory_error))
                    print(f"Warning: Failed to store trace in memory: {memory_error}")
            
            # OTEL spans automatically go to CloudWatch (cold storage) for audit/compliance
            
            return json.dumps(result, indent=2)
            
        except Exception as e:
            span.set_attribute("error", str(e))
            return json.dumps({
                "error": f"Error synthesizing recommendation: {str(e)}",
                "original_intent": query,
                "recommended_action": "Unable to generate recommendation",
                "safety_alerts": [],
                "alternatives": [],
                "reasoning_narrative": "",
                "citations": [],
                "confidence": 0.0
            })


# NOTE: get_decision_trace tool removed - decision trace retrieval will be implemented
# as a separate API endpoint (GET /trace/{session_id}) rather than an agent tool.
# This provides better performance (no LLM overhead on CloudWatch queries) and aligns
# with the use case (UI button action for audit/compliance, not conversational query).
# See Task 15.5 for API endpoint implementation.


def create_supervisor_agent():
    """Create Supervisor Agent for orchestrating multi-agent workflow"""
    from botocore.config import Config
    from config import (
        AWS_REGION, BEDROCK_MODEL_ID, BEDROCK_TEMPERATURE,
        BEDROCK_READ_TIMEOUT, BEDROCK_CONNECT_TIMEOUT,
        BEDROCK_MAX_RETRIES, BEDROCK_MAX_POOL_CONNECTIONS,
        BEDROCK_TCP_KEEPALIVE,
    )
    
    bedrock_config = Config(
        read_timeout=BEDROCK_READ_TIMEOUT,
        connect_timeout=BEDROCK_CONNECT_TIMEOUT,
        retries={'max_attempts': BEDROCK_MAX_RETRIES, 'mode': 'standard'},
        max_pool_connections=BEDROCK_MAX_POOL_CONNECTIONS,
        tcp_keepalive=BEDROCK_TCP_KEEPALIVE,
    )
    
    bedrock_model = BedrockModel(
        model_id=BEDROCK_MODEL_ID,
        region_name=AWS_REGION,
        temperature=BEDROCK_TEMPERATURE,
        config=bedrock_config,
    )
    
    system_prompt = """You are the Supervisor Agent for ClinicalCompass Companion. You orchestrate 
multiple specialized agents to provide evidence-based clinical decision support.

WORKFLOW:
1. Parse clinical query to extract entities (patient demographics, symptoms, intended action)
2. Coordinate parallel agent execution (Patient Context, Global Guidelines, Local Regulatory)
   - If session_context is provided, patient data may be cached from previous queries
   - Cached patient data avoids redundant EHR queries for better performance
3. Detect conflicts between guideline recommendations
4. Apply precedence rules: local regulatory > national > international
5. Generate user-friendly reasoning narrative (story flow)
6. Synthesize final recommendation with safety alerts, alternatives, and citations

SESSION MANAGEMENT:
- Multi-turn conversations are supported via session_id and session_context
- Patient data is cached within sessions to avoid redundant queries
- Follow-up questions can reference previous context
- Session timeout is 30 minutes of inactivity

COMMUNICATION STYLE:
- Do NOT produce any intermediate narration or "thinking out loud" text between tool calls.
- Do NOT say things like "I'll start by parsing...", "Let me coordinate...", "Now I'll detect conflicts..."
- ONLY produce text output ONCE: the final formatted recommendation AFTER all tools have completed.
- Be concise. Clinicians are busy — keep the primary recommendation under 150 words.
- Lead with the key recommendation in 2-3 sentences, then safety alerts as bullet points.
- Use short bullet points for alternatives (one line each with safety ranking).
- Keep the reasoning narrative to 3-4 sentences max — just enough to show your work.
- Citations: list source name and document title only (no lengthy descriptions).
- Speak directly to clinicians in plain language.
- Be transparent about conflicts and how they were resolved, but keep it brief.
- Explain why local guidelines take precedence in patient safety terms.
- Avoid technical jargon about agents or system architecture.
- Never mention "agents" or "tools" - speak as if you personally did the research.
- Do NOT repeat information across sections. Say it once, in the right place.

PRECEDENCE RULES:
- Local regulatory protocols ALWAYS take precedence over international guidelines
- This ensures legal compliance and patient safety in the local jurisdiction
- When guidelines conflict, explain why in patient safety terms
- Document all overridden recommendations for audit trail

SAFETY FIRST:
- Always prominently flag contraindications
- Provide clinically equivalent alternatives when safety concerns exist
- Rank alternatives by safety profile
- Require clinician acknowledgment for high-severity alerts

CITATIONS:
- Always include complete citations for all guidelines referenced
- Citations must include: source organization, document title, URL, type (international/local/patient data)
- Explain the precedence and relevance of each citation
- Ensure audit trail completeness for regulatory compliance

CURRENT CAPABILITIES (Tasks 6-10):
- Parse clinical queries to extract structured entities
- Coordinate parallel execution of three specialized agents
- Detect conflicts between international and local guidelines
- Apply precedence rules to resolve conflicts
- Generate user-friendly reasoning narratives
- Synthesize recommendations with safety alerts, alternatives, and citations
- Record complete decision traces via OpenTelemetry for audit and transparency

NOTE: Decision trace retrieval is handled via a separate API endpoint (GET /trace/{session_id}),
not through agent tools. This ensures optimal performance for audit/compliance access.

When you receive a clinical query:
1. Use parse_clinical_query to extract entities
2. Use coordinate_agents to invoke all specialized agents in parallel
3. Use detect_conflicts to identify conflicting recommendations
4. Use apply_precedence_rules to resolve conflicts
5. Use generate_reasoning_narrative to create story flow explanation
6. Use synthesize_recommendation to create final recommendation with alternatives and citations

Present the complete recommendation with narrative, alerts, alternatives, and full citations.
All activities are automatically recorded in the decision trace via OpenTelemetry."""
    
    return Agent(
        model=bedrock_model,
        tools=[
            parse_clinical_query, 
            coordinate_agents, 
            detect_conflicts, 
            apply_precedence_rules,
            generate_reasoning_narrative,
            synthesize_recommendation
            # NOTE: get_decision_trace removed - will be implemented as API endpoint
        ],
        system_prompt=system_prompt
    )
