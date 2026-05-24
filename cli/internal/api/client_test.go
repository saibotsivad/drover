package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestClient(t *testing.T, h http.HandlerFunc) *Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return New(srv.URL, "sk-test")
}

func TestAuthAndAcceptHeaders(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer sk-test" {
			t.Errorf("Authorization = %q", got)
		}
		if got := r.Header.Get("Accept"); got != "application/json" {
			t.Errorf("Accept = %q", got)
		}
		w.Write([]byte("[]"))
	})
	if _, err := c.ListContainers(context.Background()); err != nil {
		t.Fatalf("ListContainers: %v", err)
	}
}

func TestErrorDetailString(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`{"detail":"container not found"}`))
	})
	_, err := c.GetContainer(context.Background(), "missing")
	ae, ok := err.(*Error)
	if !ok {
		t.Fatalf("err type = %T, want *Error", err)
	}
	if ae.Status != 404 || ae.Detail != "container not found" || ae.ExitCode() != 1 {
		t.Errorf("Error = %+v (exit %d)", ae, ae.ExitCode())
	}
	if !IsNotFound(err) {
		t.Errorf("IsNotFound = false, want true")
	}
}

func TestErrorDetailValidationList(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		w.Write([]byte(`{"detail":[{"loc":["body","image"],"msg":"field required"}]}`))
	})
	_, err := c.GetContainer(context.Background(), "x")
	ae := err.(*Error)
	if !strings.Contains(ae.Detail, "field required") {
		t.Errorf("detail = %q, want it to contain the validation message", ae.Detail)
	}
}

func TestErrorNonJSONBody(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte("upstream boom"))
	})
	_, err := c.GetContainer(context.Background(), "x")
	ae := err.(*Error)
	if ae.Detail != "upstream boom" {
		t.Errorf("detail = %q", ae.Detail)
	}
}

func TestTransportError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // close so the connection is refused
	c := New(url, "sk-test")
	_, err := c.ListContainers(context.Background())
	ae, ok := err.(*Error)
	if !ok || ae.Kind != "request_failed" {
		t.Fatalf("err = %v (%T), want request_failed Error", err, err)
	}
}
