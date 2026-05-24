package config

import (
	"errors"
	"testing"

	"github.com/saibotsivad/drover/cli/internal/output"
)

func TestLoad(t *testing.T) {
	tests := []struct {
		name     string
		url      string
		key      string
		wantErr  bool
		wantKind string
		wantBase string
		setURL   bool
		setKey   bool
	}{
		{name: "valid", url: "https://drover.example.com", key: "sk-1", setURL: true, setKey: true, wantBase: "https://drover.example.com"},
		{name: "trailing slash trimmed", url: "https://drover.example.com/", key: "sk-1", setURL: true, setKey: true, wantBase: "https://drover.example.com"},
		{name: "missing url", key: "sk-1", setKey: true, wantErr: true, wantKind: "missing_configuration"},
		{name: "missing key", url: "https://x", setURL: true, wantErr: true, wantKind: "missing_configuration"},
		{name: "blank url", url: "   ", key: "sk-1", setURL: true, setKey: true, wantErr: true, wantKind: "missing_configuration"},
		{name: "no scheme", url: "drover.example.com", key: "sk-1", setURL: true, setKey: true, wantErr: true, wantKind: "invalid_configuration"},
		{name: "ftp scheme", url: "ftp://x", key: "sk-1", setURL: true, setKey: true, wantErr: true, wantKind: "invalid_configuration"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("DROVER_API_URL", "")
			t.Setenv("DROVER_API_KEY", "")
			if tc.setURL {
				t.Setenv("DROVER_API_URL", tc.url)
			}
			if tc.setKey {
				t.Setenv("DROVER_API_KEY", tc.key)
			}

			cfg, err := Load()
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil")
				}
				var f *output.Failure
				if !errors.As(err, &f) {
					t.Fatalf("expected *output.Failure, got %T", err)
				}
				if f.ExitCode() != 2 {
					t.Errorf("exit code = %d, want 2", f.ExitCode())
				}
				if f.Err != tc.wantKind {
					t.Errorf("error kind = %q, want %q", f.Err, tc.wantKind)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if cfg.BaseURL != tc.wantBase {
				t.Errorf("BaseURL = %q, want %q", cfg.BaseURL, tc.wantBase)
			}
			if cfg.APIKey != tc.key {
				t.Errorf("APIKey = %q, want %q", cfg.APIKey, tc.key)
			}
		})
	}
}
