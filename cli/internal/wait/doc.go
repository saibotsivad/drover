// Package wait holds the bespoke polling loop used by the start/stop/destroy
// lifecycle commands: poll an endpoint on an interval until a done condition
// or a deadline derived from the orchestrator's transition_timeout_seconds.
// (Scaffolding only; the loop lands in a later phase.)
package wait
