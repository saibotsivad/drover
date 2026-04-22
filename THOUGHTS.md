# Thoughts on drover.yml and On-Demand Container Building

## Summary of the Scenario

The goal is to enable repositories to define their execution environment via a `drover.yml` file (similar to a Dockerfile but simplified), and have Drover:
1. Parse the definition
2. Compute a content hash
3. Check if a matching container image already exists
4. If not, use a privileged container to build the image
5. Launch the appropriate micro-container for actual work

This is essentially a **build-on-demand with content-addressable caching** system layered on top of Drover's existing container orchestration.

---

## Key Patterns That Emerge

### Pattern 1: Separation of Concerns — Orchestrator vs. Builder

The current Drover orchestrator has a clean, focused responsibility: manage the lifecycle of micro-containers. It does not build images—it assumes they exist (with the `drover/` prefix).

The proposed feature is **orchestration of builds**, not just container lifecycle. This suggests:

> **The builder should be a separate layer**—either:
> - A new API module (`/builds` endpoints)
> - A separate service that calls the orchestrator
> - A client-side tool that coordinates multiple orchestrator calls

Keeping this out of the core orchestrator maintains the architectural boundary: the orchestrator manages running containers; the builder manages image creation workflows.

---

### Pattern 2: The `drover.yml` as a Declarative Environment Spec

A `drover.yml` might look like:

```yaml
# drover.yml
base: ubuntu:22.04
packages:
  - python3
  - python3-pip
  - git
run: |
  pip install numpy pandas
env:
  PYTHONPATH: /app
```

This is intentionally simpler than a Dockerfile. Key observations:

1. **Deterministic by design**: The same `drover.yml` should always produce the same environment (or as close as package managers allow)
2. **Hashable**: The entire file content (normalized) becomes the cache key
3. **Composable**: Future versions could support `extends: other-drover-image` for layer-like inheritance

The builder would need:
- A YAML parser with strict validation
- A normalizer (consistent key ordering, whitespace) for reliable hashing
- A generator that transforms this into actual Docker build steps

---

### Pattern 3: Content-Addressable Image Naming

The hash of `drover.yml` becomes the image identifier:

```
drover/build-a1b2c3d4e5f6...
```

Or perhaps with human-readable prefixes:

```
drover/build-myproject-a1b2c3d4...
```

This mirrors Docker's content-addressable layers but at a higher level of abstraction. The orchestrator's existing image listing (`GET /images`) already filters by `drover/` prefix, so these build images would appear naturally.

---

### Pattern 4: The Two-Phase Container Pattern

The workflow becomes:

```
Client wants to run work in environment E
        ↓
Compute hash(E) → check if drover/build-<hash> exists
        ↓
    ┌───┴───┐
 EXISTS     NOT EXISTS
    ↓           ↓
Use it    Launch privileged container
          with build tools + docker socket
                ↓
          Run build steps from drover.yml
                ↓
          Tag result as drover/build-<hash>
                ↓
          Destroy build container
                ↓
          Use newly created image
```

This is a **workflow composed of orchestrator primitives**:
1. `POST /containers` (privileged) — create build container
2. `POST /containers/{id}/exec` — run build commands
3. `DELETE /containers/{id}` — cleanup build container
4. `POST /containers` (standard) — create work container from new image

The builder layer coordinates these calls.

---

### Pattern 5: The Privileged Container as Build Environment

The existing `PRIVILEGED_IMAGE` mechanism is the perfect hook for this. A privileged container:
- Has Docker socket access (can build images)
- Runs without gVisor (no syscall interception overhead for I/O-heavy builds)
- Has the same lifecycle as any other container

The builder would need a standard "builder image" that contains:
- Docker CLI (or uses the socket directly)
- `drover.yml` parser/builder tool
- Package managers (apt, pip, npm, etc.)

This could be the same as `PRIVILEGED_IMAGE` or a specialized variant.

---

## API Design Options

### Option A: New `/builds` Endpoint (Recommended)

Add a new router for build orchestration:

```
POST /builds
{
  "drover_yaml": "base: ubuntu:22.04\npackages:\n  - python3...",
  "label": "myproject-ci"
}

→ Returns immediately with build ID:
{
  "build_id": "build-abc123",
  "status": "pending",  // or "cached" if image exists
  "image": "drover/build-myproject-a1b2c3d4..."
}

GET /builds/{build_id}
→ Poll for status: pending → building → complete | failed

// The build itself runs in a privileged container that:
// 1. Parses the YAML
// 2. Checks if image exists (hash match)
// 3. If not, executes build steps
// 4. Tags the result
```

**Pros:**
- Clean separation from core orchestrator
- Async by design (builds can take minutes)
- Can stream build logs via existing WebSocket mechanisms
- Build history persisted in database

**Cons:**
- New domain to model (Build vs Container)
- More complex state machine

---

### Option B: Workspace/Session API

A higher-level abstraction that combines environment setup + execution:

```
POST /workspaces
{
  "drover_yaml": "...",
  "repository": "https://github.com/org/repo",
  "commands": ["pytest", "python -m build"]
}

→ Creates environment (if needed), clones repo, runs commands
```

**Pros:**
- Very ergonomic for CI/CD use cases
- Single API call for common workflow

**Cons:**
- Blurs the line between orchestrator and workflow engine
- Less flexible (what if you want to run multiple commands over time?)
- Repository access credentials become the orchestrator's concern

---

### Option C: Client-Side Build Coordination

Don't add to orchestrator at all. Provide a client library/tool that:
1. Hashes local `drover.yml`
2. Checks if image exists via `GET /images/{hash}`
3. If not, creates privileged container, runs build, tags image
4. Then creates work container

**Pros:**
- Zero changes to orchestrator
- Client controls retry logic, caching strategy

**Cons:**
- Every client reimplements the same workflow
- No centralized build history/logs
- Long-running builds tie up client connection

---

## Open Questions & Considerations

### 1. Build Reproducibility

Package managers (`apt`, `pip`, `npm`) are not inherently reproducible—`apt install python3` today may differ from tomorrow. Options:
- Document this as a limitation (acceptable for many homelab use cases)
- Support lockfiles (`drover.lock.yml` with exact versions)
- Support base images that are themselves version-pinned

### 2. Build Caching Strategy

What invalidates a build?
- **Never**: Images accumulate forever (simple, may fill disk)
- **Time-based**: TTL on build images
- **LRU**: Evict oldest when disk threshold reached
- **Explicit**: API to purge specific builds

This could leverage the orchestrator's existing reaper pattern.

### 3. Build Secrets

Some builds need secrets (private package registries, Git credentials). The privileged container has access to host Docker socket—should it also have access to:
- Mounted secret files?
- Environment variables passed at build creation time?
- Integration with Docker BuildKit-style secrets?

### 4. Multi-Stage Builds

Should `drover.yml` support multi-stage builds like Dockerfiles?

```yaml
stages:
  build:
    base: node:18
    run: npm ci && npm run build
  runtime:
    base: nginx:alpine
    copy: { from: build, src: /dist, dest: /usr/share/nginx/html }
```

This adds complexity but enables smaller, more secure runtime images.

### 5. Buildkit vs. Legacy Docker Build

Modern Docker uses BuildKit for better caching and performance. The builder container needs to either:
- Use `docker buildx` commands
- Or output a Dockerfile and use standard `docker build`

### 6. Parallel Build Safety

If two clients request the same (uncached) build simultaneously:
- Both should wait for a single build (deduplication)
- Or both build independently (wasteful but simpler)

The `/builds` endpoint would need to track in-flight builds and coalesce requests.

---

## Recommended Architecture

Based on the patterns above, here's a proposed architecture:

```
┌─────────────────────────────────────────────────────────────┐
│                      Client / CI System                      │
│                         (calls API)                          │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                    Drover Builder API                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ /builds     │  │ /workspaces │  │ (future) /pipelines │  │
│  │ (env mgmt)  │  │ (env+exec)  │  │ (complex workflows) │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│                                                              │
│  - drover.yml parser/normalizer                              │
│  - content hash calculator                                   │
│  - build orchestration (manages privileged containers)       │
│  - build state persistence (SQLite: builds table)            │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                Drover Orchestrator (core)                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ /containers │  │ /images     │  │ /ws/*               │  │
│  │ (existing)  │  │ (existing)  │  │ (streaming)         │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│                                                              │
│  - Container lifecycle (unchanged)                           │
│  - Image listing (build images appear as drover/build-*)     │
│  - Privileged container support (for builds)                 │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│              Host Docker (rootless)                          │
│  - Standard micro-containers (gVisor)                        │
│  - Privileged builder containers (Docker socket access)      │
│  - Built images (drover/build-* namespace)                   │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **Builder is a layer on top**: The core orchestrator remains unchanged. The builder imports the orchestrator's client or makes HTTP calls to it.

2. **Builds are first-class resources**: With their own table in SQLite, their own state machine (pending → building → complete | failed), and their own API endpoints.

3. **Streaming builds**: Build output streams via the same WebSocket infrastructure used for command output.

4. **Image naming convention**: `drover/build-{project}-{hash}` where project is optional/user-provided for human identification.

5. **No breaking changes**: Existing `/containers` and `/images` endpoints work unchanged. Build images just appear in `/images` listings.

---

## Implementation Sketch

### Database Schema Addition

```sql
CREATE TABLE builds (
    id TEXT PRIMARY KEY,
    status TEXT CHECK(status IN ('pending', 'building', 'complete', 'failed')),
    drover_yaml_hash TEXT UNIQUE NOT NULL,  -- content hash
    drover_yaml_content TEXT NOT NULL,       -- for debugging/reproducibility
    image_name TEXT,                         -- drover/build-xxx
    container_id TEXT,                       -- build container (while building)
    label TEXT,
    created_at TEXT,
    completed_at TEXT,
    error_message TEXT
);
```

### Build State Machine

```
[Create] → pending
            ↓ (build container starts)
         building → [completes] → complete
                 → [fails] → failed
                 → [cancelled] → failed
```

### Build Process (inside privileged container)

```python
# Pseudo-code for the builder agent (subclass of drover_executor.Agent)

class BuildAgent(Agent):
    async def on_connect(self):
        # Parse drover.yml from environment or mounted file
        config = parse_drover_yaml(os.environ['DROVER_YAML'])
        
        # Check if image already exists
        image_name = f"drover/build-{config.hash}"
        if image_exists(image_name):
            self.send_result(image_name, cached=True)
            self.send_done()
            return
        
        # Generate Dockerfile from config
        dockerfile = generate_dockerfile(config)
        
        # Build image
        await build_image(dockerfile, image_name)
        
        # Report success
        self.send_result(image_name, cached=False)
        self.send_done()
```

---

## Summary

The scenario describes a **declarative, cached build system** that fits naturally on top of Drover's existing primitives. The key insight is that this should be a **separate layer**—not baked into the core orchestrator—composed of:

1. A YAML-based environment specification (`drover.yml`)
2. Content-addressable image naming (hash-based)
3. Build orchestration using privileged containers
4. First-class build resources with their own API and state

This maintains Drover's architectural clarity while enabling powerful higher-level workflows.
