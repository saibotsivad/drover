// Package ws handles WebSocket streaming of exec output: it dials the
// per-worker endpoint, filters frames by command_id, and passes matching
// frames through unchanged. (Scaffolding only; the stream lands in a later
// phase.)
package ws
