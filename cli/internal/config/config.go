// Package config loads and validates the two environment variables the CLI
// runs on. There is no config file (see docs/cli.md "Configuration").
package config

import (
	"net/url"
	"os"
	"strings"

	"github.com/saibotsivad/drover/cli/internal/output"
)

// Config holds the validated runtime configuration.
type Config struct {
	BaseURL string
	APIKey  string
}

// Load reads DROVER_API_URL and DROVER_API_KEY from the environment and
// validates them. A missing or malformed value returns an *output.Failure
// with exit code 2.
func Load() (Config, error) {
	rawURL := strings.TrimSpace(os.Getenv("DROVER_API_URL"))
	key := strings.TrimSpace(os.Getenv("DROVER_API_KEY"))

	if rawURL == "" {
		return Config{}, &output.Failure{Code: 2, Err: "missing_configuration", Detail: "DROVER_API_URL is not set"}
	}
	if key == "" {
		return Config{}, &output.Failure{Code: 2, Err: "missing_configuration", Detail: "DROVER_API_KEY is not set"}
	}

	u, err := url.Parse(rawURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return Config{}, &output.Failure{Code: 2, Err: "invalid_configuration", Detail: "DROVER_API_URL must be an http(s) URL with a host"}
	}

	return Config{BaseURL: strings.TrimRight(rawURL, "/"), APIKey: key}, nil
}
