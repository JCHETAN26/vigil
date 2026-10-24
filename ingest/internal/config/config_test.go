package config

import (
	"testing"
	"time"
)

func TestWriterFromEnv(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
		want func(Writer) bool
	}{
		{
			name: "defaults",
			want: func(w Writer) bool {
				return w.MaxInflightBytes == 128<<20 &&
					w.RetryInitialBackoff == 100*time.Millisecond &&
					w.RetryMaxBackoff == 5*time.Second &&
					w.RetryWindow == 25*time.Second
			},
		},
		{
			name: "overrides",
			env: map[string]string{
				"VIGIL_WRITER_MAX_INFLIGHT_BYTES":    "1048576",
				"VIGIL_WRITER_RETRY_INITIAL_BACKOFF": "50ms",
				"VIGIL_WRITER_RETRY_MAX_BACKOFF":     "1s",
				"VIGIL_WRITER_RETRY_WINDOW":          "10s",
			},
			want: func(w Writer) bool {
				return w.MaxInflightBytes == 1<<20 &&
					w.RetryInitialBackoff == 50*time.Millisecond &&
					w.RetryMaxBackoff == time.Second &&
					w.RetryWindow == 10*time.Second
			},
		},
		{
			name: "zero disables the inflight budget",
			env:  map[string]string{"VIGIL_WRITER_MAX_INFLIGHT_BYTES": "0"},
			want: func(w Writer) bool { return w.MaxInflightBytes == 0 },
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			for _, k := range []string{
				"VIGIL_WRITER_MAX_INFLIGHT_BYTES", "VIGIL_WRITER_RETRY_INITIAL_BACKOFF",
				"VIGIL_WRITER_RETRY_MAX_BACKOFF", "VIGIL_WRITER_RETRY_WINDOW",
			} {
				t.Setenv(k, "")
			}
			for k, v := range tt.env {
				t.Setenv(k, v)
			}
			if w := WriterFromEnv(); !tt.want(w) {
				t.Errorf("WriterFromEnv() = %+v", w)
			}
		})
	}
}
