"""Tamper Detector Lambda - detects unauthorized S3 guideline modifications.

Evaluates S3 events from EventBridge and alerts on unauthorized access to the
guidelines bucket. All events (authorized and unauthorized) are logged with
principal ARN, action, and object key.
"""
import json
import logging
import os
from dataclasses import asdict, dataclass

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sns_client = boto3.client("sns")

SECURITY_ALERTS_TOPIC_ARN = os.environ.get("SECURITY_ALERTS_TOPIC_ARN", "")
ALLOWED_ROLES = json.loads(os.environ.get("ALLOWED_ROLES", "[]"))
GUIDELINES_BUCKET = os.environ.get("GUIDELINES_BUCKET", "")


@dataclass
class TamperEvent:
    """Represents a tamper detection event."""

    principal_arn: str
    action: str
    object_key: str
    bucket_name: str
    timestamp: str
    is_authorized: bool


def classify_principal(principal_arn: str, allowed_roles: list[str] | None = None) -> bool:
    """Check if a principal is authorized to modify guidelines.

    Args:
        principal_arn: The ARN or identifier of the principal that performed the action.
        allowed_roles: List of allowed role ARNs. Defaults to ALLOWED_ROLES env var.

    Returns:
        True if the principal is authorized, False otherwise.
    """
    if allowed_roles is None:
        allowed_roles = ALLOWED_ROLES

    if not principal_arn:
        return False

    for allowed_role in allowed_roles:
        if not allowed_role:
            continue
        # Exact match
        if principal_arn == allowed_role:
            return True
        # Handle assumed-role principals: arn:aws:sts::ACCOUNT:assumed-role/ROLE_NAME/SESSION
        # The allowed_roles contain IAM role ARNs like: arn:aws:iam::ACCOUNT:role/ROLE_NAME
        if ":assumed-role/" in principal_arn:
            assumed_parts = principal_arn.split(":assumed-role/")
            if len(assumed_parts) == 2:
                role_name = assumed_parts[1].split("/")[0]
                if f":role/{role_name}" in allowed_role or f"/{role_name}" in allowed_role:
                    return True
        # Check if the allowed role is a substring of the principal ARN
        if allowed_role in principal_arn:
            return True

    return False


def process_event(event: dict, allowed_roles: list[str] | None = None) -> TamperEvent:
    """Process an EventBridge S3 event and determine if it's authorized.

    Args:
        event: The EventBridge event dict.
        allowed_roles: Optional list of allowed role ARNs for testing.

    Returns:
        TamperEvent with classification result.
    """
    detail = event.get("detail", {})
    detail_type = event.get("detail-type", "")

    # Extract event details
    bucket_name = detail.get("bucket", {}).get("name", "")
    object_key = detail.get("object", {}).get("key", "")
    timestamp = event.get("time", "")

    # Map detail-type to action
    action = "ObjectCreated" if "Created" in detail_type else "ObjectRemoved"

    # Extract principal information from EventBridge S3 event
    user_identity = detail.get("userIdentity", {})
    principal_arn = (
        user_identity.get("principalId", "")
        or user_identity.get("arn", "")
        or detail.get("requester", "")
        or event.get("account", "")
    )

    # Classify the principal
    is_authorized = classify_principal(principal_arn, allowed_roles)

    tamper_event = TamperEvent(
        principal_arn=principal_arn,
        action=action,
        object_key=object_key,
        bucket_name=bucket_name,
        timestamp=timestamp,
        is_authorized=is_authorized,
    )

    # Log ALL events (authorized and unauthorized) — Requirement 8.5
    log_entry = {
        "event_type": "guideline_modification",
        "principal_arn": principal_arn,
        "action": action,
        "object_key": object_key,
        "bucket_name": bucket_name,
        "timestamp": timestamp,
        "is_authorized": is_authorized,
    }
    logger.info(json.dumps(log_entry))

    # Publish SNS alert for unauthorized modifications — Requirement 8.3
    if not is_authorized and SECURITY_ALERTS_TOPIC_ARN:
        alert_message = {
            "alert_type": "UNAUTHORIZED_GUIDELINE_MODIFICATION",
            "severity": "HIGH",
            "details": {
                "principal_arn": principal_arn,
                "action": action,
                "object_key": object_key,
                "bucket_name": bucket_name,
                "timestamp": timestamp,
            },
            "message": (
                f"Unauthorized modification detected on guidelines bucket. "
                f"Principal: {principal_arn}, Action: {action}, Object: {object_key}"
            ),
        }
        try:
            sns_client.publish(
                TopicArn=SECURITY_ALERTS_TOPIC_ARN,
                Subject="ALERT: Unauthorized Guideline Modification Detected",
                Message=json.dumps(alert_message, indent=2),
            )
            logger.info(f"SNS alert published for unauthorized access by {principal_arn}")
        except Exception as e:
            logger.error(f"Failed to publish SNS alert: {e}")

    return tamper_event


def lambda_handler(event, context):
    """Lambda entry point — processes EventBridge S3 events.

    Args:
        event: EventBridge event dict.
        context: Lambda context object.

    Returns:
        Response dict with status code and body.
    """
    logger.info(f"Tamper detector invoked with event: {json.dumps(event)}")

    try:
        tamper_event = process_event(event)
        return {
            "statusCode": 200,
            "body": json.dumps(asdict(tamper_event)),
        }
    except Exception as e:
        logger.error(f"Error processing tamper detection event: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
