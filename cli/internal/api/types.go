// Package api is the HTTP client over the orchestrator REST API. Display
// commands (images, ps) receive the orchestrator's JSON verbatim so the
// response shape can grow without breaking jq-based callers; worker
// lifecycle methods additionally decode a typed view (Worker) for the
// fields the polling logic reads (status, transition_timeout_seconds).
package api

import "encoding/json"

// Status mirrors orchestrator WorkerStatus.
type Status string

// Worker status values, mirroring orchestrator WorkerStatus.
const (
	StatusInitializing Status = "initializing"
	StatusRunning      Status = "running"
	StatusStopping     Status = "stopping"
	StatusStopped      Status = "stopped"
	StatusResuming     Status = "resuming"
	StatusDestroying   Status = "destroying"
	StatusDestroyed    Status = "destroyed"
	StatusError        Status = "error"
)

// CreateWorkerRequest is the POST /workers body. Fields mirror the
// orchestrator's CreateWorkerRequest; omitempty lets the server apply its
// own defaults (e.g. timeout_seconds).
type CreateWorkerRequest struct {
	Image          string            `json:"image"`
	Privileged     bool              `json:"privileged,omitempty"`
	Env            map[string]string `json:"env,omitempty"`
	Label          string            `json:"label,omitempty"`
	TimeoutSeconds int               `json:"timeout_seconds,omitempty"`
}

// Worker is the typed view of a worker response, holding the fields the
// CLI's lifecycle logic inspects. A renamed or removed field surfaces as a
// zero value here; the full server response is preserved in WorkerResult.Raw.
type Worker struct {
	ID                       string  `json:"id"`
	Image                    string  `json:"image"`
	Privileged               bool    `json:"privileged"`
	Status                   Status  `json:"status"`
	Label                    *string `json:"label,omitempty"`
	TimeoutSeconds           int     `json:"timeout_seconds"`
	ErrorCode                *string `json:"error_code,omitempty"`
	TransitionTimeoutSeconds *int    `json:"transition_timeout_seconds,omitempty"`
}

// WorkerResult pairs the decoded typed view with the raw response bytes so
// commands can print the orchestrator's JSON unchanged while logic reads the
// typed fields.
type WorkerResult struct {
	Worker
	Raw json.RawMessage
}

// ExecRequest is the POST /workers/{id}/execs body.
type ExecRequest struct {
	Command string `json:"command"`
}

// ExecResponse is the POST /workers/{id}/execs response.
type ExecResponse struct {
	CommandID string `json:"command_id"`
}
