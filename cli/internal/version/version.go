// Package version holds build-time metadata for the CLI. The vars below are
// overwritten via `-ldflags -X` at build time (see the Makefile and
// .goreleaser.yaml). The defaults are what a bare `go build`/`go install`
// with no ldflags produces.
package version

import "fmt"

var (
	// Version is the CLI version, e.g. a git tag or `git describe` output.
	Version = "(devel)"
	// Commit is the short git SHA the binary was built from.
	Commit = "none"
	// Date is the UTC build timestamp.
	Date = "unknown"
)

// String renders the single line shown by `drover --version`.
func String() string {
	return fmt.Sprintf("%s (commit %s, built %s)", Version, Commit, Date)
}
