"""
WebSocket Ping Handler
Handles keep-alive ping messages and responds with pong
"""

import boto3
import os
import time
import json
import logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['CONNECTIONS_TABLE_NAME'])

def lambda_handler(event, context):
    """
    Handle ping message and respond with pong
    
    Args:
        event: API Gateway WebSocket event
        context: Lambda context
        
    Returns:
        dict: Status code 200 on success
    """
    connection_id = event['requestContext']['connectionId']
    domain_name = event['requestContext']['domainName']
    stage = event['requestContext']['stage']
    
    # Update last active time
    try:
        table.update_item(
            Key={'connectionId': connection_id},
            UpdateExpression='SET lastActiveAt = :now',
            ExpressionAttributeValues={':now': int(time.time())}
        )
    except Exception as e:
        logger.warning(f"Failed to update activity: {e}")
    
    # Send pong response
    try:
        apigw_management = boto3.client('apigatewaymanagementapi',
                                       endpoint_url=f'https://{domain_name}/{stage}')
        
        apigw_management.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps({
                'type': 'pong',
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            }).encode('utf-8')
        )
        
        logger.info(f"Pong sent to {connection_id}")
    except Exception as e:
        logger.error(f"Failed to send pong: {e}")
    
    return {'statusCode': 200}
