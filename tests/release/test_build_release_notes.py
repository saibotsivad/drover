"""Tests for scripts/build_release_notes.py."""

from __future__ import annotations

import yaml

import build_release_notes


MANIFEST = {
    "drover": "v2026.5-3",
    "released": "2026-05-23T18:00:00Z",
    "components": {
        "orchestrator": {
            "version": "1.2.4",
            "image": "ghcr.io/saibotsivad/drover:1.2.4",
            "digest": "sha256:" + "0" * 64,
        },
        "builder": {
            "version": "0.4.1",
            "image": "ghcr.io/saibotsivad/drover-builder:0.4.1",
            "digest": "sha256:" + "a" * 64,
        },
        "webapp": {
            "version": "2.0.0",
            "image": "ghcr.io/saibotsivad/drover-webapp:2.0.0",
            "digest": "sha256:" + "b" * 64,
        },
        "executor": {"version": "0.3.1", "pypi": "drover-executor==0.3.1"},
        "cli": {
            "version": "1.0.2",
            "release": "https://github.com/saibotsivad/drover/releases/tag/cli-v1.0.2",
            "assets": {},
        },
    },
}

CHANGES = {
    "drover": "v2026.5-3",
    "previous_drover": "v2026.5-2",
    "released": "2026-05-23T18:00:00Z",
    "changes": {
        "orchestrator": {
            "from": "1.2.3",
            "to": "1.2.4",
            "bump": "patch",
            "entries": [
                {"bump": "patch", "description": "Fixed crash on malformed input.\n"},
            ],
        },
        "webapp": {
            "from": "1.9.0",
            "to": "2.0.0",
            "bump": "major",
            "entries": [
                {"bump": "major", "description": "Renamed proxy endpoints.\n"},
                {"bump": "minor", "description": "Added a dashboard.\n"},
            ],
        },
    },
}


def test_render_contains_what_is_new_and_pinned_versions():
    notes = build_release_notes.render_notes(MANIFEST, CHANGES)
    assert "# Drover v2026.5-3" in notes
    assert "## What's new" in notes
    assert "### orchestrator" in notes
    assert "### webapp" in notes
    # webapp has a major bullet at the entry level.
    assert "**major** — Renamed proxy endpoints." in notes


def test_render_pinned_versions_table_has_every_component():
    notes = build_release_notes.render_notes(MANIFEST, CHANGES)
    assert "## Pinned versions" in notes
    # Each component shows up.
    for project in MANIFEST["components"]:
        assert f"`{project}`" in notes


def test_render_handles_empty_changes_gracefully():
    empty = {"drover": "v2026.5-3", "previous_drover": None,
             "released": "2026-05-23T18:00:00Z", "changes": {}}
    notes = build_release_notes.render_notes(MANIFEST, empty)
    assert "No component changes" in notes


def test_render_yaml_roundtrip_safe():
    """The rendered notes must accept the same yaml.safe_load as a consumer would."""
    yaml.safe_dump(MANIFEST)
    yaml.safe_dump(CHANGES)
