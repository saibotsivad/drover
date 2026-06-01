package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
)

// ListWorkers returns the raw JSON array from GET /workers, passed
// through unchanged for display.
func (c *Client) ListWorkers(ctx context.Context) (json.RawMessage, error) {
	return c.do(ctx, http.MethodGet, "/workers", nil)
}

// GetWorker fetches a single worker by exact id.
func (c *Client) GetWorker(ctx context.Context, id string) (*WorkerResult, error) {
	data, err := c.do(ctx, http.MethodGet, "/workers/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	return decodeWorker(data)
}

// CreateWorker maps to POST /workers.
func (c *Client) CreateWorker(ctx context.Context, req CreateWorkerRequest) (*WorkerResult, error) {
	data, err := c.do(ctx, http.MethodPost, "/workers", req)
	if err != nil {
		return nil, err
	}
	return decodeWorker(data)
}

// StopWorker maps to POST /workers/{id}/stop.
func (c *Client) StopWorker(ctx context.Context, id string) (*WorkerResult, error) {
	data, err := c.do(ctx, http.MethodPost, "/workers/"+url.PathEscape(id)+"/stop", nil)
	if err != nil {
		return nil, err
	}
	return decodeWorker(data)
}

// DestroyWorker maps to DELETE /workers/{id}. Destroy is a DELETE, not a
// POST.
func (c *Client) DestroyWorker(ctx context.Context, id string) (*WorkerResult, error) {
	data, err := c.do(ctx, http.MethodDelete, "/workers/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	return decodeWorker(data)
}

func decodeWorker(data []byte) (*WorkerResult, error) {
	var w Worker
	if err := json.Unmarshal(data, &w); err != nil {
		return nil, &Error{Kind: "decode_error", Detail: err.Error()}
	}
	return &WorkerResult{Worker: w, Raw: data}, nil
}
