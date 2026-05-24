package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
)

// ListContainers returns the raw JSON array from GET /containers, passed
// through unchanged for display.
func (c *Client) ListContainers(ctx context.Context) (json.RawMessage, error) {
	return c.do(ctx, http.MethodGet, "/containers", nil)
}

// GetContainer fetches a single container by exact id.
func (c *Client) GetContainer(ctx context.Context, id string) (*ContainerResult, error) {
	data, err := c.do(ctx, http.MethodGet, "/containers/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	return decodeContainer(data)
}

// CreateContainer maps to POST /containers.
func (c *Client) CreateContainer(ctx context.Context, req CreateContainerRequest) (*ContainerResult, error) {
	data, err := c.do(ctx, http.MethodPost, "/containers", req)
	if err != nil {
		return nil, err
	}
	return decodeContainer(data)
}

// StopContainer maps to POST /containers/{id}/stop.
func (c *Client) StopContainer(ctx context.Context, id string) (*ContainerResult, error) {
	data, err := c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/stop", nil)
	if err != nil {
		return nil, err
	}
	return decodeContainer(data)
}

// DestroyContainer maps to DELETE /containers/{id}. Destroy is a DELETE, not a
// POST.
func (c *Client) DestroyContainer(ctx context.Context, id string) (*ContainerResult, error) {
	data, err := c.do(ctx, http.MethodDelete, "/containers/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	return decodeContainer(data)
}

func decodeContainer(data []byte) (*ContainerResult, error) {
	var ct Container
	if err := json.Unmarshal(data, &ct); err != nil {
		return nil, &Error{Kind: "decode_error", Detail: err.Error()}
	}
	return &ContainerResult{Container: ct, Raw: data}, nil
}
