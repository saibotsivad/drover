package commands

import (
	"net/http"
	"strings"
	"testing"
)

func TestPs(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/workers" {
			t.Errorf("path = %s", r.URL.Path)
		}
		w.Write([]byte(`[{"id":"c1","image":"img","status":"running"}]`))
	})
	out, errOut, code := runCmd(t, "ps")
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, errOut)
	}
	golden(t, "ps", out)
}

func TestImages(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[{"name":"alpine","tags":["3.20"]}]`))
	})
	out, _, code := runCmd(t, "images")
	if code != 0 {
		t.Fatalf("exit = %d", code)
	}
	golden(t, "images", out)
}

func TestImageDetail(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/images/alpine" {
			t.Errorf("path = %s", r.URL.Path)
		}
		w.Write([]byte(`{"name":"alpine","id":"sha256:abc","os":"linux"}`))
	})
	out, _, code := runCmd(t, "image", "alpine")
	if code != 0 {
		t.Fatalf("exit = %d", code)
	}
	golden(t, "image", out)
}

func TestPsAPIError(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte(`{"detail":"db down"}`))
	})
	out, errOut, code := runCmd(t, "ps")
	if code != 1 {
		t.Errorf("exit = %d, want 1", code)
	}
	if out != "" {
		t.Errorf("stdout = %q, want empty on error", out)
	}
	if !strings.Contains(errOut, `"error":"api_error"`) || !strings.Contains(errOut, "db down") {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestMissingConfigExit2(t *testing.T) {
	t.Setenv("DROVER_API_URL", "")
	t.Setenv("DROVER_API_KEY", "")
	out, errOut, code := runCmd(t, "ps")
	if code != 2 {
		t.Errorf("exit = %d, want 2", code)
	}
	if out != "" {
		t.Errorf("stdout = %q, want empty", out)
	}
	if !strings.Contains(errOut, "missing_configuration") {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestUnknownFlagIsError(t *testing.T) {
	fakeOrchestrator(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`[]`))
	})
	_, errOut, code := runCmd(t, "ps", "--bogus")
	if code != 1 {
		t.Errorf("exit = %d, want 1", code)
	}
	if !strings.Contains(errOut, "unknown flag") {
		t.Errorf("stderr = %q, want unknown flag error", errOut)
	}
}
