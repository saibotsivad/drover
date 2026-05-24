// Package wait holds the bespoke polling loop used by the start/stop/destroy
// lifecycle commands.
package wait

import (
	"context"
	"errors"
	"time"
)

// ErrTimeout is returned when the deadline elapses before done reports true.
var ErrTimeout = errors.New("wait: deadline exceeded")

// Wait polls fetch until done reports true (success), the deadline passes
// (ErrTimeout), the context is cancelled (ctx.Err()), or fetch errors. fetch
// is always called at least once. interval is the sleep *between* requests, so
// request latency doesn't shorten the budget. On timeout the most recent
// fetched value is returned alongside ErrTimeout so callers can report the
// transitional status.
func Wait[T any](
	ctx context.Context,
	interval time.Duration,
	deadline time.Time,
	fetch func(context.Context) (T, error),
	done func(T) bool,
) (T, error) {
	if interval <= 0 {
		interval = time.Second
	}
	var last T
	for {
		v, err := fetch(ctx)
		if err != nil {
			return last, err
		}
		last = v
		if done(v) {
			return v, nil
		}
		if !time.Now().Before(deadline) {
			return last, ErrTimeout
		}
		select {
		case <-ctx.Done():
			return last, ctx.Err()
		case <-time.After(interval):
		}
	}
}
