package commands

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/coder/websocket"
)

// execServer handles both the POST /containers/{id}/execs call and the
// WebSocket upgrade at /containers/{id}/ws, replaying the given frames. It
// records the command string posted so flag-passthrough can be asserted.
type execServer struct {
	frames     []string
	gotCommand string
}

func (s *execServer) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/execs"):
			b, _ := io.ReadAll(r.Body)
			var req struct {
				Command string `json:"command"`
			}
			_ = json.Unmarshal(b, &req)
			s.gotCommand = req.Command
			w.WriteHeader(http.StatusCreated)
			fmt.Fprint(w, `{"command_id":"cmd-1"}`)
		case strings.HasSuffix(r.URL.Path, "/ws"):
			c, err := websocket.Accept(w, r, nil)
			if err != nil {
				return
			}
			for _, f := range s.frames {
				if err := c.Write(r.Context(), websocket.MessageText, []byte(f)); err != nil {
					return
				}
			}
			c.Close(websocket.StatusNormalClosure, "")
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
		}
	}
}

func TestExecStreamsAndExits(t *testing.T) {
	s := &execServer{frames: []string{
		`{"type":"output","command_id":"cmd-1","stream":"stdout","data":"hi"}`,
		`{"type":"status","command_id":"cmd-1","status":"complete","exit_code":5}`,
	}}
	fakeOrchestrator(t, s.handler(t))
	out, errOut, code := runCmd(t, "exec", "c1", "--", "echo", "hi")
	if code != 5 {
		t.Fatalf("exit = %d, want 5 (propagated)", code)
	}
	if errOut != "" {
		t.Errorf("stderr = %q, want empty (SilentExit)", errOut)
	}
	if !strings.Contains(out, `"stream":"stdout"`) || !strings.Contains(out, `"status":"complete"`) {
		t.Errorf("stdout = %q", out)
	}
}

func TestExecExitZero(t *testing.T) {
	s := &execServer{frames: []string{`{"type":"status","command_id":"cmd-1","status":"complete","exit_code":0}`}}
	fakeOrchestrator(t, s.handler(t))
	_, _, code := runCmd(t, "exec", "c1", "--", "true")
	if code != 0 {
		t.Errorf("exit = %d, want 0", code)
	}
}

func TestExecForwardsFlagsAfterDash(t *testing.T) {
	s := &execServer{frames: []string{`{"type":"status","command_id":"cmd-1","status":"complete","exit_code":0}`}}
	fakeOrchestrator(t, s.handler(t))
	runCmd(t, "exec", "c1", "--", "ls", "--bar", "-la")
	if s.gotCommand != "ls --bar -la" {
		t.Errorf("posted command = %q, want %q", s.gotCommand, "ls --bar -la")
	}
}

func TestExecBareIsInteractiveError(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("should not reach server for bare exec")
	})
	_, errOut, code := runCmd(t, "exec", "c1")
	if code != 1 {
		t.Errorf("exit = %d, want 1", code)
	}
	if !strings.Contains(errOut, "interactive_exec_unsupported") {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestExecUnknownFlagBeforeDash(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("should not reach server on unknown flag")
	})
	_, errOut, code := runCmd(t, "exec", "--bogus", "c1", "--", "ls")
	if code != 1 {
		t.Errorf("exit = %d, want 1", code)
	}
	if !strings.Contains(errOut, "unknown flag") {
		t.Errorf("stderr = %q", errOut)
	}
}
