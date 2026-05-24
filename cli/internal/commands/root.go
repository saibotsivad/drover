// Package commands assembles the drover cobra command tree. Each user-facing
// subcommand lives in its own file in this package and is registered with the
// root command in newRootCmd. The package exposes a single entry point,
// Execute, which cmd/drover/main.go calls.
package commands

import (
	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/output"
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
// code. Errors are rendered as JSON to stderr and mapped to their exit code
// by output.PrintError (see the exit-code table in PLAN.md).
func Execute() int {
	root := newRootCmd()
	if err := root.Execute(); err != nil {
		return output.PrintError(root.ErrOrStderr(), err)
	}
	return 0
}
