package commands

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"

	"github.com/spf13/cobra"
)

func newKeygenCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "keygen",
		Short: "Generate a new API key and its SHA-256 hash",
		Long: `Generate a random API key and print the plain-text key alongside its
SHA-256 hash. Set DROVER_API_KEY on the server to the hash, and use the
plain-text key in DROVER_API_KEY on the CLI and in Authorization headers.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			raw := make([]byte, 32)
			if _, err := rand.Read(raw); err != nil {
				return err
			}
			plainKey := base64.RawURLEncoding.EncodeToString(raw)

			sum := sha256.Sum256([]byte(plainKey))
			hashedKey := hex.EncodeToString(sum[:])

			out := cmd.OutOrStdout()
			fmt.Fprintln(out, "Drover Server Key:")
			fmt.Fprintf(out, "    DROVER_API_KEY=%s\n", hashedKey)
			fmt.Fprintln(out)
			fmt.Fprintln(out, "Drover API Header:")
			fmt.Fprintf(out, "    Authorization: Bearer %s\n", plainKey)
			fmt.Fprintln(out)
			fmt.Fprintln(out, "Drover CLI Key:")
			fmt.Fprintf(out, "    export DROVER_API_KEY=%s\n", plainKey)
			return nil
		},
	}
}
