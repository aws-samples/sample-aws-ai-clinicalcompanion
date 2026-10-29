"""Lambda handler for guideline API endpoints.

GET /guidelines — List all guidelines from DynamoDB guideline registry.
"""

import json
import os
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["GUIDELINE_REGISTRY_TABLE_NAME"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    """Handle GET /guidelines request."""
    logger.info("Received event: %s", json.dumps(event))

    try:
        return list_guidelines()
    except Exception as e:
        logger.error("Error processing request: %s", str(e))
        return _response(500, {"error": "Internal server error"})


def list_guidelines():
    """Scan DynamoDB guideline registry and return summary list."""
    response = table.scan(
        ProjectionExpression="source_name, display_name, #s",
        ExpressionAttributeNames={"#s": "status"},
    )

    items = response.get("Items", [])

    # Handle pagination for large datasets
    while "LastEvaluatedKey" in response:
        response = table.scan(
            ProjectionExpression="source_name, display_name, #s",
            ExpressionAttributeNames={"#s": "status"},
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    # Map to API response format
    guidelines = [
        {
            "source_name": item.get("source_name", ""),
            "display_name": item.get("display_name", ""),
            "status": item.get("status", "active"),
        }
        for item in items
    ]

    return _response(200, {"guidelines": guidelines})


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
