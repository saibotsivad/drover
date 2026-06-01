package commands

import (
	"context"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/api"
)

func newStartCmd() *cobra.Command {
	var (
		privileged bool
		label      string
		envPairs   []string
		timeout    int
		noWait     bool
		interval   int
	)
	cmd := &cobra.Command{
		Use:   "start <image-name>",
		Short: "Launch a worker (blocks until running by default)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			env, err := parseEnv(envPairs)
			if err != nil {
				return err
			}
			req := api.CreateWorkerRequest{
				Image:          args[0],
				Privileged:     privileged,
				Env:            env,
				Label:          label,
				TimeoutSeconds: timeout,
			}
			return runLifecycle(cmd, noWait, interval,
				func(ctx context.Context, c *api.Client) (*api.WorkerResult, error) {
					return c.CreateWorker(ctx, req)
				},
				lifecycleSpec{target: api.StatusRunning, failOnError: true},
			)
		},
	}
	cmd.Flags().BoolVar(&privileged, "privileged", false, "run as a privileged worker")
	cmd.Flags().StringVar(&label, "label", "", "arbitrary label string")
	cmd.Flags().StringArrayVar(&envPairs, "env", nil, "environment variable KEY=VALUE (repeatable)")
	cmd.Flags().IntVar(&timeout, "timeout", 0, "server-side worker lifetime cap in seconds (0 = server default)")
	cmd.Flags().BoolVar(&noWait, "no-wait", false, "return as soon as the worker is created")
	cmd.Flags().IntVar(&interval, "interval", 1, "seconds between poll requests while waiting")
	return cmd
}
