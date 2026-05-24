package api

import (
	"bytes"
	"encoding/json"
)

// Error is the typed error for any non-2xx response or transport failure.
// It carries the HTTP status (0 for transport errors) so callers can inspect
// it, renders as a JSON object on stderr, and reports exit code 1 (generic
// API error, per the exit-code table).
type Error struct {
	Status int    `json:"-"`
	Kind   string `json:"error"`
	Detail string `json:"detail,omitempty"`
}

func (e *Error) Error() string {
	if e.Detail != "" {
		return e.Kind + ": " + e.Detail
	}
	return e.Kind
}

// ExitCode reports the generic API-error exit code.
func (e *Error) ExitCode() int { return 1 }

// IsNotFound reports whether the error is an HTTP 404.
func IsNotFound(err error) bool {
	ae, ok := err.(*Error)
	return ok && ae.Status == 404
}

// parseError builds an Error from a non-2xx response body. FastAPI
// returns {"detail": "..."} for most errors and {"detail": [...]} for
// validation errors; both are surfaced as the detail string.
func parseError(status int, body []byte) *Error {
	detail := ""
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(body, &env) == nil && len(env.Detail) > 0 {
		var s string
		if json.Unmarshal(env.Detail, &s) == nil {
			detail = s
		} else {
			detail = string(env.Detail)
		}
	} else if trimmed := bytes.TrimSpace(body); len(trimmed) > 0 {
		detail = string(trimmed)
	}
	return &Error{Status: status, Kind: "api_error", Detail: detail}
}
