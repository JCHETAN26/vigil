package consumer

import "testing"

func TestDedupToken(t *testing.T) {
	got := DedupToken("otlp.spans.raw", 3, 100, 199)
	want := "otlp.spans.raw:3:100:199"
	if got != want {
		t.Errorf("DedupToken = %q, want %q", got, want)
	}

	// The token is a pure function of (topic, partition, first, last): the same range
	// reproduced on replay yields the same token.
	if DedupToken("otlp.spans.raw", 3, 100, 199) != got {
		t.Error("DedupToken is not deterministic for the same range")
	}
	// A different range must produce a different token.
	if DedupToken("otlp.spans.raw", 3, 100, 200) == got {
		t.Error("DedupToken collided for different ranges")
	}
}
