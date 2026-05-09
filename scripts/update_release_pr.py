#!/usr/bin/env python3
"""Consume change files in changes/ and update per-project CHANGELOG.yml files.

Invoked by the update-release-pr workflow. The workflow handles git
operations (branch, commit, push) and PR create/update via gh; this script
only mutates files on disk and emits a Markdown PR body to stdout (or to
--pr-body-out if given).

Exit codes:
    0  changes were processed and files updated (or --dry-run completed).
    2  no change files present — caller should skip the rest of the workflow.
    1  validation error in a change file or unexpected failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import yaml


BUMP_LEVELS = ("major", "minor", "patch")
BUMP_RANK = {level: i for i, level in enumerate(BUMP_LEVELS)}  # major=0 highest
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ChangeFileError(Exception):
    """Raised when a change file is malformed."""


# --------------------------------------------------------------------------- #
# YAML formatting helpers
# --------------------------------------------------------------------------- #

def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    """Render multi-line strings as block (|) scalars; everything else default."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _ChangelogDumper(yaml.SafeDumper):
    pass


_ChangelogDumper.add_representer(str, _str_representer)


def _dump_yaml(data) -> str:
    return yaml.dump(
        data,
        Dumper=_ChangelogDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10_000,  # don't auto-wrap descriptions
    )


# --------------------------------------------------------------------------- #
# Semver helpers
# --------------------------------------------------------------------------- #

def parse_semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(value.strip())
    if not match:
        raise ChangeFileError(f"not a valid semver: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def bump_semver(current: str, level: str) -> str:
    major, minor, patch = parse_semver(current)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ChangeFileError(f"unknown bump level: {level!r}")


def highest_bump(levels: Iterable[str]) -> str:
    levels = list(levels)
    if not levels:
        raise ChangeFileError("no bump levels supplied")
    return min(levels, key=lambda lvl: BUMP_RANK[lvl])


# --------------------------------------------------------------------------- #
# Change file loading
# --------------------------------------------------------------------------- #

def load_change_files(paths: Iterable[Path]) -> list[tuple[Path, list[dict]]]:
    """Load and validate the given change files.

    Raises ChangeFileError if any file is malformed.
    """
    out: list[tuple[Path, list[dict]]] = []
    for path in sorted(paths):
        if not path.is_file():
            raise ChangeFileError(f"{path}: file does not exist")

        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ChangeFileError(f"{path}: invalid YAML: {exc}") from exc

        if raw is None:
            raise ChangeFileError(f"{path}: file is empty")
        if not isinstance(raw, list):
            raise ChangeFileError(
                f"{path}: top-level must be a list of entries, got {type(raw).__name__}"
            )

        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ChangeFileError(
                    f"{path}: entry #{index + 1} must be a mapping, got {type(entry).__name__}"
                )
            for field in ("project", "bump", "description"):
                if field not in entry:
                    raise ChangeFileError(
                        f"{path}: entry #{index + 1} missing required field {field!r}"
                    )
            if entry["bump"] not in BUMP_LEVELS:
                raise ChangeFileError(
                    f"{path}: entry #{index + 1} has invalid bump "
                    f"{entry['bump']!r}; must be one of {BUMP_LEVELS}"
                )

        out.append((path, raw))
    return out


def discover_change_files(changes_dir: Path) -> list[Path]:
    """Return every YAML file in changes_dir, or an empty list if missing."""
    if not changes_dir.is_dir():
        return []
    return [
        path for path in changes_dir.iterdir()
        if path.is_file() and path.suffix in (".yaml", ".yml")
    ]


# --------------------------------------------------------------------------- #
# Grouping + version planning
# --------------------------------------------------------------------------- #

def group_by_project(
    files: list[tuple[Path, list[dict]]],
    repo_root: Path,
) -> dict[str, list[dict]]:
    """Bucket entries by project; verify each project has a CHANGELOG.yml."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for path, entries in files:
        for entry in entries:
            project = entry["project"]
            changelog = repo_root / project / "CHANGELOG.yml"
            if not changelog.is_file():
                raise ChangeFileError(
                    f"{path}: project {project!r} has no CHANGELOG.yml at {changelog}"
                )
            grouped[project].append(entry)
    return dict(grouped)


def plan_versions(
    grouped: dict[str, list[dict]],
    repo_root: Path,
) -> dict[str, dict]:
    """For each project, compute current/next version and aggregated bump."""
    plan: dict[str, dict] = {}
    for project, entries in grouped.items():
        changelog_path = repo_root / project / "CHANGELOG.yml"
        with changelog_path.open() as fh:
            current = yaml.safe_load(fh) or {}
        published = current.get("published", "0.0.0")
        bump = highest_bump(entry["bump"] for entry in entries)
        next_version = bump_semver(published, bump)
        plan[project] = {
            "current": published,
            "next": next_version,
            "bump": bump,
            "entries": entries,
            "changelog_path": changelog_path,
            "existing": current,
        }
    return plan


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def update_changelog(project_plan: dict, today: str) -> None:
    existing = project_plan["existing"] or {}
    history = existing.get("changes") or []

    new_entry = {
        "version": project_plan["next"],
        "date": today,
        "entries": [
            {"bump": entry["bump"], "description": entry["description"]}
            for entry in project_plan["entries"]
        ],
    }

    updated = {
        "published": project_plan["next"],
        "changes": [new_entry, *history],
    }

    project_plan["changelog_path"].write_text(_dump_yaml(updated))


def render_pr_body(plan: dict[str, dict]) -> str:
    sections = []
    for project in sorted(plan.keys()):
        info = plan[project]
        header = (
            f"## {project} — {info['current']} → {info['next']} ({info['bump']})"
        )
        bullets = []
        for entry in info["entries"]:
            description = entry["description"].strip()
            first_line, *rest = description.splitlines()
            bullets.append(f"- {first_line}")
            for line in rest:
                bullets.append(f"  {line}")
        sections.append(header + "\n\n" + "\n".join(bullets))
    return "\n\n".join(sections) + "\n"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: cwd).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions; don't modify or delete files.",
    )
    parser.add_argument(
        "--pr-body-out",
        type=Path,
        default=None,
        help="Write the rendered PR body Markdown to this path (in addition to stdout).",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override the date used for new changelog entries (YYYY-MM-DD). "
             "Defaults to today's UTC date.",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        type=Path,
        default=None,
        help="Operate on exactly these change files instead of scanning "
             "changes/. Implies --dry-run semantics for callers that want "
             "rendering only — pair with --dry-run to suppress writes.",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    changes_dir = repo_root / "changes"

    if args.files is not None:
        paths = [p.resolve() for p in args.files]
    else:
        paths = discover_change_files(changes_dir)

    try:
        files = load_change_files(paths)
    except ChangeFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Filter out the README/.gitkeep — load_change_files only globs *.yaml/*.yml,
    # but be explicit about treating an empty list as a no-op.
    payload_files = [(path, entries) for path, entries in files if entries]
    if not payload_files:
        print("no change files present; nothing to do", file=sys.stderr)
        return 2

    try:
        grouped = group_by_project(payload_files, repo_root)
        plan = plan_versions(grouped, repo_root)
    except ChangeFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    today = args.today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    pr_body = render_pr_body(plan)

    if args.dry_run:
        print("DRY RUN — no files will be modified", file=sys.stderr)
        for project, info in sorted(plan.items()):
            print(
                f"  {project}: {info['current']} -> {info['next']} ({info['bump']}, "
                f"{len(info['entries'])} entries)",
                file=sys.stderr,
            )
        print("  would delete change files:", file=sys.stderr)
        for path, _ in payload_files:
            print(f"    {path.relative_to(repo_root)}", file=sys.stderr)
        print("\n--- PR body ---\n", file=sys.stderr)
        print(pr_body)
        return 0

    for project_plan in plan.values():
        update_changelog(project_plan, today)

    for path, _ in payload_files:
        path.unlink()

    if args.pr_body_out is not None:
        args.pr_body_out.write_text(pr_body)

    print(pr_body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
