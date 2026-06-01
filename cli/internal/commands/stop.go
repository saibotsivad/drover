package commands

import (
	"context"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/api"
)

func newStopCmd() *cobra.Command {
	var (
		noWait   bool
		interval int
	)
	cmd := &cobra.Command{
		Use:   "stop <worker-id>",
		Short: "Stop a worker (blocks until stopped by default)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := args[0]
			return runLifecycle(cmd, noWait, interval,
				func(ctx context.Context, c *api.Client) (*api.WorkerResult, error) {
					return c.StopWorker(ctx, id)
				},
				lifecycleSpec{target: api.StatusStopped},
			)
		},
	}
	cmd.Flags().BoolVar(&noWait, "no-wait", false, "return as soon as the transition is accepted")
	cmd.Flags().IntVar(&interval, "interval", 1, "seconds between poll requests while waiting")
	return cmd
}
