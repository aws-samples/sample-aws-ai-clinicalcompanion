"""Lambda handler for patient API endpoints.

GET /patients — List all patients from DynamoDB patient index.
GET /patients/{patient_id} — Get a single patient record by ID.
"""

import json
import os
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["PATIENT_INDEX_TABLE_NAME"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def _get_caller_identity(event):
    """Return the authenticated caller's Cognito identity, or None.

    API Gateway's JWT authorizer has already validated the token by the time
    this Lambda runs; the verified claims are exposed here. We re-check that a
    principal is actually present so the handler fails closed and never serves
    PHI without an authenticated identity (defense in depth, not relying solely
    on the gateway).
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    return claims.get("sub") or claims.get("username")


def lambda_handler(event, context):
    """Route requests based on HTTP method and path parameters."""
    # Avoid logging the whole event (it can contain tokens/claims). Log only
    # the routing fields we need.
    path_parameters = event.get("pathParameters") or {}
    patient_id = path_parameters.get("patient_id")

    caller = _get_caller_identity(event)
    if not caller:
        # No authenticated principal — refuse to return any patient data.
        logger.warning("Unauthenticated patients-API request rejected")
        return _response(401, {"error": "Unauthorized"})

    # Threat model T9 (BOLA) is only PARTIALLY FIXED (authentication is
    # enforced above) and per-clinician authorization is DEFERRED: the demo
    # uses a single shared set of synthetic patients, so every authenticated
    # clinician may list and read the whole demo roster. A production build
    # MUST enforce per-clinician entitlement here (e.g. a patient<->clinician
    # assignment table) so a caller can only read patients they are authorized
    # for. See README "Security & Production Readiness" for the pattern.
    try:
        if patient_id:
            logger.info("Patient record accessed: patient=%s by caller=%s", patient_id, caller)
            return get_patient(patient_id)
        else:
            logger.info("Patient list accessed by caller=%s", caller)
            return list_patients()
    except Exception as e:
        logger.error("Error processing request: %s", str(e))
        return _response(500, {"error": "Internal server error"})


def list_patients():
    """Scan DynamoDB patient index and return summary list."""
    response = table.scan(
        ProjectionExpression="patient_id, patient_name, demographics",
    )

    items = response.get("Items", [])

    # Handle pagination for large datasets
    while "LastEvaluatedKey" in response:
        response = table.scan(
            ProjectionExpression="patient_id, patient_name, demographics",
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    # Map to API response format — extract age/sex from nested demographics
    patients = []
    for item in items:
        demographics = item.get("demographics", {})
        age = demographics.get("age")
        sex = demographics.get("sex_at_birth", "")
        patients.append({
            "patient_id": item.get("patient_id", ""),
            "patient_name": item.get("patient_name", ""),
            "age": int(age) if age is not None else None,
            "sex": sex,
        })

    return _response(200, {"patients": patients})


def get_patient(patient_id):
    """Get a single patient record by patient_id."""
    response = table.get_item(Key={"patient_id": patient_id})

    item = response.get("Item")
    if not item:
        return _response(404, {"error": f"Patient '{patient_id}' not found"})

    return _response(200, item)


def _response(status_code, body):
    """Build HTTP API response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, default=str),
    }
