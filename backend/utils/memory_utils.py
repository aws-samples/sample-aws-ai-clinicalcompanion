"""
Memory utilities for ESR Chatbot using Short-Term Memory
Provides immediate access to session data without LTM processing delays

Lambda-compatible version: Uses boto3 client instead of bedrock_agentcore library
"""

import json
import os
import logging
import boto3
import sys
from pathlib import Path

# Add deployment directory to path for config access
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'deployment'))
from config import AWS_REGION

logger = logging.getLogger(__name__)

# Memory configuration — resolve STM_MEMORY_ID from:
# 1. Environment variable (set in Dockerfile or Lambda config)
# 2. SSM Parameter Store (set by deployment/scripts/setup_memory.py)
STM_MEMORY_ID = os.environ.get('STM_MEMORY_ID') or None
REGION = os.environ.get('AWS_REGION', AWS_REGION)

if not STM_MEMORY_ID:
    # Try SSM Parameter Store
    try:
        _ssm = boto3.client('ssm', region_name=REGION)
        _resp = _ssm.get_parameter(Name='/clinicalcompass/stm-memory-id')
        _val = _resp['Parameter']['Value']
        if _val and _val != 'PLACEHOLDER' and len(_val) >= 12:
            STM_MEMORY_ID = _val
            os.environ['STM_MEMORY_ID'] = STM_MEMORY_ID
            logger.info(f"Loaded STM_MEMORY_ID from SSM: {STM_MEMORY_ID}")
        else:
            logger.warning(f"SSM has invalid STM_MEMORY_ID: '{_val}' — memory disabled")
    except Exception as e:
        logger.warning(f"Could not load STM_MEMORY_ID from SSM: {e}")

# Initialize boto3 client for bedrock-agentcore
client = None
if STM_MEMORY_ID:
    try:
        client = boto3.client('bedrock-agentcore', region_name=REGION)
        logger.info(f"Initialized bedrock-agentcore client for region {REGION}")
    except Exception as e:
        logger.warning(f"Failed to initialize bedrock-agentcore client: {e}")
        client = None
else:
    logger.warning("STM_MEMORY_ID not set (env var and SSM both empty) - memory functions will not work")

def store_session_data(actor_id: str, session_id: str, data: dict):
    """
    Store session data in STM for immediate retrieval
    
    Args:
        actor_id: User/referral identifier
        session_id: ESR session identifier
        data: Dictionary of session data to store
    """
    if not client:
        logger.warning("Memory client not initialized - skipping store_session_data")
        return
        
    try:
        from datetime import datetime
        
        # Format as conversation for natural storage
        user_msg = f"Session data: {json.dumps(data)}"
        assistant_msg = "Data stored successfully"
        
        # Use boto3 API with correct format
        client.create_event(
            memoryId=STM_MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(),
            payload=[
                {
                    'conversational': {
                        'content': {'text': user_msg},
                        'role': 'USER'
                    }
                },
                {
                    'conversational': {
                        'content': {'text': assistant_msg},
                        'role': 'ASSISTANT'
                    }
                }
            ]
        )
        
        logger.info(f"Stored session data for {actor_id}/{session_id}: {list(data.keys())}")
        
    except Exception as e:
        logger.error(f"Failed to store session data: {e}")
        raise

def get_session_data(actor_id: str, session_id: str) -> dict:
    """
    Retrieve session data from STM immediately (no wait)
    
    Args:
        actor_id: User/referral identifier
        session_id: ESR session identifier
        
    Returns:
        Dictionary of session data
    """
    if not client:
        logger.warning("Memory client not initialized - returning empty session data")
        return {}
        
    try:
        # Get events using boto3 list_events API
        response = client.list_events(
            memoryId=STM_MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=20,
            includePayloads=True
        )
        
        events = response.get('events', [])
        
        # Extract and merge session data from all events
        session_data = {}
        for event in events:
            payload = event.get('payload', [])
            for item in payload:
                conversational = item.get('conversational', {})
                if conversational.get('role') == 'USER':
                    content = conversational.get('content', {}).get('text', '')
                    if 'Session data:' in content:
                        # Extract JSON
                        json_start = content.index('{')
                        json_end = content.rindex('}') + 1
                        data = json.loads(content[json_start:json_end])
                        # Merge (later data overwrites earlier)
                        session_data.update(data)
        
        logger.info(f"Retrieved session data for {actor_id}/{session_id}: {list(session_data.keys())}")
        return session_data
        
    except Exception as e:
        logger.error(f"Failed to get session data: {e}")
        return {}

def store_workflow_event(actor_id: str, session_id: str, event_type: str, data: dict):
    """
    Store workflow event in STM
    
    Args:
        actor_id: User/referral identifier
        session_id: ESR session identifier
        event_type: Type of event (e.g., "SESSION_CREATED", "ACTION")
        data: Event data
    """
    if not client:
        logger.warning("Memory client not initialized - skipping store_workflow_event")
        return
        
    try:
        from datetime import datetime
        
        user_msg = f"{event_type}: {json.dumps(data)}"
        assistant_msg = f"{event_type} recorded"
        
        # Use boto3 API with correct format
        client.create_event(
            memoryId=STM_MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(),
            payload=[
                {
                    'conversational': {
                        'content': {'text': user_msg},
                        'role': 'USER'
                    }
                },
                {
                    'conversational': {
                        'content': {'text': assistant_msg},
                        'role': 'ASSISTANT'
                    }
                }
            ]
        )
        
        logger.info(f"Stored workflow event: {event_type} for {actor_id}/{session_id}")
        
    except Exception as e:
        logger.error(f"Failed to store workflow event: {e}")
        raise

def get_workflow_history(actor_id: str, session_id: str) -> list:
    """
    Get workflow history from STM
    
    Args:
        actor_id: User/referral identifier
        session_id: ESR session identifier
        
    Returns:
        List of workflow events
    """
    if not client:
        logger.warning("Memory client not initialized - returning empty workflow history")
        return []
        
    try:
        # Get events using boto3 list_events API
        response = client.list_events(
            memoryId=STM_MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=50,
            includePayloads=True
        )
        
        events_list = response.get('events', [])
        
        workflow_events = []
        for event in events_list:
            payload = event.get('payload', [])
            for item in payload:
                conversational = item.get('conversational', {})
                if conversational.get('role') == 'USER':
                    content = conversational.get('content', {}).get('text', '')
                    # Parse event type and data
                    if ':' in content and not content.startswith('Session data:'):
                        event_type, json_str = content.split(':', 1)
                        try:
                            data = json.loads(json_str.strip())
                            workflow_events.append({
                                'type': event_type.strip(),
                                'data': data
                            })
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
        
        logger.info(f"Retrieved {len(workflow_events)} workflow events for {actor_id}/{session_id}")
        return workflow_events
        
    except Exception as e:
        logger.error(f"Failed to get workflow history: {e}")
        return []
