"""
WebSocket $connect route authorizer Lambda.

Validates a Cognito access token from the query string by calling Cognito's
GetUser API. This avoids bundling extra JWT libraries into the Lambda package.
Returns an IAM policy document (Allow/Deny) for API Gateway REQUEST authorizer.
"""

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cognito = boto3.client("cognito-idp")


def handler(event: dict, context: Any) -> dict:
    """Lambda REQUEST authorizer for WebSocket $connect route."""
    logger.info("WebSocket authorizer invoked")

    # Extract token from query string parameters
    token = _extract_token(event)
    if not token:
        logger.warning("No token found in request")
        return _generate_policy("Deny", event)

    # Validate the Cognito access token
    try:
        claims = _validate_access_token(token)
        logger.info(f"Token validated for sub: {claims.get('sub', 'unknown')}")
        return _generate_policy("Allow", event, claims)
    except Exception as e:
        logger.warning(f"Token validation failed: {e}")
        return _generate_policy("Deny", event)


def _extract_token(event: dict) -> str | None:
    """Extract JWT token from query string parameters."""
    query_params = event.get("queryStringParameters") or {}

    # Try 'Authorization' first, then 'token' as fallback
    token = query_params.get("Authorization") or query_params.get("token")

    if token and token.startswith("Bearer "):
        token = token[7:]

    return token


def _validate_access_token(token: str) -> dict:
    """Validate an access token via Cognito and normalize a minimal claims map."""
    try:
        response = cognito.get_user(AccessToken=token)
    except ClientError as exc:
        raise ValueError(exc.response["Error"]["Message"]) from exc

    attributes = {
        item["Name"]: item["Value"]
        for item in response.get("UserAttributes", [])
    }
    username = response.get("Username", "")

    return {
        "sub": attributes.get("sub", username),
        "email": attributes.get("email", ""),
        "cognito:username": username,
        "token_use": "access",
    }


def _generate_policy(effect: str, event: dict, claims: dict | None = None) -> dict:
    """Generate IAM policy document for API Gateway authorizer response."""
    # Extract the method ARN from the event
    method_arn = event.get("methodArn", "")

    # Build a wildcard ARN to allow all routes on this API+stage
    # Format: arn:aws:execute-api:{region}:{account}:{apiId}/{stage}/{method}/{resource}
    if method_arn:
        arn_parts = method_arn.split(":")
        api_gateway_arn = ":".join(arn_parts[:5])
        api_id_stage = arn_parts[5].split("/")
        resource_arn = f"{api_gateway_arn}:{api_id_stage[0]}/{api_id_stage[1]}/*"
    else:
        resource_arn = "*"

    # Principal ID (sub from token or 'unauthorized')
    principal_id = "unauthorized"
    if claims:
        principal_id = claims.get("sub", claims.get("cognito:username", "user"))

    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource_arn,
                }
            ],
        },
    }

    # Pass claims as context (accessible in $connect Lambda via requestContext.authorizer)
    if claims and effect == "Allow":
        policy["context"] = {
            "sub": claims.get("sub", ""),
            "email": claims.get("email", ""),
            "username": claims.get("cognito:username", ""),
        }

    return policy
