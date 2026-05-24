// Package commands assembles the drover cobra command tree. Each user-facing
// subcommand lives in its own file in this package and is registered with the
// root command in newRootCmd. The package exposes a single entry point,
// Execute, which cmd/drover/main.go calls.
package commands

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/version"
)

// newRootCmd builds the root command and registers every subcommand.
func newRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:   "drover",
		Short: "Interact with the Drover orchestrator from the terminal",
		// Errors are rendered by us (as JSON, once output wiring lands), so
		// stop cobra from printing usage and the raw error on failure.
		SilenceUsage:  true,
		SilenceErrors: true,
		Version:       version.String(),
	}
	root.SetVersionTemplate("drover {{.Version}}\n")

	// Subcommands are registered here as they land:
	//   images, image, ps, start, stop, destroy, exec.

	return root
}

// Execute builds the command tree, runs it, and returns the process exit
// code. Exit-code mapping per command is filled in as the commands land; for
// now any error is a generic failure (1).
func Execute() int {
	if err := newRootCmd().Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}
