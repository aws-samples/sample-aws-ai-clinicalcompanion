"""Tests for AgentCore Memory resolution in deployment/scripts/post_cdk_setup.py.

Regression: after a full teardown, setup_memory reused a memory ID that was
still DELETING (from list_memories) or already gone (cached in SSM), leaving
the agent pointed at a non-existent memory.
"""

import importlib.util
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SCRIPT = Path(__file__).resolve().parents[2] / "deployment" / "scripts" / "post_cdk_setup.py"
_spec = importlib.util.spec_from_file_location("post_cdk_setup", SCRIPT)
post_cdk_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post_cdk_setup)

NOT_FOUND = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetMemory")


class FakeSsm:
    def __init__(self, value=None):
        self.value = value
        self.put = None

    def get_parameter(self, Name):
        if self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.put = Value


class FakeControl:
    def __init__(self, memories, statuses, create_outcomes=("ACTIVE",)):
        self.memories = memories          # list_memories() entries
        self.statuses = statuses          # memory id -> status; missing = not found
        self.create_outcomes = list(create_outcomes)  # final status of each successive create
        self.created_ids = []
        self.deleted_ids = []

    @property
    def created(self):
        return bool(self.created_ids)

    def list_memories(self):
        return {"memories": self.memories}

    def get_memory(self, memoryId):
        if memoryId not in self.statuses:
            raise NOT_FOUND
        return {"memory": {"id": memoryId, "status": self.statuses[memoryId], "name": "other",
                           "failureReason": "Failed to provision Vector Store for the customer"}}

    def create_memory(self, name, description, eventExpiryDuration):
        n = len(self.created_ids)
        mid = "clinicalcompass_mem-NEW" if n == 0 else f"clinicalcompass_mem-NEW{n}"
        self.statuses[mid] = self.create_outcomes[min(n, len(self.create_outcomes) - 1)]
        self.created_ids.append(mid)
        return {"memory": {"id": mid}}

    def delete_memory(self, memoryId):
        self.deleted_ids.append(memoryId)
        self.statuses.pop(memoryId, None)


class FakeSession:
    def __init__(self, control, ssm):
        self._clients = {"bedrock-agentcore-control": control, "ssm": ssm}

    def client(self, name, **_):
        return self._clients[name]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(post_cdk_setup.time, "sleep", lambda _s: None)


def test_reuses_active_memory_from_ssm():
    control = FakeControl([], {"clinicalcompass_mem-OK": "ACTIVE"})
    ssm = FakeSsm("clinicalcompass_mem-OK")

    assert post_cdk_setup.setup_memory(FakeSession(control, ssm), "us-east-1") == "clinicalcompass_mem-OK"
    assert not control.created


def test_stale_ssm_id_for_deleted_memory_creates_new_one():
    control = FakeControl([], {})
    ssm = FakeSsm("clinicalcompass_mem-GONE")

    assert post_cdk_setup.setup_memory(FakeSession(control, ssm), "us-east-1") == "clinicalcompass_mem-NEW"
    assert control.created
    assert ssm.put == "clinicalcompass_mem-NEW"


def test_deleting_memory_in_listing_is_not_reused():
    control = FakeControl(
        [{"id": "clinicalcompass_mem-OLD", "status": "DELETING"}],
        {"clinicalcompass_mem-OLD": "DELETING"},
    )
    ssm = FakeSsm(None)

    assert post_cdk_setup.setup_memory(FakeSession(control, ssm), "us-east-1") == "clinicalcompass_mem-NEW"
    assert control.created


def test_active_memory_in_listing_is_reused():
    control = FakeControl(
        [{"id": "clinicalcompass_mem-LIVE", "status": "ACTIVE"}],
        {"clinicalcompass_mem-LIVE": "ACTIVE"},
    )
    ssm = FakeSsm(None)

    assert post_cdk_setup.setup_memory(FakeSession(control, ssm), "us-east-1") == "clinicalcompass_mem-LIVE"
    assert not control.created
    assert ssm.put == "clinicalcompass_mem-LIVE"


def test_transient_failure_is_retried_and_failed_memory_deleted():
    # AgentCore reports FAILED ("Failed to provision Vector Store") on the first attempt
    control = FakeControl([], {}, create_outcomes=("FAILED", "ACTIVE"))
    ssm = FakeSsm(None)

    result = post_cdk_setup.setup_memory(FakeSession(control, ssm), "us-east-1")

    assert result == "clinicalcompass_mem-NEW1"
    assert control.deleted_ids == ["clinicalcompass_mem-NEW"]
    assert ssm.put == "clinicalcompass_mem-NEW1"


def test_gives_up_after_max_attempts_and_cleans_up():
    control = FakeControl([], {}, create_outcomes=("FAILED",))
    ssm = FakeSsm(None)

    result = post_cdk_setup.setup_memory(FakeSession(control, ssm), "us-east-1")

    assert result is None
    assert len(control.created_ids) == post_cdk_setup.MEMORY_CREATE_ATTEMPTS
    assert control.deleted_ids == control.created_ids  # no FAILED memory left behind
    assert ssm.put is None  # SSM is not pointed at a failed memory
