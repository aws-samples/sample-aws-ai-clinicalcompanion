"""
Patient Context Agent - Retrieves patient-specific data from EHR knowledge base
"""

import json
import boto3
import logging
from typing import Dict, Any
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
def query_ehr(patient_id: str, query_context: str) -> str:
    """
    Query EHR knowledge base for patient data.
    
    Args:
        patient_id: Patient identifier (e.g., "001", "002", "003")
        query_context: Clinical context to guide retrieval (e.g., "kidney function for contrast imaging")
        
    Returns:
        JSON string with patient data including demographics, lab results, medications, allergies, conditions
    """
    try:
        kb_id = KB_IDS.get('ehr_mock', {}).get('kb_id')
        if not kb_id:
            return json.dumps({"error": "EHR Knowledge Base not configured"})
        
        # Build query combining patient ID and context for semantic search
        # Include patient name if available for better retrieval accuracy
        query_text = f"Patient ID {patient_id} patient_{patient_id}: {query_context}"
        
        logger.info(f"Querying EHR KB {kb_id} with: {query_text}")
        
        # Query Bedrock Knowledge Base (Requirement 7.5: optimized query parameters)
        from config import KB_SEARCH_TYPE
        retrieval_config = {
            'vectorSearchConfiguration': {
                'numberOfResults': 5,  # Use more results for patient data to ensure we find the right patient
                'overrideSearchType': KB_SEARCH_TYPE,
            }
        }
        
        # Note: metadata filtering is not used because KB documents don't have
        # structured metadata. Rely on semantic search with patient ID in query text.
        
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': query_text},
            retrievalConfiguration=retrieval_config
        )
        
        # Extract and structure patient data from retrieval results
        patient_data = {
            "patient_id": patient_id,
            "demographics": {},
            "lab_results": [],
            "medications": [],
            "allergies": [],
            "conditions": [],
            "data_source": "EHR Mock Knowledge Base",
            "timestamp": "",
            "raw_results": []
        }
        
        # Process retrieval results
        for result in response.get('retrievalResults', []):
            content = result.get('content', {}).get('text', '')
            score = result.get('score', 0.0)
            
            patient_data['raw_results'].append({
                "content": content,
                "confidence_score": score
            })
        
        return json.dumps(patient_data, indent=2)
        
    except Exception as e:
        logger.error(f"Error querying EHR: {e}")
        return json.dumps({"error": str(e)})


@tool
def extract_clinical_values(patient_data: str) -> str:
    """
    Extract structured clinical values from patient data for easier analysis.
    
    This tool parses the raw EHR data and extracts key clinical values like lab results,
    medications, and conditions into a structured format. It does NOT interpret safety -
    that's the job of the Supervisor Agent using clinical guidelines.
    
    Args:
        patient_data: JSON string of patient data from query_ehr
        
    Returns:
        JSON string with extracted clinical values in structured format
    """
    try:
        data = json.loads(patient_data)
        
        extracted_values = {
            "patient_id": data.get('patient_id', 'unknown'),
            "lab_results": {},
            "medications": [],
            "allergies": [],
            "conditions": [],
            "demographics": {}
        }
        
        # Parse raw results to extract structured values
        for result in data.get('raw_results', []):
            content = result.get('content', '')
            content_lower = content.lower()
            
            # Extract eGFR values
            import re
            egfr_match = re.search(r'egfr[:\s]+(\d+(?:\.\d+)?)', content, re.IGNORECASE)
            if egfr_match:
                extracted_values['lab_results']['eGFR'] = {
                    "value": float(egfr_match.group(1)),
                    "unit": "mL/min/1.73m²",
                    "source": "EHR"
                }
            
            # Extract creatinine values
            creat_match = re.search(r'creatinine[:\s]+(\d+(?:\.\d+)?)', content, re.IGNORECASE)
            if creat_match:
                extracted_values['lab_results']['creatinine'] = {
                    "value": float(creat_match.group(1)),
                    "unit": "mg/dL",
                    "source": "EHR"
                }
            
            # Extract medications (look for common patterns)
            if 'medication' in content_lower or 'taking' in content_lower:
                # Extract medication names (simplified - could be enhanced)
                med_keywords = ['metformin', 'aspirin', 'lisinopril', 'atorvastatin', 'insulin']
                for med in med_keywords:
                    if med in content_lower:
                        extracted_values['medications'].append({
                            "name": med.capitalize(),
                            "source": "EHR"
                        })
            
            # Extract allergies
            if 'allergy' in content_lower or 'allergic' in content_lower:
                extracted_values['allergies'].append({
                    "description": content,
                    "source": "EHR"
                })
            
            # Extract conditions
            condition_keywords = ['diabetes', 'hypertension', 'ckd', 'kidney disease', 'heart disease']
            for condition in condition_keywords:
                if condition in content_lower:
                    extracted_values['conditions'].append({
                        "name": condition.title(),
                        "source": "EHR"
                    })
        
        # Remove duplicates from lists
        extracted_values['medications'] = list({json.dumps(m, sort_keys=True): m for m in extracted_values['medications']}.values())
        extracted_values['conditions'] = list({json.dumps(c, sort_keys=True): c for c in extracted_values['conditions']}.values())
        
        return json.dumps(extracted_values, indent=2)
        
    except Exception as e:
        logger.error(f"Error extracting clinical values: {e}")
        return json.dumps({"error": str(e)})


def create_patient_context_agent():
    """Create Patient Context Agent with EHR querying and value extraction capabilities"""
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
    
    system_prompt = """You are the Patient Context Agent for ClinicalCompass Companion. You retrieve patient-specific data from the EHR and extract structured clinical values.

RESPONSIBILITIES:
- Query EHR knowledge base for relevant patient data
- Extract structured clinical values (lab results, medications, allergies, conditions)
- Present data in a clear, structured format for analysis by other agents

IMPORTANT: You do NOT interpret safety or make clinical recommendations. That's the job of the 
Supervisor Agent, which will analyze your data alongside clinical guidelines to determine safety.

WORKFLOW:
1. When given a patient ID and clinical context, call query_ehr to retrieve patient data
2. Call extract_clinical_values to parse the raw data into structured format
3. Present the structured data clearly

DATA EXTRACTION FOCUS:
- Extract quantitative values (eGFR, creatinine, etc.) with units
- List all medications found in the record
- Document allergies if present
- Note chronic conditions
- Always include data source and confidence

COMMUNICATION STYLE:
- Be factual and objective
- Present data without interpretation
- Use medical terminology appropriately
- Note when data is missing or incomplete
- Let other agents handle safety analysis

EXAMPLE INTERACTION:
User: "Get patient data for patient 001 relevant to contrast imaging"
You: 
1. Call query_ehr(patient_id="001", query_context="kidney function, medications, allergies for contrast imaging")
2. Call extract_clinical_values with the retrieved data
3. Present: "Patient 001 data retrieved:
   - Lab Results: eGFR 32 mL/min/1.73m²
   - Medications: Metformin, Lisinopril
   - Conditions: Type 2 Diabetes, CKD Stage 3B
   - Allergies: None documented
   
   Data source: EHR Mock Knowledge Base"

Note: Safety analysis will be performed by the Supervisor Agent using clinical guidelines.
"""
    
    return Agent(
        model=bedrock_model,
        tools=[query_ehr, extract_clinical_values],
        system_prompt=system_prompt
    )
