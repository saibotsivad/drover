package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
)

// CreateExec posts a command to POST /containers/{id}/execs and returns the
// command_id the WebSocket stream is then filtered by.
func (c *Client) CreateExec(ctx context.Context, id, command string) (string, error) {
	data, err := c.do(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/execs", ExecRequest{Command: command})
	if err != nil {
		return "", err
	}
	var r ExecResponse
	if err := json.Unmarshal(data, &r); err != nil {
		return "", &APIError{Kind: "decode_error", Detail: err.Error()}
	}
	return r.CommandID, nil
}
