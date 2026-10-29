#!/usr/bin/env python3
"""
Full cleanup — deletes ALL ClinicalCompass resources.
Handles both CDK-managed and script-managed resources, including orphans
that survive stack deletion (RETAIN buckets, ECR, HealthLake, OpenSearch).

Order:
1. Bedrock Knowledge Bases + data sources (script-managed)
2. AgentCore Runtime + Memory (script-managed)
3. CDK stack (handles ROLLBACK_FAILED and DELETE_FAILED states)
4. Orphaned resources: S3 buckets, ECR, OpenSearch, HealthLake, SSM, IAM

Usage:
    python deployment/scripts/cleanup_all.py          # interactive (prompts)
    python deployment/scripts/cleanup_all.py --yes    # non-interactive
"""

import sys
import time
import json
import logging
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import AWS_PROFILE, AWS_REGION, AWS_ACCOUNT_ID

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

STACK_NAME = 'ClinicalCompassStack'
session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)


def safe(fn, label):
    try:
        fn()
        logger.info(f"  ✓ {label}")
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('ResourceNotFoundException', 'NotFoundException', 'NoSuchEntity',
                    'NoSuchBucket', 'RepositoryNotFoundException', 'ConflictException',
                    '404', 'BucketNotEmpty'):
            logger.info(f"  - {label} (not found)")
        else:
            logger.warning(f"  ✗ {label}: {e.response['Error']['Message']}")
    except Exception as e:
        logger.warning(f"  ✗ {label}: {e}")


def _clinicalcompass_bucket_names():
    """Return all known ClinicalCompass buckets, including retained or legacy ones."""
    expected = {
        f'clinicalcompass-ehr-data-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-ehr-mock-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-guidelines-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-global-guidelines-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-regulatory-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-local-regulatory-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-frontend-{AWS_ACCOUNT_ID}',
        f'clinicalcompass-lambda-packages-{AWS_ACCOUNT_ID}-{AWS_REGION}',
    }

    try:
        s3_client = session.client('s3')
        for bucket in s3_client.list_buckets().get('Buckets', []):
            name = bucket['Name']
            if name.startswith('clinicalcompass-'):
                expected.add(name)
    except Exception as e:
        logger.warning(f"    Could not enumerate ClinicalCompass buckets: {e}")

    return sorted(expected)


def step1_knowledge_bases():
    logger.info("\n[1/4] Bedrock Knowledge Bases...")
    ba = session.client('bedrock-agent')
    try:
        kbs = ba.list_knowledge_bases().get('knowledgeBaseSummaries', [])
        for kb in kbs:
            if 'ClinicalCompass' in kb.get('name', '') or 'clinicalcompass' in kb.get('name', '').lower():
                kb_id = kb['knowledgeBaseId']
                try:
                    for ds in ba.list_data_sources(knowledgeBaseId=kb_id).get('dataSourceSummaries', []):
                        safe(lambda d=ds['dataSourceId']: ba.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=d),
                             f"DataSource {ds['dataSourceId']}")
                except Exception:
                    pass
                safe(lambda: ba.delete_knowledge_base(knowledgeBaseId=kb_id), f"KB {kb['name']} ({kb_id})")
    except Exception as e:
        logger.warning(f"  Could not list KBs: {e}")


def step2_agentcore():
    logger.info("\n[2/4] AgentCore Runtime + Memory...")
    try:
        ac = session.client('bedrock-agentcore-control')

        # Delete runtimes
        for rt in ac.list_agent_runtimes().get('agentRuntimes', []):
            if 'clinicalcompass' in rt.get('agentRuntimeName', '').lower():
                safe(lambda rid=rt['agentRuntimeId']: ac.delete_agent_runtime(agentRuntimeId=rid),
                     f"Runtime {rt['agentRuntimeName']}")

        # Delete memories
        for mem in ac.list_memories().get('memories', []):
            mid = mem.get('id') or mem.get('memoryId')
            if mid and 'clinicalcompass' in mid.lower():
                safe(lambda m=mid: ac.delete_memory(memoryId=m), f"Memory {mid}")
    except Exception as e:
        logger.warning(f"  Could not clean AgentCore: {e}")


def step3_cdk_stack():
    logger.info("\n[3/4] CDK Stack...")
    cfn = session.client('cloudformation')

    # Check if stack exists
    try:
        resp = cfn.describe_stacks(StackName=STACK_NAME)
        status = resp['Stacks'][0]['StackStatus']
        logger.info(f"  Stack status: {status}")
    except ClientError:
        logger.info("  Stack does not exist — skipping")
        return

    # IMPORTANT: Empty all S3 buckets BEFORE deleting the stack.
    # CloudFormation cannot delete non-empty buckets.
    logger.info("  Emptying S3 buckets before stack deletion...")
    _empty_all_buckets()

    # Handle ROLLBACK_FAILED or ROLLBACK_COMPLETE: delete without retain first
    if status in ('ROLLBACK_FAILED', 'ROLLBACK_COMPLETE'):
        logger.info(f"  Stack in {status} — initiating delete...")
        cfn.delete_stack(StackName=STACK_NAME)
        _wait_for_stack_delete(cfn, expect_failure=(status == 'ROLLBACK_FAILED'))
        # Re-check if stack still exists
        try:
            resp = cfn.describe_stacks(StackName=STACK_NAME)
            status = resp['Stacks'][0]['StackStatus']
        except ClientError:
            logger.info("  ✓ Stack deleted")
            return

    # Handle DELETE_FAILED: retry with retain for stuck resources
    if status == 'DELETE_FAILED':
        logger.warning("  Stack in DELETE_FAILED — finding stuck resources to retain...")
        events = cfn.describe_stack_events(StackName=STACK_NAME).get('StackEvents', [])
        retain = set()
        for ev in events:
            if ev.get('ResourceStatus') == 'DELETE_FAILED':
                logical_id = ev['LogicalResourceId']
                if logical_id != STACK_NAME:
                    retain.add(logical_id)
        # Filter out already-deleted resources
        try:
            resources = cfn.list_stack_resources(StackName=STACK_NAME).get('StackResourceSummaries', [])
            still_exists = {r['LogicalResourceId'] for r in resources if r['ResourceStatus'] != 'DELETE_COMPLETE'}
            retain = retain & still_exists
        except Exception:
            pass
        retain = list(retain)
        if retain:
            logger.info(f"  Retrying delete with retain: {retain}")
            cfn.delete_stack(StackName=STACK_NAME, RetainResources=retain)
        else:
            cfn.delete_stack(StackName=STACK_NAME)
        _wait_for_stack_delete(cfn, expect_failure=False)
        return

    # Normal deletable states
    if status in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE',
                  'IMPORT_COMPLETE', 'IMPORT_ROLLBACK_COMPLETE'):
        logger.info("  Deleting stack (this may take several minutes)...")
        cfn.delete_stack(StackName=STACK_NAME)
        _wait_for_stack_delete(cfn, expect_failure=False)
    else:
        logger.warning(f"  Stack in unexpected state: {status} — waiting for it to stabilize...")
        # Wait for in-progress operations to finish
        for _ in range(40):
            time.sleep(15)
            try:
                resp = cfn.describe_stacks(StackName=STACK_NAME)
                status = resp['Stacks'][0]['StackStatus']
                if 'IN_PROGRESS' not in status:
                    break
            except ClientError:
                logger.info("  ✓ Stack deleted")
                return
        # Retry deletion
        step3_cdk_stack()


def _empty_all_buckets():
    """Empty all ClinicalCompass S3 buckets so CloudFormation can delete them."""
    s3_client = session.client('s3')
    s3_resource = session.resource('s3')

    for name in _clinicalcompass_bucket_names():
        try:
            s3_client.head_bucket(Bucket=name)
            bucket = s3_resource.Bucket(name)
            bucket.object_versions.all().delete()
            logger.info(f"    ✓ Emptied {name}")
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in ('404', 'NoSuchBucket'):
                pass
            else:
                logger.warning(f"    ✗ {name}: {e.response['Error']['Message']}")


def _wait_for_stack_delete(cfn, expect_failure=False):
    """Wait for stack deletion to complete or fail."""
    max_wait = 600
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(15)
        elapsed += 15
        try:
            resp = cfn.describe_stacks(StackName=STACK_NAME)
            status = resp['Stacks'][0]['StackStatus']
            logger.info(f"  [{elapsed}s] Status: {status}")
            if status == 'DELETE_COMPLETE':
                logger.info("  ✓ Stack deleted")
                return
            if status == 'DELETE_FAILED':
                if expect_failure:
                    logger.info("  Delete failed as expected (stuck resources)")
                    return
                # Retry with retain — only include resources that actually failed to delete
                events = cfn.describe_stack_events(StackName=STACK_NAME).get('StackEvents', [])
                retain = set()
                for ev in events:
                    if ev.get('ResourceStatus') == 'DELETE_FAILED':
                        logical_id = ev['LogicalResourceId']
                        # Don't retain the stack itself
                        if logical_id != STACK_NAME:
                            retain.add(logical_id)
                
                # Filter out resources that have since been deleted
                try:
                    resources = cfn.list_stack_resources(StackName=STACK_NAME).get('StackResourceSummaries', [])
                    still_exists = {r['LogicalResourceId'] for r in resources if r['ResourceStatus'] != 'DELETE_COMPLETE'}
                    retain = retain & still_exists
                except Exception:
                    pass
                
                retain = list(retain)
                if retain:
                    logger.info(f"  Retrying with retain: {retain}")
                    cfn.delete_stack(StackName=STACK_NAME, RetainResources=retain)
                else:
                    # All failed resources were already deleted — try without retain
                    cfn.delete_stack(StackName=STACK_NAME)
                time.sleep(30)
                return
        except ClientError as e:
            if 'does not exist' in str(e):
                logger.info("  ✓ Stack deleted")
                return
            raise
    logger.warning(f"  Stack deletion timed out after {max_wait}s")


def step4_orphaned_resources():
    logger.info("\n[4/4] Cleaning orphaned resources...")

    # ── S3 Buckets (empty + delete, handles versioned buckets) ──
    logger.info("  S3 buckets...")
    s3_client = session.client('s3')
    s3_resource = session.resource('s3')

    for name in _clinicalcompass_bucket_names():
        try:
            s3_client.head_bucket(Bucket=name)
            # Bucket exists — empty it (versions + delete markers) then delete
            bucket = s3_resource.Bucket(name)
            bucket.object_versions.all().delete()
            bucket.delete()
            logger.info(f"    ✓ Deleted bucket {name}")
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in ('404', 'NoSuchBucket'):
                pass  # Bucket doesn't exist, skip silently
            else:
                logger.warning(f"    ✗ Bucket {name}: {e.response['Error']['Message']}")

    # ── ECR Repository ──
    logger.info("  ECR...")
    ecr = session.client('ecr')
    safe(lambda: ecr.delete_repository(repositoryName='bedrock-agentcore-clinicalcompass', force=True),
         "ECR repo bedrock-agentcore-clinicalcompass")

    # ── HealthLake Datastores ──
    logger.info("  HealthLake...")
    hl = session.client('healthlake')
    try:
        all_datastores = hl.list_fhir_datastores().get('DatastorePropertiesList', [])
        for ds in all_datastores:
            ds_id = ds['DatastoreId']
            ds_name = ds.get('DatastoreName', '')
            status = ds['DatastoreStatus']

            if 'clinicalcompass' not in ds_name.lower() and status == 'DELETED':
                continue

            if 'clinicalcompass' in ds_name.lower():
                if status == 'CREATING':
                    logger.info(f"    HealthLake {ds_id} ({ds_name}) is CREATING — waiting up to 10 min...")
                    for i in range(20):
                        time.sleep(30)
                        desc = hl.describe_fhir_datastore(DatastoreId=ds_id)
                        status = desc['DatastoreProperties']['DatastoreStatus']
                        if status != 'CREATING':
                            break
                        logger.info(f"    [{(i+1)*30}s] Still CREATING...")

                if status == 'ACTIVE':
                    safe(lambda d=ds_id: hl.delete_fhir_datastore(DatastoreId=d),
                         f"HealthLake {ds_name} ({ds_id})")
                elif status in ('DELETED', 'DELETING'):
                    logger.info(f"    - HealthLake {ds_name} already {status}")
                else:
                    logger.warning(f"    ✗ HealthLake {ds_name} in unexpected state: {status}")
    except Exception as e:
        logger.warning(f"    HealthLake: {e}")

    # ── OpenSearch Serverless ──
    logger.info("  OpenSearch Serverless...")
    aoss = session.client('opensearchserverless')
    try:
        collections = aoss.list_collections().get('collectionSummaries', [])
        for c in collections:
            if c['name'].startswith('cc-'):
                if c.get('status') == 'ACTIVE':
                    safe(lambda cid=c['id']: aoss.delete_collection(id=cid),
                         f"Collection {c['name']}")
                elif c.get('status') == 'CREATING':
                    logger.info(f"    - Collection {c['name']} still CREATING (will be cleaned on next run)")
                else:
                    logger.info(f"    - Collection {c['name']} status: {c.get('status')}")
    except Exception as e:
        logger.warning(f"    OpenSearch collections: {e}")

    # OpenSearch policies
    for name in ['ccehrmock', 'ccglobalguidelines', 'cclocalregulatory']:
        for suffix, ptype in [('-enc', 'encryption'), ('-net', 'network')]:
            safe(lambda n=f"{name}{suffix}", t=ptype: aoss.delete_security_policy(name=n, type=t),
                 f"Policy {name}{suffix}")
        safe(lambda n=f"{name}-data": aoss.delete_access_policy(name=n, type='data'),
             f"Access policy {name}-data")

    # ── SSM Parameters ──
    logger.info("  SSM Parameters...")
    ssm = session.client('ssm')
    try:
        params = ssm.get_parameters_by_path(Path='/clinicalcompass', Recursive=True).get('Parameters', [])
        for p in params:
            safe(lambda n=p['Name']: ssm.delete_parameter(Name=n), f"SSM {p['Name']}")
        if not params:
            logger.info("    - No SSM parameters found")
    except Exception:
        pass

    # ── IAM Roles (script-created, not CDK-managed) ──
    logger.info("  IAM Roles...")
    iam_client = session.client('iam')
    role_names = [
        'BedrockAgentCoreRuntime-clinicalcompass',
        'clinicalcompass-websocket-lambda-role',  # legacy shared role (pre-T15 split)
        'clinicalcompass-ws-connect-role',
        'clinicalcompass-ws-disconnect-role',
        'clinicalcompass-ws-ping-role',
        'clinicalcompass-ws-default-role',
        'clinicalcompass-ws-query-role',
        'BedrockKB-ClinicalCompass-EHR-Mock-Role',
        'BedrockKB-ClinicalCompass-Global-Guidelines-Role',
        'BedrockKB-ClinicalCompass-Local-Regulatory-Role',
    ]
    for role_name in role_names:
        try:
            # Detach all policies first
            for p in iam_client.list_attached_role_policies(RoleName=role_name).get('AttachedPolicies', []):
                iam_client.detach_role_policy(RoleName=role_name, PolicyArn=p['PolicyArn'])
            for p in iam_client.list_role_policies(RoleName=role_name).get('PolicyNames', []):
                iam_client.delete_role_policy(RoleName=role_name, PolicyName=p)
            # Delete instance profiles if any
            for ip in iam_client.list_instance_profiles_for_role(RoleName=role_name).get('InstanceProfiles', []):
                iam_client.remove_role_from_instance_profile(
                    InstanceProfileName=ip['InstanceProfileName'], RoleName=role_name)
            safe(lambda rn=role_name: iam_client.delete_role(RoleName=rn), f"Role {role_name}")
        except ClientError as e:
            if 'NoSuchEntity' in str(e):
                pass
            else:
                logger.warning(f"    ✗ Role {role_name}: {e}")


def main():
    parser = argparse.ArgumentParser(description='ClinicalCompass — Full Cleanup')
    parser.add_argument('--yes', '-y', action='store_true', help='Skip confirmation prompt')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  ClinicalCompass — Full Cleanup")
    logger.info("=" * 60)
    logger.info(f"  Account: {AWS_ACCOUNT_ID}")
    logger.info(f"  Region:  {AWS_REGION}")
    logger.info("")

    if not args.yes:
        response = input("This will DELETE all ClinicalCompass resources. Continue? [y/N] ")
        if response.lower() != 'y':
            logger.info("Cancelled.")
            return

    step1_knowledge_bases()
    step2_agentcore()
    step3_cdk_stack()
    step4_orphaned_resources()

    logger.info("\n" + "=" * 60)
    logger.info("  CLEANUP COMPLETE")
    logger.info("=" * 60)
    logger.info("\n  Fresh deployment:")
    logger.info("    cd infra && npx cdk deploy --require-approval never")
    logger.info("    cd .. && python deployment/scripts/build_and_push_image.py")
    logger.info("    python deployment/scripts/post_cdk_setup.py")
    logger.info("")


if __name__ == '__main__':
    main()
