from datetime import datetime

import pytest
from pydantic import ValidationError

from orchestrator.models import (
    CommandStatus,
    ContainerResponse,
    ContainerStatus,
    CreateContainerRequest,
    ExecStatusResponse,
    ImageDetail,
    ImageSummary,
)


class TestCreateContainerRequest:
    def test_defaults(self):
        req = CreateContainerRequest(image="python-runner")
        assert req.image == "python-runner"
        assert req.privileged is False
        assert req.env == {}
        assert req.label is None
        assert req.timeout_seconds == 300

    def test_custom_values(self):
        req = CreateContainerRequest(
            image="node-runner",
            privileged=True,
            env={"FOO": "bar"},
            label="my-container",
            timeout_seconds=600,
        )
        assert req.privileged is True
        assert req.env == {"FOO": "bar"}
        assert req.label == "my-container"
        assert req.timeout_seconds == 600

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", timeout_seconds=0)
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", timeout_seconds=-1)


class TestImageSummary:
    def test_from_docker(self):
        data = {
            "RepoTags": ["drover/python-runner:latest", "drover/python-runner:1.0"],
            "Size": 123456,
            "Created": 1700000000,
        }
        summary = ImageSummary.from_docker(data)
        assert summary.name == "python-runner"
        assert summary.tags == ["latest", "1.0"]
        assert summary.size == 123456

    def test_from_docker_no_tags(self):
        data = {"RepoTags": None, "Size": 0, "Created": 1700000000}
        summary = ImageSummary.from_docker(data)
        assert summary.name == ""
        assert summary.tags == []


class TestImageDetail:
    def test_from_docker_inspect(self):
        data = {
            "RepoTags": ["drover/python-runner:latest"],
            "Size": 50000,
            "Created": "2024-01-01T00:00:00Z",
            "Id": "sha256:abc123",
            "Architecture": "amd64",
            "Os": "linux",
        }
        detail = ImageDetail.from_docker_inspect("python-runner", data)
        assert detail.name == "python-runner"
        assert detail.id == "sha256:abc123"
        assert detail.architecture == "amd64"
        assert detail.os == "linux"
        assert detail.tags == ["latest"]


class TestContainerStatus:
    def test_all_statuses(self):
        expected = {"running", "stopping", "stopped", "resuming", "destroying", "destroyed"}
        assert {s.value for s in ContainerStatus} == expected


class TestExecStatusResponse:
    def test_defaults(self):
        resp = ExecStatusResponse(
            command_id="CMD01",
            status=CommandStatus.pending,
        )
        assert resp.exit_code is None
        assert resp.messages == []
