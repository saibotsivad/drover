package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

func TestCreateExec(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/containers/c1/execs" {
			t.Errorf("got %s %s", r.Method, r.URL.Path)
		}
		b, _ := io.ReadAll(r.Body)
		var req ExecRequest
		if err := json.Unmarshal(b, &req); err != nil || req.Command != "ls -la" {
			t.Errorf("body = %s (err %v)", b, err)
		}
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(`{"command_id":"cmd-1"}`))
	})
	id, err := c.CreateExec(context.Background(), "c1", "ls -la")
	if err != nil {
		t.Fatalf("CreateExec: %v", err)
	}
	if id != "cmd-1" {
		t.Errorf("command_id = %q", id)
	}
}

func TestCreateExecError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`{"detail":"no such container"}`))
	})
	id, err := c.CreateExec(context.Background(), "missing", "ls")
	if id != "" || !IsNotFound(err) {
		t.Errorf("id=%q err=%v, want empty id + not-found", id, err)
	}
}
