"""
WebSocket Disconnect Handler
Cleans up connection data when client disconnects
"""

import boto3
import os
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['CONNECTIONS_TABLE_NAME'])

def lambda_handler(event, context):
    """
    Handle WebSocket disconnection
    
    Args:
        event: API Gateway WebSocket event
        context: Lambda context
        
    Returns:
        dict: Status code 200 (always succeeds)
    """
    connection_id = event['requestContext']['connectionId']
    
    try:
        # Remove connection from DynamoDB
        table.delete_item(Key={'connectionId': connection_id})
        logger.info(f"Connection removed: {connection_id}")
    except Exception as e:
        # Don't fail on disconnect errors
        logger.warning(f"Failed to remove connection: {e}")
    
    return {'statusCode': 200}
