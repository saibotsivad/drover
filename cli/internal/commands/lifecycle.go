package commands

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/saibotsivad/drover/cli/internal/api"
	"github.com/saibotsivad/drover/cli/internal/output"
	"github.com/saibotsivad/drover/cli/internal/wait"
)

// lifecycleSpec describes how a lifecycle command decides it is done.
type lifecycleSpec struct {
	target      api.Status // terminal status we poll toward
	failOnError bool       // start: an `error` status is a non-timeout failure
}

// action performs the initial transition request (POST create / POST stop /
// DELETE destroy) and returns the orchestrator's transitional response.
type action func(context.Context, *api.Client) (*api.WorkerResult, error)

// runLifecycle runs the shared create/stop/destroy flow: perform the
// transition, then (unless --no-wait) poll until the terminal state or the
// orchestrator-advertised timeout, printing the resulting worker JSON.
func runLifecycle(cmd *cobra.Command, noWait bool, interval int, do action, spec lifecycleSpec) error {
	client, err := clientFromEnv()
	if err != nil {
		return err
	}
	ctx := cmd.Context()

	res, err := do(ctx, client)
	if err != nil {
		return err
	}

	if noWait {
		return output.PrintJSON(cmd.OutOrStdout(), res.Raw)
	}
	if res.TransitionTimeoutSeconds == nil {
		_, _ = fmt.Fprintln(cmd.ErrOrStderr(), `{"warning":"orchestrator returned no transition_timeout_seconds; returning without waiting"}`)
		return output.PrintJSON(cmd.OutOrStdout(), res.Raw)
	}

	id := res.ID
	deadline := time.Now().Add(time.Duration(*res.TransitionTimeoutSeconds) * time.Second)

	final, werr := wait.Wait(ctx, time.Duration(interval)*time.Second, deadline,
		func(c context.Context) (*api.WorkerResult, error) { return client.GetWorker(c, id) },
		func(r *api.WorkerResult) bool {
			if spec.failOnError && r.Status == api.StatusError {
				return true
			}
			return r.Status == spec.target
		},
	)
	if werr != nil {
		switch {
		case errors.Is(werr, wait.ErrTimeout):
			status := ""
			if final != nil {
				status = string(final.Status)
			}
			return &output.Failure{Code: 3, Err: "timeout", ID: id, Status: status}
		case errors.Is(werr, context.Canceled):
			return &output.Failure{Code: 130, Err: "interrupted", ID: id}
		default:
			return werr
		}
	}

	if spec.failOnError && final.Status == api.StatusError {
		return &output.Failure{Code: 4, Err: "start_failed", ID: id, Status: string(api.StatusError)}
	}
	return output.PrintJSON(cmd.OutOrStdout(), final.Raw)
}

// parseEnv turns repeated --env KEY=VALUE pairs into a map, erroring on a
// missing `=` or empty key so a typo fails loudly.
func parseEnv(pairs []string) (map[string]string, error) {
	if len(pairs) == 0 {
		return nil, nil
	}
	env := make(map[string]string, len(pairs))
	for _, p := range pairs {
		k, v, ok := strings.Cut(p, "=")
		if !ok || k == "" {
			return nil, &output.Failure{Code: 1, Err: "invalid_env", Detail: fmt.Sprintf("expected KEY=VALUE, got %q", p)}
		}
		env[k] = v
	}
	return env, nil
}
