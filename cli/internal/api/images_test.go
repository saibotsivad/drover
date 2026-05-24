package api

import (
	"context"
	"net/http"
	"testing"
)

func TestListImagesPassthrough(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/images" {
			t.Errorf("path = %s", r.URL.Path)
		}
		w.Write([]byte(`[{"name":"img","extra":true}]`))
	})
	raw, err := c.ListImages(context.Background())
	if err != nil {
		t.Fatalf("ListImages: %v", err)
	}
	if string(raw) != `[{"name":"img","extra":true}]` {
		t.Errorf("raw = %s", raw)
	}
}

func TestGetImagePathEscape(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/images/my/img" {
			t.Errorf("path = %q, want /images/my/img", r.URL.Path)
		}
		w.Write([]byte(`{"name":"my/img"}`))
	})
	if _, err := c.GetImage(context.Background(), "my/img"); err != nil {
		t.Fatalf("GetImage: %v", err)
	}
}
