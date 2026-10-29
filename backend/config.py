"""
Backend Runtime Configuration
Loads from environment variables with sensible defaults for local development.
"""

import os

# AWS Region
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Bedrock Model Configuration
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
BEDROCK_TEMPERATURE = float(os.environ.get("BEDROCK_TEMPERATURE", "0"))

# Bedrock client timeouts
BEDROCK_READ_TIMEOUT = int(os.environ.get("BEDROCK_READ_TIMEOUT", "300"))
BEDROCK_CONNECT_TIMEOUT = int(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10"))
BEDROCK_MAX_RETRIES = int(os.environ.get("BEDROCK_MAX_RETRIES", "2"))
BEDROCK_MAX_POOL_CONNECTIONS = int(os.environ.get("BEDROCK_MAX_POOL_CONNECTIONS", "50"))

# Knowledge Base query optimization (Requirement 7.5)
# Reduced from 5/10 to 3 to minimize retrieval latency while maintaining relevance
KB_NUMBER_OF_RESULTS = int(os.environ.get("KB_NUMBER_OF_RESULTS", "3"))
KB_SEARCH_TYPE = os.environ.get("KB_SEARCH_TYPE", "HYBRID")

# Connection reuse: TCP keep-alive prevents connections from being closed between requests,
# reducing latency on subsequent calls by reusing established TCP connections.
BEDROCK_TCP_KEEPALIVE = os.environ.get("BEDROCK_TCP_KEEPALIVE", "true").lower() == "true"
