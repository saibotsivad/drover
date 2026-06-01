package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestListWorkersPassthrough(t *testing.T) {
	body := `[{"id":"c1","status":"running","unknown_future_field":42}]`
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/workers" {
			t.Errorf("got %s %s", r.Method, r.URL.Path)
		}
		w.Write([]byte(body))
	})
	raw, err := c.ListWorkers(context.Background())
	if err != nil {
		t.Fatalf("ListWorkers: %v", err)
	}
	// Unknown fields must survive (passthrough, not re-marshalled from a struct).
	if !strings.Contains(string(raw), "unknown_future_field") {
		t.Errorf("raw lost unknown field: %s", raw)
	}
}

func TestGetWorkerDecodeAndRaw(t *testing.T) {
	body := `{"id":"c1","image":"img","privileged":false,"status":"initializing","timeout_seconds":300,"transition_timeout_seconds":30,"created_at":"2026-05-24T00:00:00Z"}`
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(body))
	})
	res, err := c.GetWorker(context.Background(), "c1")
	if err != nil {
		t.Fatalf("GetWorker: %v", err)
	}
	if res.Status != StatusInitializing {
		t.Errorf("status = %q", res.Status)
	}
	if res.TransitionTimeoutSeconds == nil || *res.TransitionTimeoutSeconds != 30 {
		t.Errorf("transition_timeout_seconds = %v", res.TransitionTimeoutSeconds)
	}
	// created_at isn't in the typed view but must survive in Raw.
	if !strings.Contains(string(res.Raw), "created_at") {
		t.Errorf("Raw dropped created_at: %s", res.Raw)
	}
}

func TestCreateWorkerBody(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/workers" {
			t.Errorf("got %s %s", r.Method, r.URL.Path)
		}
		b, _ := io.ReadAll(r.Body)
		var req CreateWorkerRequest
		if err := json.Unmarshal(b, &req); err != nil {
			t.Fatalf("bad body: %v", err)
		}
		if req.Image != "myimg" || !req.Privileged || req.Env["K"] != "V" {
			t.Errorf("decoded request = %+v", req)
		}
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(`{"id":"c1","status":"initializing","transition_timeout_seconds":30}`))
	})
	res, err := c.CreateWorker(context.Background(), CreateWorkerRequest{
		Image: "myimg", Privileged: true, Env: map[string]string{"K": "V"},
	})
	if err != nil {
		t.Fatalf("CreateWorker: %v", err)
	}
	if res.ID != "c1" {
		t.Errorf("id = %q", res.ID)
	}
}

func TestStopWorkerMethodAndPath(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/workers/c1/stop" {
			t.Errorf("got %s %s, want POST /workers/c1/stop", r.Method, r.URL.Path)
		}
		w.Write([]byte(`{"id":"c1","status":"stopping","transition_timeout_seconds":10}`))
	})
	if _, err := c.StopWorker(context.Background(), "c1"); err != nil {
		t.Fatalf("StopWorker: %v", err)
	}
}

func TestDestroyWorkerIsDelete(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete || r.URL.Path != "/workers/c1" {
			t.Errorf("got %s %s, want DELETE /workers/c1", r.Method, r.URL.Path)
		}
		w.Write([]byte(`{"id":"c1","status":"destroying","transition_timeout_seconds":10}`))
	})
	if _, err := c.DestroyWorker(context.Background(), "c1"); err != nil {
		t.Fatalf("DestroyWorker: %v", err)
	}
}
