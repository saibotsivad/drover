package commands

import (
	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/output"
)

func newImageCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "image <name>",
		Short: "Show details for an image",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := clientFromEnv()
			if err != nil {
				return err
			}
			raw, err := client.GetImage(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			return output.PrintJSON(cmd.OutOrStdout(), raw)
		},
	}
}
