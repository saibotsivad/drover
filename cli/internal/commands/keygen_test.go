package commands

import (
	"strings"
	"testing"
)

func TestKeygen(t *testing.T) {
	out, errOut, code := runCmd(t, "keygen")
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, errOut)
	}
	if errOut != "" {
		t.Errorf("stderr = %q, want empty", errOut)
	}
	if !strings.Contains(out, "Drover Server Key:") {
		t.Errorf("output missing 'Drover Server Key:'\n%s", out)
	}
	if !strings.Contains(out, "Drover API Header:") {
		t.Errorf("output missing 'Drover API Header:'\n%s", out)
	}
	if !strings.Contains(out, "Drover CLI Key:") {
		t.Errorf("output missing 'Drover CLI Key:'\n%s", out)
	}
	if !strings.Contains(out, "DROVER_API_KEY=") {
		t.Errorf("output missing 'DROVER_API_KEY='\n%s", out)
	}
	if !strings.Contains(out, "Authorization: Bearer ") {
		t.Errorf("output missing 'Authorization: Bearer'\n%s", out)
	}
	if !strings.Contains(out, "export DROVER_API_KEY=") {
		t.Errorf("output missing 'export DROVER_API_KEY='\n%s", out)
	}
}

func TestKeygenUniqueEachRun(t *testing.T) {
	out1, _, _ := runCmd(t, "keygen")
	out2, _, _ := runCmd(t, "keygen")
	if out1 == out2 {
		t.Error("keygen produced identical output on two runs; expected unique keys")
	}
}

func TestKeygenNoArgs(t *testing.T) {
	_, errOut, code := runCmd(t, "keygen", "--bogus")
	if code == 0 {
		t.Error("exit = 0, want non-zero for unknown flag")
	}
	if !strings.Contains(errOut, "unknown flag") {
		t.Errorf("stderr = %q, want unknown flag error", errOut)
	}
}
