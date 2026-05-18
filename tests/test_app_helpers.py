"""Unit tests for orchestrator startup helpers in `orchestrator.app`."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from orchestrator import app as orchestrator_app


_HEX64 = "a" * 64
_HEX64_B = "b" * 64


@pytest.mark.parametrize(
	"cgroup_text,expected",
	[
		# cgroupv1 with cgroupfs driver — leading `/docker/<hex>`.
		(f"12:cpu,cpuacct:/docker/{_HEX64}\n", _HEX64),
		# cgroupv2 with cgroupfs driver — single `0::/<...>` line.
		(f"0::/docker/{_HEX64}\n", _HEX64),
		# cgroupv1 with systemd driver — `/system.slice/docker-<hex>.scope`.
		(f"12:cpu,cpuacct:/system.slice/docker-{_HEX64}.scope\n", _HEX64),
		# cgroupv2 with systemd driver — same shape, single line. This is
		# the default layout on GitHub Actions ubuntu-latest runners.
		(f"0::/system.slice/docker-{_HEX64}.scope\n", _HEX64),
		# Multiple controllers; the parser stops at the first match.
		(
			"11:freezer:/\n"
			"10:memory:/\n"
			f"0::/system.slice/docker-{_HEX64}.scope\n",
			_HEX64,
		),
		# Non-docker host — no 64-hex id present.
		("0::/init.scope\n", None),
		# Empty cgroup file.
		("", None),
	],
)
def test_detect_own_container_id_parses_common_layouts(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
	cgroup_text: str,
	expected: str | None,
) -> None:
	cgroup_file = tmp_path / "cgroup"
	cgroup_file.write_text(cgroup_text)

	original_read_text = Path.read_text

	def fake_read_text(self: Path, *args, **kwargs):
		if str(self) == "/proc/self/cgroup":
			return cgroup_file.read_text(*args, **kwargs)
		return original_read_text(self, *args, **kwargs)

	monkeypatch.setattr(Path, "read_text", fake_read_text)
	assert orchestrator_app._detect_own_container_id() == expected


def test_detect_own_container_id_returns_none_when_proc_missing(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	def fake_read_text(self: Path, *args, **kwargs):
		raise OSError("no such file")

	monkeypatch.setattr(Path, "read_text", fake_read_text)
	# Force the hostname fallback to also fail so the helper's None-path
	# is exercised in isolation.
	monkeypatch.setattr(socket, "gethostname", lambda: "not-a-container-id")
	assert orchestrator_app._detect_own_container_id() is None


def test_parse_cgroup_loose_match_catches_unrecognized_delimiter() -> None:
	# A kubernetes-style nested cgroup with no `/` or `-` directly before
	# the container id — the strict pass misses it but the loose 64-hex
	# scan still finds the id.
	text = f"0::/kubepods.slice/kubepods-pod1234.slice/cri-containerd:{_HEX64_B}.scope\n"
	assert orchestrator_app._parse_cgroup_for_container_id(text) == _HEX64_B


def test_parse_mountinfo_finds_docker_container_path() -> None:
	# Real Docker bind-mounts /etc/hostname, /etc/hosts, /etc/resolv.conf
	# from /var/lib/docker/containers/<id>/ — the `/containers/<id>/`
	# substring is the signal.
	text = (
		"2076 1953 0:135 / / rw,relatime master:608 - overlay overlay "
		"rw,lowerdir=/var/lib/docker/overlay2/l/X:/var/lib/docker/overlay2/l/Y\n"
		"2090 2076 0:178 "
		f"/var/lib/docker/containers/{_HEX64}/hostname "
		"/etc/hostname ro,relatime - tmpfs tmpfs rw\n"
		f"2091 2076 0:178 /var/lib/docker/containers/{_HEX64}/hosts "
		"/etc/hosts ro,relatime - tmpfs tmpfs rw\n"
	)
	assert orchestrator_app._parse_mountinfo_for_container_id(text) == _HEX64


def test_parse_mountinfo_returns_none_outside_container() -> None:
	# A bare-metal host's mountinfo has no /containers/<hex>/ paths.
	text = (
		"22 28 0:21 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw\n"
		"23 28 0:4 / /proc rw,nosuid,nodev,noexec,relatime shared:14 - proc proc rw\n"
	)
	assert orchestrator_app._parse_mountinfo_for_container_id(text) is None


def test_detect_falls_back_to_mountinfo_when_cgroup_unrecognized(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# cgroup present but no 64-hex id; mountinfo carries the id.
	def fake_read_text(self: Path, *a, **kw) -> str:
		path = str(self)
		if path == "/proc/self/cgroup":
			return "0::/init.scope\n"
		if path == "/proc/self/mountinfo":
			return (
				"2090 2076 0:178 "
				f"/var/lib/docker/containers/{_HEX64_B}/hostname "
				"/etc/hostname ro,relatime - tmpfs tmpfs rw\n"
			)
		return ""

	monkeypatch.setattr(Path, "read_text", fake_read_text)
	monkeypatch.setattr(socket, "gethostname", lambda: "orchestrator")
	assert orchestrator_app._detect_own_container_id() == _HEX64_B


def test_detect_falls_back_to_hostname_when_cgroup_and_mountinfo_silent(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# Neither file carries a docker container id; hostname is the short id.
	def fake_read_text(self: Path, *a, **kw) -> str:
		path = str(self)
		if path == "/proc/self/cgroup":
			return "0::/init.scope\n"
		if path == "/proc/self/mountinfo":
			return "22 28 0:21 / /sys rw - sysfs sysfs rw\n"
		return ""

	monkeypatch.setattr(Path, "read_text", fake_read_text)
	monkeypatch.setattr(socket, "gethostname", lambda: "abcdef012345")
	assert orchestrator_app._detect_own_container_id() == "abcdef012345"


def test_detect_ignores_nonhex_hostname(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	def fake_read_text(self: Path, *a, **kw) -> str:
		path = str(self)
		if path == "/proc/self/cgroup":
			return "0::/init.scope\n"
		if path == "/proc/self/mountinfo":
			return "22 28 0:21 / /sys rw - sysfs sysfs rw\n"
		return ""

	monkeypatch.setattr(Path, "read_text", fake_read_text)
	# Operator (or compose) overrode --hostname to a service name.
	monkeypatch.setattr(socket, "gethostname", lambda: "orchestrator")
	assert orchestrator_app._detect_own_container_id() is None


def test_detect_prefers_cgroup_over_mountinfo_and_hostname(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	def fake_read_text(self: Path, *a, **kw) -> str:
		path = str(self)
		if path == "/proc/self/cgroup":
			return f"0::/docker/{_HEX64}\n"
		if path == "/proc/self/mountinfo":
			return (
				"2090 2076 0:178 "
				f"/var/lib/docker/containers/{_HEX64_B}/hostname "
				"/etc/hostname - tmpfs tmpfs rw\n"
			)
		return ""

	monkeypatch.setattr(Path, "read_text", fake_read_text)
	monkeypatch.setattr(socket, "gethostname", lambda: "ffffffffffff")
	# Cgroup wins, so we get _HEX64 — not _HEX64_B from mountinfo, nor
	# the short hostname.
	assert orchestrator_app._detect_own_container_id() == _HEX64
