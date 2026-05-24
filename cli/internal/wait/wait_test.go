package wait

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestWaitDoneFirstCall(t *testing.T) {
	calls := 0
	v, err := Wait(context.Background(), time.Millisecond, time.Now().Add(time.Second),
		func(context.Context) (int, error) { calls++; return 7, nil },
		func(int) bool { return true },
	)
	if err != nil || v != 7 || calls != 1 {
		t.Fatalf("v=%d err=%v calls=%d", v, err, calls)
	}
}

func TestWaitDoneAfterSeveral(t *testing.T) {
	calls := 0
	v, err := Wait(context.Background(), time.Millisecond, time.Now().Add(time.Second),
		func(context.Context) (int, error) { calls++; return calls, nil },
		func(n int) bool { return n >= 3 },
	)
	if err != nil || v != 3 || calls != 3 {
		t.Fatalf("v=%d err=%v calls=%d", v, err, calls)
	}
}

func TestWaitTimeoutReturnsLast(t *testing.T) {
	v, err := Wait(context.Background(), time.Millisecond, time.Now(),
		func(context.Context) (string, error) { return "initializing", nil },
		func(string) bool { return false },
	)
	if !errors.Is(err, ErrTimeout) {
		t.Fatalf("err = %v, want ErrTimeout", err)
	}
	if v != "initializing" {
		t.Errorf("last value = %q", v)
	}
}

func TestWaitFetchError(t *testing.T) {
	boom := errors.New("boom")
	_, err := Wait(context.Background(), time.Millisecond, time.Now().Add(time.Second),
		func(context.Context) (int, error) { return 0, boom },
		func(int) bool { return true },
	)
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v, want boom", err)
	}
}

func TestWaitContextCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	calls := 0
	_, err := Wait(ctx, 10*time.Millisecond, time.Now().Add(time.Minute),
		func(context.Context) (int, error) {
			calls++
			if calls == 1 {
				cancel()
			}
			return 0, nil
		},
		func(int) bool { return false },
	)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
}
