#!/usr/bin/env python3
"""Assemble a Drover umbrella release.

Emits four files into the output directory:

    manifest.yaml       Cross-link manifest for the release.
    changes.yml         Aggregated change feed for downstream tooling.
    install.sh          CLI installer rendered from install.sh.template.
    docker-compose.yml  Operator compose with every image: pinned by digest.

All four are generated from the same component data in one pass, so
they cannot drift. See docs/releases.md for the contract.

Usage example (from CI):

    python3 scripts/build_manifest.py \\
        --drover-version v2026.5-3 \\
        --previous-manifest /tmp/prev.yaml \\
        --component orchestrator=1.2.4:ghcr.io/saibotsivad/drover@sha256:abc... \\
        --component builder=0.4.1:ghcr.io/saibotsivad/drover-builder@sha256:def... \\
        --cli-assets /tmp/cli-release-assets.json \\
        --compose-source docker-compose.yml \\
        --installer-template scripts/install.sh.template \\
        --output-dir release-staging/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import yaml


# --------------------------------------------------------------------------- #
# Canonical project list
# --------------------------------------------------------------------------- #
#
# Kept in sync with docs/versioning.md's "Versioned projects" table. When a
# new project lands there, add it here too.
#
# "kind" controls how a component's manifest entry is built and where its
# change feed comes from:
#   container  GHCR image, pinned by digest.
#   pypi       PyPI package (executor is tracked here but not yet published).
#   binary     The CLI: a GitHub Release of cross-compiled binaries. Its
#              manifest entry (binary URLs + SHA-256s) comes from the
#              cli-release-assets.json sidecar (see _build_cli_entry), but its
#              change feed is read from cli/CHANGELOG.yml like every other
#              project.

PROJECTS: dict[str, dict] = {
    "orchestrator": {
        "kind": "container",
        "image": "ghcr.io/saibotsivad/drover",
        "changelog": "orchestrator/CHANGELOG.yml",
    },
    "builder": {
        "kind": "container",
        "image": "ghcr.io/saibotsivad/drover-builder",
        "changelog": "builder/CHANGELOG.yml",
    },
    "webapp": {
        "kind": "container",
        "image": "ghcr.io/saibotsivad/drover-webapp",
        "changelog": "webapp/CHANGELOG.yml",
    },
    "executor": {
        "kind": "pypi",
        "pypi_name": "drover-executor",
        "changelog": "executor/CHANGELOG.yml",
    },
    "cli": {
        "kind": "binary",
        "changelog": "cli/CHANGELOG.yml",
    },
}


BUMP_RANK = {"major": 0, "minor": 1, "patch": 2}
COMPONENT_SPEC_RE = re.compile(
    r"^(?P<name>[a-z][a-z0-9_-]*)="
    r"(?P<version>[^:]+):"
    r"(?P<image>[^@]+)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)
VALUES_MARKER = "# --- VALUES BLOCK ---"
PLATFORM_KEYS = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
    "windows-amd64",
)


class BuildError(Exception):
    """Surfaced to the CLI; printed without a traceback."""


# --------------------------------------------------------------------------- #
# YAML dumping
# --------------------------------------------------------------------------- #

def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _ManifestDumper(yaml.SafeDumper):
    pass


_ManifestDumper.add_representer(str, _str_representer)


def _dump_yaml(data) -> str:
    return yaml.dump(
        data,
        Dumper=_ManifestDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10_000,
    )


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def parse_component_spec(spec: str) -> dict:
    match = COMPONENT_SPEC_RE.match(spec)
    if not match:
        raise BuildError(
            f"--component {spec!r} is malformed; expected "
            "<name>=<version>:<image>@sha256:<64-hex-digest>"
        )
    return match.groupdict()


def load_yaml_file(path: Path):
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as exc:
        raise BuildError(f"required file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise BuildError(f"failed to parse YAML at {path}: {exc}") from exc


def load_cli_assets(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise BuildError(f"--cli-assets file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"--cli-assets {path}: invalid JSON: {exc}") from exc

    for field in ("version", "release_url", "assets"):
        if field not in raw:
            raise BuildError(f"--cli-assets {path}: missing field {field!r}")

    assets = raw["assets"]
    if not isinstance(assets, dict):
        raise BuildError(f"--cli-assets {path}: 'assets' must be a mapping")
    for platform in PLATFORM_KEYS:
        if platform not in assets:
            raise BuildError(
                f"--cli-assets {path}: 'assets' is missing platform {platform!r}"
            )
        entry = assets[platform]
        if not isinstance(entry, dict):
            raise BuildError(
                f"--cli-assets {path}: assets[{platform!r}] must be a mapping"
            )
        for field in ("url", "sha256"):
            if field not in entry:
                raise BuildError(
                    f"--cli-assets {path}: assets[{platform!r}] missing {field!r}"
                )

    return raw


# --------------------------------------------------------------------------- #
# Manifest assembly
# --------------------------------------------------------------------------- #

def _changelog_published(repo_root: Path, project: str) -> str | None:
    rel = PROJECTS[project]["changelog"]
    path = repo_root / rel
    if not path.is_file():
        return None
    data = load_yaml_file(path)
    return data.get("published")


def _component_entry_container(project: str, version: str, image: str, digest: str) -> dict:
    return {
        "version": version,
        "image": f"{image}:{version}",
        "digest": digest,
    }


def _component_entry_pypi(project: str, version: str) -> dict:
    spec = PROJECTS[project].get("pypi_name")
    entry: dict = {"version": version}
    if spec:
        entry["pypi"] = f"{spec}=={version}"
    return entry


def assemble_components(
    repo_root: Path,
    changed: dict[str, dict],
    previous: dict,
    cli_assets: dict | None,
) -> dict[str, dict]:
    prev_components: dict = (previous or {}).get("components") or {}
    components: dict[str, dict] = {}

    for project, meta in PROJECTS.items():
        kind = meta["kind"]

        # The CLI ships as a GitHub Release of cross-compiled binaries: its
        # manifest entry comes from the cli-release-assets.json sidecar, or is
        # carried forward when the CLI didn't move this release.
        if kind == "binary":
            entry = _build_cli_entry(prev_components, cli_assets, changed)
            if entry is not None:
                components[project] = entry
            continue

        if project in changed:
            spec = changed[project]
            if kind == "container":
                components[project] = _component_entry_container(
                    project, spec["version"], meta["image"], spec["digest"],
                )
            elif kind == "pypi":
                components[project] = _component_entry_pypi(project, spec["version"])
            else:
                raise BuildError(
                    f"--component {project!r} provided but project kind is {kind!r}; "
                    "not implemented"
                )
            continue

        if project in prev_components:
            components[project] = dict(prev_components[project])
            continue

        published = _changelog_published(repo_root, project)
        if published is None:
            raise BuildError(
                f"project {project!r}: no --component arg, no previous manifest "
                f"entry, and no CHANGELOG.yml at {meta['changelog']}"
            )

        if kind == "container":
            entry: dict = {
                "version": published,
                "image": f"{meta['image']}:{published}",
            }
            print(
                f"warning: project {project!r} carried in with no digest "
                "(first release ever, component not changed in this release). "
                "Manifest entry is missing 'digest' until a future release "
                "re-publishes this component.",
                file=sys.stderr,
            )
            components[project] = entry
        elif kind == "pypi":
            components[project] = _component_entry_pypi(project, published)
        else:
            raise BuildError(f"project {project!r}: unknown kind {kind!r}")

    return components


def _build_cli_entry(
    prev_components: dict,
    cli_assets: dict | None,
    changed: dict[str, dict],
) -> dict | None:
    if cli_assets is not None:
        assets = {}
        for platform in PLATFORM_KEYS:
            entry = cli_assets["assets"][platform]
            assets[platform] = {"url": entry["url"], "sha256": entry["sha256"]}
        return {
            "version": cli_assets["version"],
            "release": cli_assets["release_url"],
            "assets": assets,
        }

    if "cli" in prev_components:
        return dict(prev_components["cli"])

    return None


# --------------------------------------------------------------------------- #
# Change feed
# --------------------------------------------------------------------------- #

def _highest_bump(levels: Iterable[str]) -> str:
    levels = list(levels)
    if not levels:
        raise BuildError("internal error: _highest_bump called with empty list")
    return min(levels, key=lambda lvl: BUMP_RANK[lvl])


def _find_changelog_entry(repo_root: Path, project: str, version: str) -> list[dict]:
    meta = PROJECTS.get(project)
    if meta is None:
        raise BuildError(
            f"project {project!r} not in canonical list; cannot resolve "
            "CHANGELOG.yml for changes.yml"
        )
    data = load_yaml_file(repo_root / meta["changelog"])
    for entry in (data.get("changes") or []):
        if str(entry.get("version")) == str(version):
            return [
                {"bump": item["bump"], "description": item["description"]}
                for item in (entry.get("entries") or [])
            ]
    raise BuildError(
        f"project {project!r}: CHANGELOG.yml has no entry for version "
        f"{version!r}; was the release PR constructed correctly?"
    )


def assemble_changes(
    repo_root: Path,
    changed: dict[str, dict],
    previous: dict,
    drover_version: str,
    previous_drover: str | None,
    released_at: str,
) -> dict:
    if not changed:
        raise BuildError(
            "refusing to build changes.yml: no --component arguments supplied "
            "(no-op release)"
        )

    prev_components: dict = (previous or {}).get("components") or {}
    changes: dict = {}

    for project in PROJECTS:
        if project not in changed:
            continue
        spec = changed[project]
        entries = _find_changelog_entry(repo_root, project, spec["version"])
        if not entries:
            raise BuildError(
                f"project {project!r}: CHANGELOG.yml entry for version "
                f"{spec['version']!r} has no entries"
            )
        from_version = (prev_components.get(project) or {}).get("version")
        changes[project] = {
            "from": from_version,
            "to": spec["version"],
            "bump": _highest_bump(item["bump"] for item in entries),
            "entries": entries,
        }

    return {
        "drover": drover_version,
        "previous_drover": previous_drover,
        "released": released_at,
        "changes": changes,
    }


# --------------------------------------------------------------------------- #
# install.sh rendering
# --------------------------------------------------------------------------- #


def _bash_squote(value: str) -> str:
    """Always single-quote a value for bash assignment, escaping inner '."""
    return "'" + value.replace("'", "'\"'\"'") + "'"

def render_install_sh(
    template_path: Path,
    drover_version: str,
    released_at: str,
    cli_entry: dict | None,
) -> str:
    template = template_path.read_text()
    marker_count = template.count(VALUES_MARKER)
    if marker_count != 1:
        raise BuildError(
            f"install.sh template at {template_path} must contain exactly one "
            f"{VALUES_MARKER!r} marker, found {marker_count}"
        )

    if cli_entry is None:
        raise BuildError(
            "cannot render install.sh: no CLI component data (need --cli-assets "
            "or a previous manifest carrying a cli block)"
        )

    lines: list[str] = [
        f"# Generated for Drover {drover_version} at {released_at}",
        "",
        f"CLI_VERSION={_bash_squote(cli_entry['version'])}",
    ]
    for platform in PLATFORM_KEYS:
        asset = cli_entry["assets"].get(platform)
        if asset is None:
            raise BuildError(
                f"CLI entry missing assets for platform {platform!r}"
            )
        var_platform = platform.replace("-", "_")
        lines.append(
            f"ASSET_{var_platform}_URL={_bash_squote(asset['url'])}"
        )
        lines.append(
            f"ASSET_{var_platform}_SHA256={_bash_squote(asset['sha256'])}"
        )

    values_block = "\n".join(lines)
    rendered = template.replace(VALUES_MARKER, values_block)
    return rendered


# --------------------------------------------------------------------------- #
# Pinned docker-compose.yml rendering
# --------------------------------------------------------------------------- #

def render_pinned_compose(
    source_path: Path,
    components: dict[str, dict],
) -> str:
    text = source_path.read_text()

    container_projects = [
        (name, meta) for name, meta in PROJECTS.items()
        if meta["kind"] == "container"
    ]

    for project, meta in container_projects:
        entry = components.get(project)
        if entry is None:
            raise BuildError(
                f"compose render: component {project!r} missing from assembled "
                "manifest; cannot pin its image line"
            )
        if "digest" not in entry:
            raise BuildError(
                f"compose render: component {project!r} has no digest in the "
                "assembled manifest; cannot pin its image line"
            )
        image_base = re.escape(meta["image"])
        pattern = re.compile(
            rf"^(?P<prefix>[ \t]*image:[ \t]*){image_base}(?::[^\s@]*)?[ \t]*$",
            re.MULTILINE,
        )
        matches = pattern.findall(text)
        if len(matches) == 0:
            raise BuildError(
                f"compose render: no image: line found for component {project!r} "
                f"(expected base {meta['image']!r})"
            )
        if len(matches) > 1:
            raise BuildError(
                f"compose render: multiple image: lines found for component "
                f"{project!r} (expected exactly one)"
            )
        replacement = (
            rf"\g<prefix>{meta['image']}:{entry['version']}@{entry['digest']}"
        )
        text = pattern.sub(replacement, text)

    if ":latest" in text:
        offending = [
            line for line in text.splitlines()
            if ":latest" in line and not line.lstrip().startswith("#")
        ]
        if offending:
            raise BuildError(
                "compose render: rewritten output still contains :latest on "
                f"non-comment lines: {offending}"
            )

    return text


# --------------------------------------------------------------------------- #
# Top-level build
# --------------------------------------------------------------------------- #

def build(
    drover_version: str,
    repo_root: Path,
    previous_manifest_path: Path | None,
    components_specs: list[str],
    cli_assets_path: Path | None,
    released_at: str,
    compose_source: Path,
    installer_template: Path,
    output_dir: Path,
) -> None:
    if previous_manifest_path is not None and previous_manifest_path.is_file():
        previous = load_yaml_file(previous_manifest_path)
    else:
        previous = {}

    changed: dict[str, dict] = {}
    for spec in components_specs:
        parsed = parse_component_spec(spec)
        changed[parsed["name"]] = parsed

    cli_assets = load_cli_assets(cli_assets_path) if cli_assets_path else None
    if cli_assets is not None:
        # Marks the CLI as "changed this release" so its change feed is read
        # from cli/CHANGELOG.yml; the manifest entry itself comes from cli_assets.
        changed["cli"] = {"version": cli_assets["version"]}

    components = assemble_components(repo_root, changed, previous, cli_assets)

    manifest = {
        "drover": drover_version,
        "released": released_at,
        "components": components,
    }

    changes = assemble_changes(
        repo_root,
        changed,
        previous,
        drover_version,
        (previous or {}).get("drover"),
        released_at,
    )

    cli_entry = components.get("cli")
    install_sh = render_install_sh(
        installer_template, drover_version, released_at, cli_entry,
    ) if cli_entry else None

    compose = render_pinned_compose(compose_source, components)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.yaml").write_text(_dump_yaml(manifest))
    (output_dir / "changes.yml").write_text(_dump_yaml(changes))
    (output_dir / "docker-compose.yml").write_text(compose)
    if install_sh is not None:
        (output_dir / "install.sh").write_text(install_sh)
        (output_dir / "install.sh").chmod(0o755)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drover-version", required=True,
                        help="CalVer string, e.g. v2026.5-3.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="Repository root (default: cwd).")
    parser.add_argument("--previous-manifest", type=Path, default=None,
                        help="Path to the previous release's manifest.yaml. "
                             "Absent on the very first umbrella release.")
    parser.add_argument("--component", dest="components", action="append",
                        default=[],
                        help="Repeatable. Format: "
                             "<name>=<version>:<image>@sha256:<digest>")
    parser.add_argument("--cli-assets", type=Path, default=None,
                        help="Path to cli-release-assets.json from the CLI "
                             "release job.")
    parser.add_argument("--released-at", default=None,
                        help="ISO 8601 UTC timestamp (default: now).")
    parser.add_argument("--compose-source", type=Path,
                        default=Path("docker-compose.yml"),
                        help="Path to the repo-root docker-compose.yml.")
    parser.add_argument("--installer-template", type=Path,
                        default=Path("scripts/install.sh.template"),
                        help="Path to install.sh.template.")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Directory to write release assets into.")
    args = parser.parse_args(argv)

    released_at = args.released_at or (
        dt.datetime.now(dt.timezone.utc)
          .replace(microsecond=0)
          .isoformat()
          .replace("+00:00", "Z")
    )

    try:
        build(
            drover_version=args.drover_version,
            repo_root=args.repo_root.resolve(),
            previous_manifest_path=args.previous_manifest,
            components_specs=args.components,
            cli_assets_path=args.cli_assets,
            released_at=released_at,
            compose_source=args.compose_source,
            installer_template=args.installer_template,
            output_dir=args.output_dir,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
