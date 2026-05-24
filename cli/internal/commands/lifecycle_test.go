package commands

import (
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

// lifecycleServer is a fake orchestrator for lifecycle tests. The transition
// endpoints return `transitionStatus` with `transitionTimeout`; GET returns
// `getStatus`. getCalls counts polls so tests can assert --no-wait skips them.
type lifecycleServer struct {
	transitionStatus  string
	transitionTimeout *int // nil => omitted from response
	getStatus         string
	getCalls          atomic.Int32
}

func (s *lifecycleServer) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/containers/c1":
			s.getCalls.Add(1)
			fmt.Fprintf(w, `{"id":"c1","status":%q}`, s.getStatus)
		case r.Method == http.MethodPost && r.URL.Path == "/containers":
			w.WriteHeader(http.StatusCreated)
			s.writeTransition(w)
		case r.Method == http.MethodPost && r.URL.Path == "/containers/c1/stop":
			s.writeTransition(w)
		case r.Method == http.MethodDelete && r.URL.Path == "/containers/c1":
			s.writeTransition(w)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func (s *lifecycleServer) writeTransition(w http.ResponseWriter) {
	if s.transitionTimeout == nil {
		fmt.Fprintf(w, `{"id":"c1","status":%q}`, s.transitionStatus)
		return
	}
	fmt.Fprintf(w, `{"id":"c1","status":%q,"transition_timeout_seconds":%d}`, s.transitionStatus, *s.transitionTimeout)
}

func ptr(i int) *int { return &i }

func TestStartHappy(t *testing.T) {
	s := &lifecycleServer{transitionStatus: "initializing", transitionTimeout: ptr(30), getStatus: "running"}
	fakeOrchestrator(t, s.handler(t))
	out, errOut, code := runCmd(t, "start", "myimg")
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, errOut)
	}
	if !strings.Contains(out, `"status":"running"`) {
		t.Errorf("stdout = %q", out)
	}
}

func TestStartErrorState(t *testing.T) {
	s := &lifecycleServer{transitionStatus: "initializing", transitionTimeout: ptr(30), getStatus: "error"}
	fakeOrchestrator(t, s.handler(t))
	_, errOut, code := runCmd(t, "start", "myimg")
	if code != 4 {
		t.Fatalf("exit = %d, want 4", code)
	}
	if !strings.Contains(errOut, `"error":"start_failed"`) {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestStartTimeout(t *testing.T) {
	// transition_timeout_seconds=0 => deadline is now, so the first poll that
	// shows a non-terminal status times out immediately (no sleep).
	s := &lifecycleServer{transitionStatus: "initializing", transitionTimeout: ptr(0), getStatus: "initializing"}
	fakeOrchestrator(t, s.handler(t))
	_, errOut, code := runCmd(t, "start", "myimg")
	if code != 3 {
		t.Fatalf("exit = %d, want 3", code)
	}
	if !strings.Contains(errOut, `"error":"timeout"`) || !strings.Contains(errOut, `"status":"initializing"`) {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestStartNoWait(t *testing.T) {
	s := &lifecycleServer{transitionStatus: "initializing", transitionTimeout: ptr(30), getStatus: "running"}
	fakeOrchestrator(t, s.handler(t))
	out, _, code := runCmd(t, "start", "myimg", "--no-wait")
	if code != 0 {
		t.Fatalf("exit = %d", code)
	}
	if !strings.Contains(out, `"status":"initializing"`) {
		t.Errorf("stdout = %q, want transitional status", out)
	}
	if n := s.getCalls.Load(); n != 0 {
		t.Errorf("GET polled %d times, want 0 with --no-wait", n)
	}
}

func TestStartNilTransitionTimeoutWarns(t *testing.T) {
	s := &lifecycleServer{transitionStatus: "initializing", transitionTimeout: nil, getStatus: "running"}
	fakeOrchestrator(t, s.handler(t))
	out, errOut, code := runCmd(t, "start", "myimg")
	if code != 0 {
		t.Fatalf("exit = %d", code)
	}
	if !strings.Contains(errOut, "warning") {
		t.Errorf("stderr = %q, want a warning", errOut)
	}
	if !strings.Contains(out, `"status":"initializing"`) {
		t.Errorf("stdout = %q", out)
	}
	if n := s.getCalls.Load(); n != 0 {
		t.Errorf("GET polled %d times, want 0 when no transition timeout", n)
	}
}

func TestStopHappy(t *testing.T) {
	s := &lifecycleServer{transitionStatus: "stopping", transitionTimeout: ptr(10), getStatus: "stopped"}
	fakeOrchestrator(t, s.handler(t))
	out, errOut, code := runCmd(t, "stop", "c1")
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, errOut)
	}
	if !strings.Contains(out, `"status":"stopped"`) {
		t.Errorf("stdout = %q", out)
	}
}

func TestDestroyHappy(t *testing.T) {
	s := &lifecycleServer{transitionStatus: "destroying", transitionTimeout: ptr(10), getStatus: "destroyed"}
	fakeOrchestrator(t, s.handler(t))
	out, errOut, code := runCmd(t, "destroy", "c1")
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, errOut)
	}
	if !strings.Contains(out, `"status":"destroyed"`) {
		t.Errorf("stdout = %q", out)
	}
}

func TestStartInvalidEnv(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("should not reach server on invalid env")
	})
	_, errOut, code := runCmd(t, "start", "myimg", "--env", "NOEQUALS")
	if code != 1 {
		t.Errorf("exit = %d, want 1", code)
	}
	if !strings.Contains(errOut, "invalid_env") {
		t.Errorf("stderr = %q", errOut)
	}
}
