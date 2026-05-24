package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
)

// ListImages returns the raw JSON array from GET /images.
func (c *Client) ListImages(ctx context.Context) (json.RawMessage, error) {
	return c.do(ctx, http.MethodGet, "/images", nil)
}

// GetImage returns the raw JSON object from GET /images/{name}.
func (c *Client) GetImage(ctx context.Context, name string) (json.RawMessage, error) {
	return c.do(ctx, http.MethodGet, "/images/"+url.PathEscape(name), nil)
}
