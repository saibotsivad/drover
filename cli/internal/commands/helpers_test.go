package commands

import (
	"bytes"
	"flag"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

var update = flag.Bool("update", false, "update golden files")

// fakeOrchestrator starts an httptest server with the given handler and points
// the environment at it, so clientFromEnv builds a client aimed at the fake.
func fakeOrchestrator(t *testing.T, h http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	t.Setenv("DROVER_API_URL", srv.URL)
	t.Setenv("DROVER_API_KEY", "sk-test")
}

// runCmd builds a fresh command tree, runs it with the given args, and returns
// captured stdout, stderr, and the process exit code.
func runCmd(t *testing.T, args ...string) (stdout, stderr string, code int) {
	t.Helper()
	root := newRootCmd()
	var out, errBuf bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&errBuf)
	root.SetArgs(args)
	code = execute(root)
	return out.String(), errBuf.String(), code
}

// golden compares got against testdata/<name>.golden, rewriting it when
// -update is passed (Phase 0 goldenfile policy).
func golden(t *testing.T, name, got string) {
	t.Helper()
	path := filepath.Join("testdata", name+".golden")
	if *update {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden %s: %v (run `go test -run %s -update`)", path, err, t.Name())
	}
	if got != string(want) {
		t.Errorf("output mismatch for %s:\n got: %q\nwant: %q", name, got, want)
	}
}
