package commands

import (
	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/output"
)

func newImagesCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "images",
		Short: "List available images",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			client, err := clientFromEnv()
			if err != nil {
				return err
			}
			raw, err := client.ListImages(cmd.Context())
			if err != nil {
				return err
			}
			return output.PrintJSON(cmd.OutOrStdout(), raw)
		},
	}
}
