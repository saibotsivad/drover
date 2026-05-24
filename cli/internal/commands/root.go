// Package commands assembles the drover cobra command tree. Each user-facing
// subcommand lives in its own file in this package and is registered with the
// root command in newRootCmd. The package exposes a single entry point,
// Execute, which cmd/drover/main.go calls.
package commands

import (
	"os"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/api"
	"github.com/saibotsivad/drover/cli/internal/config"
	"github.com/saibotsivad/drover/cli/internal/output"
	"github.com/saibotsivad/drover/cli/internal/version"
)

// newRootCmd builds the root command and registers every subcommand.
func newRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:   "drover",
		Short: "Interact with the Drover orchestrator from the terminal",
		// Errors are rendered by us as JSON, so stop cobra from printing usage
		// and the raw error on failure.
		SilenceUsage:  true,
		SilenceErrors: true,
		Version:       version.String(),
	}
	root.SetVersionTemplate("drover {{.Version}}\n")

	root.AddCommand(
		newImagesCmd(),
		newImageCmd(),
		newPsCmd(),
	)

	return root
}

// Execute builds the command tree, runs it, and returns the process exit code.
func Execute() int {
	root := newRootCmd()
	root.SetArgs(os.Args[1:])
	return execute(root)
}

// execute runs an already-built root command and maps any error to a process
// exit code, rendering it as JSON to the command's stderr. Split out from
// Execute so tests can supply their own args and capture out/err.
func execute(root *cobra.Command) int {
	if err := root.Execute(); err != nil {
		return output.PrintError(root.ErrOrStderr(), err)
	}
	return 0
}

// clientFromEnv builds an API client from the environment, returning the
// config error (exit 2) unchanged when the environment is incomplete.
func clientFromEnv() (*api.Client, error) {
	cfg, err := config.Load()
	if err != nil {
		return nil, err
	}
	return api.New(cfg.BaseURL, cfg.APIKey), nil
}
