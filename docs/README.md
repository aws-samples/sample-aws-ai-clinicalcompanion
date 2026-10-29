# ClinicalCompass Documentation

Complete documentation for the ClinicalCompass clinical decision support system.

---

## Getting Started

- **[Architecture](./architecture.md)** — System architecture and design
- **[Observability](./observability.md)** — Monitoring, logging, and tracing
- **[Future Enhancements](./future-enhancements.md)** — Roadmap and planned features
- **[CDK Infrastructure](../infra/README.md)** — Full resource inventory and deployment details

---

## Quick Reference

**Frontend Demo:** `http://localhost:8080` (run `python3 -m http.server 8080` in `frontend/`)

**Login:** Create a Cognito user post-deploy, then sign in with that username/password

**Test Patient IDs:** `001` through `020`

---

## Testing

```bash
# Run all backend tests (first time: pip install -r backend/requirements-dev.txt)
cd backend && python -m pytest -q

# Run frontend tests
cd frontend && node tests/test_agui_properties.js && node tests/test_tool_badge_removal.js

# CDK security assertion tests and synth check
cd infra && npx jest && npx cdk synth --quiet
```

To check a deployed stack end to end, sign in through the CloudFront URL and run a query (see the main README for creating a user).

---

## Deployment

```bash
# Fresh deploy
cd infra && npx cdk deploy --require-approval never
cd .. && python deployment/scripts/build_and_push_image.py
python deployment/scripts/post_cdk_setup.py

# Full teardown
python deployment/scripts/cleanup_all.py --yes
```

See [infra/README.md](../infra/README.md) for detailed resource inventory and troubleshooting.

---

## Support

- View Lambda logs: `aws logs tail /aws/lambda/clinicalcompass-ws-query --follow --region us-east-1`
- View AgentCore logs: `python deployment/scripts/check_agentcore_logs.py`
- Check stack status: `aws cloudformation describe-stacks --stack-name ClinicalCompassStack --query 'Stacks[0].StackStatus'`
