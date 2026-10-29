---
title: Development
weight: 5
prev: installation
next: agents
---

Day-to-day workflows for modifying agents, updating infrastructure, and managing knowledge bases.

## Updating Backend Agents

When you modify code in `backend/agents/` or `backend/utils/`:

```bash
python deployment/scripts/build_and_push_image.py
```

This rebuilds the Docker image and pushes it to ECR. If the `clinicalcompass` runtime
already exists, the script also updates it to the newly pushed image and waits for it to
return to `READY`. During a fresh deploy, no runtime exists yet, so the script just builds
and pushes the image.

For faster iteration during development:

```bash
bash deployment/scripts/quick_update_agentcore.sh
```

## Updating the Frontend

When you modify frontend HTML, JS, or CSS:

```bash
python deployment/scripts/deploy_frontend.py
```

This regenerates `config.js`, uploads all files to S3, and invalidates the CloudFront cache. Changes are live in 1-2 minutes.

For local development (after `post_cdk_setup.py` has generated `config.js`):

```bash
cd frontend && python3 -m http.server 8080
```

## Updating Lambda Handlers

When you modify code in `backend/lambda/websocket/`:

```bash
cd infra && npx cdk deploy
```

CDK detects the code change and updates only the affected Lambda functions.

## Updating Infrastructure

When you modify the CDK stack (new resources, policy changes):

```bash
cd infra
npx cdk diff     # preview changes
npx cdk deploy   # apply
```

## Updating Knowledge Base Data

When you add or modify clinical data in `knowledge-bases/data/`:

```bash
python deployment/scripts/post_cdk_setup.py --skip-runtime --skip-memory
```

This re-uploads data to S3 and triggers a KB sync without touching the runtime or memory configuration.

## Project Structure

```
aws-clinicalcompanion/
├── backend/
│   ├── agents/              # Multi-agent system
│   │   ├── supervisor_agent.py
│   │   ├── patient_context_agent.py
│   │   ├── global_guidelines_agent.py
│   │   └── local_regulatory_agent.py
│   ├── utils/               # Shared utilities
│   │   ├── session_manager.py
│   │   ├── memory_utils.py
│   │   ├── agui_emitter.py
│   │   ├── metadata_extractor.py
│   │   ├── clinical_observability.py
│   │   └── kb_config.py
│   ├── lambda/websocket/    # 5 Lambda handlers
│   ├── app.py               # AgentCore container entry point
│   └── requirements.txt
├── frontend/
│   ├── css/                 # Styles (agui, panel, auth)
│   ├── js/                  # App logic + auth
│   └── index.html
├── infra/                   # CDK infrastructure (TypeScript)
│   ├── lib/
│   └── bin/app.ts
├── knowledge-bases/
│   ├── data/                # Clinical data (EHR, guidelines, regulatory)
│   └── upload_data.py
├── deployment/
│   ├── scripts/             # Deployment and setup scripts
│   └── config.py            # Centralised configuration
└── tests/                   # Integration and unit tests
```

## Running Tests

```bash
# Backend unit tests (first time: pip install -r backend/requirements-dev.txt)
cd backend && python -m pytest -q

# CDK security assertion tests
cd infra && npx jest
```

To check a deployed stack end to end, sign in through the CloudFront URL and run a query. The WebSocket API requires a Cognito access token, so unauthenticated tools such as a plain `wscat` connection are rejected with `401`.

## Verifying Data Pipelines

### EHR Pipeline

```bash
# Upload patient data to trigger ingestion
aws s3 cp knowledge-bases/data/ehr/patient_001_jean_pierre_dubois.json \
  s3://clinicalcompass-ehr-data-<ACCOUNT_ID>/ \
  --profile <YOUR_PROFILE> --region us-east-1

# Verify DynamoDB was populated
aws dynamodb scan --table-name clinicalcompass-patient-index \
  --profile <YOUR_PROFILE> --region us-east-1 --query 'Count'

# Verify via REST API
curl $(aws ssm get-parameter --name /clinicalcompass/http-api-endpoint \
  --query 'Parameter.Value' --output text)/patients
```

### Guidelines Pipeline

```bash
# Upload a guideline document
aws s3 cp knowledge-bases/data/guidelines/acr_contrast_manual_2024.md \
  s3://clinicalcompass-guidelines-<ACCOUNT_ID>/ \
  --profile <YOUR_PROFILE> --region us-east-1

# Check guideline registry
aws dynamodb scan --table-name clinicalcompass-guideline-registry \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'Items[*].{source:source_name.S,status:status.S}' --output table
```

### Knowledge Base Vectorisation

```bash
# Test RAG retrieval (proof vectorisation works)
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <KB_ID> \
  --retrieval-query '{"text": "patient with renal impairment eGFR 32"}' \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'retrievalResults[0].content.text' --output text
```

## Running the Frontend Locally

```bash
cd frontend && python3 -m http.server 8080
# Open http://localhost:8080
```

Test query (patient `001`, Jean-Pierre Dubois):

> "68-year-old male with eGFR 32, needs MRI with gadolinium contrast"

## CI/CD Integration

The build script supports versioned tags for pipeline use:

```bash
# Tag with commit SHA
python deployment/scripts/build_and_push_image.py --tag $CI_COMMIT_SHA

# Build only (no push)
python deployment/scripts/build_and_push_image.py --no-push
```

## Post-CDK Script Options

The `post_cdk_setup.py` script supports selective execution:

```bash
python deployment/scripts/post_cdk_setup.py                # full setup
python deployment/scripts/post_cdk_setup.py --skip-kb       # skip Knowledge Base creation
python deployment/scripts/post_cdk_setup.py --skip-runtime  # skip AgentCore Runtime
python deployment/scripts/post_cdk_setup.py --skip-memory   # skip Memory creation
python deployment/scripts/post_cdk_setup.py --skip-data     # skip KB data upload
python deployment/scripts/post_cdk_setup.py --skip-config   # skip config file updates
```
