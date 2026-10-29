"""The query Lambda's AgentCore client must tolerate long gaps in the agent stream.

Regression: with botocore's default 60s read timeout, a cold-start query failed with
ReadTimeoutError while the agent was still working, and the user got no answer.
"""

import importlib
import os
import sys
from unittest.mock import patch

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda", "websocket")


def _import_query_capturing_clients():
    env = {
        "CONNECTIONS_TABLE_NAME": "test-connections",
        "AWS_BEDROCK_REGION": "us-east-1",
        "AGENT_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
    }
    sys.path.insert(0, LAMBDA_DIR)
    try:
        with patch.dict(os.environ, env), patch("boto3.resource"), patch("boto3.client") as client:
            sys.modules.pop("query", None)
            module = importlib.import_module("query")
            return module, client.call_args_list
    finally:
        sys.path.remove(LAMBDA_DIR)
        sys.modules.pop("query", None)


def test_agentcore_client_uses_extended_read_timeout():
    module, calls = _import_query_capturing_clients()
    agentcore_calls = [c for c in calls if c.args and c.args[0] == "bedrock-agentcore"]

    assert len(agentcore_calls) == 1
    config = agentcore_calls[0].kwargs["config"]
    assert config.read_timeout == module.AGENTCORE_READ_TIMEOUT_S
    assert module.AGENTCORE_READ_TIMEOUT_S >= 300


def test_read_timeout_fits_inside_lambda_guard():
    module, _ = _import_query_capturing_clients()
    assert module.AGENTCORE_READ_TIMEOUT_S < module.LAMBDA_TIMEOUT_THRESHOLD_S
