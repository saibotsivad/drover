# ADR: Container Templates for Environment Reuse

**Date:** 2026-04-16
**Status:** Proposed

## Context

Drover's current workflow requires pre-built images (with the `drover/` prefix) to launch micro-containers. This creates friction for users who want to customize their execution environment: they must build a Docker image from primitives with the privileged container, and then reference it via the API.

A common use case is iterative environment setup: install dependencies, configure tools, verify the setup works, then reuse that configured state for subsequent work. Docker supports this via `docker commit`, which creates a new image from a container's filesystem state. We want to expose this capability through Drover's API as a **template** system.

Key constraints and requirements:

1. **Predictable entry point**: For the orchestrator to reliably start containers, the guest agent must be at a known location.

2. **Single-level inheritance**: Templates should be simple. Once a container is converted to a template, that template cannot itself be templatized. This prevents deep inheritance chains that are hard to reason about and debug.

3. **Explicit workflow**: The user drives the process: start a container, execute setup commands, then explicitly "freeze" the state as a template.

4. **Safe container launch**: Because we control the agent location, we can reliably start containers from templates without worrying about lost `CMD` or `ENTRYPOINT` metadata.

## Decision

We will implement a **single-level container template** system with the following design:

### 1. Standardized Agent Location

All Drover micro-container images (both base images and templates) **must** provide an executable at `/usr/local/bin/drover`. This executable will be invoked as the container's main process (`Cmd` in Docker API terms).

The reference implementation will be the [`drover_executor`](../../executor/README.md) Python package from this repository, but custom implementations are permitted as long as they conform to the socket protocol.

### 2. Template Creation Workflow

A template is created from an existing micro-container via a new `create-template` API endpoint:

```
POST /containers/{id}/template
{
  "name": "my-python-env"
}
```

This operation:

1. Verifies the container is stopped (filesystem state is stable)
2. Creates a new image via `docker commit` named `drover/template-{name}-{hash}`
3. Records the template in the database
4. The original container remains unchanged

### 3. Single-Level Constraint

Templates **cannot** be created from templates. The API will reject `create-template` requests where the container was started from a template image.

This keeps the mental model simple: base images -> templates -> working containers, with no deeper nesting allowed.

### 4. Starting Containers from Templates

Containers can be started from templates using a new field in the create request:

```
POST /containers
{
  "template": "my-python-env",  // instead of "image"
  "env": { "KEY": "value" },
  "timeout_seconds": 300
}
```

The orchestrator will:

1. Resolve the template name to an image
2. Mount the new container's socket at `/run/orchestrator.sock`
3. Launch the container with `/usr/local/bin/drover` as the command

### 5. No Template-of-Template

Template images will be stored with a distinct prefix (`drover/template-*` vs `drover/*` for base images). The template creation endpoint will reject containers started from images with this prefix.

## Reasoning

### Why `/usr/local/bin/drover` as the hardcoded path?

Docker's `docker export`/`docker import` workflow flattens an image to a single layer and **discards all metadata** including `CMD` and `ENTRYPOINT`. By standardizing on a known path, we sidestep this issue entirely:

- The orchestrator always sets `Cmd: ["/usr/local/bin/drover"]` when creating containers
- Template images don't need to preserve metadata—they just need the executable at that path
- The executor can be baked into base images or installed via setup commands before templating

This trades flexibility (can't use arbitrary images) for reliability (containers always start correctly).

### Why single-level templates?

Multi-level inheritance (template of template of template) creates complexity:

- **Debugging difficulty**: A failure in a grandchild container requires understanding the entire inheritance chain
- **Cache invalidation**: Changing a base template should propagate, but detecting and managing that is complex
- **Storage bloat**: Each commit creates a new image; deep chains multiply storage
- **Conceptual overhead**: Users must track the lineage

Single-level templates provide 80% of the value (customized, reusable environments) with 20% of the complexity. Users who need more sophisticated layering can use external Docker build pipelines and import the result as a base image.

### Why `docker commit` instead of `docker export`/`import`?

Both approaches work, but `docker commit` is preferable because:

1. **Preserves layer history**: The committed image retains the base image layers, making future pulls/pushes more efficient
2. **Faster**: No need to export to tar and re-import
3. **Metadata preservation**: While we don't rely on `CMD`, preserving other metadata (environment variables set in the Dockerfile) is useful

The only scenario where `export`/`import` makes sense is for creating a completely flattened, minimal image—and that's an optimization we can add later if needed.

### Why require stopped containers for templating?

Filesystem consistency. A running container may have:

- Open files being written
- Temporary state that shouldn't persist
- Processes that modify state unpredictably

Stopping the container ensures the filesystem is in a known, quiescent state before we snapshot it. This matches Docker's recommendation and prevents "works on my machine" template issues.

## Consequences

### For Image Builders

- All `drover/*` base images must install the executor at `/usr/local/bin/drover`
- The executor must be the default entry point for template-based workflows
- The builder Dockerfile in this repo should be updated to reflect this requirement

### For API Users

- New workflow: create container → exec setup commands → stop → create template → launch from template
- Template names are user-provided and scoped (likely per-API-key or global)
- Templates appear in `GET /images` listings with their `drover/template-*` prefix

### Open Topics to Settle

**API Syntax for Template-Based Containers**

The current proposal uses `"template": "name"` instead of `"image": "name"` in the create request. Alternatives:

- `"image": "template:my-python-env"` (prefix convention)
- `"image": "my-python-env", "image_type": "template"` (explicit type field)
- Keep `"image"` but allow template names to resolve before base image names

**Database Schema for Templates**

We need to track:

- Template name, creation time, source container ID
- The underlying Docker image name (`drover/template-{name}-{hash}`)
- Whether a container was started from a template (for the single-level constraint)

Options:

1. Extend `containers` table with `is_template`, `template_source_id` columns
2. New `templates` table with foreign key to source container
3. Treat templates as a special image category in the existing images logic

**Template Naming and Collision**

- Should template names be unique globally, per-API-key, or per-user?
- How to handle hash collisions (two different containers producing same filesystem hash)?
- Should we support template versioning (`my-env:v1`, `my-env:v2`)?

**Template Lifecycle**

- Can templates be deleted? What happens to running containers started from them?
- Should templates have TTLs or size quotas?
- How to garbage collect orphaned template images?

**Error Handling**

- What if the source container has no filesystem changes (pristine base image)?
- What if `/usr/local/bin/drover` is missing from the committed image?
- How to communicate build/commit failures to the caller?

## Related Decisions

- [2026-04-11: WebSockets for streaming](2026-04-11-websockets-for-streaming.md) — Templates will use the same streaming infrastructure for setup commands
- Image naming convention (`drover/` prefix) — Templates extend this with `drover/template-*`
