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


def test_detect_falls_back_to_hostname_when_cgroup_unrecognized(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# cgroup file present but no 64-hex id anywhere.
	monkeypatch.setattr(
		Path,
		"read_text",
		lambda self, *a, **kw: "0::/init.scope\n" if str(self) == "/proc/self/cgroup" else "",
	)
	monkeypatch.setattr(socket, "gethostname", lambda: "abcdef012345")  # 12-char short id
	assert orchestrator_app._detect_own_container_id() == "abcdef012345"


def test_detect_ignores_nonhex_hostname(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(
		Path,
		"read_text",
		lambda self, *a, **kw: "0::/init.scope\n" if str(self) == "/proc/self/cgroup" else "",
	)
	# Operator overrode --hostname to something arbitrary.
	monkeypatch.setattr(socket, "gethostname", lambda: "drover-prod-01")
	assert orchestrator_app._detect_own_container_id() is None


def test_detect_prefers_cgroup_over_hostname(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(
		Path,
		"read_text",
		lambda self, *a, **kw: f"0::/docker/{_HEX64}\n" if str(self) == "/proc/self/cgroup" else "",
	)
	monkeypatch.setattr(socket, "gethostname", lambda: "ffffffffffff")
	# cgroup match wins so we get the full 64-hex id, not the short hostname.
	assert orchestrator_app._detect_own_container_id() == _HEX64
