"""Tests for scripts/build_manifest.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import build_manifest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
INSTALLER_TEMPLATE = REPO_ROOT / "scripts" / "install.sh.template"
COMPOSE_SOURCE = REPO_ROOT / "docker-compose.yml"


SHA = "sha256:" + ("0" * 64)
SHA2 = "sha256:" + ("a" * 64)
SHA3 = "sha256:" + ("b" * 64)


# --------------------------------------------------------------------------- #
# Fake repo-root construction
# --------------------------------------------------------------------------- #


def _make_changelog(path: Path, published: str, history: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"published": published, "changes": history}, sort_keys=False))


def make_repo(tmp_path: Path) -> Path:
    """Return a synthetic repo root with CHANGELOG.yml for every project."""
    root = tmp_path / "repo"
    root.mkdir()

    _make_changelog(root / "orchestrator/CHANGELOG.yml", "1.2.4", [
        {"version": "1.2.4", "date": "2026-05-23", "entries": [
            {"bump": "patch", "description": "Fixed crash on malformed input.\n"},
        ]},
        {"version": "1.2.3", "date": "2026-05-22", "entries": [
            {"bump": "patch", "description": "Earlier patch.\n"},
        ]},
    ])
    _make_changelog(root / "builder/CHANGELOG.yml", "0.4.1", [
        {"version": "0.4.1", "date": "2026-05-23", "entries": [
            {"bump": "patch", "description": "Bumped base image.\n"},
        ]},
        {"version": "0.4.0", "date": "2026-05-22", "entries": [
            {"bump": "minor", "description": "Earlier minor.\n"},
        ]},
    ])
    _make_changelog(root / "webapp/CHANGELOG.yml", "2.0.0", [
        {"version": "2.0.0", "date": "2026-05-23", "entries": [
            {"bump": "major", "description": "Renamed proxy endpoints.\n"},
            {"bump": "minor", "description": "Added a dashboard.\n"},
        ]},
        {"version": "1.9.0", "date": "2026-05-22", "entries": [
            {"bump": "minor", "description": "Earlier minor.\n"},
        ]},
    ])
    _make_changelog(root / "executor/CHANGELOG.yml", "0.3.1", [
        {"version": "0.3.1", "date": "2026-05-23", "entries": [
            {"bump": "patch", "description": "Patch executor.\n"},
        ]},
        {"version": "0.3.0", "date": "2026-05-22", "entries": [
            {"bump": "patch", "description": "Earlier patch.\n"},
        ]},
    ])

    # Real docker-compose.yml from the repo — exercises the actual file shape.
    shutil.copy(COMPOSE_SOURCE, root / "docker-compose.yml")
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def output_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


def _run_build(
    repo,
    output_dir,
    *,
    components,
    previous=None,
    cli_assets=None,
    drover_version="v2026.5-3",
    released_at="2026-05-23T18:00:00Z",
):
    build_manifest.build(
        drover_version=drover_version,
        repo_root=repo,
        previous_manifest_path=previous,
        components_specs=components,
        cli_assets_path=cli_assets,
        released_at=released_at,
        compose_source=repo / "docker-compose.yml",
        installer_template=INSTALLER_TEMPLATE,
        output_dir=output_dir,
    )


def _read(output_dir: Path, name: str) -> dict:
    return yaml.safe_load((output_dir / name).read_text())


# --------------------------------------------------------------------------- #
# parse_component_spec
# --------------------------------------------------------------------------- #


def test_parse_component_spec_ok():
    parsed = build_manifest.parse_component_spec(
        f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"
    )
    assert parsed == {
        "name": "orchestrator",
        "version": "1.2.4",
        "image": "ghcr.io/saibotsivad/drover",
        "digest": SHA,
    }


@pytest.mark.parametrize("spec", [
    "orchestrator=1.2.4",
    "orchestrator=1.2.4:ghcr.io/x",
    f"orchestrator=1.2.4:ghcr.io/x@{SHA[:-1]}",  # short digest
    f"BAD=1.2.4:ghcr.io/x@{SHA}",
])
def test_parse_component_spec_bad(spec):
    with pytest.raises(build_manifest.BuildError):
        build_manifest.parse_component_spec(spec)


# --------------------------------------------------------------------------- #
# manifest.yaml assembly
# --------------------------------------------------------------------------- #


def test_no_previous_one_component_changed_partial_manifest(repo):
    """No previous + one container in --component: manifest assembly is
    partial (other containers lack digest). Compose rendering can't run on
    that — exercised separately below."""
    components_specs = [f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"]
    changed = {p["name"]: p for p in (
        build_manifest.parse_component_spec(s) for s in components_specs
    )}
    components = build_manifest.assemble_components(
        repo_root=repo, changed=changed, previous={}, cli_assets=None,
    )
    assert components["orchestrator"] == {
        "version": "1.2.4",
        "image": "ghcr.io/saibotsivad/drover:1.2.4",
        "digest": SHA,
    }
    # First-release fallbacks for the unchanged container components carry
    # version + image but no digest (warning logged to stderr).
    assert components["builder"] == {
        "version": "0.4.1",
        "image": "ghcr.io/saibotsivad/drover-builder:0.4.1",
    }
    assert components["webapp"]["version"] == "2.0.0"
    assert "digest" not in components["webapp"]
    assert components["executor"] == {"version": "0.3.1", "pypi": "drover-executor==0.3.1"}


def test_no_previous_all_containers_changed_full_build(repo, output_dir):
    """The realistic first-release: every container is bumped together."""
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        cli_assets=FIXTURES / "cli_assets.json",
    )
    manifest = _read(output_dir, "manifest.yaml")
    assert manifest["drover"] == "v2026.5-3"
    for project, sha in (
        ("orchestrator", SHA), ("builder", SHA2), ("webapp", SHA3),
    ):
        assert manifest["components"][project]["digest"] == sha
    assert manifest["components"]["executor"] == {
        "version": "0.3.1", "pypi": "drover-executor==0.3.1",
    }
    assert manifest["components"]["cli"]["version"] == "1.0.2"


def test_no_previous_partial_first_release_compose_render_fails(repo, output_dir):
    """If first release lacks digests for some containers, compose render fails."""
    with pytest.raises(build_manifest.BuildError, match="no digest"):
        _run_build(
            repo, output_dir,
            components=[f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"],
            cli_assets=FIXTURES / "cli_assets.json",
        )


def test_no_components_no_op_release_errors_on_changes_render(repo, output_dir):
    with pytest.raises(build_manifest.BuildError, match="no-op release"):
        _run_build(
            repo, output_dir,
            components=[],
            previous=FIXTURES / "previous_manifest.yaml",
        )


def test_carry_forward_when_only_one_container_changed(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA}"],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    manifest = _read(output_dir, "manifest.yaml")
    # webapp updated.
    assert manifest["components"]["webapp"]["version"] == "2.0.0"
    assert manifest["components"]["webapp"]["digest"] == SHA
    # orchestrator/builder/executor/cli all carry forward.
    assert manifest["components"]["orchestrator"]["version"] == "1.2.3"
    assert manifest["components"]["builder"]["version"] == "0.4.0"
    assert manifest["components"]["executor"]["version"] == "0.3.0"
    assert manifest["components"]["cli"]["version"] == "1.0.1"


def test_cli_changed_containers_unchanged(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[],
        previous=FIXTURES / "previous_manifest.yaml",
        cli_assets=FIXTURES / "cli_assets.json",
    )
    manifest = _read(output_dir, "manifest.yaml")
    # CLI moved.
    assert manifest["components"]["cli"]["version"] == "1.0.2"
    # Containers carried forward.
    assert manifest["components"]["orchestrator"]["version"] == "1.2.3"


def test_deterministic_yaml_key_order(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    raw = (output_dir / "manifest.yaml").read_text()
    # drover key first, then released, then components.
    assert raw.index("drover:") < raw.index("released:") < raw.index("components:")


# --------------------------------------------------------------------------- #
# changes.yml assembly
# --------------------------------------------------------------------------- #


def test_changes_first_release_from_and_previous_drover_null(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        cli_assets=FIXTURES / "cli_assets.json",
    )
    changes = _read(output_dir, "changes.yml")
    assert changes["previous_drover"] is None
    assert changes["changes"]["orchestrator"]["from"] is None
    assert changes["changes"]["orchestrator"]["to"] == "1.2.4"


def test_changes_bump_rollup_picks_highest(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA}"],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    changes = _read(output_dir, "changes.yml")
    # webapp 2.0.0 changelog has one major + one minor; component bump = major.
    assert changes["changes"]["webapp"]["bump"] == "major"
    assert changes["changes"]["webapp"]["from"] == "1.9.0"
    assert changes["changes"]["webapp"]["to"] == "2.0.0"


def test_changes_only_changed_components_appear(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    changes = _read(output_dir, "changes.yml")
    assert set(changes["changes"]) == {"orchestrator"}


def test_changes_missing_changelog_entry_errors(repo, output_dir):
    with pytest.raises(build_manifest.BuildError, match="no entry for version"):
        _run_build(
            repo, output_dir,
            components=[f"orchestrator=9.9.9:ghcr.io/saibotsivad/drover@{SHA}"],
            previous=FIXTURES / "previous_manifest.yaml",
        )


def test_changes_and_manifest_agree_on_to_version(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA2}",
        ],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    manifest = _read(output_dir, "manifest.yaml")
    changes = _read(output_dir, "changes.yml")
    for project in changes["changes"]:
        if project == "cli":
            continue
        assert changes["changes"][project]["to"] == manifest["components"][project]["version"]


# --------------------------------------------------------------------------- #
# install.sh rendering
# --------------------------------------------------------------------------- #


def test_install_sh_bash_n_parses(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    result = subprocess.run(
        ["bash", "-n", str(output_dir / "install.sh")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_install_sh_and_manifest_agree(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        cli_assets=FIXTURES / "cli_assets.json",
    )
    manifest = _read(output_dir, "manifest.yaml")
    install_sh = (output_dir / "install.sh").read_text()
    cli = manifest["components"]["cli"]
    assert f"CLI_VERSION='{cli['version']}'" in install_sh
    for platform, entry in cli["assets"].items():
        var = platform.replace("-", "_")
        assert entry["url"] in install_sh, f"URL for {platform} missing"
        assert entry["sha256"] in install_sh, f"sha256 for {platform} missing"
        assert f"ASSET_{var}_URL=" in install_sh
        assert f"ASSET_{var}_SHA256=" in install_sh


def test_install_sh_shell_escapes_special_chars(tmp_path, repo, output_dir):
    """A malformed asset value must not be able to break out of its assignment."""
    bad_assets = {
        "version": '1.0.0"; rm -rf /; echo "',
        "release_url": "https://example.com/release",
        "assets": {
            platform: {
                "url": 'https://example.com/$(touch /tmp/pwned)',
                "sha256": "`whoami`",
            } for platform in build_manifest.PLATFORM_KEYS
        },
    }
    bad_assets_path = tmp_path / "bad_assets.json"
    bad_assets_path.write_text(json.dumps(bad_assets))

    _run_build(
        repo, output_dir,
        components=[],
        previous=FIXTURES / "previous_manifest.yaml",
        cli_assets=bad_assets_path,
    )
    install_sh = output_dir / "install.sh"
    result = subprocess.run(["bash", "-n", str(install_sh)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    # Verify the literal evil string is exposed (no breakout).
    cmd = (
        f"set -a; source {install_sh}; printf %s \"$CLI_VERSION\""
    )
    # We can't actually source it without running the whole installer, so
    # just check the assignment line is properly single-quoted.
    text = install_sh.read_text()
    assert '$(touch /tmp/pwned)' in text  # the string IS present...
    # ...but always inside a shlex-quoted assignment, never bare.
    for line in text.splitlines():
        if line.startswith("CLI_VERSION=") or line.startswith("ASSET_"):
            # Each such line must be a simple NAME='...' assignment (no
            # unquoted $ or ` or " etc.).
            name, _, value = line.partition("=")
            # shlex-quoted values are either single-quoted or simple words.
            assert value.startswith("'") and value.endswith("'"), \
                f"unsafe assignment line: {line!r}"


def test_install_sh_missing_marker_errors(tmp_path):
    bad_template = tmp_path / "bad.template"
    bad_template.write_text("#!/usr/bin/env bash\n# no marker here\n")
    with pytest.raises(build_manifest.BuildError, match="exactly one"):
        build_manifest.render_install_sh(
            bad_template, "v2026.5-3", "2026-05-23T18:00:00Z",
            {"version": "1.0.0", "assets": {
                p: {"url": "u", "sha256": "s"} for p in build_manifest.PLATFORM_KEYS
            }},
        )


def test_install_sh_duplicate_marker_errors(tmp_path):
    bad_template = tmp_path / "bad.template"
    bad_template.write_text(
        "#!/usr/bin/env bash\n# --- VALUES BLOCK ---\n# --- VALUES BLOCK ---\n"
    )
    with pytest.raises(build_manifest.BuildError, match="exactly one"):
        build_manifest.render_install_sh(
            bad_template, "v2026.5-3", "2026-05-23T18:00:00Z",
            {"version": "1.0.0", "assets": {
                p: {"url": "u", "sha256": "s"} for p in build_manifest.PLATFORM_KEYS
            }},
        )


# --------------------------------------------------------------------------- #
# docker-compose.yml rendering
# --------------------------------------------------------------------------- #


def test_compose_no_latest_remains(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    compose = (output_dir / "docker-compose.yml").read_text()
    for line in compose.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert ":latest" not in line, f"residual :latest in line: {line!r}"


def test_compose_agrees_with_manifest_digests(repo, output_dir):
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    compose = (output_dir / "docker-compose.yml").read_text()
    manifest = _read(output_dir, "manifest.yaml")
    for project in ("orchestrator", "builder", "webapp"):
        entry = manifest["components"][project]
        pinned = f"{entry['image']}@{entry['digest']}"
        assert pinned in compose, f"compose missing pinned ref for {project}: {pinned}"


def test_compose_preserves_comment_block(repo, output_dir):
    """The leading documentation block must not be touched."""
    original = COMPOSE_SOURCE.read_text()
    head = "\n".join(original.splitlines()[:40])

    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    rendered = (output_dir / "docker-compose.yml").read_text()
    rendered_head = "\n".join(rendered.splitlines()[:40])
    assert rendered_head == head


def test_compose_missing_image_line_errors(repo, output_dir):
    """If the source compose drops a component's image: line, builder errors."""
    compose = repo / "docker-compose.yml"
    text = compose.read_text()
    # Remove the webapp service's image: line.
    text = "\n".join(
        line for line in text.splitlines()
        if not (line.strip().startswith("image:")
                and "drover-webapp" in line)
    )
    compose.write_text(text)
    with pytest.raises(build_manifest.BuildError, match="no image: line"):
        _run_build(
            repo, output_dir,
            components=[
                f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
                f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
                f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
            ],
            previous=FIXTURES / "previous_manifest.yaml",
        )


def test_compose_duplicate_image_line_errors(repo, output_dir):
    compose = repo / "docker-compose.yml"
    text = compose.read_text()
    # Duplicate the orchestrator's image: line.
    text = text.replace(
        "image: ghcr.io/saibotsivad/drover:latest",
        "image: ghcr.io/saibotsivad/drover:latest\n    image: ghcr.io/saibotsivad/drover:latest",
        1,
    )
    compose.write_text(text)
    with pytest.raises(build_manifest.BuildError, match="multiple image: lines"):
        _run_build(
            repo, output_dir,
            components=[
                f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
                f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
                f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
            ],
            previous=FIXTURES / "previous_manifest.yaml",
        )


def test_compose_smoke_test_docker_compose_config_parses(repo, output_dir):
    """Optional: if `docker compose` is available, validate the rendered file."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    _run_build(
        repo, output_dir,
        components=[
            f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}",
            f"builder=0.4.1:ghcr.io/saibotsivad/drover-builder@{SHA2}",
            f"webapp=2.0.0:ghcr.io/saibotsivad/drover-webapp@{SHA3}",
        ],
        previous=FIXTURES / "previous_manifest.yaml",
    )
    result = subprocess.run(
        ["docker", "compose", "-f", str(output_dir / "docker-compose.yml"), "config"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 and "compose" not in result.stderr.lower():
        pytest.skip(f"docker compose unavailable: {result.stderr}")
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# CLI assets validation
# --------------------------------------------------------------------------- #


def test_cli_assets_missing_platform_errors(tmp_path, repo, output_dir):
    bad = {
        "version": "1.0.2",
        "release_url": "https://example.com/x",
        "assets": {
            "linux-amd64": {"url": "u", "sha256": "s"},
        },
    }
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad))
    with pytest.raises(build_manifest.BuildError, match="missing platform"):
        _run_build(
            repo, output_dir,
            components=[f"orchestrator=1.2.4:ghcr.io/saibotsivad/drover@{SHA}"],
            previous=FIXTURES / "previous_manifest.yaml",
            cli_assets=bad_path,
        )
