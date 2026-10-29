#!/usr/bin/env python3
"""
Post-CDK Setup Script for ClinicalCompass

Run this AFTER `cdk deploy` and build_and_push_image.py to complete the setup:
1. Read CDK outputs and update deployment/config.py + frontend/config.js
2. Create OpenSearch indices, Bedrock Knowledge Bases and data sources
3. (Reminder) The Docker image must already be in ECR
4. Create AgentCore Memory and store its ID in SSM (before the runtime, see main())
5. Create the AgentCore Runtime and store its ARN in SSM
6. Upload Knowledge Base data and trigger ingestion
7. Upload initial patient data and guidelines to the CDK-managed buckets

Usage:
    python deployment/scripts/post_cdk_setup.py [--skip-config] [--skip-kb] [--skip-memory]
        [--skip-runtime] [--skip-data] [--skip-initial-upload]
"""

import sys
import os
import re
import json
import time
import subprocess
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

STACK_NAME = 'ClinicalCompassStack'
MEMORY_NAME = 'clinicalcompass_mem'
SSM_MEMORY_PARAM = '/clinicalcompass/stm-memory-id'
PROJECT_ROOT = Path(__file__).parent.parent.parent


def get_session():
    """Create boto3 session using config.py profile if available."""
    try:
        from config import AWS_PROFILE, AWS_REGION
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION), AWS_REGION
    except Exception:
        region = os.environ.get('AWS_REGION', 'us-east-1')
        return boto3.Session(region_name=region), region


# ── Step 1: Read CDK outputs and update config files ─────────────

def get_cdk_outputs(session) -> dict:
    """Read CloudFormation outputs from the deployed CDK stack."""
    cfn = session.client('cloudformation')
    try:
        resp = cfn.describe_stacks(StackName=STACK_NAME)
        outputs = {}
        for o in resp['Stacks'][0].get('Outputs', []):
            outputs[o['OutputKey']] = o['OutputValue']
        return outputs
    except ClientError as e:
        logger.error(f"Could not read CDK stack outputs: {e}")
        logger.error("Make sure you've run 'cdk deploy' first.")
        sys.exit(1)


def update_deployment_config(outputs: dict):
    """Update deployment/config.py with CDK output values."""
    config_path = PROJECT_ROOT / 'deployment' / 'config.py'
    if not config_path.exists():
        logger.warning(f"  {config_path} not found — skipping")
        return

    content = config_path.read_text()

    replacements = {
        'ECR_REPOSITORY': 'bedrock-agentcore-clinicalcompass',
    }

    for key, value in replacements.items():
        if not value:
            continue
        # Only replace top-level assignments (start of line)
        content = re.sub(
            rf'^({key})\s*=\s*"[^"]*"',
            f'{key} = "{value}"',
            content,
            count=1,
            flags=re.MULTILINE,
        )

    config_path.write_text(content)
    logger.info(f"  ✓ Updated deployment/config.py")
    for k, v in replacements.items():
        if v:
            logger.info(f"    {k} = {v}")


def update_frontend_config(outputs: dict):
    """Update frontend/config.js with WebSocket URL, HTTP API URL, and Cognito config."""
    ws_url = outputs.get('WebSocketUrl', '')
    http_api_url = outputs.get('HttpApiEndpoint', '')

    if not ws_url and not http_api_url:
        logger.warning("  No WebSocketUrl or HttpApiEndpoint in CDK outputs — skipping frontend config")
        return

    config_path = PROJECT_ROOT / 'frontend' / 'config.js'

    # Create from example if it doesn't exist
    example_path = PROJECT_ROOT / 'frontend' / 'config.example.js'
    if not config_path.exists() and example_path.exists():
        import shutil
        shutil.copy(example_path, config_path)

    if not config_path.exists():
        logger.warning(f"  {config_path} not found — skipping")
        return

    content = config_path.read_text()

    if ws_url:
        content = re.sub(
            r"websocketUrl:\s*'[^']*'",
            f"websocketUrl: '{ws_url}'",
            content,
        )

    # Set HTTP API base URL for patient/guideline REST endpoints
    if http_api_url:
        content = re.sub(
            r"apiBaseUrl:\s*'[^']*'",
            f"apiBaseUrl: '{http_api_url}'",
            content,
        )

    # Add Cognito config
    cognito_pool_id = outputs.get('CognitoUserPoolId', '')
    cognito_client_id = outputs.get('CognitoClientId', '')
    if cognito_pool_id:
        content = re.sub(r"cognitoUserPoolId:\s*'[^']*'", f"cognitoUserPoolId: '{cognito_pool_id}'", content)
    if cognito_client_id:
        content = re.sub(r"cognitoClientId:\s*'[^']*'", f"cognitoClientId: '{cognito_client_id}'", content)

    config_path.write_text(content)
    logger.info(f"  ✓ Updated frontend/config.js")
    if ws_url:
        logger.info(f"    websocketUrl = {ws_url}")
    if http_api_url:
        logger.info(f"    apiBaseUrl = {http_api_url}")
    if cognito_pool_id:
        logger.info(f"    cognitoUserPoolId = {cognito_pool_id}")
    if cognito_client_id:
        logger.info(f"    cognitoClientId = {cognito_client_id}")


def update_kb_ids_from_ssm(session):
    """Read KB IDs from SSM (set by CDK) and write knowledge_base_ids.json."""
    ssm = session.client('ssm')
    kb_ids_path = PROJECT_ROOT / 'knowledge-bases' / 'knowledge_base_ids.json'

    kb_map = {
        'ehr_mock': '/clinicalcompass/kb/cc-ehr-mock/id',
        'global_guidelines': '/clinicalcompass/kb/cc-global-guidelines/id',
        'local_regulatory': '/clinicalcompass/kb/cc-local-regulatory/id',
    }

    bucket_map = {
        'ehr_mock': f'clinicalcompass-ehr-mock-{session.client("sts").get_caller_identity()["Account"]}',
        'global_guidelines': f'clinicalcompass-guidelines-{session.client("sts").get_caller_identity()["Account"]}',
        'local_regulatory': f'clinicalcompass-local-regulatory-{session.client("sts").get_caller_identity()["Account"]}',
    }

    result = {}
    for key, param_name in kb_map.items():
        try:
            resp = ssm.get_parameter(Name=param_name)
            kb_id = resp['Parameter']['Value']
            result[key] = {
                'kb_id': kb_id,
                'bucket_name': bucket_map[key],
            }
            logger.info(f"    {key}: {kb_id}")
        except ClientError:
            logger.warning(f"    {key}: SSM parameter not found ({param_name})")
            result[key] = {'error': f'SSM parameter {param_name} not found'}

    with open(kb_ids_path, 'w') as f:
        json.dump(result, f, indent=2)

    logger.info(f"  ✓ Updated knowledge-bases/knowledge_base_ids.json")


# ── Step 2: Knowledge Base setup (index + KB + data source) ──────

KB_CONFIGS = [
    {'key': 'ehr_mock',          'name': 'ClinicalCompass-EHR-Mock-v2',             'role_name': 'ClinicalCompass-EHR-Mock',             'collection': 'cc-ehr-mock',          'bucket_suffix': 'ehr-mock',          'description': 'Mock EHR patient data'},
    {'key': 'global_guidelines', 'name': 'ClinicalCompass-Global-Guidelines-v2',    'role_name': 'ClinicalCompass-Global-Guidelines',    'collection': 'cc-global-guidelines', 'bucket_suffix': 'global-guidelines', 'description': 'International clinical guidelines'},
    {'key': 'local_regulatory',  'name': 'ClinicalCompass-Local-Regulatory-v2',     'role_name': 'ClinicalCompass-Local-Regulatory',     'collection': 'cc-local-regulatory',  'bucket_suffix': 'local-regulatory',  'description': 'Local regulatory protocols (HAS France)'},
]


def _ensure_kb_role_in_access_policy(aoss, collection_name: str, kb_role_arn: str, account: str):
    """Ensure the KB role is included in the OpenSearch Serverless data access policy.

    This prevents the 403 Forbidden error when Bedrock KB tries to ingest data.
    Idempotent — no-ops if the role is already present.
    """
    import json as _json

    # CDK names policies like: ccehrmock-data, ccglobalguidelines-data, etc.
    policy_name = f"{collection_name.replace('-', '')}-data"

    try:
        resp = aoss.get_access_policy(type='data', name=policy_name)
        policy_version = resp['accessPolicyDetail']['policyVersion']
        policy_doc = resp['accessPolicyDetail']['policy']

        # Check if KB role already present
        principals = policy_doc[0].get('Principal', [])
        if kb_role_arn in principals:
            return  # Already there

        # Add KB role to principals
        principals.append(kb_role_arn)
        policy_doc[0]['Principal'] = principals

        aoss.update_access_policy(
            type='data',
            name=policy_name,
            policyVersion=policy_version,
            policy=_json.dumps(policy_doc),
        )
        logger.info(f"  ✓ Added KB role to access policy: {policy_name}")
    except Exception as e:
        logger.warning(f"  Could not update access policy {policy_name}: {e}")
        logger.warning(f"  You may need to manually add {kb_role_arn} to the data access policy")


KB_INDEX_VISIBILITY_ATTEMPTS = 6
KB_INDEX_VISIBILITY_DELAY_S = 30


def _create_knowledge_base(bedrock_agent, kb_name: str, kb_kwargs: dict) -> str:
    """Create a Bedrock KB, waiting for a newly created OpenSearch index to become visible.

    Right after an OpenSearch Serverless index is created, Bedrock can still report
    "no such index" for a while. That error is retried; any other error is raised.
    Returns the new knowledge base ID.
    """
    for attempt in range(1, KB_INDEX_VISIBILITY_ATTEMPTS + 1):
        try:
            resp = bedrock_agent.create_knowledge_base(name=kb_name, **kb_kwargs)
            return resp['knowledgeBase']['knowledgeBaseId']
        except ClientError as e:
            if 'no such index' not in str(e) or attempt == KB_INDEX_VISIBILITY_ATTEMPTS:
                raise
            logger.warning(
                f"  Index not visible to Bedrock yet (attempt {attempt}/{KB_INDEX_VISIBILITY_ATTEMPTS}); "
                f"retrying in {KB_INDEX_VISIBILITY_DELAY_S}s..."
            )
            time.sleep(KB_INDEX_VISIBILITY_DELAY_S)


def setup_knowledge_bases(session, region: str):
    """Create OpenSearch indices, Bedrock KBs, and data sources."""
    account = session.client('sts').get_caller_identity()['Account']
    aoss = session.client('opensearchserverless')
    bedrock_agent = session.client('bedrock-agent')
    ssm = session.client('ssm')

    kb_ids_path = PROJECT_ROOT / 'knowledge-bases' / 'knowledge_base_ids.json'
    result = {}

    for cfg in KB_CONFIGS:
        logger.info(f"\n  --- {cfg['name']} ---")
        collection_name = cfg['collection']
        bucket_name = f"clinicalcompass-{cfg['bucket_suffix']}-{account}"
        role_name = f"BedrockKB-{cfg.get('role_name', cfg['name'])}-Role"
        role_arn = f"arn:aws:iam::{account}:role/{role_name}"

        # 1. Get collection endpoint
        try:
            resp = aoss.batch_get_collection(names=[collection_name])
            details = resp.get('collectionDetails', [])
            if not details:
                logger.error(f"  Collection {collection_name} not found — run cdk deploy first")
                result[cfg['key']] = {'error': 'Collection not found'}
                continue
            collection_arn = details[0]['arn']
            endpoint = details[0].get('collectionEndpoint', '')
            status = details[0].get('status', '')
            if status != 'ACTIVE':
                logger.warning(f"  Collection status: {status} — waiting...")
                for _ in range(30):
                    time.sleep(10)
                    resp2 = aoss.batch_get_collection(names=[collection_name])
                    if resp2['collectionDetails'][0].get('status') == 'ACTIVE':
                        endpoint = resp2['collectionDetails'][0].get('collectionEndpoint', '')
                        break
            logger.info(f"  Collection: {collection_name} (ACTIVE)")
        except Exception as e:
            logger.error(f"  Failed to get collection: {e}")
            result[cfg['key']] = {'error': str(e)}
            continue

        # 2. Wait for DNS resolution (OpenSearch Serverless endpoints take 2-10 min after creation)
        host = endpoint.replace('https://', '')
        dns_ready = False
        for dns_attempt in range(2):
            try:
                import socket
                socket.getaddrinfo(host, 443)
                dns_ready = True
                logger.info(f"  DNS resolved: {host}")
                break
            except socket.gaierror:
                wait_time = 300  # 5 minutes
                logger.warning(f"  DNS not ready for {host} — waiting {wait_time // 60} min (attempt {dns_attempt + 1}/2)...")
                time.sleep(wait_time)

        if not dns_ready:
            logger.error(f"  DNS resolution failed for {host} after 2 attempts (10 min total). Try again later.")
            result[cfg['key']] = {'error': f'DNS resolution failed for {host}'}
            continue

        # 3. Create OpenSearch index (with retry)
        try:
            from opensearchpy import OpenSearch, RequestsHttpConnection
            from requests_aws4auth import AWS4Auth

            credentials = session.get_credentials().get_frozen_credentials()
            awsauth = AWS4Auth(credentials.access_key, credentials.secret_key, region, 'aoss',
                               session_token=credentials.token)
            os_client = OpenSearch(
                hosts=[{'host': host, 'port': 443}],
                http_auth=awsauth, use_ssl=True, verify_certs=True,
                connection_class=RequestsHttpConnection, timeout=60,
            )

            if os_client.indices.exists(index=collection_name):
                logger.info(f"  Index already exists: {collection_name}")
            else:
                logger.info(f"  Creating index: {collection_name}")
                for attempt in range(5):
                    try:
                        os_client.indices.create(index=collection_name, body={
                            'settings': {'index': {'knn': True, 'knn.algo_param.ef_search': 512}},
                            'mappings': {'properties': {
                                'embedding': {'type': 'knn_vector', 'dimension': 1024,
                                              'method': {'name': 'hnsw', 'engine': 'faiss',
                                                         'parameters': {'ef_construction': 512, 'm': 16}}},
                                'text': {'type': 'text'},
                                'metadata': {'type': 'text', 'index': False},
                                'AMAZON_BEDROCK_METADATA': {'type': 'text', 'index': False},
                                'AMAZON_BEDROCK_TEXT_CHUNK': {'type': 'text'},
                            }},
                        })
                        logger.info(f"  ✓ Index created")
                        break
                    except Exception as ie:
                        if attempt < 4:
                            logger.warning(f"  Index creation attempt {attempt+1} failed, retrying in 15s...")
                            time.sleep(15)
                        else:
                            raise ie
                time.sleep(10)  # Let index settle
        except ImportError:
            logger.error("  opensearch-py and requests-aws4auth required: pip install opensearch-py requests-aws4auth")
            result[cfg['key']] = {'error': 'Missing opensearch-py'}
            continue
        except Exception as e:
            logger.error(f"  Failed to create index: {e}")
            result[cfg['key']] = {'error': str(e)}
            continue

        # 3. Create Bedrock Knowledge Base (if not exists, or recreate if stale)
        kb_id = None
        kb_name = cfg['name']
        try:
            existing_kbs = bedrock_agent.list_knowledge_bases().get('knowledgeBaseSummaries', [])
            for kb in existing_kbs:
                kb_candidate_name = kb['name']
                if not (
                    kb_candidate_name == cfg['name']
                    or kb_candidate_name.startswith(f"{cfg['name']}-")
                ):
                    continue

                if kb.get('status') == 'DELETE_UNSUCCESSFUL':
                    logger.warning(
                        f"  KB {kb['knowledgeBaseId']} is stuck in DELETE_UNSUCCESSFUL "
                        f"for name {kb_candidate_name}"
                    )
                    continue

                if kb_candidate_name == cfg['name'] or kb_candidate_name.startswith(f"{cfg['name']}-"):
                    existing_kb_id = kb['knowledgeBaseId']
                    # Validate the KB points to the current collection ARN
                    kb_detail = bedrock_agent.get_knowledge_base(knowledgeBaseId=existing_kb_id)['knowledgeBase']
                    existing_collection_arn = (
                        kb_detail.get('storageConfiguration', {})
                        .get('opensearchServerlessConfiguration', {})
                        .get('collectionArn', '')
                    )
                    if existing_collection_arn == collection_arn:
                        kb_id = existing_kb_id
                        kb_name = kb_candidate_name
                        logger.info(f"  KB already exists and is current: {kb_id}")
                    else:
                        # Stale KB — delete and recreate
                        logger.warning(f"  KB {existing_kb_id} points to stale collection ({existing_collection_arn})")
                        logger.info(f"  Deleting stale KB and its data sources...")
                        ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=existing_kb_id).get('dataSourceSummaries', [])
                        for ds in ds_list:
                            bedrock_agent.delete_data_source(knowledgeBaseId=existing_kb_id, dataSourceId=ds['dataSourceId'])
                            time.sleep(2)
                        bedrock_agent.delete_knowledge_base(knowledgeBaseId=existing_kb_id)
                        logger.info(f"  ✓ Deleted stale KB {existing_kb_id}")
                        time.sleep(5)
                    break
        except Exception as e:
            logger.warning(f"  Error checking existing KBs: {e}")

        if not kb_id:
            kb_kwargs = {
                'description': cfg['description'],
                'roleArn': role_arn,
                'knowledgeBaseConfiguration': {
                    'type': 'VECTOR',
                    'vectorKnowledgeBaseConfiguration': {
                        'embeddingModelArn': f'arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0',
                    },
                },
                'storageConfiguration': {
                    'type': 'OPENSEARCH_SERVERLESS',
                    'opensearchServerlessConfiguration': {
                        'collectionArn': collection_arn,
                        'vectorIndexName': collection_name,
                        'fieldMapping': {'vectorField': 'embedding', 'textField': 'text', 'metadataField': 'metadata'},
                    },
                },
            }
            try:
                kb_id = _create_knowledge_base(bedrock_agent, kb_name, kb_kwargs)
                logger.info(f"  ✓ KB created: {kb_id}")
                time.sleep(5)
            except Exception as e:
                message = str(e)
                if 'ConflictException' in message and 'already exists' in message:
                    kb_name = f"{cfg['name']}-{int(time.time())}"
                    logger.warning(
                        f"  Canonical KB name is blocked; retrying with fallback name {kb_name}"
                    )
                    try:
                        kb_id = _create_knowledge_base(bedrock_agent, kb_name, kb_kwargs)
                        logger.info(f"  ✓ KB created with fallback name: {kb_id}")
                        time.sleep(5)
                    except Exception as retry_error:
                        logger.error(f"  Failed to create KB with fallback name: {retry_error}")
                        result[cfg['key']] = {'error': str(retry_error)}
                        continue
                else:
                    logger.error(f"  Failed to create KB: {e}")
                    result[cfg['key']] = {'error': str(e)}
                    continue

        # 4. Create data source (if not exists)
        ds_id = None
        try:
            ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id).get('dataSourceSummaries', [])
            if ds_list:
                ds_id = ds_list[0]['dataSourceId']
                logger.info(f"  Data source already exists: {ds_id}")
        except Exception:
            pass

        if not ds_id:
            try:
                resp = bedrock_agent.create_data_source(
                    knowledgeBaseId=kb_id,
                    name=f"{cfg['name']}-DataSource",
                    dataSourceConfiguration={'type': 'S3', 's3Configuration': {'bucketArn': f'arn:aws:s3:::{bucket_name}'}},
                    vectorIngestionConfiguration={
                        'chunkingConfiguration': {
                            'chunkingStrategy': 'FIXED_SIZE',
                            'fixedSizeChunkingConfiguration': {'maxTokens': 512, 'overlapPercentage': 20},
                        },
                    },
                )
                ds_id = resp['dataSource']['dataSourceId']
                logger.info(f"  ✓ Data source created: {ds_id}")
            except Exception as e:
                logger.error(f"  Failed to create data source: {e}")

        # Store KB ID in SSM
        if kb_id:
            ssm.put_parameter(
                Name=f'/clinicalcompass/kb/{collection_name}/id',
                Value=kb_id, Type='String', Overwrite=True,
            )

            # Ensure the KB role is in the OpenSearch access policy
            _ensure_kb_role_in_access_policy(aoss, collection_name, role_arn, account)

        result[cfg['key']] = {
            'kb_id': kb_id, 'bucket_name': bucket_name,
            'data_source_id': ds_id, 'collection_name': collection_name,
            'kb_name': kb_name,
        }

    # Save to knowledge_base_ids.json
    with open(kb_ids_path, 'w') as f:
        json.dump(result, f, indent=2)
    logger.info(f"\n  ✓ Saved knowledge_base_ids.json")


# ── Step 3: Create AgentCore Runtime ─────────────────────────────

SSM_AGENT_ARN_PARAM = '/clinicalcompass/agent-runtime-arn'

def setup_agentcore_runtime(session, outputs: dict, region: str):
    """Create AgentCore Runtime if it doesn't exist, store ARN in SSM."""
    control = session.client('bedrock-agentcore-control', region_name=region)
    ssm = session.client('ssm')

    ecr_uri = outputs.get('EcrRepositoryUri', '')
    role_arn = outputs.get('AgentCoreRoleArn', '')

    if not ecr_uri:
        logger.error("  No EcrRepositoryUri in CDK outputs")
        return None

    # Check SSM for existing ARN
    try:
        resp = ssm.get_parameter(Name=SSM_AGENT_ARN_PARAM)
        existing = resp['Parameter']['Value']
        if existing and existing != 'PLACEHOLDER':
            logger.info(f"  Runtime already configured in SSM: {existing}")
            return existing
    except ClientError:
        pass

    # Find existing runtime
    agent_arn = None
    try:
        runtimes = control.list_agent_runtimes().get('agentRuntimes', [])
        for rt in runtimes:
            if rt.get('agentRuntimeName') == 'clinicalcompass':
                agent_arn = rt['agentRuntimeArn']
                logger.info(f"  Found existing runtime: {agent_arn}")
                break
    except Exception as e:
        logger.warning(f"  Could not list runtimes: {e}")

    # Create if not found
    if not agent_arn:
        logger.info("  Creating AgentCore runtime...")
        try:
            result = control.create_agent_runtime(
                agentRuntimeName='clinicalcompass',
                agentRuntimeArtifact={
                    'containerConfiguration': {'containerUri': f'{ecr_uri}:latest'}
                },
                roleArn=role_arn,
                networkConfiguration={'networkMode': 'PUBLIC'},
                protocolConfiguration={'serverProtocol': 'HTTP'},
            )
            agent_arn = result['agentRuntimeArn']
            logger.info(f"  Created runtime: {agent_arn}")
            logger.info("  Waiting 30s for runtime to initialize...")
            time.sleep(30)
        except Exception as e:
            logger.error(f"  Failed to create runtime: {e}")
            return None

    # Store in SSM
    if agent_arn:
        ssm.put_parameter(Name=SSM_AGENT_ARN_PARAM, Value=agent_arn, Type='String', Overwrite=True)
        logger.info(f"  ✓ Stored runtime ARN in SSM: {agent_arn}")

        # Also update the Lambda query handler's AGENT_ARN env var
        try:
            lam = session.client('lambda')
            resp = lam.get_function_configuration(FunctionName='clinicalcompass-ws-query')
            env = resp.get('Environment', {}).get('Variables', {})
            env['AGENT_ARN'] = agent_arn
            lam.update_function_configuration(
                FunctionName='clinicalcompass-ws-query',
                Environment={'Variables': env},
            )
            logger.info("  ✓ Updated Lambda query handler AGENT_ARN")
        except Exception as e:
            logger.warning(f"  Could not update Lambda env: {e}")

    return agent_arn


# ── Step 3: Create AgentCore Memory ──────────────────────────────

USABLE_MEMORY_STATUSES = ('ACTIVE', 'CREATING')
MEMORY_CREATE_ATTEMPTS = 3
MEMORY_ACTIVE_TIMEOUT_S = 300
MEMORY_RETRY_DELAY_S = 60


def _wait_for_memory_status(control, memory_id: str, timeout_s: int) -> tuple:
    """Poll until the memory is ACTIVE or FAILED. Returns (status, failure_reason)."""
    deadline = time.time() + timeout_s
    status, reason = '', None
    while time.time() < deadline:
        time.sleep(10)
        try:
            memory = control.get_memory(memoryId=memory_id).get('memory', {})
        except ClientError:
            continue
        status, reason = memory.get('status', ''), memory.get('failureReason')
        if status in ('ACTIVE', 'FAILED'):
            break
    return status, reason


def _delete_memory_and_wait(control, memory_id: str, timeout_s: int = 600):
    """Delete a memory and wait until it no longer exists."""
    try:
        control.delete_memory(memoryId=memory_id)
    except ClientError as e:
        logger.warning(f"  Could not delete memory {memory_id}: {e}")
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _memory_status(control, memory_id):
            return
        time.sleep(10)
    logger.warning(f"  Memory {memory_id} still present after {timeout_s}s")


def _create_memory_with_retry(control):
    """Create the AgentCore Memory, retrying when the service reports FAILED.

    AgentCore Memory provisioning can fail transiently ("Failed to provision Vector
    Store for the customer"). Each failed attempt is deleted before retrying so it
    does not linger in the account. Returns the ACTIVE memory ID, or None.
    """
    for attempt in range(1, MEMORY_CREATE_ATTEMPTS + 1):
        logger.info(f"  Creating memory: {MEMORY_NAME} (attempt {attempt}/{MEMORY_CREATE_ATTEMPTS})")
        try:
            resp = control.create_memory(
                name=MEMORY_NAME,
                description='ClinicalCompass short-term memory for session management',
                eventExpiryDuration=30,
            )
        except ClientError as e:
            logger.error(f"  Failed to create memory: {e}")
            return None
        memory_id = (resp.get('memory', {}).get('id')
                     or resp.get('memory', {}).get('memoryId')
                     or resp.get('memoryId')
                     or resp.get('id'))
        logger.info(f"  Created memory: {memory_id}. Waiting for it to become active...")

        status, reason = _wait_for_memory_status(control, memory_id, MEMORY_ACTIVE_TIMEOUT_S)
        if status == 'ACTIVE':
            return memory_id

        logger.warning(f"  Memory {memory_id} ended as {status or 'not ACTIVE'}: {reason or 'no reason given'}")
        _delete_memory_and_wait(control, memory_id)
        if attempt < MEMORY_CREATE_ATTEMPTS:
            logger.info(f"  Retrying in {MEMORY_RETRY_DELAY_S}s...")
            time.sleep(MEMORY_RETRY_DELAY_S)

    logger.error(f"  Memory creation failed after {MEMORY_CREATE_ATTEMPTS} attempts")
    return None


def _memory_status(control, memory_id: str) -> str:
    """Return the AgentCore Memory status, or '' if it doesn't exist."""
    try:
        return control.get_memory(memoryId=memory_id).get('memory', {}).get('status', '')
    except ClientError:
        return ''


def setup_memory(session, region: str):
    """Create AgentCore Memory if it doesn't exist, store ID in SSM."""
    control = session.client('bedrock-agentcore-control', region_name=region)
    ssm = session.client('ssm')

    # Check SSM first — but only reuse the ID if that memory still exists.
    # After a teardown the parameter can point at a deleted (or DELETING) memory.
    try:
        resp = ssm.get_parameter(Name=SSM_MEMORY_PARAM)
        existing = resp['Parameter']['Value']
        if existing and existing != 'PLACEHOLDER':
            status = _memory_status(control, existing)
            if status in USABLE_MEMORY_STATUSES:
                logger.info(f"  Memory already configured in SSM: {existing} ({status})")
                return existing
            logger.info(f"  SSM memory {existing} is not usable (status: {status or 'not found'}); resolving again")
    except ClientError:
        pass

    # Find existing memory
    memory_id = None
    try:
        resp = control.list_memories()
        for mem in resp.get('memories', []):
            mid = mem.get('id') or mem.get('memoryId')
            if not mid:
                continue
            # list_memories also returns memories that are still being deleted
            if mem.get('status') and mem['status'] not in USABLE_MEMORY_STATUSES:
                continue
            # Name isn't in list response — check via get_memory or match ID prefix
            if mid.startswith(MEMORY_NAME):
                memory_id = mid
                logger.info(f"  Found existing memory: {memory_id}")
                break
            # Fallback: try get_memory for the name
            try:
                details = control.get_memory(memoryId=mid)
                name = details.get('memory', {}).get('name', '')
                if name == MEMORY_NAME:
                    memory_id = mid
                    logger.info(f"  Found existing memory: {memory_id} (name: {name})")
                    break
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"  Could not list memories: {e}")

    # Create if not found
    if not memory_id:
        memory_id = _create_memory_with_retry(control)
        if not memory_id:
            return None

    # Store in SSM
    if memory_id:
        ssm.put_parameter(
            Name=SSM_MEMORY_PARAM,
            Value=memory_id,
            Type='String',
            Overwrite=True,
        )
        logger.info(f"  ✓ Stored memory ID in SSM: {memory_id}")

    return memory_id


# ── Step 4: Upload KB data and trigger ingestion ─────────────────

def upload_kb_data(session):
    """Upload clinical data to S3 buckets and trigger ingestion."""
    s3 = session.client('s3')
    bedrock_agent = session.client('bedrock-agent')

    kb_ids_path = PROJECT_ROOT / 'knowledge-bases' / 'knowledge_base_ids.json'
    if not kb_ids_path.exists():
        logger.warning("  knowledge_base_ids.json not found — skipping data upload")
        return

    with open(kb_ids_path) as f:
        kb_ids = json.load(f)

    data_map = {
        'ehr_mock': PROJECT_ROOT / 'knowledge-bases' / 'data' / 'ehr',
        'global_guidelines': PROJECT_ROOT / 'knowledge-bases' / 'data' / 'guidelines',
        'local_regulatory': PROJECT_ROOT / 'knowledge-bases' / 'data' / 'regulatory',
    }

    for kb_key, data_path in data_map.items():
        kb_info = kb_ids.get(kb_key, {})
        bucket = kb_info.get('bucket_name')
        kb_id = kb_info.get('kb_id')

        if not bucket or not kb_id:
            logger.warning(f"  {kb_key}: missing bucket or KB ID — skipping")
            continue

        if not data_path.exists():
            logger.warning(f"  {kb_key}: data path not found ({data_path}) — skipping")
            continue

        # Upload files
        file_count = 0
        for file_path in data_path.rglob('*'):
            if file_path.is_file():
                s3_key = str(file_path.relative_to(data_path)).replace('\\', '/')
                s3.upload_file(str(file_path), bucket, s3_key)
                file_count += 1

        logger.info(f"  {kb_key}: uploaded {file_count} files to s3://{bucket}")

        # Find data source ID
        ds_id = kb_info.get('data_source_id')
        if not ds_id:
            try:
                resp = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id)
                sources = resp.get('dataSourceSummaries', [])
                if sources:
                    ds_id = sources[0]['dataSourceId']
            except Exception as e:
                logger.warning(f"  {kb_key}: could not find data source: {e}")
                continue

        if not ds_id:
            logger.warning(f"  {kb_key}: no data source found — skipping ingestion")
            continue

        # Trigger ingestion
        try:
            resp = bedrock_agent.start_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=ds_id,
            )
            job_id = resp['ingestionJob']['ingestionJobId']
            logger.info(f"  {kb_key}: ingestion started (job: {job_id})")

            # Update kb_ids with data_source_id for future use
            kb_ids[kb_key]['data_source_id'] = ds_id
            kb_ids[kb_key]['ingestion_job_id'] = job_id
        except Exception as e:
            logger.warning(f"  {kb_key}: ingestion failed: {e}")

    # Save updated kb_ids
    with open(kb_ids_path, 'w') as f:
        json.dump(kb_ids, f, indent=2)

    logger.info("  ✓ Data upload and ingestion complete")


# ── Step 5: Initial data upload to CDK-managed buckets + KB sync ─

# SSM parameter names for CDK-managed resources
SSM_EHR_DATA_BUCKET = '/clinicalcompass/ehr-data-bucket'
SSM_GUIDELINES_BUCKET = '/clinicalcompass/guidelines-bucket'
SSM_KB_EHR_PARAM = '/clinicalcompass/kb/cc-ehr-mock/id'
SSM_KB_GUIDELINES_PARAM = '/clinicalcompass/kb/cc-global-guidelines/id'


def get_ssm_parameter(ssm_client, param_name: str) -> str:
    """Read a single SSM parameter value. Returns empty string if not found."""
    try:
        resp = ssm_client.get_parameter(Name=param_name)
        value = resp['Parameter']['Value']
        if value == 'PLACEHOLDER':
            return ''
        return value
    except ClientError as e:
        if e.response['Error']['Code'] == 'ParameterNotFound':
            logger.warning(f"  SSM parameter not found: {param_name}")
            return ''
        raise


def upload_initial_data(session):
    """Upload initial patient data and guidelines to CDK-managed S3 buckets.

    This triggers the EHR Ingestion Lambda and Guideline Ingestion Lambda
    via S3 event notifications configured in CDK. After uploads, triggers
    Bedrock KB data sync for immediate availability.

    Reads bucket names from SSM Parameter Store (CDK outputs).
    Idempotent: safe to re-run — S3 PutObject overwrites existing objects.
    """
    ssm = session.client('ssm')
    s3 = session.client('s3')
    bedrock_agent = session.client('bedrock-agent')

    # Read resource identifiers from SSM Parameter Store (CDK outputs)
    ehr_bucket = get_ssm_parameter(ssm, SSM_EHR_DATA_BUCKET)
    guidelines_bucket = get_ssm_parameter(ssm, SSM_GUIDELINES_BUCKET)
    ehr_kb_id = get_ssm_parameter(ssm, SSM_KB_EHR_PARAM)
    guidelines_kb_id = get_ssm_parameter(ssm, SSM_KB_GUIDELINES_PARAM)

    if not ehr_bucket:
        logger.warning("  EHR data bucket not found in SSM — skipping EHR upload")
    if not guidelines_bucket:
        logger.warning("  Guidelines bucket not found in SSM — skipping guidelines upload")

    # ── Upload patient data from knowledge-bases/data/ehr/ to S3 EHR bucket ──
    ehr_data_path = PROJECT_ROOT / 'knowledge-bases' / 'data' / 'ehr'
    ehr_file_count = 0

    if ehr_bucket and ehr_data_path.exists():
        logger.info(f"  Uploading patient data to s3://{ehr_bucket}/")
        for file_path in sorted(ehr_data_path.glob('*.json')):
            if file_path.is_file():
                s3_key = file_path.name
                s3.upload_file(str(file_path), ehr_bucket, s3_key)
                ehr_file_count += 1
        logger.info(f"  ✓ Uploaded {ehr_file_count} patient files to s3://{ehr_bucket}/")
    elif ehr_bucket:
        logger.warning(f"  EHR data path not found: {ehr_data_path}")

    # ── Upload guidelines from knowledge-bases/data/guidelines/ to S3 guidelines bucket ──
    guidelines_data_path = PROJECT_ROOT / 'knowledge-bases' / 'data' / 'guidelines'
    guidelines_file_count = 0

    if guidelines_bucket and guidelines_data_path.exists():
        logger.info(f"  Uploading guidelines to s3://{guidelines_bucket}/")
        for file_path in sorted(guidelines_data_path.iterdir()):
            if file_path.is_file():
                s3_key = file_path.name
                s3.upload_file(str(file_path), guidelines_bucket, s3_key)
                guidelines_file_count += 1
        logger.info(f"  ✓ Uploaded {guidelines_file_count} guideline files to s3://{guidelines_bucket}/")
    elif guidelines_bucket:
        logger.warning(f"  Guidelines data path not found: {guidelines_data_path}")

    # ── Trigger Bedrock KB data sync after uploads ──
    logger.info("  Triggering Bedrock KB data sync...")

    kb_sync_targets = []
    if ehr_kb_id:
        kb_sync_targets.append(('EHR KB', ehr_kb_id))
    if guidelines_kb_id:
        kb_sync_targets.append(('Guidelines KB', guidelines_kb_id))

    for kb_label, kb_id in kb_sync_targets:
        try:
            # Find the data source for this KB
            resp = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id)
            sources = resp.get('dataSourceSummaries', [])
            if not sources:
                logger.warning(f"  {kb_label} ({kb_id}): no data source found — skipping sync")
                continue

            ds_id = sources[0]['dataSourceId']
            sync_resp = bedrock_agent.start_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=ds_id,
            )
            job_id = sync_resp['ingestionJob']['ingestionJobId']
            logger.info(f"  ✓ {kb_label} ({kb_id}): sync started (job: {job_id})")
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'ConflictException':
                # An ingestion job started in the previous step is still running; it will
                # pick up the same files, so nothing is lost.
                logger.info(f"  ✓ {kb_label} ({kb_id}): ingestion already in progress, no new sync needed")
            else:
                logger.warning(f"  {kb_label} ({kb_id}): sync failed: {e}")
        except Exception as e:
            logger.warning(f"  {kb_label} ({kb_id}): unexpected error: {e}")

    total = ehr_file_count + guidelines_file_count
    if total > 0:
        logger.info(f"  ✓ Initial data upload complete ({total} files total)")
    else:
        logger.warning("  No files uploaded — check SSM parameters and data directories")


# ── Main ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Post-CDK setup for ClinicalCompass')
    parser.add_argument('--skip-kb', action='store_true', help='Skip Knowledge Base setup')
    parser.add_argument('--skip-runtime', action='store_true', help='Skip AgentCore Runtime creation')
    parser.add_argument('--skip-memory', action='store_true', help='Skip AgentCore Memory setup')
    parser.add_argument('--skip-data', action='store_true', help='Skip KB data upload and ingestion')
    parser.add_argument('--skip-config', action='store_true', help='Skip config file updates')
    parser.add_argument('--skip-initial-upload', action='store_true', help='Skip initial data upload to CDK-managed buckets')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  ClinicalCompass — Post-CDK Setup")
    logger.info("=" * 60)

    session, region = get_session()
    account = session.client('sts').get_caller_identity()['Account']
    logger.info(f"  Account: {account}")
    logger.info(f"  Region:  {region}")

    # Step 1: Read CDK outputs and update configs
    logger.info("\n[1/7] Reading CDK outputs and updating config files...")
    outputs = get_cdk_outputs(session)
    logger.info(f"  Found {len(outputs)} outputs from {STACK_NAME}")

    if not args.skip_config:
        update_deployment_config(outputs)
        update_frontend_config(outputs)
    else:
        logger.info("  Skipped (--skip-config)")

    # Step 2: Knowledge Base setup (OpenSearch index + Bedrock KB + data source)
    logger.info("\n[2/7] Setting up Knowledge Bases...")
    if not args.skip_kb:
        setup_knowledge_bases(session, region)
    else:
        logger.info("  Skipped (--skip-kb)")

    # Step 3: Docker image — must be done separately BEFORE the runtime step
    logger.info("\n[3/7] Docker image...")
    logger.info("  Run separately BEFORE this script (the runtime step needs the image in ECR):")
    logger.info("    python deployment/scripts/build_and_push_image.py")

    # Step 4: AgentCore Memory — created BEFORE the runtime. Agent containers read the
    # memory ID from SSM once at start-up, and the keep-alive Lambda can start a container
    # as soon as the runtime exists; creating memory first ensures that container sees it.
    logger.info("\n[4/7] Setting up AgentCore Memory...")
    memory_ok = True
    if not args.skip_memory:
        memory_ok = bool(setup_memory(session, region))
    else:
        logger.info("  Skipped (--skip-memory)")

    # Step 5: AgentCore Runtime (needs Docker image in ECR)
    logger.info("\n[5/7] Setting up AgentCore Runtime...")
    if not args.skip_runtime:
        agent_arn = setup_agentcore_runtime(session, outputs, region)
    else:
        logger.info("  Skipped (--skip-runtime)")

    # Step 6: KB data upload and ingestion (legacy KB buckets)
    logger.info("\n[6/7] Uploading KB data and triggering ingestion...")
    if not args.skip_data:
        upload_kb_data(session)
    else:
        logger.info("  Skipped (--skip-data)")

    # Step 7: Initial data upload to CDK-managed buckets + KB sync
    logger.info("\n[7/7] Uploading initial data to CDK-managed buckets and triggering KB sync...")
    if not args.skip_initial_upload:
        upload_initial_data(session)
    else:
        logger.info("  Skipped (--skip-initial-upload)")

    # Summary
    ws_url = outputs.get('WebSocketUrl', 'N/A')
    if not memory_ok:
        logger.error("\n" + "=" * 60)
        logger.error("  POST-CDK SETUP INCOMPLETE: AgentCore Memory could not be created")
        logger.error("=" * 60)
        logger.error("  The app works without memory, but session continuity is disabled.")
        logger.error("  Retry only the memory step later, then refresh the agent with")
        logger.error("  build_and_push_image.py so it picks up the memory ID (README: AgentCore Memory):")
        logger.error("    python deployment/scripts/post_cdk_setup.py --skip-kb --skip-runtime "
                     "--skip-data --skip-initial-upload --skip-config")
        sys.exit(1)
    logger.info("\n" + "=" * 60)
    logger.info("  POST-CDK SETUP COMPLETE")
    logger.info("=" * 60)
    logger.info(f"\n  WebSocket URL: {ws_url}")
    logger.info(f"\n  To run the frontend:")
    logger.info(f"    cd frontend && python3 -m http.server 8080")
    logger.info(f"    Open http://localhost:8080")
    logger.info("")


if __name__ == '__main__':
    main()
