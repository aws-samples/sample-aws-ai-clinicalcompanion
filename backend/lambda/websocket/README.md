# WebSocket Lambda Functions

This directory contains Lambda functions for the ClinicalCompass WebSocket streaming implementation.

## Functions

### 1. ConnectHandler (`connect.py`)
- **Route:** `$connect`
- **Purpose:** Handle new WebSocket connections
- **Actions:**
  - Store connection ID in DynamoDB
  - Set TTL for 24-hour expiration
  - Return success/failure

### 2. DisconnectHandler (`disconnect.py`)
- **Route:** `$disconnect`
- **Purpose:** Clean up when client disconnects
- **Actions:**
  - Remove connection from DynamoDB
  - Log disconnection
  - Always return success

### 3. QueryHandler (`query.py`)
- **Route:** `sendquery`
- **Purpose:** Process clinical queries and stream AgentCore responses
- **Actions:**
  - Parse query message
  - Retrieve session from DynamoDB
  - Send progress updates
  - Invoke AgentCore Runtime with streaming
  - Forward each event to client via Management API
  - Update session after completion
  - Handle errors gracefully

### 4. PingHandler (`ping.py`)
- **Route:** `ping`
- **Purpose:** Keep connection alive
- **Actions:**
  - Update lastActiveAt timestamp
  - Send pong response
  - Return success

### 5. DefaultHandler (`default.py`)
- **Route:** `$default`
- **Purpose:** Handle unknown message types
- **Actions:**
  - Send helpful error message
  - List valid actions
  - Return success

## Environment Variables

All functions require:
- `CONNECTIONS_TABLE_NAME`: DynamoDB table name (e.g., `clinicalcompass-websocket-connections`)

QueryHandler additionally requires:
- `AGENT_ARN`: AgentCore Runtime ARN
- `AWS_BEDROCK_REGION`: AWS region for Bedrock

## Message Formats

### Client → Server

#### Send Query
```json
{
  "action": "sendquery",
  "query": "68-year-old male with eGFR 32, needs MRI with gadolinium",
  "patientId": "001",
  "sessionId": "esr_session_456"
}
```

#### Ping
```json
{
  "action": "ping"
}
```

### Server → Client

#### Progress
```json
{
  "type": "progress",
  "message": "Processing your query...",
  "timestamp": "2026-02-19T14:30:00.000Z"
}
```

#### Chunk
```json
{
  "type": "chunk",
  "data": "Based on the patient's eGFR of 32...",
  "chunkNumber": 42,
  "timestamp": "2026-02-19T14:30:01.500Z"
}
```

#### Complete
```json
{
  "type": "complete",
  "sessionId": "esr_session_456",
  "totalChunks": 4902,
  "timestamp": "2026-02-19T14:32:05.000Z"
}
```

#### Error
```json
{
  "type": "error",
  "error": "AgentCore invocation failed: timeout",
  "timestamp": "2026-02-19T14:30:30.000Z"
}
```

#### Pong
```json
{
  "type": "pong",
  "timestamp": "2026-02-19T14:30:00.000Z"
}
```

## Deployment

### Automated Deployment

WebSocket Lambda functions are deployed automatically via the CDK stack. See `infra/README.md` for deployment instructions.

### Manual Deployment

1. Package each function:
```bash
cd lambda/websocket
zip connect.zip connect.py
zip disconnect.zip disconnect.py
zip query.zip query.py
zip ping.zip ping.py
zip default.zip default.py
```

2. Create Lambda functions:
```bash
aws lambda create-function \
  --function-name clinicalcompass-ws-connect \
  --runtime python3.11 \
  --role arn:aws:iam::ACCOUNT_ID:role/lambda-role \
  --handler connect.lambda_handler \
  --zip-file fileb://connect.zip \
  --timeout 30 \
  --memory-size 256 \
  --environment Variables={CONNECTIONS_TABLE_NAME=clinicalcompass-websocket-connections} \
  --region us-east-1 \
  --profile <YOUR_PROFILE>
```

3. Repeat for other functions

## Testing

### Unit Tests

```bash
cd tests/websocket
python -m pytest test_connect_handler.py
python -m pytest test_disconnect_handler.py
python -m pytest test_query_handler.py
python -m pytest test_ping_handler.py
python -m pytest test_default_handler.py
```

### Integration Tests

```bash
# Set WebSocket URL
export WEBSOCKET_URL="wss://YOUR_API_ID.execute-api.us-east-1.amazonaws.com/production"

# Run tests
python tests/websocket/test_websocket_basic.py $WEBSOCKET_URL
```

### Manual Testing with wscat

The `$connect` route requires a Cognito access token (see "Authentication" in the root README for creating a user):

```bash
TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id <COGNITO_CLIENT_ID> --region us-east-1 \
  --auth-parameters 'USERNAME=doctor,PASSWORD=<STRONG_PASSWORD>' \
  --query 'AuthenticationResult.AccessToken' --output text)

# Connect (wscat runs via npx, no global install needed)
npx wscat -c "wss://<API_ID>.execute-api.us-east-1.amazonaws.com/production?Authorization=$TOKEN"

# Send ping
> {"action": "ping"}

# Send query
> {"action": "sendquery", "query": "Test query", "patientId": "001"}
```

## Monitoring

### CloudWatch Logs

Each function has its own log group:
- `/aws/lambda/clinicalcompass-ws-connect`
- `/aws/lambda/clinicalcompass-ws-disconnect`
- `/aws/lambda/clinicalcompass-ws-query`
- `/aws/lambda/clinicalcompass-ws-ping`
- `/aws/lambda/clinicalcompass-ws-default`

### CloudWatch Metrics

Monitor:
- Lambda invocations
- Lambda errors
- Lambda duration
- DynamoDB read/write capacity
- API Gateway connection count
- API Gateway message count

## Troubleshooting

### Connection Fails

Check CloudWatch Logs:
```bash
aws logs tail /aws/lambda/clinicalcompass-ws-connect --follow
```

### Query Timeout

Increase Lambda timeout:
```bash
aws lambda update-function-configuration \
  --function-name clinicalcompass-ws-query \
  --timeout 900
```

### DynamoDB Access Denied

Each handler has its own least-privilege role (`clinicalcompass-ws-<name>-role`, defined in `infra/lib/clinicalcompass-stack.ts`). A new DynamoDB call in a handler needs a matching action on that handler's role. Inspect a role's policy:
```bash
aws iam list-role-policies --role-name clinicalcompass-ws-query-role
aws iam get-role-policy --role-name clinicalcompass-ws-query-role \
  --policy-name <policy-name-from-previous-command>
```

## References

- [AWS WebSocket API Documentation](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-websocket-api.html)
