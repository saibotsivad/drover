// Package commands assembles the drover cobra command tree. Each user-facing
// subcommand lives in its own file in this package and is registered with the
// root command in newRootCmd. The package exposes a single entry point,
// Execute, which cmd/drover/main.go calls.
package commands

import (
	"context"
	"os"
	"os/signal"

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
		newStartCmd(),
		newStopCmd(),
		newDestroyCmd(),
		newExecCmd(),
		newKeygenCmd(),
	)

	return root
}

// Execute builds the command tree and runs it under a context cancelled on
// SIGINT, returning the process exit code. A cancelled context surfaces as
// exit 130 via the commands that honour it.
func Execute() int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	root := newRootCmd()
	root.SetArgs(os.Args[1:])
	if err := root.ExecuteContext(ctx); err != nil {
		return output.PrintError(root.ErrOrStderr(), err)
	}
	return 0
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
