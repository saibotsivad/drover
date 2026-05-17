"""Unit tests for orchestrator startup helpers in `orchestrator.app`."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import app as orchestrator_app


_HEX64 = "a" * 64


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
	assert orchestrator_app._detect_own_container_id() is None
