"""
WebSocket Connect Handler
Handles new WebSocket connections and stores connection info in DynamoDB
"""

import boto3
import os
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['CONNECTIONS_TABLE_NAME'])

def lambda_handler(event, context):
    """
    Handle new WebSocket connection
    
    Args:
        event: API Gateway WebSocket event
        context: Lambda context
        
    Returns:
        dict: Status code 200 on success, 500 on error
    """
    connection_id = event['requestContext']['connectionId']

    # Capture the authenticated Cognito identity from the $connect authorizer
    # context. Storing it on the connection lets later routes (e.g. the query
    # handler's rate limiter) key on the stable user identity instead of the
    # per-connection id — otherwise a client could reset its rate-limit window
    # simply by reconnecting.
    authorizer_ctx = event['requestContext'].get('authorizer') or {}
    user_sub = authorizer_ctx.get('sub') or authorizer_ctx.get('username') or ''

    try:
        # Store connection in DynamoDB with TTL
        item = {
            'connectionId': connection_id,
            'connectedAt': int(time.time()),
            'lastActiveAt': int(time.time()),
            'ttl': int(time.time()) + 86400  # 24 hours
        }
        if user_sub:
            item['userSub'] = user_sub

        table.put_item(Item=item)

        logger.info(f"Connection established: {connection_id}")
        return {'statusCode': 200}
        
    except Exception as e:
        logger.error(f"Connection failed: {e}", exc_info=True)
        return {'statusCode': 500}
