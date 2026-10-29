"""
WebSocket Default Handler
Handles unknown message types and sends helpful error messages
"""

import boto3
import json
import logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Handle unknown message types
    
    Args:
        event: API Gateway WebSocket event
        context: Lambda context
        
    Returns:
        dict: Status code 200 (error sent to client)
    """
    connection_id = event['requestContext']['connectionId']
    domain_name = event['requestContext']['domainName']
    stage = event['requestContext']['stage']
    
    body = json.loads(event.get('body', '{}'))
    action = body.get('action', 'none')
    
    logger.info(f"Unknown action received: {action} from {connection_id}")
    
    try:
        apigw_management = boto3.client('apigatewaymanagementapi',
                                       endpoint_url=f'https://{domain_name}/{stage}')
        
        apigw_management.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps({
                'type': 'error',
                'error': f"Unknown action: {action}",
                'validActions': ['sendquery', 'ping'],
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            }).encode('utf-8')
        )
    except Exception as e:
        logger.error(f"Failed to send error: {e}")
    
    return {'statusCode': 200}
