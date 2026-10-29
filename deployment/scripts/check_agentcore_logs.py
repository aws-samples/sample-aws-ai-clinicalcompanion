#!/usr/bin/env python3
"""
Show recent AgentCore Runtime logs.

The runtime ID is resolved from SSM (/clinicalcompass/agent-runtime-arn, written by
post_cdk_setup.py), so it always matches the deployed runtime.

Usage:
    python deployment/scripts/check_agentcore_logs.py [--minutes 30] [--limit 200]
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent.parent))

AGENT_ARN_PARAM = '/clinicalcompass/agent-runtime-arn'


def get_session():
    """Use AWS_PROFILE/AWS_REGION from deployment/config.py, else the default credential chain."""
    try:
        from config import AWS_PROFILE, AWS_REGION
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    except Exception:
        return boto3.Session(region_name=os.environ.get('AWS_REGION', 'us-east-1'))


def get_runtime_id(session) -> str:
    """Return the deployed AgentCore runtime ID from its ARN in SSM."""
    try:
        arn = session.client('ssm').get_parameter(Name=AGENT_ARN_PARAM)['Parameter']['Value']
    except ClientError as e:
        sys.exit(f"Could not read {AGENT_ARN_PARAM} from SSM ({e}). Run post_cdk_setup.py first.")
    if '/' not in arn:
        sys.exit(f"{AGENT_ARN_PARAM} does not contain a runtime ARN (value: {arn!r}). Run post_cdk_setup.py first.")
    return arn.split('/')[-1]


def main():
    parser = argparse.ArgumentParser(description='Show recent AgentCore Runtime logs')
    parser.add_argument('--minutes', type=int, default=30, help='How far back to look (default: 30)')
    parser.add_argument('--limit', type=int, default=200, help='Maximum number of events to print (default: 200)')
    args = parser.parse_args()

    session = get_session()
    runtime_id = get_runtime_id(session)
    log_group = f'/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT'
    logs = session.client('logs')

    print(f"Runtime:   {runtime_id}")
    print(f"Log group: {log_group}")
    print(f"Window:    last {args.minutes} minutes\n")

    start_time = int((datetime.now() - timedelta(minutes=args.minutes)).timestamp() * 1000)
    events, kwargs = [], {'logGroupName': log_group, 'startTime': start_time}
    try:
        while True:
            resp = logs.filter_log_events(**kwargs)
            events.extend(resp['events'])
            if 'nextToken' not in resp:
                break
            kwargs['nextToken'] = resp['nextToken']
    except logs.exceptions.ResourceNotFoundException:
        sys.exit(f"Log group not found: {log_group}. The runtime may not have been invoked yet.")

    if not events:
        print("No log events in this window.")
        return

    events.sort(key=lambda e: e['timestamp'])
    shown = events[-args.limit:]
    if len(events) > len(shown):
        print(f"(showing the last {len(shown)} of {len(events)} events)\n")
    for event in shown:
        timestamp = datetime.fromtimestamp(event['timestamp'] / 1000)
        print(f"[{timestamp}] {event['message'].rstrip()}")


if __name__ == '__main__':
    main()
