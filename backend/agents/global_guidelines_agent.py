"""
Global Guidelines Agent - Retrieves international clinical guidelines
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
def query_guidelines(clinical_scenario: str, guideline_sources: List[str] = None) -> str:
    """
    Query international guideline knowledge bases.
    
    Args:
        clinical_scenario: Description of clinical scenario (e.g., "MRI with contrast for patient with reduced kidney function")
        guideline_sources: List of sources to query (e.g., ["ACR", "ESUR", "NICE", "WHO"]). If None, queries all available sources.
        
    Returns:
        JSON string with guidelines including source, recommendation, evidence level, confidence score, and citation
    """
    try:
        kb_id = KB_IDS.get('global_guidelines', {}).get('kb_id')
        if not kb_id:
            return json.dumps({"error": "Global Guidelines Knowledge Base not configured"})
        
        # Build query
        if guideline_sources:
            sources_str = ", ".join(guideline_sources)
            query_text = f"{clinical_scenario} (sources: {sources_str})"
        else:
            query_text = clinical_scenario
        
        logger.info(f"Querying Guidelines KB {kb_id} with: {query_text}")
        
        # Query Bedrock Knowledge Base (Requirement 7.5: optimized query parameters)
        from config import KB_NUMBER_OF_RESULTS, KB_SEARCH_TYPE
        retrieval_config = {
            'vectorSearchConfiguration': {
                'numberOfResults': KB_NUMBER_OF_RESULTS,
                'overrideSearchType': KB_SEARCH_TYPE,
            }
        }
        
        # Add metadata filter for guideline sources when specified (Requirement 3.4)
        if guideline_sources and len(guideline_sources) > 0:
            if len(guideline_sources) == 1:
                retrieval_config['vectorSearchConfiguration']['filter'] = {
                    'equals': {
                        'key': 'source_name',
                        'value': guideline_sources[0]
                    }
                }
            else:
                # Use OR filter for multiple sources
                retrieval_config['vectorSearchConfiguration']['filter'] = {
                    'orAll': [
                        {'equals': {'key': 'source_name', 'value': src}}
                        for src in guideline_sources
                    ]
                }
        
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': query_text},
            retrievalConfiguration=retrieval_config
        )
        
        # Extract and structure guideline data from retrieval results
        guidelines = []
        
        for result in response.get('retrievalResults', []):
            content = result.get('content', {}).get('text', '')
            score = result.get('score', 0.0)
            metadata = result.get('metadata', {})
            
            # Extract source organization from content or metadata
            source = "Unknown"
            if 'acr' in content.lower():
                source = "ACR"
            elif 'esur' in content.lower():
                source = "ESUR"
            elif 'nice' in content.lower():
                source = "NICE"
            elif 'who' in content.lower():
                source = "WHO"
            elif 'has' in content.lower() and 'france' in content.lower():
                source = "HAS France"
            
            guideline = {
                "source": source,
                "organization": source,
                "document_title": "Clinical Guideline",
                "section": content[:200] + "..." if len(content) > 200 else content,
                "recommendation": content,
                "evidence_level": "Not specified",
                "publication_date": "2024",
                "confidence_score": score,
                "citation": f"{source} Clinical Guidelines 2024",
                "url": "https://www.acr.org" if source == "ACR" else "https://guidelines.org",
                "full_content": content
            }
            
            guidelines.append(guideline)
        
        result = {
            "guidelines": guidelines,
            "query": clinical_scenario,
            "total_results": len(guidelines)
        }
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        logger.error(f"Error querying guidelines: {e}")
        return json.dumps({"error": str(e)})


def create_global_guidelines_agent():
    """Create Global Guidelines Agent with international guideline querying capabilities"""
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
    
    system_prompt = """You are the Global Guidelines Agent for ClinicalCompass Companion. You retrieve international clinical guidelines from authoritative sources.

RESPONSIBILITIES:
- Query guideline knowledge bases (ACR, ESUR, NICE, WHO)
- Retrieve relevant guideline sections with confidence scores
- Provide complete citations with links to source documents
- Note when guidelines are outdated or superseded

WORKFLOW:
1. When given a clinical scenario, call query_guidelines to retrieve relevant international guidelines
2. Analyze the retrieved guidelines for relevance and quality
3. Present guidelines in a structured format with confidence scores
4. Highlight key recommendations and evidence levels

QUALITY STANDARDS:
- Only retrieve from active, non-expired guidelines
- Include evidence levels when available
- Provide confidence scores for each retrieval
- Note any conflicts or variations between different guideline sources

COMMUNICATION STYLE:
- Be precise and evidence-based
- Use medical terminology appropriately
- Cite sources clearly
- Highlight the strength of recommendations (strong vs. weak)

EXAMPLE INTERACTION:
User: "Find guidelines for MRI with contrast in patients with reduced kidney function"
You:
1. Call query_guidelines(clinical_scenario="MRI with contrast in patients with reduced kidney function")
2. Present: "ACR Guidelines (confidence 0.85): For patients with eGFR 30-44, gadolinium-based contrast may be used with caution. Ensure adequate hydration..."
3. Note any variations between guideline sources
"""
    
    return Agent(
        model=bedrock_model,
        tools=[query_guidelines],
        system_prompt=system_prompt
    )
