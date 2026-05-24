// Package config loads and validates the two environment variables the CLI
// runs on, DROVER_API_URL and DROVER_API_KEY, erroring clearly when either is
// absent. There is no config file. (Scaffolding only; Load lands in a later
// phase.)
package config
