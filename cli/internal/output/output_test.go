package output

import (
	"bytes"
	"errors"
	"testing"
)

func TestPrintJSON(t *testing.T) {
	var buf bytes.Buffer
	if err := PrintJSON(&buf, map[string]string{"id": "abc"}); err != nil {
		t.Fatalf("PrintJSON: %v", err)
	}
	if got := buf.String(); got != "{\"id\":\"abc\"}\n" {
		t.Errorf("PrintJSON = %q", got)
	}
}

func TestPrintErrorFailure(t *testing.T) {
	var buf bytes.Buffer
	code := PrintError(&buf, &Failure{Code: 3, Err: "timeout", ID: "c1", Status: "initializing"})
	if code != 3 {
		t.Errorf("code = %d, want 3", code)
	}
	if got := buf.String(); got != "{\"error\":\"timeout\",\"id\":\"c1\",\"status\":\"initializing\"}\n" {
		t.Errorf("body = %q", got)
	}
}

func TestPrintErrorPlain(t *testing.T) {
	var buf bytes.Buffer
	code := PrintError(&buf, errors.New("unknown flag: --foo"))
	if code != 1 {
		t.Errorf("code = %d, want 1", code)
	}
	if got := buf.String(); got != "{\"error\":\"unknown flag: --foo\"}\n" {
		t.Errorf("body = %q", got)
	}
}

func TestPrintErrorWrapped(t *testing.T) {
	var buf bytes.Buffer
	wrapped := errors.Join(errors.New("context"), &Failure{Code: 2, Err: "missing_configuration", Detail: "x"})
	if code := PrintError(&buf, wrapped); code != 2 {
		t.Errorf("wrapped exit code = %d, want 2", code)
	}
}
