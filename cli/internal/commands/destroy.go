package commands

import (
	"context"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/api"
)

func newDestroyCmd() *cobra.Command {
	var (
		noWait   bool
		interval int
	)
	cmd := &cobra.Command{
		Use:   "destroy <container-id>",
		Short: "Destroy a container (blocks until destroyed by default)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := args[0]
			return runLifecycle(cmd, noWait, interval,
				func(ctx context.Context, c *api.Client) (*api.ContainerResult, error) {
					return c.DestroyContainer(ctx, id)
				},
				lifecycleSpec{target: api.StatusDestroyed},
			)
		},
	}
	cmd.Flags().BoolVar(&noWait, "no-wait", false, "return as soon as the transition is accepted")
	cmd.Flags().IntVar(&interval, "interval", 1, "seconds between poll requests while waiting")
	return cmd
}
