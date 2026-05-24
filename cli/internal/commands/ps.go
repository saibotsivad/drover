package commands

import (
	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/output"
)

func newPsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "ps",
		Short: "List micro-containers",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			client, err := clientFromEnv()
			if err != nil {
				return err
			}
			raw, err := client.ListContainers(cmd.Context())
			if err != nil {
				return err
			}
			return output.PrintJSON(cmd.OutOrStdout(), raw)
		},
	}
}
