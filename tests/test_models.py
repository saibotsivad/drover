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

    # -- image validation --

    def test_image_with_dots_and_hyphens(self):
        req = CreateContainerRequest(image="my-image.v2")
        assert req.image == "my-image.v2"

    def test_image_with_slashes(self):
        req = CreateContainerRequest(image="myorg/my-image")
        assert req.image == "myorg/my-image"

    def test_image_with_underscores(self):
        req = CreateContainerRequest(image="my_image_v2")
        assert req.image == "my_image_v2"

    def test_image_single_char(self):
        req = CreateContainerRequest(image="a")
        assert req.image == "a"

    def test_image_rejects_empty(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="")

    def test_image_rejects_spaces(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="my image")

    def test_image_rejects_leading_hyphen(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="-my-image")

    def test_image_rejects_trailing_hyphen(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="my-image-")

    def test_image_rejects_special_chars(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="my:image")
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="my@image")
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="my$image")

    def test_image_rejects_double_slash(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="org//image")

    def test_image_rejects_slash_with_leading_hyphen(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="org/-image")

    def test_image_too_long(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="a" * 257)

    # -- label validation --

    def test_label_accepts_printable(self):
        req = CreateContainerRequest(image="test", label="Hello, World! 123")
        assert req.label == "Hello, World! 123"

    def test_label_accepts_unicode(self):
        req = CreateContainerRequest(image="test", label="café résumé 日本語")
        assert req.label == "café résumé 日本語"

    def test_label_accepts_tabs_and_newlines(self):
        req = CreateContainerRequest(image="test", label="line1\nline2\ttab")
        assert req.label == "line1\nline2\ttab"

    def test_label_rejects_control_chars(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", label="bad\x00label")
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", label="bad\x1flabel")
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", label="bad\x7flabel")

    def test_label_too_long(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", label="a" * 1025)

    def test_label_none_is_valid(self):
        req = CreateContainerRequest(image="test", label=None)
        assert req.label is None

    # -- timeout validation --

    def test_timeout_max_24_hours(self):
        req = CreateContainerRequest(image="test", timeout_seconds=86400)
        assert req.timeout_seconds == 86400

    def test_timeout_exceeds_max(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", timeout_seconds=86401)

    # -- env validation --

    def test_env_valid_keys(self):
        req = CreateContainerRequest(
            image="test",
            env={"HOME": "/root", "_SECRET": "val", "MY_VAR_2": "val"},
        )
        assert len(req.env) == 3

    def test_env_rejects_key_starting_with_digit(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", env={"1BAD": "val"})

    def test_env_rejects_key_with_hyphen(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", env={"BAD-KEY": "val"})

    def test_env_rejects_key_with_equals(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", env={"BAD=KEY": "val"})

    def test_env_rejects_empty_key(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", env={"": "val"})

    def test_env_key_too_long(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", env={"A" * 257: "val"})

    def test_env_value_too_long(self):
        with pytest.raises(ValidationError):
            CreateContainerRequest(image="test", env={"KEY": "x" * 32_769})


class TestImageSummary:
    def test_from_docker(self):
        data = {
            "RepoTags": ["ghcr.io/example/python-runner:latest", "ghcr.io/example/python-runner:1.0"],
            "Labels": {"drover.managed": "true", "drover.name": "python-runner"},
            "Size": 123456,
            "Created": 1700000000,
        }
        summary = ImageSummary.from_docker(data)
        assert summary.name == "python-runner"
        assert summary.tags == ["latest", "1.0"]
        assert summary.labels == {
            "drover.managed": "true",
            "drover.name": "python-runner",
        }
        assert summary.size == 123456

    def test_from_docker_no_tags(self):
        data = {
            "RepoTags": None,
            "Labels": {"drover.managed": "true", "drover.name": "python-runner"},
            "Size": 0,
            "Created": 1700000000,
        }
        summary = ImageSummary.from_docker(data)
        assert summary.name == "python-runner"
        assert summary.tags == []
        assert summary.labels == {
            "drover.managed": "true",
            "drover.name": "python-runner",
        }

    def test_from_docker_missing_labels(self):
        """An image without the drover.name label yields an empty name."""
        data = {
            "RepoTags": ["random/image:latest"],
            "Labels": None,
            "Size": 0,
            "Created": 1700000000,
        }
        summary = ImageSummary.from_docker(data)
        assert summary.name == ""
        assert summary.tags == ["latest"]
        assert summary.labels == {}

    def test_from_docker_filters_non_drover_labels(self):
        """Non-drover labels (e.g. OCI image labels) are not exposed."""
        data = {
            "RepoTags": [],
            "Labels": {
                "drover.managed": "true",
                "drover.name": "python-runner",
                "drover.template": "true",
                "org.opencontainers.image.source": "https://example.com/repo",
                "maintainer": "ops@example.com",
            },
            "Size": 0,
            "Created": 1700000000,
        }
        summary = ImageSummary.from_docker(data)
        assert summary.labels == {
            "drover.managed": "true",
            "drover.name": "python-runner",
            "drover.template": "true",
        }


class TestImageDetail:
    def test_from_docker_inspect(self):
        data = {
            "RepoTags": ["ghcr.io/example/python-runner:latest"],
            "Size": 50000,
            "Created": "2024-01-01T00:00:00Z",
            "Id": "sha256:abc123",
            "Architecture": "amd64",
            "Os": "linux",
            "Config": {
                "Labels": {"drover.managed": "true", "drover.name": "python-runner"},
            },
        }
        detail = ImageDetail.from_docker_inspect(data)
        assert detail.name == "python-runner"
        assert detail.id == "sha256:abc123"
        assert detail.architecture == "amd64"
        assert detail.os == "linux"
        assert detail.tags == ["latest"]
        assert detail.labels == {
            "drover.managed": "true",
            "drover.name": "python-runner",
        }

    def test_from_docker_inspect_no_config(self):
        """Defensive: an inspect payload missing Config still parses."""
        data = {
            "RepoTags": [],
            "Size": 50000,
            "Created": "2024-01-01T00:00:00Z",
            "Id": "sha256:abc123",
        }
        detail = ImageDetail.from_docker_inspect(data)
        assert detail.name == ""
        assert detail.tags == []
        assert detail.labels == {}


class TestContainerStatus:
    def test_all_statuses(self):
        expected = {
            "initializing",
            "running",
            "stopping",
            "stopped",
            "resuming",
            "destroying",
            "destroyed",
            "error",
        }
        assert {s.value for s in ContainerStatus} == expected


class TestExecStatusResponse:
    def test_defaults(self):
        resp = ExecStatusResponse(
            command_id="CMD01",
            status=CommandStatus.pending,
        )
        assert resp.exit_code is None
        assert resp.messages == []
