---
title: Installation
weight: 4
prev: architecture
next: development
---

Deploy ClinicalCompass in your AWS account. The system uses AWS CDK for infrastructure provisioning and a post-deploy script for runtime configuration.

## Prerequisites

- AWS CLI v2, configured (`aws configure`). Use a recent version that includes the AgentCore commands: `aws bedrock-agentcore-control help` must work
- Node.js 18+ and npm
- Python 3.10+ (3.12 recommended)
- Docker running, with **buildx** (Docker Desktop includes it; with Colima, `brew install docker-buildx` and link it into `~/.docker/cli-plugins/`). AgentCore Runtime only runs **linux/arm64** images; on an x86 Linux machine or CI runner, enable arm64 emulation once: `docker run --privileged --rm tonistiigi/binfmt --install arm64`
- AWS CDK: `npm install -g aws-cdk`
- Access to Amazon Bedrock foundation models (Claude Sonnet)

```bash
# Install Python dependencies
pip install boto3 opensearch-py requests-aws4auth
pip install -r backend/requirements.txt

# Install WebSocket authorizer Lambda dependencies (bundled for Lambda deployment)
# These are installed into the Lambda directory for CDK to package them.
# Target platform must be Linux x86_64 to match the Lambda runtime.
pip install -r backend/lambda/websocket_authorizer/requirements.txt \
  -t backend/lambda/websocket_authorizer/ \
  --platform manylinux2014_x86_64 \
  --only-binary=:all:
```

## What Gets Deployed

### CDK-managed resources (incremental deploys via `cdk diff`)

| Resource | Purpose |
|----------|---------|
| Cognito User Pool + App Client | Authentication |
| DynamoDB tables | Connections, patient-index, guideline-registry |
| ECR repository | AgentCore container images |
| IAM roles and policies | AgentCore, Lambda, KB access |
| 12 Lambda functions | 5 WebSocket handlers, WebSocket authorizer, EHR/guideline ingestion, patient/guideline APIs, keep-alive, guideline tamper detector |
| WebSocket API Gateway | Real-time agent communication |
| HTTP API Gateway | REST endpoints (`/patients`, `/guidelines`) |
| S3 buckets | Frontend static files, EHR data, guidelines, regulatory, KB data x3 |
| CloudFront + OAC | HTTPS frontend hosting from S3 |
| OpenSearch Serverless | Vector search for KB retrieval |
| SSM Parameter Store | Resource IDs, ARNs, endpoints, frontend hosting config |
| CloudWatch Events | AgentCore keep-alive (every 5 min) |

### Script-managed resources (runtime operations)

| Script | Purpose |
|--------|---------|
| `post_cdk_setup.py` | HealthLake, OpenSearch indices, Bedrock KBs, AgentCore Memory, AgentCore Runtime, data upload, config updates |
| `build_and_push_image.py` | Docker build and push to ECR; refresh existing AgentCore runtime when present |
| `deploy_frontend.py` | Generate config.js, upload to S3, invalidate CloudFront |
| `cleanup_all.py` | Full teardown of all resources including orphans |

## Fresh Deployment

### Step 1: Create deployment config

```bash
cp deployment/config.example.py deployment/config.py
```

Edit `deployment/config.py` and set:
- `AWS_PROFILE` - your named AWS CLI profile
- `AWS_REGION` - target region (default: `us-east-1`)
- `AWS_ACCOUNT_ID` - your 12-digit account ID

The CDK and AWS CLI commands below do not read `config.py`. Point them at the same profile and Region so everything deploys to the same account:

```bash
export AWS_PROFILE=<YOUR_PROFILE> AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
```

### Step 2: Deploy infrastructure

```bash
cd infra
npm install
npx cdk bootstrap    # first time only
npx cdk deploy --require-approval never
```

**Validate:** the WebSocket API is up and rejects unauthenticated connections.
```bash
npx wscat -c wss://<API_ID>.execute-api.us-east-1.amazonaws.com/production
# Expected: error: Unexpected server response: 401
```

### Step 3: Build and push the agent container

Requires Docker running with buildx (Docker Desktop, or `colima start` with the buildx plugin). The script always builds a `linux/arm64` image, the only architecture AgentCore Runtime runs, and pushes it straight to ECR.

```bash
cd ..
python deployment/scripts/build_and_push_image.py
```

**Validate:**
```bash
aws ecr describe-images --repository-name bedrock-agentcore-clinicalcompass \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'imageDetails[0].imageTags'
```

{{< callout type="warning" >}}
**Common issues:**
- `Docker: NOT RUNNING` - Start Docker Desktop or run `colima start`
- `docker buildx is required` - Install the buildx plugin (see Prerequisites)
- Runtime rejects the image as the wrong architecture - Make sure you use this script (it builds `linux/arm64`), not a plain `docker build`
- `no basic auth credentials` - Remove `credsStore` from `~/.docker/config.json`
- `denied: Your authorization token has expired` - Re-run the script (it re-authenticates)
{{< /callout >}}

### Step 4: Complete setup (HealthLake, KBs, memory, runtime, data)

```bash
python deployment/scripts/post_cdk_setup.py
```

This script:
1. Creates HealthLake FHIR datastore (if configured)
2. Creates OpenSearch indices
3. Creates Bedrock Knowledge Bases
4. Provisions short-term memory (AgentCore Memory), before the runtime so agent containers find the memory ID when they start
5. Creates the AgentCore Runtime
6. Uploads clinical data to KBs
7. Updates `frontend/config.js` and `knowledge-bases/knowledge_base_ids.json`

**Validate:**
```bash
aws bedrock-agentcore-control list-agent-runtimes \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'agentRuntimes[?agentRuntimeName==`clinicalcompass`].{name:agentRuntimeName,status:status}'
```

{{< callout type="warning" >}}
**Common issues:**
- `opensearch-py and requests-aws4auth required` - Install them, then re-run with `--skip-runtime --skip-memory --skip-config`
- `Collection status: CREATING` - OpenSearch collections take 2-5 min after CDK deploy. Wait and re-run.
- `Failed to create runtime` - Check Docker image was pushed (Step 3) and IAM role exists.
- `Index not visible to Bedrock yet ... retrying` right after `✓ Index created` - Expected. A new OpenSearch Serverless index takes a short time to become visible to Bedrock, and the script retries for up to 3 minutes. If it still ends with `Failed to create KB ... no such index`, re-run `python deployment/scripts/post_cdk_setup.py --skip-runtime --skip-initial-upload`.
- `Failed to provision Vector Store for the customer` during the memory step - A transient AgentCore Memory failure. The script retries up to 3 times. If it still fails, re-run only the memory step a few minutes later with `python deployment/scripts/post_cdk_setup.py --skip-kb --skip-runtime --skip-data --skip-initial-upload --skip-config`, then refresh the agent with `python deployment/scripts/build_and_push_image.py`.
{{< /callout >}}

### Step 5: Deploy frontend to CloudFront

```bash
python deployment/scripts/deploy_frontend.py
```

This generates `config.js`, uploads all frontend files to S3, and invalidates the CloudFront cache. Access via the CloudFront URL printed at the end.

Create a Cognito user with the post-deploy CLI commands in the Authentication section, then log in with that username and password.

{{< callout type="info" >}}
For local development, you can also serve the frontend directly after step 4 (which generates `config.js`):
```bash
cd frontend && python3 -m http.server 8080
# Open http://localhost:8080
```
{{< /callout >}}

## Configuration

`deployment/config.py` holds only your AWS profile, region, and account ID (plus the ECR repository name). Everything else is discovered at run time from the CloudFormation stack outputs and SSM Parameter Store (runtime ARN, memory ID). `post_cdk_setup.py` also writes:
- `frontend/config.js` - Cognito + WebSocket endpoint
- `knowledge-bases/knowledge_base_ids.json` - KB identifiers

Config files are gitignored. Only `*.example.*` templates are committed.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region | `us-east-1` |
| `BEDROCK_MODEL_ID` | Bedrock model | `us.anthropic.claude-sonnet-4-6` |
| `BEDROCK_TEMPERATURE` | Model temperature | `0` |
| `STM_MEMORY_ID` | Short-term memory ID | Read from SSM |
| `EHR_KB_ID` | EHR Knowledge Base ID | Read from SSM |
| `GUIDELINES_KB_ID` | Guidelines KB ID | Read from SSM |
| `REGULATORY_KB_ID` | Regulatory KB ID | Read from SSM |

## Authentication

CDK deploys a Cognito User Pool (`clinicalcompass-users`) with:
- An app client configured for browser use (no client secret)
- `USER_PASSWORD_AUTH` and `USER_SRP_AUTH` flows enabled

Create login users after deployment with the Cognito admin CLI:

```bash
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

Choose a password that satisfies the pool policy: at least 8 characters with uppercase, lowercase, and a digit. The frontend authenticates directly via the Cognito `InitiateAuth` API. Tokens are stored in `sessionStorage` (cleared on tab close). No external SDK required.

## Full Teardown

```bash
python deployment/scripts/cleanup_all.py        # interactive
python deployment/scripts/cleanup_all.py --yes  # non-interactive
```

S3 buckets and the ECR repository have `RETAIN` removal policy and won't be deleted by `cdk destroy` alone. The cleanup script handles them.

AWS Bedrock may still leave deleted Knowledge Base names behind in `DELETE_UNSUCCESSFUL`. A subsequent deploy is still safe because `post_cdk_setup.py` detects those stale names and falls back to a reusable alternate Knowledge Base name when needed.
