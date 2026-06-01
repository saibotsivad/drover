# Drover Capabilities

The `drover.capabilities` image label advertises which capability-gated
features a worker launched from that image actually supports. It is the
single source of truth that both the orchestrator (enforcement) and the
webapp (UI gating) consult.

## Overview

Drover-managed images carry a small set of `drover.*` labels:

| Label | Meaning |
|---|---|
| `drover.managed` | Marks the image for discovery by the orchestrator |
| `drover.name` | Short name used to refer to the image (e.g. `builder`) |
| `drover.capabilities` | Comma-separated list of capability keys the image supports |

The value of `drover.capabilities` is a comma-separated list of capability
keys, for example:

```dockerfile
LABEL drover.capabilities="exec"
```

Whitespace around each key is ignored and duplicates collapse, so
`exec, exec ,foo` parses to the set `{exec, foo}`.

Enforcement happens in two places:

- **Orchestrator** — the authoritative security gate. It independently
  rejects any request that requires a capability the image has not declared,
  returning `422 Unprocessable Entity`. The UI cannot bypass this.
- **Webapp** — a convenience layer. It hides controls that require a
  capability the image does not declare, so operators don't submit requests
  that are guaranteed to fail.

## Absent or empty label

An **absent** label and an **empty** value (`drover.capabilities=""`) are
treated identically: the image declares **no capabilities**. Every
capability-gated feature is denied for that image. There is no implicit
allowlist — a capability must be explicitly named to be granted.

The same deny-if-unknown rule applies when an image can no longer be
resolved (deleted or renamed after a worker was launched from it): the
orchestrator denies the request rather than assuming the capability is
present, and the webapp hides the corresponding control.

## Capability reference

This is the authoritative list of capability keys. A new key must be added
here before it appears on any image label.

| Key | What it grants | Which images should declare it |
|---|---|---|
| `exec` | The worker responds to exec requests via the orchestrator socket. Commands submitted through `POST /workers/{id}/execs` (and the "Exec Commands" UI) are queued and executed by the worker agent (`drover-executor`). | All images that ship `drover-executor` as their `CMD`. |

## Adding a new capability

1. Add a row to the capability reference table above.
2. Add enforcement in the orchestrator (`orchestrator/worker_manager.py`)
   so requests that need the capability are rejected with `422` when it is
   absent.
3. Update the webapp so the relevant control is hidden when the image does
   not declare the capability.
4. Add the label to every Dockerfile whose image supports the capability.
