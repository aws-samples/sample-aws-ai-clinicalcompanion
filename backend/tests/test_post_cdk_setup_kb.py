"""Tests for Knowledge Base creation in deployment/scripts/post_cdk_setup.py.

Regression: right after the OpenSearch Serverless index was created, Bedrock
CreateKnowledgeBase failed with "no such index" and the Global-Guidelines KB was
never created on a fresh deploy.
"""

import importlib.util
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SCRIPT = Path(__file__).resolve().parents[2] / "deployment" / "scripts" / "post_cdk_setup.py"
_spec = importlib.util.spec_from_file_location("post_cdk_setup_kb", SCRIPT)
post_cdk_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post_cdk_setup)


def _error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, "CreateKnowledgeBase")


NO_SUCH_INDEX = _error(
    "ValidationException",
    "The knowledge base storage configuration provided is invalid... Dependency error document "
    "status code: 404, error message: no such index [cc-global-guidelines]",
)


class FakeBedrockAgent:
    def __init__(self, failures):
        self.failures = list(failures)  # errors raised before a successful create
        self.calls = 0

    def create_knowledge_base(self, name, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"knowledgeBase": {"knowledgeBaseId": "KBID123456"}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(post_cdk_setup.time, "sleep", lambda _s: None)


def test_index_not_yet_visible_is_retried():
    agent = FakeBedrockAgent([NO_SUCH_INDEX, NO_SUCH_INDEX])

    assert post_cdk_setup._create_knowledge_base(agent, "kb", {}) == "KBID123456"
    assert agent.calls == 3


def test_gives_up_after_max_attempts():
    agent = FakeBedrockAgent([NO_SUCH_INDEX] * post_cdk_setup.KB_INDEX_VISIBILITY_ATTEMPTS)

    with pytest.raises(ClientError, match="no such index"):
        post_cdk_setup._create_knowledge_base(agent, "kb", {})
    assert agent.calls == post_cdk_setup.KB_INDEX_VISIBILITY_ATTEMPTS


def test_other_errors_are_not_retried():
    # e.g. a blocked name: the caller handles this by retrying with a fallback name
    agent = FakeBedrockAgent([_error("ConflictException", "KB with name already exists")])

    with pytest.raises(ClientError, match="already exists"):
        post_cdk_setup._create_knowledge_base(agent, "kb", {})
    assert agent.calls == 1
