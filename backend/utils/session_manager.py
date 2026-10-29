"""
Session Manager for ClinicalCompass Companion
Provides multi-turn conversation support with context persistence using AgentCore Memory
"""

import uuid
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from utils.memory_utils import (
    store_session_data,
    get_session_data,
    store_workflow_event,
    get_workflow_history
)

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages clinical decision support sessions with context persistence.
    
    Features:
    - Unique session ID generation
    - Patient data caching within sessions
    - Context retrieval for follow-up queries
    - Session timeout handling (30 minutes)
    - Session archival for audit
    """
    
    def __init__(self, timeout_minutes: int = 30):
        """
        Initialize session manager.
        
        Args:
            timeout_minutes: Session timeout in minutes (default: 30)
        """
        self.timeout_minutes = timeout_minutes
    
    def create_session(
        self,
        patient_id: str,
        initial_query: str = None,
        session_id: str | None = None,
    ) -> str:
        """
        Create a new clinical session.
        
        Args:
            patient_id: Patient identifier
            initial_query: Initial clinical query (optional)
            
        Returns:
            str: Unique session ID
        """
        session_id = session_id or str(uuid.uuid4())
        
        # Initialize session context
        session_context = {
            'session_id': session_id,
            'patient_id': patient_id,
            'created_at': datetime.now().isoformat(),
            'last_activity': datetime.now().isoformat(),
            'query_count': 0,
            'initial_query': initial_query,
            'patient_data_cache': {},
            'conversation_history': []
        }
        
        # Store in AgentCore Memory
        store_session_data(patient_id, session_id, session_context)
        
        # Record session creation event
        store_workflow_event(patient_id, session_id, "SESSION_CREATED", {
            'session_id': session_id,
            'patient_id': patient_id,
            'timestamp': datetime.now().isoformat()
        })
        
        logger.info(f"Created session: {session_id} for patient: {patient_id}")
        
        return session_id
    
    def get_session_context(self, patient_id: str, session_id: str) -> Dict[str, Any]:
        """
        Retrieve session context for follow-up queries.
        
        Args:
            patient_id: Patient identifier
            session_id: Session identifier
            
        Returns:
            dict: Session context including cached patient data and conversation history
        """
        try:
            session_data = get_session_data(patient_id, session_id)
            
            if not session_data:
                logger.warning(f"No session context found for {session_id}")
                return {}
            
            # Check session timeout
            last_activity = session_data.get('last_activity')
            if last_activity:
                last_activity_time = datetime.fromisoformat(last_activity)
                timeout_threshold = datetime.now() - timedelta(minutes=self.timeout_minutes)
                
                if last_activity_time < timeout_threshold:
                    logger.warning(f"Session {session_id} has timed out")
                    return {
                        'session_id': session_id,
                        'status': 'timeout',
                        'message': f'Session inactive for {self.timeout_minutes} minutes. Would you like to continue or start a new session?'
                    }
            
            logger.info(f"Retrieved session context for {session_id}")
            return session_data
            
        except Exception as e:
            logger.error(f"Failed to retrieve session context: {e}")
            return {}
    
    def update_session_activity(self, patient_id: str, session_id: str, query: str, response: Dict[str, Any]):
        """
        Update session with new query and response.
        
        Args:
            patient_id: Patient identifier
            session_id: Session identifier
            query: Clinical query
            response: Agent response (dict or string)
        """
        try:
            # Get current session data
            session_data = get_session_data(patient_id, session_id)
            
            if not session_data:
                logger.warning(f"Session {session_id} not found, creating new context")
                session_data = {
                    'session_id': session_id,
                    'patient_id': patient_id,
                    'created_at': datetime.now().isoformat(),
                    'query_count': 0,
                    'patient_data_cache': {},
                    'conversation_history': []
                }
            
            # Update activity timestamp
            session_data['last_activity'] = datetime.now().isoformat()
            session_data['query_count'] = session_data.get('query_count', 0) + 1
            
            # Parse response if it's a string
            if isinstance(response, str):
                try:
                    response_dict = json.loads(response)
                except (json.JSONDecodeError, TypeError, ValueError):
                    response_dict = {"text": response}
            else:
                response_dict = response
            
            # Add to conversation history
            conversation_entry = {
                'timestamp': datetime.now().isoformat(),
                'query': query,
                'response_summary': response_dict.get('recommended_action', response_dict.get('text', '')),
                'confidence': response_dict.get('confidence', 0.0)
            }
            
            if 'conversation_history' not in session_data:
                session_data['conversation_history'] = []
            
            session_data['conversation_history'].append(conversation_entry)
            
            # Store updated session data
            store_session_data(patient_id, session_id, session_data)
            
            # Record activity event
            store_workflow_event(patient_id, session_id, "QUERY_PROCESSED", {
                'query_number': session_data['query_count'],
                'timestamp': datetime.now().isoformat()
            })
            
            logger.info(f"Updated session {session_id} - query count: {session_data['query_count']}")
            
        except Exception as e:
            logger.error(f"Failed to update session activity: {e}")
    
    def cache_patient_data(self, patient_id: str, session_id: str, patient_data: Dict[str, Any]):
        """
        Cache patient data within session to avoid redundant KB queries.
        
        Args:
            patient_id: Patient identifier
            session_id: Session identifier
            patient_data: Patient data from EHR
        """
        try:
            session_data = get_session_data(patient_id, session_id)
            
            if not session_data:
                logger.warning(f"Session {session_id} not found for patient data caching")
                return
            
            # Update patient data cache
            session_data['patient_data_cache'] = {
                'data': patient_data,
                'cached_at': datetime.now().isoformat()
            }
            
            # Store updated session data
            store_session_data(patient_id, session_id, session_data)
            
            logger.info(f"Cached patient data for session {session_id}")
            
        except Exception as e:
            logger.error(f"Failed to cache patient data: {e}")
    
    def get_cached_patient_data(self, patient_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached patient data from session.
        
        Args:
            patient_id: Patient identifier
            session_id: Session identifier
            
        Returns:
            dict: Cached patient data or None if not available
        """
        try:
            session_data = get_session_data(patient_id, session_id)
            
            if not session_data:
                return None
            
            patient_cache = session_data.get('patient_data_cache', {})
            
            if not patient_cache:
                return None
            
            # Check cache age (refresh if older than 1 hour)
            cached_at = patient_cache.get('cached_at')
            if cached_at:
                cached_time = datetime.fromisoformat(cached_at)
                cache_age = datetime.now() - cached_time
                
                if cache_age > timedelta(hours=1):
                    logger.info(f"Patient data cache expired for session {session_id}")
                    return None
            
            logger.info(f"Retrieved cached patient data for session {session_id}")
            return patient_cache.get('data')
            
        except Exception as e:
            logger.error(f"Failed to retrieve cached patient data: {e}")
            return None
    
    def check_session_timeout(self, patient_id: str, session_id: str) -> Dict[str, Any]:
        """
        Check if session has timed out.
        
        Args:
            patient_id: Patient identifier
            session_id: Session identifier
            
        Returns:
            dict: Status information with timeout flag
        """
        try:
            session_data = get_session_data(patient_id, session_id)
            
            if not session_data:
                return {
                    'session_id': session_id,
                    'status': 'not_found',
                    'timed_out': True,
                    'message': 'Session not found. Please start a new session.'
                }
            
            last_activity = session_data.get('last_activity')
            if not last_activity:
                return {
                    'session_id': session_id,
                    'status': 'active',
                    'timed_out': False
                }
            
            last_activity_time = datetime.fromisoformat(last_activity)
            timeout_threshold = datetime.now() - timedelta(minutes=self.timeout_minutes)
            
            if last_activity_time < timeout_threshold:
                inactive_minutes = int((datetime.now() - last_activity_time).total_seconds() / 60)
                
                return {
                    'session_id': session_id,
                    'status': 'timeout',
                    'timed_out': True,
                    'inactive_minutes': inactive_minutes,
                    'message': f'Session inactive for {inactive_minutes} minutes. Would you like to continue or start a new session?'
                }
            
            return {
                'session_id': session_id,
                'status': 'active',
                'timed_out': False
            }
            
        except Exception as e:
            logger.error(f"Failed to check session timeout: {e}")
            return {
                'session_id': session_id,
                'status': 'error',
                'timed_out': False,
                'error': str(e)
            }
    
    def archive_session(self, patient_id: str, session_id: str, reason: str = "session_closed"):
        """
        Archive complete session data for audit purposes.
        
        Args:
            patient_id: Patient identifier
            session_id: Session identifier
            reason: Reason for archival (default: "session_closed")
        """
        try:
            # Get complete session data
            session_data = get_session_data(patient_id, session_id)
            
            if not session_data:
                logger.warning(f"No session data to archive for {session_id}")
                return
            
            # Get workflow history
            workflow_history = get_workflow_history(patient_id, session_id)
            
            # Create archive record
            archive_data = {
                'session_id': session_id,
                'patient_id': patient_id,
                'archived_at': datetime.now().isoformat(),
                'archive_reason': reason,
                'session_data': session_data,
                'workflow_history': workflow_history,
                'query_count': session_data.get('query_count', 0),
                'duration_minutes': self._calculate_session_duration(session_data)
            }
            
            # Store archive in AgentCore Memory (LTM)
            # Using a special archive session ID for long-term storage
            archive_session_id = f"archive_{session_id}"
            store_session_data(patient_id, archive_session_id, archive_data)
            
            # Record archival event
            store_workflow_event(patient_id, session_id, "SESSION_ARCHIVED", {
                'session_id': session_id,
                'archive_session_id': archive_session_id,
                'reason': reason,
                'timestamp': datetime.now().isoformat()
            })
            
            logger.info(f"Archived session {session_id} as {archive_session_id}")
            
        except Exception as e:
            logger.error(f"Failed to archive session: {e}")
    
    def _calculate_session_duration(self, session_data: Dict[str, Any]) -> int:
        """
        Calculate session duration in minutes.
        
        Args:
            session_data: Session data dictionary
            
        Returns:
            int: Duration in minutes
        """
        try:
            created_at = session_data.get('created_at')
            last_activity = session_data.get('last_activity')
            
            if not created_at or not last_activity:
                return 0
            
            created_time = datetime.fromisoformat(created_at)
            last_activity_time = datetime.fromisoformat(last_activity)
            
            duration = last_activity_time - created_time
            return int(duration.total_seconds() / 60)
            
        except Exception as e:
            logger.error(f"Failed to calculate session duration: {e}")
            return 0


# Global session manager instance
session_manager = SessionManager(timeout_minutes=30)


def create_session(
    patient_id: str,
    initial_query: str = None,
    session_id: str | None = None,
) -> str:
    """
    Create a new clinical session.
    
    Args:
        patient_id: Patient identifier
        initial_query: Initial clinical query (optional)
        
    Returns:
        str: Unique session ID
    """
    return session_manager.create_session(patient_id, initial_query, session_id)


def get_session_context(patient_id: str, session_id: str) -> Dict[str, Any]:
    """
    Retrieve session context for follow-up queries.
    
    Args:
        patient_id: Patient identifier
        session_id: Session identifier
        
    Returns:
        dict: Session context
    """
    return session_manager.get_session_context(patient_id, session_id)


def update_session_activity(patient_id: str, session_id: str, query: str, response: Dict[str, Any]):
    """
    Update session with new query and response.
    
    Args:
        patient_id: Patient identifier
        session_id: Session identifier
        query: Clinical query
        response: Agent response (dict or string)
    """
    session_manager.update_session_activity(patient_id, session_id, query, response)


def cache_patient_data(patient_id: str, session_id: str, patient_data: Dict[str, Any]):
    """
    Cache patient data within session.
    
    Args:
        patient_id: Patient identifier
        session_id: Session identifier
        patient_data: Patient data from EHR
    """
    session_manager.cache_patient_data(patient_id, session_id, patient_data)


def check_session_timeout(patient_id: str, session_id: str) -> Dict[str, Any]:
    """
    Check if session has timed out.
    
    Args:
        patient_id: Patient identifier
        session_id: Session identifier
        
    Returns:
        dict: Status information
    """
    return session_manager.check_session_timeout(patient_id, session_id)
