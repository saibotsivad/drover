#!/usr/bin/env python3
"""Render the GitHub Release body for a Drover umbrella release.

Reads manifest.yaml + changes.yml produced by build_manifest.py and emits a
Markdown document on stdout (or to --output). The two YAML files are the
only inputs — keeping this script pure makes it trivial to snapshot-test.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


SECTION_ORDER = ("orchestrator", "builder", "webapp", "executor", "cli")
COSIGN_HINT = (
    "cosign verify-blob <asset> --signature <asset>.sig "
    "--certificate-identity-regexp '...' --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'"
)


def _ordered(items):
    seen = set()
    out = []
    for name in SECTION_ORDER:
        if name in items:
            out.append(name)
            seen.add(name)
    for name in items:
        if name not in seen:
            out.append(name)
    return out


def _render_changes(changes_doc: dict) -> str:
    changes = changes_doc.get("changes") or {}
    if not changes:
        return "_No component changes in this release._"

    sections: list[str] = []
    for project in _ordered(changes):
        info = changes[project]
        from_v = info.get("from")
        to_v = info.get("to")
        bump = info.get("bump", "patch")
        title_arrow = f"{from_v or '∅'} → {to_v} ({bump})"
        lines = [f"### {project} — {title_arrow}"]
        for entry in info.get("entries") or []:
            entry_bump = entry.get("bump", "patch")
            description = (entry.get("description") or "").strip()
            first_line, *rest = description.splitlines() or [""]
            lines.append(f"- **{entry_bump}** — {first_line}")
            for tail in rest:
                lines.append(f"  {tail}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_pinned_versions(manifest: dict) -> str:
    components = manifest.get("components") or {}
    rows = ["| Component | Version | Published at |", "|---|---|---|"]
    for project in _ordered(components):
        entry = components[project]
        version = entry.get("version", "?")
        if "image" in entry and "digest" in entry:
            published = f"`{entry['image']}@{entry['digest']}`"
        elif "image" in entry:
            published = f"`{entry['image']}` (digest pending)"
        elif "pypi" in entry:
            published = f"`{entry['pypi']}`"
        elif "release" in entry:
            published = f"[per-component release]({entry['release']})"
        else:
            published = "_not yet published_"
        rows.append(f"| `{project}` | `{version}` | {published} |")
    return "\n".join(rows)


def _render_verification(manifest: dict) -> str:
    lines = [
        "All release assets are cosign-signed (keyless OIDC). Each asset has "
        "a sibling `<asset>.sig`. Verify with:",
        "",
        "```sh",
        COSIGN_HINT,
        "```",
        "",
        "Assets:",
        "",
        "- `manifest.yaml` / `manifest.yaml.sig`",
        "- `changes.yml` / `changes.yml.sig`",
        "- `docker-compose.yml` / `docker-compose.yml.sig`",
        "- `install.sh` / `install.sh.sig`",
        "- `checksums.txt`",
    ]
    return "\n".join(lines)


def render_notes(manifest: dict, changes_doc: dict) -> str:
    drover_version = manifest.get("drover", "?")
    released = manifest.get("released", "?")

    parts = [
        f"# Drover {drover_version}",
        "",
        f"_Released {released}_",
        "",
        "## What's new",
        "",
        _render_changes(changes_doc),
        "",
        "## Pinned versions",
        "",
        _render_pinned_versions(manifest),
        "",
        "## Verification",
        "",
        _render_verification(manifest),
        "",
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--changes", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    manifest = yaml.safe_load(args.manifest.read_text()) or {}
    changes_doc = yaml.safe_load(args.changes.read_text()) or {}

    body = render_notes(manifest, changes_doc)

    if args.output is None:
        sys.stdout.write(body)
    else:
        args.output.write_text(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
