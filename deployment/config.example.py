"""
ClinicalCompass Deployment Configuration — EXAMPLE
Copy this file to config.py and fill in your values.

    cp deployment/config.example.py deployment/config.py

Used by the scripts in deployment/scripts/. Everything else (API endpoints,
Knowledge Base IDs, the AgentCore runtime ARN, memory ID) is discovered at run
time from the CloudFormation stack outputs and SSM Parameter Store.
"""

# AWS Configuration
AWS_PROFILE = "your-aws-profile"
AWS_REGION = "us-east-1"
AWS_ACCOUNT_ID = "123456789012"

# ECR Configuration (fallback when the stack output is not available)
ECR_REPOSITORY = "bedrock-agentcore-clinicalcompass"
ECR_URI = f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com/{ECR_REPOSITORY}"
