#!/usr/bin/env python3
"""
Build and push the AgentCore Docker image to ECR.

Designed to be run standalone during development or from CI/CD pipelines.
Reads ECR URI from CDK stack outputs or deployment/config.py.

Usage:
    python deployment/scripts/build_and_push_image.py
    python deployment/scripts/build_and_push_image.py --tag v1.2.3
"""

import sys
import os
import shlex
import subprocess
import logging
import argparse
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
STACK_NAME = 'ClinicalCompassStack'
REPO_NAME = 'bedrock-agentcore-clinicalcompass'
RUNTIME_NAME = 'clinicalcompass'
PLATFORM = 'linux/arm64'  # AgentCore Runtime only runs arm64 container images


def get_session():
    try:
        from config import AWS_PROFILE, AWS_REGION
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    except Exception:
        return boto3.Session(region_name=os.environ.get('AWS_REGION', 'us-east-1'))


def get_ecr_uri(session) -> str:
    """Get ECR URI from CDK outputs, falling back to config.py."""
    # Try CDK outputs first
    try:
        cfn = session.client('cloudformation')
        resp = cfn.describe_stacks(StackName=STACK_NAME)
        for o in resp['Stacks'][0].get('Outputs', []):
            if o['OutputKey'] == 'EcrRepositoryUri':
                return o['OutputValue']
    except Exception:
        pass

    # Fallback to config.py
    try:
        from config import ECR_URI
        return ECR_URI
    except Exception:
        pass

    # Construct from account/region
    account = session.client('sts').get_caller_identity()['Account']
    region = session.region_name
    return f"{account}.dkr.ecr.{region}.amazonaws.com/{REPO_NAME}"


def _succeeded(result) -> bool:
    """Return True if a completed process succeeded; log stderr otherwise.

    Only inspects an already-completed CompletedProcess so that every
    subprocess invocation stays a literal, statically-auditable call at its
    own call site (no dynamic command strings, no shell interpretation).
    """
    if result.returncode != 0:
        logger.error(result.stderr[-1000:] if result.stderr else 'No error output')
        return False
    return True


def _get_runtime_configuration(control, runtime_name: str) -> tuple[str | None, dict | None]:
    """Return the runtime id and current runtime configuration, if it exists."""
    try:
        runtimes = control.list_agent_runtimes().get('agentRuntimes', [])
    except Exception as exc:
        logger.warning(f"  Could not list AgentCore runtimes: {exc}")
        return None, None

    for runtime in runtimes:
        if runtime.get('agentRuntimeName') != runtime_name:
            continue

        runtime_id = runtime['agentRuntimeId']
        try:
            return runtime_id, control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:
            logger.warning(f"  Could not read runtime configuration for {runtime_name}: {exc}")
            return runtime_id, None

    return None, None


def _wait_for_runtime_ready(control, runtime_id: str, expected_image: str) -> bool:
    """Poll until the runtime is READY on the requested image."""
    for attempt in range(18):
        runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = runtime.get('status')
        current_image = (
            runtime.get('agentRuntimeArtifact', {})
            .get('containerConfiguration', {})
            .get('containerUri')
        )
        logger.info(f"  Poll {attempt + 1}: status={status} image={current_image}")
        if status == 'READY' and current_image == expected_image:
            return True
        if status in ('FAILED', 'DELETE_FAILED'):
            return False
        time.sleep(10)
    return False


def update_existing_runtime(session, image_uri: str, runtime_name: str = RUNTIME_NAME) -> None:
    """Update the existing AgentCore runtime to the newly pushed image, if present."""
    control = session.client('bedrock-agentcore-control')
    runtime_id, runtime = _get_runtime_configuration(control, runtime_name)

    if not runtime_id:
        logger.info("  No existing AgentCore runtime found; skipping runtime update")
        return
    if not runtime:
        logger.warning("  Runtime exists but configuration could not be read; skipping update")
        return

    current_image = (
        runtime.get('agentRuntimeArtifact', {})
        .get('containerConfiguration', {})
        .get('containerUri')
    )
    if current_image == image_uri:
        logger.info(
            f"  Runtime already references {image_uri}; forcing refresh so it pulls "
            "the newly pushed image"
        )

    logger.info("\n[3/3] Updating AgentCore runtime...")
    control.update_agent_runtime(
        agentRuntimeId=runtime_id,
        roleArn=runtime['roleArn'],
        networkConfiguration=runtime['networkConfiguration'],
        protocolConfiguration=runtime['protocolConfiguration'],
        agentRuntimeArtifact={
            'containerConfiguration': {
                'containerUri': image_uri,
            }
        },
    )
    logger.info(f"  Requested runtime update for {runtime_name} -> {image_uri}")

    if _wait_for_runtime_ready(control, runtime_id, image_uri):
        logger.info("  ✓ Runtime updated")
    else:
        logger.error("  Runtime update did not reach READY in time")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Build and push AgentCore Docker image')
    parser.add_argument('--tag', default='latest', help='Image tag (default: latest)')
    parser.add_argument('--no-push', action='store_true', help='Build only, do not push')
    parser.add_argument(
        '--skip-runtime-update',
        action='store_true',
        help='Do not update an existing AgentCore runtime after push',
    )
    args = parser.parse_args()

    session = get_session()
    ecr_uri = get_ecr_uri(session)
    region = session.region_name
    account = ecr_uri.split('.')[0]
    tag = args.tag

    # Sanitize user-configurable tag for safe use in log messages
    safe_tag = shlex.quote(tag)

    logger.info("=" * 60)
    logger.info("  ClinicalCompass — Build & Push Image")
    logger.info("=" * 60)
    logger.info(f"  ECR URI: {ecr_uri}")
    logger.info(f"  Tag:     {safe_tag}")

    # Preflight: buildx is required to produce the linux/arm64 image AgentCore runs
    buildx = subprocess.run(['docker', 'buildx', 'version'], capture_output=True, text=True, shell=False)
    if buildx.returncode != 0:
        logger.error(
            "  docker buildx is required (AgentCore Runtime only runs linux/arm64 images).\n"
            "  Docker Desktop includes it. With Colima: `brew install docker-buildx` and link the\n"
            "  plugin into ~/.docker/cli-plugins/. On x86 Linux, also enable arm64 emulation:\n"
            "  `docker run --privileged --rm tonistiigi/binfmt --install arm64`."
        )
        sys.exit(1)

    # Login to ECR (use the same profile as the boto3 session, not the CLI default)
    logger.info("\n[1/3] Logging into ECR...")
    profile_args = ['--profile', session.profile_name] if session.profile_name else []
    login_pw = subprocess.run(
        ['aws', 'ecr', 'get-login-password', '--region', region] + profile_args,
        capture_output=True, text=True, shell=False
    )
    if login_pw.returncode != 0:
        logger.error(f"Failed to get ECR login: {login_pw.stderr}")
        sys.exit(1)

    login = subprocess.run(
        ['docker', 'login', '--username', 'AWS', '--password-stdin',
         f'{account}.dkr.ecr.{region}.amazonaws.com'],
        input=login_pw.stdout, capture_output=True, text=True, shell=False
    )
    if login.returncode != 0:
        logger.error(f"ECR login failed: {login.stderr}")
        sys.exit(1)
    logger.info("  ✓ Logged in")

    # Build for linux/arm64 (the only architecture AgentCore Runtime runs) and push in one
    # step. buildx works the same on Apple Silicon, Intel/AMD and CI runners, and avoids the
    # legacy builder's "cannot overwrite digest" error with the digest-pinned base image.
    dockerfile = PROJECT_ROOT / '.bedrock_agentcore' / 'testing_deployment' / 'Dockerfile'
    image_uri = f'{ecr_uri}:{tag}'
    if args.no_push:
        logger.info("\n[2/3] Building linux/arm64 image (local only, --no-push)...")
        build = subprocess.run(
            ['docker', 'buildx', 'build', '--platform', PLATFORM, '-f', str(dockerfile),
             '-t', f'{REPO_NAME}:{tag}', '--load', '.'],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, shell=False
        )
    else:
        logger.info(f"\n[2/3] Building linux/arm64 image and pushing to {ecr_uri}:{safe_tag}...")
        build = subprocess.run(
            ['docker', 'buildx', 'build', '--platform', PLATFORM, '-f', str(dockerfile),
             '-t', image_uri, '--push', '.'],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, shell=False
        )
    if not _succeeded(build):
        logger.error("  Build failed")
        sys.exit(1)
    logger.info("  ✓ Built" + ("" if args.no_push else " and pushed"))

    if args.no_push:
        logger.info("\n[3/3] Runtime update skipped (--no-push)")
    elif args.skip_runtime_update:
        logger.info("\n[3/3] Runtime update skipped (--skip-runtime-update)")
    else:
        update_existing_runtime(session, image_uri)

    logger.info("\n" + "=" * 60)
    logger.info("  IMAGE BUILD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Image: {image_uri}")
    logger.info("")


if __name__ == '__main__':
    main()
