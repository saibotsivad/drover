BINARY  := drover
PKG     := github.com/saibotsivad/drover/cli
CMD     := ./cmd/drover
BIN_DIR := bin

# Build-time version metadata. Override on the command line if needed; the
# defaults derive from git for local/dev builds. Release builds set these
# via GoReleaser instead.
VERSION ?= $(shell git describe --tags --dirty --always 2>/dev/null || echo "(devel)")
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo "none")
DATE    ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

LDFLAGS := -s -w \
	-X $(PKG)/internal/version.Version=$(VERSION) \
	-X $(PKG)/internal/version.Commit=$(COMMIT) \
	-X $(PKG)/internal/version.Date=$(DATE)

.PHONY: test lint build install release snapshot

test:
	go test ./...

lint:
	golangci-lint run

build:
	mkdir -p $(BIN_DIR)
	go build -ldflags "$(LDFLAGS)" -o $(BIN_DIR)/$(BINARY) $(CMD)

install:
	go install -ldflags "$(LDFLAGS)" $(CMD)

# release/snapshot are used by CI; documented here for local dry-runs.
release:
	goreleaser release --clean

snapshot:
	goreleaser release --snapshot --clean
