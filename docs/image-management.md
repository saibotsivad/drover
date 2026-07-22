# Image Management

How Drover discovers, labels, and builds the images it launches
micro-containers from. Read this when you want to make an image available
to the orchestrator, build a new image, or understand the label contract.

Drover does not launch arbitrary images. Only images that carry the
required Drover labels on the host Docker daemon are visible to the
orchestrator and can be launched through the API.

---

## Label contract

Workload images are identified by Docker labels baked in at build time:

| Label | Value | Required |
|---|---|---|
| `drover.managed` | `"true"` | yes |
| `drover.name` | short name used to launch containers (e.g. `"python-runner"`) | yes |
| `drover.capabilities` | comma-separated list of capability keys the image supports (e.g. `"exec"`) | no |

An image without **both** required labels is invisible to Drover. The
`drover.*` namespace is reserved for future Drover-specific metadata
(templates, versions, etc.).

The optional `drover.capabilities` label advertises which
capability-gated features a container supports; the orchestrator rejects
requests for an undeclared capability and the webapp hides the
corresponding controls. See [docs/capabilities.md](capabilities.md) for the
authoritative reference.

Because labels are baked into the image and survive re-tagging, the same
image can be pulled from any registry (e.g.
`ghcr.io/saibotsivad/drover-builder:latest`) and the orchestrator will
still recognise it by label. List and validation operations use
`docker image ls --filter label=drover.managed=true`.

The **privileged image** is the one exception: it is operator-supplied,
named by the `PRIVILEGED_IMAGE` env var, and is not managed through the
image or container API. It does not need the `drover.managed` label.

---

## Building an image

Because a privileged micro-container has access to the host Docker socket
and shares the same lifecycle as any other container, image building is
just another container workload that the Drover operator manages (the
reference [builder](../builder/README.md) image exists for exactly this).

The only constraint is that the resulting image must carry the required
labels.

### In a `Dockerfile`

```dockerfile
LABEL drover.managed="true"
LABEL drover.name="my-image"
```

### In a `docker-compose.yml` build

Apply the labels at build time via the `labels` key on the build step:

```yaml
services:
  my-image:
    build:
      context: ./my-image
      labels:
        drover.managed: "true"
        drover.name: "my-image"
```

These are build-time labels on the image itself, not runtime labels on a
service container — keep them under `build.labels`, not the top-level
`labels` field.

### Labeling a pre-built upstream image

To label an upstream image that doesn't already carry the Drover labels,
use `dockerfile_inline` to derive a thin image that just adds them:

```yaml
services:
  builder:
    image: my-org/drover-builder:latest
    build:
      context: .
      dockerfile_inline: |
        FROM ghcr.io/saibotsivad/drover-builder:latest
        LABEL drover.managed="true"
        LABEL drover.name="builder"
```

Compose has no way to attach labels to an image it merely pulls — it can
only add labels to images it builds — so the inline `FROM` is what makes
the new labels stick. If the upstream image already carries the Drover
labels (anything published from this repo does), skip the `build:` block
and just `image:` it directly; labels are baked into the image and travel
with it.

---

## Image API

| Method | Path | Description |
|---|---|---|
| `GET` | `/images` | List all Drover-managed images (those carrying `drover.managed=true`) |
| `GET` | `/images/{name}` | Get status and metadata for the image whose `drover.name` matches `{name}` |

For the full response shapes (the `name`, `labels`, and `tags` fields) see
the [orchestrator API reference](../orchestrator/README.md#images). To drive
these endpoints from a terminal, use the `drover images` and
`drover image <name>` CLI commands (see [docs/cli.md](cli.md)).
