# ClinicalCompass CDK Infrastructure

## What the CDK Stack Deploys

The `ClinicalCompassStack` provisions all AWS infrastructure in a single `npx cdk deploy` command:

### Compute

| Resource | Purpose |
|----------|---------|
| 5 Lambda functions (WebSocket) | `connect`, `disconnect`, `query`, `ping`, `default` handlers, each with its own least-privilege IAM role (`clinicalcompass-ws-<name>-role`) |
| EHR Ingestion Lambda | S3-triggered processor for patient data → DynamoDB + KB |
| Guideline Ingestion Lambda | S3-triggered processor for guideline docs → KB + registry |
| Patients API Lambda | REST handler for `GET /patients` and `GET /patients/{id}` |
| Guidelines API Lambda | REST handler for `GET /guidelines` |
| AgentCore Keep-Alive Lambda | CloudWatch Events rule pings AgentCore every 5 min |

### Networking & API

| Resource | Purpose |
|----------|---------|
| WebSocket API Gateway | Real-time clinical query interface (routes: `$connect`, `$disconnect`, `$default`, `sendquery`, `ping`) |
| HTTP API Gateway | REST endpoints for patient/guideline data (`/patients`, `/guidelines`) |

### Storage

| Resource | Purpose |
|----------|---------|
| DynamoDB `clinicalcompass-websocket-connections` | WebSocket connection tracking with TTL |
| DynamoDB `clinicalcompass-patient-index` | Structured patient records for direct lookup |
| DynamoDB `clinicalcompass-guideline-registry` | Available guideline sources for the selector |
| S3 `clinicalcompass-ehr-data-{account}` | EHR patient data (triggers ingestion Lambda) |
| S3 `clinicalcompass-guidelines-{account}` | Guideline documents (triggers ingestion Lambda) |
| S3 `clinicalcompass-ehr-mock-{account}` | KB data bucket for EHR RAG retrieval |
| S3 `clinicalcompass-global-guidelines-{account}` | KB data bucket for guidelines RAG retrieval |
| S3 `clinicalcompass-local-regulatory-{account}` | KB data bucket for regulatory RAG retrieval |
| S3 `clinicalcompass-frontend-{account}` | Frontend static files (HTML/JS/CSS) served via CloudFront |

### Frontend Hosting

| Resource | Purpose |
|----------|---------|
| S3 `clinicalcompass-frontend-{account}` | Static file origin (block all public access) |
| CloudFront Distribution | HTTPS serving with OAC for S3; caching disabled in dev |
| Origin Access Control (OAC) | SigV4 signed requests from CloudFront to S3 |

### Search & AI

| Resource | Purpose |
|----------|---------|
| 3 OpenSearch Serverless collections | Vector search for EHR, guidelines, and regulatory KBs |
| OpenSearch security policies | Encryption, network, and data access policies per collection |
| ECR repository | Container images for AgentCore runtime |

### Auth & Identity

| Resource | Purpose |
|----------|---------|
| Cognito User Pool | Authentication (`clinicalcompass-users`) |
| Cognito App Client | Browser-based auth (no secret, SRP + password flows) |

### IAM

| Resource | Purpose |
|----------|---------|
| AgentCore Role | Bedrock invoke, KB retrieve, SSM read, AOSS access, ECR pull, logs |
| WebSocket Lambda Role | DynamoDB, API Gateway manage connections, AgentCore invoke, SSM, logs |
| Per-Lambda execution roles | Auto-created by CDK for ingestion and API Lambdas |

### Configuration

| Resource | Purpose |
|----------|---------|
| SSM `/clinicalcompass/stm-memory-id` | AgentCore memory ID (set by post_cdk_setup.py) |
| SSM `/clinicalcompass/agent-runtime-arn` | AgentCore runtime ARN (set by post_cdk_setup.py) |
| SSM `/clinicalcompass/patient-index-table` | Patient DynamoDB table name |
| SSM `/clinicalcompass/guideline-registry-table` | Guideline registry table name |
| SSM `/clinicalcompass/ehr-data-bucket` | EHR data S3 bucket name |
| SSM `/clinicalcompass/guidelines-bucket` | Guidelines S3 bucket name |
| SSM `/clinicalcompass/fhir-datastore-id` | Placeholder for an optional HealthLake data store ID |
| SSM `/clinicalcompass/fhir-datastore-endpoint` | Placeholder for an optional HealthLake data store endpoint |
| SSM `/clinicalcompass/http-api-endpoint` | HTTP API Gateway URL |
| SSM `/clinicalcompass/cloudfront-url` | CloudFront distribution URL for frontend |
| SSM `/clinicalcompass/cloudfront-distribution-id` | CloudFront distribution ID (cache invalidation) |
| SSM `/clinicalcompass/frontend-bucket` | S3 bucket name for frontend static files |
| SSM `/clinicalcompass/ehr-ingestion-lambda-arn` | EHR ingestion Lambda ARN |
| SSM `/clinicalcompass/guideline-ingestion-lambda-arn` | Guideline ingestion Lambda ARN |

## What Scripts Handle (runtime operations CDK cannot manage)

| Script | Purpose | When to run |
|--------|---------|-------------|
| `post_cdk_setup.py` | OpenSearch indices, Bedrock KBs, AgentCore Memory, AgentCore Runtime, data upload, config updates | After `cdk deploy` |
| `build_and_push_image.py` | Docker build & push to ECR, then refresh an existing AgentCore runtime | After backend code changes |
| `deploy_frontend.py` | Generate config.js, upload to S3, invalidate CloudFront | After frontend code changes |
| `cleanup_all.py` | Full teardown of all resources (stack + orphans) | Before fresh redeploy |

## Deployment

### Fresh deploy (from scratch)

```bash
# 1. Configure
cp deployment/config.example.py deployment/config.py
# Edit: AWS_PROFILE, AWS_REGION, AWS_ACCOUNT_ID

# 2. Deploy infrastructure (includes S3 + CloudFront for frontend)
cd infra && npm install && npx cdk deploy --require-approval never

# 3. Build and push agent container (requires Docker)
cd .. && python deployment/scripts/build_and_push_image.py

# 4. Complete setup (KBs, memory, runtime, data, configs)
python deployment/scripts/post_cdk_setup.py

# 5. Deploy frontend to CloudFront
python deployment/scripts/deploy_frontend.py
# Access via CloudFront URL printed at the end

# 6. Create a Cognito user for login
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name ClinicalCompassStack \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username doctor \
  --temporary-password '<STRONG_PASSWORD>' \
  --message-action SUPPRESS \
  --region us-east-1

aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username doctor \
  --password '<STRONG_PASSWORD>' \
  --permanent \
  --region us-east-1
```

### Running locally (development)

After step 4 above (which generates `frontend/config.js`), you can serve the frontend locally:

```bash
cd frontend && python3 -m http.server 8080
# Open http://localhost:8080
# Login with the Cognito user you created above
```

Both methods use the same `config.js` with correct WebSocket/API/Cognito values.

### Full teardown

```bash
python deployment/scripts/cleanup_all.py --yes
```

Handles: KBs, AgentCore, CDK stack (including ROLLBACK_FAILED states), orphaned S3 buckets, ECR, HealthLake, OpenSearch, SSM, IAM roles.

Bedrock can still retain stale Knowledge Base records in `DELETE_UNSUCCESSFUL` after delete requests complete. Redeploy remains safe because `post_cdk_setup.py` automatically recovers by reusing or minting fallback Knowledge Base names when those tombstones block the canonical names.

### Day-to-day

| Change | Command |
|--------|---------|
| Backend agent code | `python deployment/scripts/build_and_push_image.py` |
| Frontend code | `python deployment/scripts/deploy_frontend.py` |
| Lambda handler code | `cd infra && npx cdk deploy` |
| Infrastructure | `cd infra && npx cdk diff && npx cdk deploy` |
| KB data | `python deployment/scripts/post_cdk_setup.py --skip-runtime --skip-memory` |

## Design Decisions

1. **No HealthLake by default** — The sample stores patient data in DynamoDB and the EHR Knowledge Base. HealthLake is an optional extension (see "Extending with AWS HealthLake" in the architecture docs).
2. **No provisioned concurrency** — Removed to avoid deployment failures when Lambda code can't initialize. Can be re-added once the system is stable.
3. **No `autoDeleteObjects` on S3 buckets** — The custom resource Lambda it requires can fail during stack creation, causing cascade rollback failures. Cleanup handled by `cleanup_all.py`.
4. **All removal policies set to DESTROY** — This is a demo/dev environment. Production would use RETAIN.
5. **OpenSearch collections** — Take 2-5 min to create. CDK handles the wait, but they're the slowest resource in the stack.

## Troubleshooting

### OpenSearch Serverless — `503 DNS resolution failure` during `post_cdk_setup.py`

**Symptom:** Index creation fails with `TransportError(503, 'DNS resolution failure')` even though the collection shows as ACTIVE.

**Cause:** OpenSearch Serverless collection DNS endpoints take 2-10 minutes to propagate after creation. The collection is created but its HTTPS endpoint isn't resolvable yet.

**Fix:** Wait 3-5 minutes, then re-run:
```bash
python deployment/scripts/post_cdk_setup.py
```
The script is idempotent — it will skip already-completed steps and retry the failed ones.

**Verify DNS is ready:**
```bash
nslookup <collection-id>.us-east-1.aoss.amazonaws.com
```
When it resolves to an IP address, index creation will succeed.

### CloudFront — 403 Access Denied after `deploy_frontend.py`

**Cause:** CloudFront distribution takes 2-5 minutes to fully deploy after `cdk deploy`. The OAC policy may not be active yet.

**Fix:** Wait 5 minutes and retry. Check distribution status:
```bash
aws cloudfront get-distribution --id <distribution-id> --query 'Distribution.Status'
```
It should return `"Deployed"`.

### AgentCore — Container not picking up new image

**Cause:** The keep-alive Lambda pings the container every 5 minutes, keeping it warm with the old image.

**Fix:** Disable keep-alive, wait 5-10 minutes for the container to go cold, then re-enable:
```bash
aws events disable-rule --name clinicalcompass-agentcore-keepalive --region us-east-1 --profile '<your-profile>'
# Wait 5-10 minutes, test with a query (triggers cold start with new image)
aws events enable-rule --name clinicalcompass-agentcore-keepalive --region us-east-1 --profile '<your-profile>'
```
