package commands

import (
	"context"
	"errors"
	"strings"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/api"
	"github.com/saibotsivad/drover/cli/internal/config"
	"github.com/saibotsivad/drover/cli/internal/output"
	"github.com/saibotsivad/drover/cli/internal/ws"
)

func newExecCmd() *cobra.Command {
	// Default (interspersed) flag parsing is what makes `--` work: cobra
	// records its position via ArgsLenAtDash and forwards everything after it
	// verbatim, while a flag *before* the container id (e.g. --bogus) is still
	// rejected. SetInterspersed(false) would instead make ArgsLenAtDash always
	// -1, so it is deliberately not used.
	return &cobra.Command{
		Use:   "exec <container-id> -- <command...>",
		Short: "Run a command in a container",
		Args:  cobra.MinimumNArgs(1),
		RunE:  runExec,
	}
}

func runExec(cmd *cobra.Command, args []string) error {
	dash := cmd.ArgsLenAtDash()
	if dash < 0 {
		return &output.Failure{
			Code:   1,
			Err:    "interactive_exec_unsupported",
			Detail: "interactive exec is not yet supported; use: drover exec <container-id> -- <command...>",
		}
	}
	if dash != 1 {
		return &output.Failure{Code: 1, Err: "usage", Detail: "expected exactly one container id before --"}
	}
	id := args[0]
	command := args[dash:]
	if len(command) == 0 {
		return &output.Failure{Code: 1, Err: "usage", Detail: "no command given after --"}
	}

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	client := api.New(cfg.BaseURL, cfg.APIKey)
	ctx := cmd.Context()

	commandID, err := client.CreateExec(ctx, id, strings.Join(command, " "))
	if err != nil {
		return err
	}

	wsURL, err := ws.URL(cfg.BaseURL, id)
	if err != nil {
		return &output.Failure{Code: 1, Err: "stream_failed", Detail: err.Error()}
	}

	code, err := ws.Stream(ctx, wsURL, cfg.APIKey, commandID, cmd.OutOrStdout())
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return &output.Failure{Code: 130, Err: "interrupted"}
		}
		return &output.Failure{Code: 1, Err: "stream_failed", Detail: err.Error()}
	}
	if code != 0 {
		return &output.SilentExit{Code: code}
	}
	return nil
}
