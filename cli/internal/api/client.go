package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"time"
)

// Client is a thin HTTP client over the orchestrator REST API. Construct it
// with New; every method takes a context so polling and exec streams can be
// cancelled.
type Client struct {
	baseURL string
	apiKey  string
	http    *http.Client
}

// New returns a Client for the given base URL and API key. The base URL must
// not have a trailing slash (config.Load already trims it).
func New(baseURL, apiKey string) *Client {
	return &Client{
		baseURL: baseURL,
		apiKey:  apiKey,
		http:    &http.Client{Timeout: 60 * time.Second},
	}
}

// do issues a request and returns the response body for 2xx, or an *Error
// for transport failures and non-2xx responses.
func (c *Client) do(ctx context.Context, method, path string, body any) ([]byte, error) {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, &Error{Kind: "request_failed", Detail: err.Error()}
		}
		reader = bytes.NewReader(b)
	}

	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return nil, &Error{Kind: "request_failed", Detail: err.Error()}
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, &Error{Kind: "request_failed", Detail: err.Error()}
	}
	defer func() { _ = resp.Body.Close() }()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, &Error{Kind: "request_failed", Detail: err.Error()}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, parseError(resp.StatusCode, data)
	}
	return data, nil
}
