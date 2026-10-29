"""
Clinical Decision Support Agents
Direct agent communication architecture for ESR clinical decision support
"""

from .patient_context_agent import create_patient_context_agent
from .global_guidelines_agent import create_global_guidelines_agent
from .local_regulatory_agent import create_local_regulatory_agent
from .supervisor_agent import create_supervisor_agent

__all__ = [
    'create_patient_context_agent',
    'create_global_guidelines_agent',
    'create_local_regulatory_agent',
    'create_supervisor_agent'
]