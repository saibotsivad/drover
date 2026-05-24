// Command drover is the entry point for the Drover CLI. It only wires the
// root command and translates its result into a process exit code; all real
// behaviour lives under internal/.
package main

import (
	"os"

	"github.com/saibotsivad/drover/cli/internal/commands"
)

func main() {
	os.Exit(commands.Execute())
}
