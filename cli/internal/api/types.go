// Package api is the HTTP client over the orchestrator REST API. Display
// commands (images, ps) receive the orchestrator's JSON verbatim so the
// response shape can grow without breaking jq-based callers; container
// lifecycle methods additionally decode a typed view (Container) for the
// fields the polling logic reads (status, transition_timeout_seconds).
package api

import "encoding/json"

// Status mirrors orchestrator ContainerStatus.
type Status string

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

// CreateContainerRequest is the POST /containers body. Fields mirror the
// orchestrator's CreateContainerRequest; omitempty lets the server apply its
// own defaults (e.g. timeout_seconds).
type CreateContainerRequest struct {
	Image          string            `json:"image"`
	Privileged     bool              `json:"privileged,omitempty"`
	Env            map[string]string `json:"env,omitempty"`
	Label          string            `json:"label,omitempty"`
	TimeoutSeconds int               `json:"timeout_seconds,omitempty"`
}

// Container is the typed view of a container response, holding the fields the
// CLI's lifecycle logic inspects. A renamed or removed field surfaces as a
// zero value here; the full server response is preserved in ContainerResult.Raw.
type Container struct {
	ID                       string  `json:"id"`
	Image                    string  `json:"image"`
	Privileged               bool    `json:"privileged"`
	Status                   Status  `json:"status"`
	Label                    *string `json:"label,omitempty"`
	TimeoutSeconds           int     `json:"timeout_seconds"`
	ErrorCode                *string `json:"error_code,omitempty"`
	TransitionTimeoutSeconds *int    `json:"transition_timeout_seconds,omitempty"`
}

// ContainerResult pairs the decoded typed view with the raw response bytes so
// commands can print the orchestrator's JSON unchanged while logic reads the
// typed fields.
type ContainerResult struct {
	Container
	Raw json.RawMessage
}

// ExecRequest is the POST /containers/{id}/execs body.
type ExecRequest struct {
	Command string `json:"command"`
}

// ExecResponse is the POST /containers/{id}/execs response.
type ExecResponse struct {
	CommandID string `json:"command_id"`
}
