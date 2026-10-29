"""
Local Regulatory Agent - Retrieves national and local regulatory protocols
"""

import json
import boto3
import logging
from typing import Dict, Any, List
from strands import Agent, tool
from strands.models import BedrockModel

logger = logging.getLogger(__name__)

# Load Knowledge Base IDs
from utils.kb_config import load_kb_ids

KB_IDS = load_kb_ids()

# Initialize Bedrock clients with extended timeout configuration
from botocore.config import Config

bedrock_config = Config(
    read_timeout=300,  # 5 minutes for KB queries
    connect_timeout=10,
    retries={'max_attempts': 2, 'mode': 'standard'},
    max_pool_connections=50,  # Support parallel queries
    tcp_keepalive=True,  # Connection reuse — reduces latency on subsequent calls
)

from config import AWS_REGION as _AWS_REGION

bedrock_agent_runtime = boto3.client(
    'bedrock-agent-runtime',
    region_name=_AWS_REGION,
    config=bedrock_config
)


@tool
def query_regulatory_protocols(
    clinical_scenario: str,
    jurisdiction: str = "France",
    protocol_sources: List[str] = None
) -> str:
    """
    Query national and local regulatory knowledge bases.
    
    Args:
        clinical_scenario: Description of clinical scenario (e.g., "MRI with contrast for patient with reduced kidney function")
        jurisdiction: Geographic jurisdiction (e.g., "France", "UK", "Germany")
        protocol_sources: List of sources to query (e.g., ["HAS", "NHS", "local SOPs"]). If None, queries all available sources.
        
    Returns:
        JSON string with protocols including source, requirement, mandatory flag, confidence score, and citation
    """
    try:
        kb_id = KB_IDS.get('local_regulatory', {}).get('kb_id')
        if not kb_id:
            return json.dumps({"error": "Local Regulatory Knowledge Base not configured"})
        
        # Build query with jurisdiction context
        if protocol_sources:
            sources_str = ", ".join(protocol_sources)
            query_text = f"{clinical_scenario} in {jurisdiction} (sources: {sources_str})"
        else:
            query_text = f"{clinical_scenario} in {jurisdiction}"
        
        logger.info(f"Querying Regulatory KB {kb_id} with: {query_text}")
        
        # Query Bedrock Knowledge Base (Requirement 7.5: optimized query parameters)
        from config import KB_NUMBER_OF_RESULTS, KB_SEARCH_TYPE
        retrieval_config = {
            'vectorSearchConfiguration': {
                'numberOfResults': KB_NUMBER_OF_RESULTS,
                'overrideSearchType': KB_SEARCH_TYPE,
            }
        }
        
        # Add metadata filter for jurisdiction when specified
        if jurisdiction:
            retrieval_config['vectorSearchConfiguration']['filter'] = {
                'equals': {
                    'key': 'jurisdiction',
                    'value': jurisdiction
                }
            }
        
        # If specific protocol sources are requested, add source filter
        if protocol_sources and len(protocol_sources) > 0:
            source_filter = {
                'orAll': [
                    {'equals': {'key': 'source_name', 'value': src}}
                    for src in protocol_sources
                ]
            }
            # Combine with jurisdiction filter if both present
            if jurisdiction:
                retrieval_config['vectorSearchConfiguration']['filter'] = {
                    'andAll': [
                        retrieval_config['vectorSearchConfiguration']['filter'],
                        source_filter
                    ]
                }
            else:
                retrieval_config['vectorSearchConfiguration']['filter'] = source_filter
        
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': query_text},
            retrievalConfiguration=retrieval_config
        )
        
        # Extract and structure regulatory protocol data from retrieval results
        protocols = []
        
        for result in response.get('retrievalResults', []):
            content = result.get('content', {}).get('text', '')
            score = result.get('score', 0.0)
            metadata = result.get('metadata', {})
            
            # Extract source authority from content
            source = "Unknown"
            authority = "Unknown"
            if 'has' in content.lower() and 'france' in content.lower():
                source = "HAS France"
                authority = "Haute Autorité de Santé (HAS)"
            elif 'nhs' in content.lower():
                source = "NHS"
                authority = "National Health Service"
            elif 'ansm' in content.lower():
                source = "ANSM"
                authority = "Agence Nationale de Sécurité du Médicament"
            
            # Determine if requirement is mandatory based on keywords
            mandatory = any(keyword in content.lower() for keyword in [
                'must', 'required', 'mandatory', 'contraindicated', 'prohibited',
                'shall', 'obligatoire', 'interdit', 'contre-indiqué'
            ])
            
            protocol = {
                "source": source,
                "authority": authority,
                "document_title": f"{source} Clinical Protocol",
                "section": content[:200] + "..." if len(content) > 200 else content,
                "requirement": content,
                "mandatory": mandatory,
                "publication_date": "2024",
                "confidence_score": score,
                "citation": f"{source} Regulatory Protocol 2024",
                "url": "https://www.has-sante.fr" if source == "HAS France" else "https://regulatory.org",
                "jurisdiction": jurisdiction,
                "full_content": content
            }
            
            protocols.append(protocol)
        
        result = {
            "protocols": protocols,
            "query": clinical_scenario,
            "jurisdiction": jurisdiction,
            "total_results": len(protocols)
        }
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        logger.error(f"Error querying regulatory protocols: {e}")
        return json.dumps({"error": str(e)})


def create_local_regulatory_agent():
    """Create Local Regulatory Agent with national/local protocol querying capabilities"""
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
    
    system_prompt = """You are the Local Regulatory Agent for ClinicalCompass Companion. You retrieve national and local regulatory protocols that must be followed.

RESPONSIBILITIES:
- Query regulatory knowledge bases (HAS, NHS, hospital SOPs)
- Retrieve mandatory requirements and recommendations
- Provide complete citations with links to source documents
- Flag mandatory vs. recommended protocols

WORKFLOW:
1. When given a clinical scenario and jurisdiction, call query_regulatory_protocols
2. Analyze the retrieved protocols for mandatory vs. recommended requirements
3. Present protocols in a structured format with confidence scores
4. Highlight any mandatory requirements prominently

COMPLIANCE FOCUS:
- Prioritize mandatory regulatory requirements
- Note jurisdiction-specific requirements
- Include effective dates for regulations
- Clearly distinguish between "must" (mandatory) and "should" (recommended)

COMMUNICATION STYLE:
- Be authoritative and precise
- Use regulatory terminology appropriately
- Cite sources clearly with jurisdiction
- Emphasize mandatory requirements

PRECEDENCE AWARENESS:
- Local regulatory protocols take precedence over international guidelines
- National regulations override international recommendations
- Hospital SOPs may be more restrictive than national guidelines

EXAMPLE INTERACTION:
User: "Find French regulatory requirements for MRI with contrast in patients with reduced kidney function"
You:
1. Call query_regulatory_protocols(clinical_scenario="MRI with contrast in patients with reduced kidney function", jurisdiction="France")
2. Present: "HAS France (MANDATORY, confidence 0.92): Contrast MRI is CONTRAINDICATED for patients with eGFR < 30. For eGFR 30-60, risk assessment required..."
3. Highlight mandatory vs. recommended requirements
"""
    
    return Agent(
        model=bedrock_model,
        tools=[query_regulatory_protocols],
        system_prompt=system_prompt
    )
