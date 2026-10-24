package consumer

import (
	"context"
	"fmt"
	"time"
)

// Retry defaults, used when the corresponding config.Writer field is zero.
const (
	defaultRetryInitial = 100 * time.Millisecond
	defaultRetryMax     = 5 * time.Second
	defaultRetryWindow  = 25 * time.Second

	// rebalanceTimeout is set explicitly on the group client (it matches franz-go's default)
	// so the retry window can be checked against it. With BlockRebalanceOnPoll a rebalance
	// first waits for the current poll iteration (one window) and then runs the revoke
	// callback (another window), so the window must stay below half of it.
	rebalanceTimeout = 60 * time.Second
)

// retryPolicy is capped exponential backoff. The overall bound is the caller's context
// deadline (flushLocked applies window); there is no jitter, since each partition has a
// single writer and the attempt schedule is easier to reason about when deterministic.
type retryPolicy struct {
	initial time.Duration
	max     time.Duration
	window  time.Duration
}

func newRetryPolicy(initial, max, window time.Duration) (retryPolicy, error) {
	p := retryPolicy{initial: initial, max: max, window: window}
	if p.initial <= 0 {
		p.initial = defaultRetryInitial
	}
	if p.max <= 0 {
		p.max = defaultRetryMax
	}
	if p.max < p.initial {
		p.max = p.initial
	}
	if p.window <= 0 {
		p.window = defaultRetryWindow
	}
	if 2*p.window >= rebalanceTimeout {
		return p, fmt.Errorf("writer retry window %s must be below half the rebalance timeout %s", p.window, rebalanceTimeout)
	}
	return p, nil
}

// backoff returns the sleep before attempt n+1 (n >= 1 failed attempts so far).
func (p retryPolicy) backoff(n int) time.Duration {
	d := p.initial
	for i := 1; i < n && d < p.max; i++ {
		d *= 2
	}
	return min(d, p.max)
}

// do calls op until it succeeds or ctx is done. Every attempt receives ctx, so a hung
// attempt is also bounded by it. onFail (may be nil) sees each failed attempt. On give-up
// the error wraps both ctx's error (Canceled or DeadlineExceeded) and the last op error.
func (p retryPolicy) do(ctx context.Context, op func(context.Context) error, onFail func(attempt int, err error)) error {
	for attempt := 1; ; attempt++ {
		err := op(ctx)
		if err == nil {
			return nil
		}
		if onFail != nil {
			onFail(attempt, err)
		}
		if ctx.Err() != nil {
			return fmt.Errorf("gave up after %d attempts (%w): %w", attempt, ctx.Err(), err)
		}
		t := time.NewTimer(p.backoff(attempt))
		select {
		case <-ctx.Done():
			t.Stop()
			return fmt.Errorf("gave up after %d attempts (%w): %w", attempt, ctx.Err(), err)
		case <-t.C:
		}
	}
}
