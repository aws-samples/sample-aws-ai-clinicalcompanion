"""
Knowledge Base configuration loader.
Reads KB IDs from (in priority order):
1. Environment variables (EHR_KB_ID, GUIDELINES_KB_ID, REGULATORY_KB_ID)
2. SSM Parameter Store (/clinicalcompass/kb/*)
3. Local JSON file (knowledge_base_ids.json)
"""

import os
import json
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

_cached_kb_ids = None


def load_kb_ids() -> Dict[str, Any]:
    """Load Knowledge Base IDs with caching."""
    global _cached_kb_ids
    if _cached_kb_ids is not None:
        return _cached_kb_ids

    _cached_kb_ids = _load_kb_ids_impl()
    return _cached_kb_ids


def _load_kb_ids_impl() -> Dict[str, Any]:
    """Load KB IDs from env vars, SSM, or file."""

    # 1. Environment variables
    ehr_kb_id = os.environ.get('EHR_KB_ID')
    if ehr_kb_id:
        logger.info("Loaded KB IDs from environment variables")
        return {
            'ehr_mock': {'kb_id': ehr_kb_id},
            'global_guidelines': {'kb_id': os.environ.get('GUIDELINES_KB_ID')},
            'local_regulatory': {'kb_id': os.environ.get('REGULATORY_KB_ID')},
        }

    # 2. SSM Parameter Store
    try:
        import boto3
        region = os.environ.get('AWS_REGION', 'us-east-1')
        ssm = boto3.client('ssm', region_name=region)

        ssm_map = {
            'ehr_mock': '/clinicalcompass/kb/cc-ehr-mock/id',
            'global_guidelines': '/clinicalcompass/kb/cc-global-guidelines/id',
            'local_regulatory': '/clinicalcompass/kb/cc-local-regulatory/id',
        }

        result = {}
        found_any = False
        for key, param_name in ssm_map.items():
            try:
                resp = ssm.get_parameter(Name=param_name)
                val = resp['Parameter']['Value']
                if val and val != 'PLACEHOLDER':
                    result[key] = {'kb_id': val}
                    found_any = True
                else:
                    result[key] = {'kb_id': None}
            except Exception:
                result[key] = {'kb_id': None}

        if found_any:
            # Cache in env vars for subsequent imports
            if result.get('ehr_mock', {}).get('kb_id'):
                os.environ['EHR_KB_ID'] = result['ehr_mock']['kb_id']
            if result.get('global_guidelines', {}).get('kb_id'):
                os.environ['GUIDELINES_KB_ID'] = result['global_guidelines']['kb_id']
            if result.get('local_regulatory', {}).get('kb_id'):
                os.environ['REGULATORY_KB_ID'] = result['local_regulatory']['kb_id']
            logger.info("Loaded KB IDs from SSM Parameter Store")
            return result

    except Exception as e:
        logger.warning(f"Could not load KB IDs from SSM: {e}")

    # 3. Local file fallback
    possible_paths = [
        'knowledge_base_ids.json',
        'clinical-compass/knowledge_base_ids.json',
        '../clinical-compass/knowledge_base_ids.json',
        '../knowledge-bases/knowledge_base_ids.json',
    ]

    for path in possible_paths:
        try:
            with open(path, 'r') as f:
                data = json.load(f)
                logger.info(f"Loaded KB IDs from file: {path}")
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            continue

    logger.error("Failed to load KB IDs from any source")
    return {}
