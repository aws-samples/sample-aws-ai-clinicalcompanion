# ClinicalCompass

AI-powered clinical decision support system providing evidence-based imaging recommendations using AWS Bedrock AgentCore and multi-agent architecture.

> **⚠️ Important Disclaimer**
>
> This application is intended for educational and informational purposes, as well as for internal testing and evaluation purposes.
> - This application is not intended for production purposes.
> - This application does not serve as a substitute for professional medical advice, diagnosis or treatment. For medical advice, diagnoses, or treatment, you should consult a medical professional.
> - This application is not intended for consequential decisions, including those that may impact a person's fundamental rights, health, or safety.
> - You should do an independent assessment of the application to determine if it meets your standards, and that your use would comply with all applicable laws, rules, and regulations that relate to you and your content.


## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Frontend (Vanilla JS)                                 │
│  Patient Selector │ Guideline Selector │ Citation Panel │ Decision Trail    │
└────────┬────────────────────────────────────────────────────┬───────────────┘
         │                      ▲                             │
         │ WebSocket            │ HTTPS                       │ HTTP API
         │                      │                             │
         │               ┌──────┴──────────┐                  │
         │               │  CloudFront      │                  │
         │               │  (OAC → S3)      │                  │
         │               └──────┬──────────┘                  │
         │                      │                             │
         │               ┌──────┴──────────┐                  │
         │               │  S3 (frontend)   │                  │
         │               │  static files    │                  │
         │               └─────────────────┘                  │
         ▼                                                    ▼
┌──────────────────┐                                ┌──────────────────────┐
│  WebSocket API   │                                │  HTTP API Gateway    │
│  Gateway         │                                │  /patients           │
│  (5 Lambdas)     │                                │  /guidelines         │
└────────┬─────────┘                                └──────────┬───────────┘
         │                                                      │
         ▼                                                      ▼
┌─────────────────┐                                ┌────────────────────────┐
│  AgentCore      │ ◄──► AgentCore Memory (STM)    │  DynamoDB              │
│  (Container)    │      session + decision trace  │  • patient-index       │
└────────┬────────┘                                │  • guideline-registry  │
         │                                         └────────────────────────┘
         ├──────────────────┬──────────────┐
         ▼                  ▼              ▼
   ┌──────────┐      ┌──────────┐   ┌──────────┐
   │ Patient  │      │ Global   │   │  Local   │
   │ Context  │      │Guidelines│   │Regulatory│
   │  Agent   │      │  Agent   │   │  Agent   │
   └────┬─────┘      └────┬─────┘   └────┬─────┘
        │                  │              │
        ▼                  ▼              ▼
   ┌──────────┐      ┌──────────┐   ┌──────────┐
   │ EHR KB   │      │ Guide KB │   │ Reg. KB  │
   │(OpenSrch)│      │(OpenSrch)│   │(OpenSrch)│
   └────┬─────┘      └────┬─────┘   └────┬─────┘
        │                  │              │
        ▼                  ▼              ▼
   ┌──────────┐      ┌──────────┐   ┌──────────┐
   │ S3 Bucket│      │ S3 Bucket│   │ S3 Bucket│
   │ ehr-mock │      │guidelines│   │local-reg │
   └──────────┘      └──────────┘   └──────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                         Data Ingestion Pipelines                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  S3 (ehr-data) ──► EHR Lambda ──► DynamoDB (patient-index) + KB sync        │
│  S3 (guidelines) ──► Guideline Lambda ──► DynamoDB (registry) + KB sync     │
│  S3 (local-regulatory) ──► KB ingestion job (no Lambda, direct sync)        │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
aws-clinicalcompanion/
├── backend/
│   ├── agents/                # Multi-agent system (Supervisor, Patient, Guidelines, Regulatory)
│   ├── utils/                 # Session management, memory, observability, progress tracking
│   ├── lambda/websocket/      # 5 Lambda handlers (connect, disconnect, query, ping, default)
│   ├── app.py                 # AgentCore container entry point
│   └── requirements.txt
├── frontend/
│   ├── css/                   # Styles (panel.css for citations/trail panels)
│   ├── js/                    # App logic, AG-UI event handling, content filter
│   ├── lib/                   # WebSocket client library
│   └── index.html
├── infra/                     # CDK infrastructure (TypeScript)
│   ├── lib/clinicalcompass-stack.ts
│   └── bin/app.ts
├── knowledge-bases/
│   ├── data/                  # Clinical data (EHR, guidelines, regulatory)
│   └── upload_data.py
├── deployment/
│   ├── scripts/               # Deployment and setup scripts
│   │   ├── post_cdk_setup.py  # Post-CDK: KBs, memory, runtime, data, configs
│   │   ├── build_and_push_image.py  # Docker build & push (CI/CD ready)
│   │   ├── deploy_frontend.py # Generate config.js, upload to S3, invalidate CloudFront
│   │   ├── cleanup_all.py     # Full teardown
│   │   └── quick_update_agentcore.sh  # Fast local Docker rebuild
│   └── config.example.py      # Config template
├── docs/                      # Architecture, API, deployment docs
└── tests/
```

## Prerequisites

- AWS CLI v2, configured (`aws configure`). Use a recent version that includes the AgentCore commands: `aws bedrock-agentcore-control help` must work (needed for the validation commands below)
- Node.js 18+ and npm
- Python 3.10+ (3.12 recommended)
- Docker running, with **buildx** (Docker Desktop includes it; with Colima, `brew install docker-buildx` and link it into `~/.docker/cli-plugins/`). AgentCore Runtime only runs **linux/arm64** images, and the build script always builds for arm64. On an x86 Linux machine or CI runner, enable arm64 emulation once: `docker run --privileged --rm tonistiigi/binfmt --install arm64`
- AWS CDK: `npm install -g aws-cdk`

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

## Data Pipelines

Three data categories, each with its own ingestion path:

| Category | Trigger | Storage | Agent Access | UI Access |
|----------|---------|---------|--------------|-----------|
| **Patient (EHR)** | S3 upload → Lambda | DynamoDB (structured) + KB (RAG) | Patient Context Agent | Patient Selector, Context Pane |
| **Guidelines** | S3 upload → Lambda | DynamoDB (metadata only) + KB (RAG) | Global Guidelines Agent | Guideline Selector dropdown |
| **Regulatory** | KB sync (no Lambda) | KB (RAG) only | Local Regulatory Agent | Always applied |

- Patient data goes to DynamoDB for direct lookups AND the KB for semantic search
- Guidelines store only metadata in DynamoDB (source name, status) for the UI dropdown — full content is in the KB for RAG
- Regulatory protocols live entirely in the KB — no DynamoDB needed (always consulted, no user selection)

See [Architecture](docs/content/docs/architecture.md) for detailed pipeline diagrams.

## Deployment

### What CDK manages (incremental deploys with `cdk diff`)

- Cognito User Pool + App Client (authentication)
- DynamoDB tables (WebSocket connections, patient index, guideline registry)
- ECR repository (AgentCore container images)
- IAM roles and policies (AgentCore, Lambda, KB roles)
- 10 Lambda functions (WebSocket handlers, EHR/guideline ingestion, patient/guideline APIs, keep-alive)
- WebSocket API Gateway + routes + integrations + stage
- HTTP API Gateway (REST endpoints for `/patients` and `/guidelines`)
- S3 buckets (EHR data, guidelines, KB data × 3, frontend static files)
- CloudFront distribution + OAC (HTTPS frontend hosting from S3)
- OpenSearch Serverless collections + security policies (vector search for RAG)
- SSM Parameter Store (resource IDs, ARNs, endpoints, frontend hosting config)
- CloudWatch Events rule (AgentCore keep-alive every 5 min)

### What scripts manage (runtime operations)

| Script | Purpose |
|---|---|
| `post_cdk_setup.py` | HealthLake datastore, OpenSearch indices, Bedrock KBs, AgentCore Memory, AgentCore Runtime, data upload, config updates |
| `build_and_push_image.py` | Docker build & push to ECR, and refresh an existing AgentCore runtime |
| `deploy_frontend.py` | Generate config.js, upload to S3, invalidate CloudFront |
| `cleanup_all.py` | Full teardown of all resources including orphans |

### Fresh deployment (from scratch)

```bash
# 1. Create deployment config
cp deployment/config.example.py deployment/config.py
# Edit: set AWS_PROFILE, AWS_REGION, AWS_ACCOUNT_ID

# The CDK and AWS CLI commands below do not read config.py; point them at the same
# profile and region so everything deploys to the same account:
export AWS_PROFILE=<YOUR_PROFILE> AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1

# 2. Deploy infrastructure
cd infra
npm install
npx cdk bootstrap    # first time only
npx cdk deploy --require-approval never

# ✅ Validate: the WebSocket API is up and rejects unauthenticated connections
npx wscat -c wss://<API_ID>.execute-api.us-east-1.amazonaws.com/production
# Expected: error: Unexpected server response: 401

# 3. Build and push the agent container (requires Docker running)
cd ..
python deployment/scripts/build_and_push_image.py

# ✅ Validate: check ECR image exists
aws ecr describe-images --repository-name bedrock-agentcore-clinicalcompass \
  --profile <YOUR_PROFILE> --region us-east-1 --query 'imageDetails[0].imageTags'

# 4. Complete setup (configs, KBs, AgentCore memory, then runtime, data upload)
python deployment/scripts/post_cdk_setup.py

# ✅ Validate: check AgentCore runtime is active
aws bedrock-agentcore-control list-agent-runtimes \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'agentRuntimes[?agentRuntimeName==`clinicalcompass`].{name:agentRuntimeName,status:status}'

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

After step 4 (which generates `frontend/config.js`), you can serve the frontend locally:

```bash
cd frontend && python3 -m http.server 8080
# Open http://localhost:8080
# Login with the Cognito user you created above
```

### Updating the agent (after code changes)

When you modify backend agents or utilities:

```bash
# Rebuild and push the Docker image
python deployment/scripts/build_and_push_image.py

# Or the faster bash version:
bash deployment/scripts/quick_update_agentcore.sh
```

If the `clinicalcompass` runtime already exists, `build_and_push_image.py` updates it to
the newly pushed image and waits for it to return to `READY`. During a fresh deploy, no
runtime exists yet, so the script just builds and pushes the image.

### Updating the frontend

When you modify frontend HTML, JS, or CSS:

```bash
python deployment/scripts/deploy_frontend.py
```

This regenerates `config.js`, uploads all files to S3, and invalidates the CloudFront cache. Changes are live in 1-2 minutes.

### Updating Lambda handlers

```bash
cd infra && npx cdk deploy
```

CDK detects the code change and updates only the affected Lambda functions.

### Updating infrastructure

```bash
cd infra
npx cdk diff     # preview changes
npx cdk deploy   # apply
```

### Updating Knowledge Base data

```bash
python deployment/scripts/post_cdk_setup.py --skip-runtime --skip-memory
```

### Full teardown

```bash
python deployment/scripts/cleanup_all.py        # interactive
python deployment/scripts/cleanup_all.py --yes  # non-interactive
```

Handles all states: `ROLLBACK_FAILED`, `DELETE_FAILED`, orphaned S3 buckets (versioned), ECR repos, HealthLake datastores (including stuck in CREATING), OpenSearch collections/policies, SSM parameters, and IAM roles.

Bedrock Knowledge Base records can occasionally linger in `DELETE_UNSUCCESSFUL` on the service side even after teardown. A fresh deploy is still supported because `post_cdk_setup.py` now detects those stale names and reuses or creates fallback Knowledge Base names automatically.

### Verifying the EHR Data Pipeline

The EHR pipeline is triggered automatically when patient JSON files are uploaded to the `clinicalcompass-ehr-data-<ACCOUNT_ID>` S3 bucket. The Lambda parses the file, stores structured data in DynamoDB, and (when configured) syncs to HealthLake and the Knowledge Base.

> In the commands below, replace the placeholders with your own values. `<ACCOUNT_ID>` is your AWS account ID (`aws sts get-caller-identity --query Account --output text`). The `<..._KB_ID>` and `<..._DATA_SOURCE_ID>` values are written to `knowledge-bases/knowledge_base_ids.json` by `post_cdk_setup.py`.

**Trigger the pipeline:**
```bash
# Upload a single patient
aws s3 cp knowledge-bases/data/ehr/patient_001_jean_pierre_dubois.json \
  s3://clinicalcompass-ehr-data-<ACCOUNT_ID>/ \
  --profile <YOUR_PROFILE> --region us-east-1

# Upload all 20 patients
for f in knowledge-bases/data/ehr/patient_0*.json; do
  aws s3 cp "$f" "s3://clinicalcompass-ehr-data-<ACCOUNT_ID>/$(basename $f)" \
    --profile <YOUR_PROFILE> --region us-east-1
done
```

**Verify DynamoDB was populated:**
```bash
# Count records (expect 20)
aws dynamodb scan --table-name clinicalcompass-patient-index \
  --profile <YOUR_PROFILE> --region us-east-1 --query 'Count'

# List patient names
aws dynamodb scan --table-name clinicalcompass-patient-index \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'Items[*].{id:patient_id.S,name:patient_name.S}' --output table
```

**Verify via REST API:**
```bash
# Get the API endpoint
API=$(aws ssm get-parameter --name /clinicalcompass/http-api-endpoint \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'Parameter.Value' --output text)

# The Patients API requires a Cognito access token (unauthenticated calls return 401)
TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id <COGNITO_CLIENT_ID> --region us-east-1 \
  --auth-parameters 'USERNAME=doctor,PASSWORD=<STRONG_PASSWORD>' \
  --query 'AuthenticationResult.AccessToken' --output text)

# List all patients
curl -H "Authorization: Bearer $TOKEN" $API/patients

# Get a specific patient
curl -H "Authorization: Bearer $TOKEN" $API/patients/001
```

**Check Lambda logs if something fails:**
```bash
aws logs tail /aws/lambda/clinicalcompass-ehr-ingestion --since 10m \
  --profile <YOUR_PROFILE> --region us-east-1
```

**Common issues:**
- `Float types are not supported` → Lambda code needs the Decimal conversion fix (redeploy Lambda)
- `FHIR_DATASTORE_ID not configured` → Expected warning if HealthLake isn't set up yet (non-blocking)
- No Lambda invocations → Check S3 event notification is configured (verify in AWS Console: S3 bucket → Properties → Event notifications)

### Verifying Knowledge Base Vectorization

Patient data is vectorized into Bedrock Knowledge Bases for RAG retrieval by the agents. This is separate from the DynamoDB pipeline — it's handled by `post_cdk_setup.py` uploading to KB buckets and triggering ingestion jobs.

**Check ingestion job status:**
```bash
# EHR KB (KB_ID and DATA_SOURCE_ID from knowledge-bases/knowledge_base_ids.json)
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id <EHR_KB_ID> \
  --data-source-id <EHR_DATA_SOURCE_ID> \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'ingestionJobSummaries[0].{status:status,stats:statistics}' --output json

# Expected: "status": "COMPLETE", statistics.numberOfDocumentsScanned > 0
```

**Test RAG retrieval (definitive proof vectorization works):**
```bash
# Search for a patient by clinical context
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <EHR_KB_ID> \
  --retrieval-query '{"text": "patient with renal impairment eGFR 32"}' \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'retrievalResults[0].content.text' --output text

# Search guidelines
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <GUIDELINES_KB_ID> \
  --retrieval-query '{"text": "contrast media renal impairment guidelines"}' \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'retrievalResults[0].content.text' --output text

# Search regulatory
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <REGULATORY_KB_ID> \
  --retrieval-query '{"text": "HAS France contrast protocol"}' \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'retrievalResults[0].content.text' --output text
```

If these return relevant text content, the data is vectorized and the agents can retrieve it.

**Check KB bucket has data:**
```bash
aws s3 ls s3://clinicalcompass-ehr-mock-<ACCOUNT_ID>/ --recursive \
  --profile <YOUR_PROFILE> --region us-east-1 | wc -l
# Expected: 20 (one file per patient)
```

**Common issues:**
- `Missing opensearch-py` in `knowledge_base_ids.json` → Install: `python -m pip install opensearch-py requests-aws4auth` then re-run `post_cdk_setup.py`
- Ingestion status `IN_PROGRESS` → Wait 1-2 minutes and check again
- Ingestion status `FAILED` → Check the data source bucket has files and the KB role has S3 read permissions
- Empty retrieval results → Ingestion may not have completed yet, or the query doesn't match any content

### Verifying Guideline & Regulatory Ingestion

Guidelines and regulatory documents follow a two-part flow:
1. **DynamoDB registry** — lightweight metadata (source name, display name, status) for the UI dropdown
2. **Knowledge Base (RAG)** — full document content vectorized for agent retrieval

**Trigger the guideline ingestion pipeline:**
```bash
# Upload a guideline document to the guidelines S3 bucket
aws s3 cp knowledge-bases/data/guidelines/acr_contrast_manual_2024.md \
  s3://clinicalcompass-guidelines-<ACCOUNT_ID>/ \
  --profile <YOUR_PROFILE> --region us-east-1

# Upload regulatory documents
aws s3 cp knowledge-bases/data/regulatory/has_france_contrast_protocols_2024.md \
  s3://clinicalcompass-local-regulatory-<ACCOUNT_ID>/ \
  --profile <YOUR_PROFILE> --region us-east-1
```

**Check the guideline registry (DynamoDB — UI dropdown data):**
```bash
aws dynamodb scan --table-name clinicalcompass-guideline-registry \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'Items[*].{source:source_name.S,display:display_name.S,status:status.S}' \
  --output table
```

**Check guideline ingestion Lambda logs:**
```bash
aws logs tail /aws/lambda/clinicalcompass-guideline-ingestion --since 30m \
  --profile <YOUR_PROFILE> --region us-east-1 | grep -i "error\|success\|registered"
```

**Check KB ingestion job status:**
```bash
# Guidelines KB
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id <GUIDELINES_KB_ID> --data-source-id <GUIDELINES_DATA_SOURCE_ID> \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'ingestionJobSummaries[0].{status:status,docs:statistics.numberOfDocumentsScanned}' \
  --output json

# Regulatory KB
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id <REGULATORY_KB_ID> --data-source-id <REGULATORY_DATA_SOURCE_ID> \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'ingestionJobSummaries[0].{status:status,docs:statistics.numberOfDocumentsScanned}' \
  --output json
```

**Test RAG retrieval (definitive proof):**
```bash
# Guidelines — search for contrast media guidance
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <GUIDELINES_KB_ID> \
  --retrieval-query '{"text": "ACR contrast media renal impairment eGFR"}' \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'retrievalResults[0].content.text' --output text | head -5

# Regulatory — search for HAS France protocols
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <REGULATORY_KB_ID> \
  --retrieval-query '{"text": "HAS France contrast protocol nephropathy"}' \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'retrievalResults[0].content.text' --output text | head -5
```

**Test via REST API (what the UI dropdown uses):**
```bash
API=$(aws ssm get-parameter --name /clinicalcompass/http-api-endpoint \
  --profile <YOUR_PROFILE> --region us-east-1 \
  --query 'Parameter.Value' --output text)

curl $API/guidelines
```

**Common issues:**
- Ingestion job `FAILED` with 0 documents → KB role doesn't have S3 read permission on the bucket. Fix: add an inline policy granting `s3:GetObject*`, `s3:GetBucket*`, `s3:List*` on the correct bucket ARN
- Guideline registry empty → The guideline ingestion Lambda wasn't triggered (check S3 event notifications) or failed before reaching the registry write (check Lambda logs)
- RAG returns "None" → Ingestion job hasn't completed yet, or the KB data source points to a different bucket than where the files were uploaded

### CI/CD integration

`build_and_push_image.py` supports versioned tags for pipeline use:

```bash
python deployment/scripts/build_and_push_image.py --tag $CI_COMMIT_SHA
python deployment/scripts/build_and_push_image.py --no-push  # build only
```

## Configuration

`deployment/config.py` holds only your AWS profile, region, and account ID (plus the ECR repository name). Everything else is discovered at run time: the deployment scripts read the CloudFormation stack outputs, and the runtime ARN and memory ID are stored in SSM Parameter Store. `post_cdk_setup.py` also writes `frontend/config.js` and `knowledge-bases/knowledge_base_ids.json`.

Config files are gitignored — only `*.example.*` templates are committed.

## Authentication

The application uses Amazon Cognito for authentication. CDK deploys a User Pool and App Client, and you create login users after deployment with the Cognito admin CLI.

Example post-deploy flow:

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

Choose a password that satisfies the pool policy: at least 8 characters with uppercase, lowercase, and a digit.

The frontend shows a login screen before the chat interface. Tokens are stored in `sessionStorage` (cleared on tab close). No external SDK is needed — the frontend authenticates directly via the Cognito `InitiateAuth` API.

Cognito config values (`cognitoUserPoolId`, `cognitoClientId`) are automatically written to `frontend/config.js` by `post_cdk_setup.py`.

## Security & Production Readiness

This sample uses **synthetic patient data only** and is **not production-ready**. Do not load real patient data into it.

The tables below show what the sample already protects, and what must be added before any real-world use. The items in the second table are known gaps of this sample. They are required for production, not accepted risks.

### Already in place

| Area | What the sample does |
|---|---|
| Sign-in | Every API and WebSocket call requires a valid Cognito sign-in. Requests without one are rejected. |
| Patient data isolation | Each request keeps its own patient context, so concurrent users never see each other's data. |
| Abuse and cost control | Queries are rate-limited per user, and the limit stays enforced even if the rate-limit store fails. |
| Least privilege | Each backend function has its own permissions, and only the query function can read patient data. |
| Safe rendering | AI responses are sanitized before display to block script injection. |
| Platform defaults | Encryption in transit and at rest, a web firewall on the website, request throttling, content guardrails, and security alerting. |

### Required before production

| Area | Gap in this sample | What production needs |
|---|---|---|
| Patient access control (T9, Critical) | Any signed-in user can see every demo patient. | Each clinician can only access the patients they are assigned to. |
| Multi-factor sign-in (T1, High) | Password-only sign-in. | MFA and account-takeover protection. |
| Private vector database (T10, High) | The search database is reachable from the internet, protected only by access policies. | Private network access only. |
| Source data integrity (T4, T5, T8, High) | Tamper detection covers guidelines only. | Write protection, tamper detection, and audit logging on all guideline and patient sources, plus human review of guideline changes. |
| Prompt injection (T11, T17, High) | **Known limitation.** Filters reduce but cannot fully prevent manipulated input or documents from influencing answers. | Stronger AI guardrails, treating retrieved content as untrusted, regular red-team testing, and **mandatory clinician review of every answer**. |
| API firewall (T14, Medium) | Only the website has a web firewall. The APIs rely on throttling. | Put the APIs behind the web firewall and block direct access. |
| Scoped permissions (T16, Medium) | One function can access any health-records store in the account. | Limit it to the single store it uses. |
| Cross-origin access (T19, Medium) | The API accepts calls from any website. | Allow only the application's own domain. |
| Sign-in token handling (T2, Medium) | The sign-in token is passed in the WebSocket URL. | Send it in a header or use a short-lived one-time ticket. |
| WebSocket authorization (T3, Medium) | One authorization grants access to all WebSocket actions. | Authorize each action separately. |
| Logging (T7, T12, Medium) | Logs contain patient identifiers and are not linked to the clinician for every action. | Mask identifiers, restrict log access, and record who did what. |

## Clinical Workflow

1. Clinician submits a query via the WebSocket frontend
2. Supervisor Agent parses the query and extracts clinical entities
3. Three specialized agents run in parallel:
   - Patient Context Agent — retrieves EHR data
   - Global Guidelines Agent — consults ACR, ESUR, NICE, WHO
   - Local Regulatory Agent — checks HAS France protocols
4. Supervisor detects conflicts between guidelines
5. Precedence rules resolve conflicts: local > national > international
6. Final recommendation with safety alerts, alternatives, and citations

## AgentCore Memory

The agent uses **AgentCore Memory (short-term memory)** to keep context across the turns of a conversation:

| What is stored | Why |
|---|---|
| Session context: the selected patient, cached patient data, query count | Follow-up questions in the same conversation reuse the context instead of starting over |
| Workflow events (session created, query processed) | Activity history for the session |
| Decision trace for each recommendation | The reasoning behind an answer, kept for review and audit |

Events are stored per patient (actor) and per conversation (session), and expire after 30 days.

How it is set up:
- `post_cdk_setup.py` creates one memory named `clinicalcompass_mem` and stores its ID in SSM (`/clinicalcompass/stm-memory-id`). It creates the memory **before** the runtime, so agent containers always find it.
- The agent reads the memory ID from SSM once, when a container starts. After the memory ID changes (for example after recreating the memory), refresh the agent so new containers pick it up: `python deployment/scripts/build_and_push_image.py`. This pushes the image and publishes a new runtime version.
- If no memory is configured, the agent still answers, but each question is handled without the conversation's earlier context.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `AWS_REGION` | AWS region | `us-east-1` |
| `BEDROCK_MODEL_ID` | Bedrock model | `us.anthropic.claude-sonnet-4-6` |
| `BEDROCK_TEMPERATURE` | Model temperature | `0` |
| `STM_MEMORY_ID` | Short-term memory ID | Read from SSM |
| `EHR_KB_ID` | EHR Knowledge Base ID | Read from SSM |
| `GUIDELINES_KB_ID` | Guidelines KB ID | Read from SSM |
| `REGULATORY_KB_ID` | Regulatory KB ID | Read from SSM |

## Testing

```bash
# Backend unit tests (no AWS access needed)
pip install -r backend/requirements-dev.txt
cd backend && python -m pytest -q && cd ..

# CDK security assertion tests
cd infra && npx jest && cd ..

# Quick WebSocket test (needs a Cognito access token; see Authentication above)
TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id <COGNITO_CLIENT_ID> --region us-east-1 \
  --auth-parameters 'USERNAME=doctor,PASSWORD=<STRONG_PASSWORD>' \
  --query 'AuthenticationResult.AccessToken' --output text)
npx wscat -c "wss://<API_ID>.execute-api.us-east-1.amazonaws.com/production?Authorization=$TOKEN" \
  --execute '{"action":"ping"}'
# Expected: {"type":"pong"}

# Run frontend locally
cd frontend && python3 -m http.server 8080

# Example query (patient 001, Jean-Pierre Dubois):
# "68-year-old male with eGFR 32, needs MRI with gadolinium contrast"
```

## Troubleshooting

Check Lambda logs:
```bash
aws logs tail /aws/lambda/clinicalcompass-ws-query --follow --region us-east-1
```

Common issues:
- Docker credential error: remove `credsStore` from `~/.docker/config.json`
- Model access denied: check IAM policy includes `inference-profile/*` resources
- OpenSearch index not found: run `post_cdk_setup.py` (creates indices before KBs)
- `Index not visible to Bedrock yet ... retrying` right after `✓ Index created`: expected. A newly created OpenSearch Serverless index takes a short time to become visible to Bedrock, and `post_cdk_setup.py` retries for up to 3 minutes. If it still ends with `Failed to create KB ... no such index`, re-run `python deployment/scripts/post_cdk_setup.py --skip-runtime --skip-initial-upload`. Existing indices, KBs, and data sources are reused, and only the missing KB is created.
- `Failed to provision Vector Store for the customer` during the memory step: a transient AgentCore Memory provisioning failure. `post_cdk_setup.py` retries up to 3 times and deletes failed attempts. If it still fails, it exits with an error (the app works without memory, but session continuity is off). Re-run only the memory step a few minutes later: `python deployment/scripts/post_cdk_setup.py --skip-kb --skip-runtime --skip-data --skip-initial-upload --skip-config`, then refresh the agent so it picks up the memory ID: `python deployment/scripts/build_and_push_image.py` (see [AgentCore Memory](#agentcore-memory)).
- `Memory not found` or `STM_MEMORY_ID not set` in the agent logs: the agent is using an old or missing memory ID. Check that `/clinicalcompass/stm-memory-id` in SSM points to an `ACTIVE` memory (re-run the memory step above if not; `post_cdk_setup.py` only reuses a memory that is `ACTIVE` or `CREATING`). Then refresh the agent so new containers read the current ID: `python deployment/scripts/build_and_push_image.py`.

## Documentation

- [Documentation site](https://aws-samples.github.io/sample-aws-ai-clinicalcompanion/)
- [Architecture](docs/content/docs/architecture.md)
- [Observability](docs/content/docs/observability.md)
- [CDK Infrastructure](infra/README.md)
- [Frontend](frontend/README.md)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

