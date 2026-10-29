# Security Mitigation Implementation Plan

## Overview

This plan addresses the 12 threats identified in the ClinicalCompass threat model. Each mitigation includes current status, implementation steps, and verification criteria.

---

## Priority 1 — Critical (Implement Before Production)

### 1. Cognito JWT Authentication at API Gateway

**Threat:** Unauthenticated access to clinical queries and patient data

**Current Status:** ⚠️ Partially implemented — Cognito exists but WebSocket API does not enforce JWT validation at the Gateway level. Authentication is checked in the Lambda handler only.

**Implementation Steps:**
1. Add a Cognito authorizer to the WebSocket API Gateway `$connect` route
2. Require `Authorization` header (or query param `token`) on WebSocket connection
3. Configure token validation (issuer, audience, expiration)
4. Add Cognito authorizer to HTTP API Gateway routes (`/patients`, `/guidelines`)
5. Reject connections/requests without valid tokens at Gateway level (before Lambda invocation)

**CDK Changes:**
```typescript
// WebSocket API — add Cognito authorizer on $connect
const wsAuthorizer = new apigwv2.WebSocketRouteAuthorizer(...);

// HTTP API — add JWT authorizer
const httpAuthorizer = new apigwv2_authorizers.HttpJwtAuthorizer('CognitoAuth', {
  jwtIssuer: `https://cognito-idp.${region}.amazonaws.com/${userPool.userPoolId}`,
  jwtAudience: [userPoolClient.userPoolClientId],
});
```

**Verification:**
- [ ] Unauthenticated WebSocket connection is rejected with 401
- [ ] Expired token is rejected
- [ ] Valid token allows connection
- [ ] HTTP API returns 401 without Authorization header

**Effort:** Medium | **Timeline:** 1 day

---

### 2. Prompt Injection Protection (Agent Safety Constraints)

**Threat:** Adversarial input causing harmful medical advice or bulk data extraction via prompt injection

**Current Status:** ⚠️ Partially implemented — System prompts include safety instructions but no input validation or guardrails

**Implementation Steps:**
1. Add Amazon Bedrock Guardrails to the AgentCore invocation
2. Configure content filters: block harmful content, PII extraction attempts
3. Add input validation in the WebSocket query Lambda (max length, character filtering)
4. Implement output validation — detect and block responses containing raw patient identifiers from other patients
5. Add rate limiting per user (max 20 queries/hour per Cognito identity)
6. Log all blocked queries for security review

**CDK/Backend Changes:**
```python
# In WebSocket query Lambda — input validation
MAX_QUERY_LENGTH = 2000
BLOCKED_PATTERNS = [
    r'ignore.*instructions',
    r'list all patients',
    r'dump.*database',
    r'show.*other.*patient',
]
```

**Verification:**
- [ ] Query exceeding 2000 chars is rejected
- [ ] Known prompt injection patterns are blocked
- [ ] Agent cannot return data for patients not in the current query context
- [ ] Rate limit enforced per user identity

**Effort:** High | **Timeline:** 3 days

---

### 3. S3 Guideline Content Tamper Detection

**Threat:** Modified clinical guidelines leading to incorrect medical advice

**Current Status:** ✅ Partially implemented — S3 versioning enabled, but no alerting on modifications

**Implementation Steps:**
1. Enable S3 EventBridge notifications for `s3:ObjectCreated:*` and `s3:ObjectRemoved:*` on guideline buckets
2. Create a Lambda function that validates file checksums against a known-good manifest
3. Send SNS alert to security team on unexpected modifications
4. Add S3 Object Lock (governance mode) on guideline source buckets
5. Implement approval workflow: only CI/CD pipeline can write to guideline buckets

**CDK Changes:**
```typescript
// Add Object Lock on guideline buckets
const guidelinesBucket = new s3.Bucket(this, 'GuidelinesBucket', {
  objectLockEnabled: true,
  // ...
});

// Add EventBridge rule for unauthorized modifications
const tamperAlertRule = new events.Rule(this, 'GuidelineTamperAlert', {
  eventPattern: { source: ['aws.s3'], detailType: ['Object Created', 'Object Deleted'] },
});
```

**Verification:**
- [ ] Unauthorized S3 write triggers SNS alert
- [ ] Object Lock prevents deletion of guideline files
- [ ] Only CI/CD IAM role can write to guideline buckets
- [ ] Tampering detection fires within 5 minutes

**Effort:** Medium | **Timeline:** 2 days

---

## Priority 2 — High (Implement Within First Sprint)

### 4. TLS Encryption in Transit

**Threat:** Data interception between browser and API, or between internal services

**Current Status:** ✅ Implemented — WebSocket uses WSS, CloudFront enforces HTTPS, API Gateway uses TLS 1.2+

**Remaining Actions:**
1. Enforce TLS 1.2 minimum on all API Gateway endpoints (disable TLS 1.0/1.1)
2. Add security headers to CloudFront (HSTS, X-Content-Type-Options, X-Frame-Options)
3. Verify OpenSearch Serverless connections use TLS (default, but confirm)

**CDK Changes:**
```typescript
// CloudFront response headers policy
const headersPolicy = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
  securityHeadersBehavior: {
    strictTransportSecurity: { accessControlMaxAge: Duration.days(365), includeSubdomains: true },
    contentTypeOptions: { override: true },
    frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY },
  },
});
```

**Verification:**
- [ ] SSL Labs scan shows A+ rating
- [ ] HSTS header present on all CloudFront responses
- [ ] No TLS 1.0/1.1 connections accepted

**Effort:** Low | **Timeline:** 0.5 day

---

### 5. Encryption at Rest

**Threat:** Data exposure from compromised storage layer

**Current Status:** ✅ Implemented — All services use AWS-managed encryption by default

**Remaining Actions:**
1. Upgrade to Customer Managed Keys (CMK) for sensitive data stores (DynamoDB Patient Index, HealthLake)
2. Document encryption key rotation policy
3. Add KMS key policy restricting decrypt access to specific roles only

**CDK Changes:**
```typescript
const patientDataKey = new kms.Key(this, 'PatientDataKey', {
  description: 'Encrypts patient index and HealthLake data',
  enableKeyRotation: true,
});
```

**Verification:**
- [ ] DynamoDB Patient Index uses CMK encryption
- [ ] KMS key policy allows only Lambda roles and AgentCore role to decrypt
- [ ] Key rotation enabled (annual)

**Effort:** Medium | **Timeline:** 1 day

---

### 6. IAM Least Privilege Hardening

**Threat:** Over-privileged roles enabling lateral movement or data exfiltration

**Current Status:** ⚠️ Partially implemented — Roles exist with correct scope but some wildcards remain

**Implementation Steps:**
1. Replace `resources: ['*']` with specific ARNs where possible:
   - AgentCore role: scope ECR access to specific repository
   - AgentCore role: scope S3 access to specific KB buckets only
   - Lambda role: scope `execute-api:ManageConnections` to specific WebSocket API
2. Add IAM Access Analyzer to detect unused permissions
3. Remove `bedrock-agentcore:*` — narrow to `InvokeAgentRuntime` + `GetAgentRuntime`
4. Add SCP or permission boundary preventing role escalation

**Verification:**
- [ ] IAM Access Analyzer reports no findings
- [ ] No IAM policy contains `"Resource": "*"` with sensitive actions
- [ ] All roles have permission boundaries set
- [ ] Periodic access review scheduled (quarterly)

**Effort:** Medium | **Timeline:** 2 days

---

### 7. API Throttling and Rate Limiting

**Threat:** Denial of service via connection flooding or excessive queries

**Current Status:** ⚠️ Partially implemented — Default API Gateway limits apply, no custom throttling

**Implementation Steps:**
1. Set WebSocket API route throttling: 100 requests/sec burst, 50 sustained
2. Set HTTP API throttling: 1000 requests/sec burst, 500 sustained
3. Add per-user rate limiting in WebSocket query Lambda (track in DynamoDB):
   - Max 30 queries per user per hour
   - Max 5 concurrent connections per user
4. Add WAF (Web Application Firewall) to CloudFront with rate-based rules
5. Configure AWS Shield Standard (enabled by default on CloudFront)

**CDK Changes:**
```typescript
// WAF for CloudFront
const waf = new wafv2.CfnWebACL(this, 'FrontendWAF', {
  scope: 'CLOUDFRONT',
  rules: [{
    name: 'RateLimit',
    action: { block: {} },
    statement: { rateBasedStatement: { limit: 1000, aggregateKeyType: 'IP' } },
  }],
});
```

**Verification:**
- [ ] 1000+ requests from single IP within 5 minutes triggers block
- [ ] Per-user query limit enforced (30/hour)
- [ ] WAF metrics visible in CloudWatch
- [ ] Load test shows graceful degradation under stress

**Effort:** Medium | **Timeline:** 2 days

---

### 8. CloudWatch Audit Logging with OpenTelemetry

**Threat:** Loss of audit trail for clinical decisions, repudiation

**Current Status:** ✅ Implemented — CloudWatch Logs + OTEL spans capture all interactions

**Remaining Actions:**
1. Enable CloudTrail for all API management actions (data events on sensitive S3 buckets)
2. Set log retention policy: 1 year minimum for clinical audit compliance
3. Add CloudWatch Insights queries for security monitoring:
   - Failed authentication attempts
   - Unusual query patterns (bulk patient lookups)
   - Agent errors and timeouts
4. Create CloudWatch Alarms for security events
5. Export logs to S3 for long-term cold storage (Glacier after 90 days)

**Verification:**
- [ ] All clinical queries have a unique Trail ID
- [ ] Log retention set to 365 days minimum
- [ ] CloudTrail captures IAM/S3/DynamoDB data events
- [ ] Alarm fires on 5+ failed auth attempts in 5 minutes
- [ ] Logs exportable for compliance audit

**Effort:** Medium | **Timeline:** 1.5 days

---

## Priority 3 — Medium (Implement Within 30 Days)

### 9. OpenSearch Network Access Controls

**Threat:** Direct access to vector stores bypassing application layer

**Current Status:** ⚠️ Network policy set to AllowFromPublic (required for Bedrock KB access)

**Implementation Steps:**
1. Restrict OpenSearch data access policies to only the AgentCore role and KB roles (already done)
2. Add VPC endpoint for OpenSearch Serverless (if moving to VPC-based architecture)
3. Monitor OpenSearch access patterns via CloudTrail
4. Review data access policy quarterly

**Verification:**
- [ ] Only AgentCore role and KB roles can read OpenSearch indices
- [ ] Direct API calls from unauthorized IAM roles return 403
- [ ] Access patterns logged and reviewed monthly

**Effort:** Low | **Timeline:** 0.5 day

---

### 10. S3 Public Access Prevention

**Threat:** Accidental public exposure of patient or guideline data

**Current Status:** ✅ Implemented — Frontend bucket uses BlockPublicAccess, OAC for CloudFront access

**Remaining Actions:**
1. Enable S3 Account-Level Public Access Block (applies to ALL buckets)
2. Add AWS Config rule `s3-bucket-public-read-prohibited`
3. Add AWS Config rule `s3-bucket-public-write-prohibited`
4. Set up Config remediation to auto-block public access if detected

**Verification:**
- [ ] Account-level S3 public access block enabled
- [ ] AWS Config rules active and compliant
- [ ] No bucket policy allows `"Principal": "*"` read/write

**Effort:** Low | **Timeline:** 0.5 day

---

### 11. Session Token Security

**Threat:** Session replay or token theft

**Current Status:** ✅ Implemented — sessionStorage (cleared on tab close), tokens expire

**Remaining Actions:**
1. Set Cognito token expiration to 1 hour (access token) / 1 day (refresh token)
2. Implement token refresh flow in frontend (currently no refresh — session expires)
3. Add `Secure` and `SameSite=Strict` attributes if migrating to cookie-based auth
4. Log all token issuance events in CloudTrail

**Verification:**
- [ ] Access token expires after 1 hour
- [ ] Refresh token rotates on each use
- [ ] Closing browser tab clears all tokens
- [ ] No tokens stored in localStorage

**Effort:** Low | **Timeline:** 0.5 day

---

### 12. Clinical Decision Disclaimer and Human Override

**Threat:** Over-reliance on AI recommendations leading to patient harm

**Current Status:** ✅ Implemented — Disclaimer per query with Trail ID, "Decision Support Only" messaging

**Remaining Actions:**
1. Add explicit "I acknowledge" button before first query in a session
2. Add confidence score threshold — flag low-confidence recommendations visually
3. Add "Report Concern" button that creates an audit event for clinical safety review
4. Implement monthly review of flagged recommendations by clinical governance team
5. Track clinician acknowledgment in audit log

**Verification:**
- [ ] Clinician must acknowledge disclaimer before first query
- [ ] Low-confidence recommendations (<60%) display amber warning
- [ ] "Report Concern" creates auditable event
- [ ] Monthly safety review process documented

**Effort:** Medium | **Timeline:** 2 days

---

## Implementation Timeline

| Week | Mitigations | Effort |
|------|-------------|--------|
| Week 1 | #1 (JWT at Gateway), #4 (TLS/HSTS), #10 (S3 public block), #11 (Token expiry) | 3 days |
| Week 2 | #2 (Prompt injection), #6 (IAM hardening) | 5 days |
| Week 3 | #3 (Tamper detection), #7 (Rate limiting + WAF) | 4 days |
| Week 4 | #5 (CMK encryption), #8 (Audit logging), #9 (OpenSearch), #12 (Clinical disclaimer) | 4.5 days |

**Total estimated effort: ~16.5 days (1 engineer, ~4 weeks)**

---

## Acceptance Criteria for Production Readiness

Before moving from sample to production:

- [ ] All 12 mitigations implemented and verified
- [ ] Penetration test completed by independent security team
- [ ] HIPAA/GDPR compliance review passed
- [ ] Data Processing Agreement (DPA) in place for patient data
- [ ] Clinical governance approval for AI decision support
- [ ] Disaster recovery plan tested (RTO < 4 hours, RPO < 1 hour)
- [ ] Security incident response runbook documented
- [ ] Annual security review cadence established

---

## Static Analysis (Semgrep) Dispositions

This section records how Semgrep HIGH findings on the frontend and deployment
tooling are handled, so reviewers can verify each was addressed rather than
silently ignored.

### XSS — `insecure-innerhtml` / `insecure-document-method` (frontend/js/app.js)

**Resolved in code.** All dynamic HTML rendering is routed through a single
`setSafeHtml(element, dirtyHtml)` helper. That helper sanitizes with DOMPurify
using `RETURN_DOM_FRAGMENT: true` and attaches the resulting nodes via
`element.replaceChildren(fragment)`. No `innerHTML` / `outerHTML` /
`document.write` sink is used for dynamic content, so the anti-pattern is
eliminated (not merely suppressed). If DOMPurify is unavailable the helper fails
closed to `textContent`. Individual field values are additionally escaped with
`escapeHtml()` before assembly (defense in depth). Remaining `innerHTML`
assignments in the file are static string literals with no interpolation.

### Command injection — `dangerous-subprocess-use-audit` (deployment/scripts/build_and_push_image.py)

**Resolved in code.** The generic `run(cmd)` wrapper that passed a variable to
`subprocess.run` was removed. Every command is now a literal argument list
(`subprocess.run(['docker', 'build', ...], shell=False)`), so no dynamic command
string reaches the subprocess and there is no shell interpretation. A
`_succeeded()` helper only inspects the completed process.

### `purify.min.js` line 2 — accepted false positive (third-party sanitizer)

DOMPurify (v3.4.17, the npm `latest` release at the time of the security review,
unmodified official release from Cure53) is the sanitizer
itself; its internal `documentElement.innerHTML` use is fundamental to how HTML
sanitization works and is not attacker-controlled application code. This is a
false positive on vetted vendor code. Handling:

- The library is vendored (not loaded from a CDN) to avoid supply-chain and
  availability risk. Pinned to exactly 3.4.17; `frontend/lib/purify.min.js` is
  the unmodified `dist/purify.min.js` from the npm tarball
  `dompurify-3.4.17.tgz`, whose registry integrity
  (`sha512-B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==`)
  was verified before extraction. To re-verify: download that tarball, check
  the integrity, and diff `package/dist/purify.min.js` against the vendored
  file (they must be byte-identical).
- `index.html` loads it with a Subresource Integrity (SRI) `integrity` hash so
  any tampering is detected by the browser.
- `.semgrepignore` excludes vendored/minified libraries
  (`**/*.min.js`, `frontend/lib/`). Scanners that do not honor `.semgrepignore`
  (e.g. when scanning a flattened export) should exclude vendored libraries at
  the scan-pipeline level; this finding is dispositioned as an accepted false
  positive on third-party code.
