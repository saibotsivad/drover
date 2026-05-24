// Package ws handles WebSocket streaming of exec output.
package ws

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/coder/websocket"
)

// readLimit is generous so a single large output frame isn't truncated.
const readLimit = 32 << 20 // 32 MiB

// URL converts an http(s) base URL into the per-container WebSocket URL.
func URL(baseURL, containerID string) (string, error) {
	u, err := url.Parse(baseURL)
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "http":
		u.Scheme = "ws"
	case "https":
		u.Scheme = "wss"
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/containers/" + url.PathEscape(containerID) + "/ws"
	return u.String(), nil
}

// frame is the minimal view of a stream frame needed to route it. The full
// bytes are passed through unchanged; this is only for inspection.
type frame struct {
	Type      string `json:"type"`
	CommandID string `json:"command_id"`
	Status    string `json:"status"`
	ExitCode  *int   `json:"exit_code"`
}

// Stream dials the per-container WebSocket, then writes every frame whose
// command_id matches to out verbatim (one JSON object per line, no
// re-marshalling). Frames for other commands and container log frames are
// dropped. It returns the exit code carried by the matching status:complete
// frame. The context cancels the dial and the read loop (Ctrl-C).
func Stream(ctx context.Context, wsURL, apiKey, commandID string, out io.Writer) (int, error) {
	conn, _, err := websocket.Dial(ctx, wsURL, &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": {"Bearer " + apiKey}},
	})
	if err != nil {
		return 0, err
	}
	defer conn.CloseNow()
	conn.SetReadLimit(readLimit)

	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			return 0, err
		}

		var f frame
		if json.Unmarshal(data, &f) != nil || f.CommandID != commandID {
			continue
		}

		switch f.Type {
		case "output", "status":
			if _, err := out.Write(append(data, '\n')); err != nil {
				return 0, err
			}
			if f.Type == "status" && f.Status == "complete" {
				code := 0
				if f.ExitCode != nil {
					code = *f.ExitCode
				}
				_ = conn.Close(websocket.StatusNormalClosure, "")
				return code, nil
			}
		}
	}
}
