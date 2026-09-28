package loadgen

import (
	"bufio"
	"errors"
	"io"
	"os"

	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/encoding/protodelim"
)

// WriteTemplates writes one length-delimited TracesData per recorded trace.
func WriteTemplates(path string, traces []*tracepb.TracesData) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	w := bufio.NewWriter(f)
	for _, td := range traces {
		if _, err := protodelim.MarshalTo(w, td); err != nil {
			f.Close()
			return err
		}
	}
	if err := w.Flush(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// ReadTemplates reads a file written by WriteTemplates.
func ReadTemplates(path string) ([]*tracepb.TracesData, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	r := bufio.NewReader(f)
	var out []*tracepb.TracesData
	for {
		td := &tracepb.TracesData{}
		err := protodelim.UnmarshalFrom(r, td)
		if errors.Is(err, io.EOF) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		out = append(out, td)
	}
}
