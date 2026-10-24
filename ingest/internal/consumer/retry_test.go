package consumer

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestRetryBackoff(t *testing.T) {
	p := retryPolicy{initial: 100 * time.Millisecond, max: time.Second, window: 10 * time.Second}
	tests := []struct {
		failed int
		want   time.Duration
	}{
		{1, 100 * time.Millisecond},
		{2, 200 * time.Millisecond},
		{3, 400 * time.Millisecond},
		{4, 800 * time.Millisecond},
		{5, time.Second}, // capped
		{50, time.Second},
	}
	for _, tt := range tests {
		if got := p.backoff(tt.failed); got != tt.want {
			t.Errorf("backoff(%d) = %s, want %s", tt.failed, got, tt.want)
		}
	}
}

func TestNewRetryPolicy(t *testing.T) {
	tests := []struct {
		name                 string
		initial, max, window time.Duration
		want                 retryPolicy
		wantErr              bool
	}{
		{name: "zero values use defaults",
			want: retryPolicy{initial: defaultRetryInitial, max: defaultRetryMax, window: defaultRetryWindow}},
		{name: "explicit", initial: 10 * time.Millisecond, max: time.Second, window: 5 * time.Second,
			want: retryPolicy{initial: 10 * time.Millisecond, max: time.Second, window: 5 * time.Second}},
		{name: "max raised to initial", initial: 2 * time.Second, max: time.Second, window: 5 * time.Second,
			want: retryPolicy{initial: 2 * time.Second, max: 2 * time.Second, window: 5 * time.Second}},
		{name: "window at half the rebalance timeout rejected", window: rebalanceTimeout / 2, wantErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := newRetryPolicy(tt.initial, tt.max, tt.window)
			if (err != nil) != tt.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, tt.wantErr)
			}
			if !tt.wantErr && got != tt.want {
				t.Errorf("policy = %+v, want %+v", got, tt.want)
			}
		})
	}
}

func TestRetryDo(t *testing.T) {
	fail := errors.New("boom")
	p := retryPolicy{initial: time.Millisecond, max: 2 * time.Millisecond, window: time.Second}

	t.Run("succeeds after failures", func(t *testing.T) {
		calls, seen := 0, 0
		err := p.do(context.Background(), func(context.Context) error {
			calls++
			if calls < 3 {
				return fail
			}
			return nil
		}, func(int, error) { seen++ })
		if err != nil || calls != 3 || seen != 2 {
			t.Errorf("err=%v calls=%d onFail=%d, want nil/3/2", err, calls, seen)
		}
	})
	t.Run("deadline wraps last error", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
		defer cancel()
		err := p.do(ctx, func(context.Context) error { return fail }, nil)
		if !errors.Is(err, fail) || !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("err = %v, want both the op error and DeadlineExceeded", err)
		}
	})
	t.Run("cancel during backoff returns promptly", func(t *testing.T) {
		slow := retryPolicy{initial: time.Hour, max: time.Hour, window: time.Hour}
		ctx, cancel := context.WithCancel(context.Background())
		time.AfterFunc(10*time.Millisecond, cancel)
		start := time.Now()
		err := slow.do(ctx, func(context.Context) error { return fail }, nil)
		if !errors.Is(err, context.Canceled) || time.Since(start) > 2*time.Second {
			t.Errorf("err=%v after %s, want prompt context.Canceled", err, time.Since(start))
		}
	})
}
