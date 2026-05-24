// Package output provides the stdout/stderr JSON emission helpers shared by
// every command, plus the structured error type that carries process exit
// codes. All control-plane output is JSON: success values to stdout, errors
// as a JSON object to stderr (see PLAN.md "Exit codes").
package output

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// PrintJSON marshals v to compact JSON and writes it to w with a trailing
// newline. Commands pass cmd.OutOrStdout() so output is capturable in tests.
func PrintJSON(w io.Writer, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "%s\n", b)
	return err
}

// exitCoder is anything that knows the process exit code it should produce.
type exitCoder interface {
	ExitCode() int
}

// SilentExit carries a process exit code without printing anything. exec uses
// it to propagate the remote command's exit_code — its output already streamed
// to stdout, so no error envelope should be written.
type SilentExit struct{ Code int }

func (e *SilentExit) Error() string { return fmt.Sprintf("exit status %d", e.Code) }

// ExitCode reports the propagated exit code.
func (e *SilentExit) ExitCode() int { return e.Code }

// PrintError writes err to w as a single JSON object and returns the process
// exit code. A SilentExit prints nothing. Other errors that marshal to a JSON
// object (Failure, api.Error) are emitted as-is; anything else is wrapped
// as {"error": "<message>"}. The exit code comes from an exitCoder in the
// chain, defaulting to 1.
func PrintError(w io.Writer, err error) int {
	var se *SilentExit
	if errors.As(err, &se) {
		return se.Code
	}

	code := 1
	var ec exitCoder
	if errors.As(err, &ec) {
		code = ec.ExitCode()
	}

	body, mErr := json.Marshal(err)
	if mErr != nil || len(body) == 0 || body[0] != '{' || string(body) == "{}" {
		body, _ = json.Marshal(struct {
			Error string `json:"error"`
		}{err.Error()})
	}
	_, _ = fmt.Fprintf(w, "%s\n", body)
	return code
}

// Failure is the CLI's structured error: it renders as a JSON object on
// stderr and carries the process exit code. Fields beyond Err are optional
// and omitted when empty, so one type covers every error shape the commands
// emit (config error, timeout, start_failed, …).
type Failure struct {
	Code   int    `json:"-"`
	Err    string `json:"error"`
	Detail string `json:"detail,omitempty"`
	ID     string `json:"id,omitempty"`
	Status string `json:"status,omitempty"`
}

func (f *Failure) Error() string {
	if f.Detail != "" {
		return f.Err + ": " + f.Detail
	}
	return f.Err
}

// ExitCode reports the process exit code for this failure.
func (f *Failure) ExitCode() int { return f.Code }
