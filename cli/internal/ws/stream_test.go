package ws

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func wsServer(t *testing.T, frames []string, closeClean bool) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		for _, f := range frames {
			if err := c.Write(r.Context(), websocket.MessageText, []byte(f)); err != nil {
				return
			}
		}
		if closeClean {
			c.Close(websocket.StatusNormalClosure, "")
		} else {
			// hold the connection open briefly without sending a status frame
			time.Sleep(50 * time.Millisecond)
			c.CloseNow()
		}
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

func TestURL(t *testing.T) {
	got, err := URL("https://drover.example.com", "c1")
	if err != nil || got != "wss://drover.example.com/containers/c1/ws" {
		t.Fatalf("URL = %q, err %v", got, err)
	}
	got, _ = URL("http://localhost:8000", "c1")
	if got != "ws://localhost:8000/containers/c1/ws" {
		t.Errorf("URL = %q", got)
	}
}

func TestStreamPassthroughAndExitCode(t *testing.T) {
	frames := []string{
		`{"type":"output","command_id":"cmd-1","stream":"stdout","data":"hi"}`,
		`{"type":"log","stream":"stdout","data":"container log line"}`,
		`{"type":"output","command_id":"other","stream":"stdout","data":"nope"}`,
		`{"type":"status","command_id":"cmd-1","status":"complete","exit_code":3}`,
	}
	url := wsServer(t, frames, true)

	var buf strings.Builder
	code, err := Stream(context.Background(), url, "sk", "cmd-1", &buf)
	if err != nil {
		t.Fatalf("Stream: %v", err)
	}
	if code != 3 {
		t.Errorf("code = %d, want 3", code)
	}
	want := frames[0] + "\n" + frames[3] + "\n"
	if buf.String() != want {
		t.Errorf("output =\n%q\nwant\n%q", buf.String(), want)
	}
}

func TestStreamExitCodeZero(t *testing.T) {
	frames := []string{`{"type":"status","command_id":"cmd-1","status":"complete","exit_code":0}`}
	url := wsServer(t, frames, true)
	code, err := Stream(context.Background(), url, "sk", "cmd-1", &strings.Builder{})
	if err != nil || code != 0 {
		t.Fatalf("code=%d err=%v", code, err)
	}
}

func TestStreamClosedBeforeComplete(t *testing.T) {
	frames := []string{`{"type":"output","command_id":"cmd-1","stream":"stdout","data":"partial"}`}
	url := wsServer(t, frames, false)
	_, err := Stream(context.Background(), url, "sk", "cmd-1", &strings.Builder{})
	if err == nil {
		t.Fatal("expected an error when the stream closes before a complete frame")
	}
}

func TestStreamContextCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := Stream(ctx, "ws://127.0.0.1:0/x", "sk", "cmd-1", &strings.Builder{})
	if err == nil {
		t.Fatal("expected error with a cancelled context")
	}
}
