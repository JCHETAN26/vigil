// Package schema holds the ClickHouse DDL for the ingestion path and exposes it as an
// ordered list of statements to apply. The .sql files are the source of truth; the
// migrate command executes them in filename order, idempotently (every statement uses
// IF NOT EXISTS).
package schema

import (
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strings"
)

//go:embed *.sql
var files embed.FS

// Statement is a single DDL statement tagged with its source file, for logging.
type Statement struct {
	File string
	SQL  string
}

// Statements returns every DDL statement across the embedded .sql files, in filename
// order and, within a file, in source order.
//
// Comments are stripped first (whole-line and trailing "--" comments), then the
// remaining SQL is split on ';'. Stripping before splitting matters: a ';' inside a
// comment must not be mistaken for a statement terminator. The DDL contains no ';' or
// "--" inside string literals, so this simple approach is safe here.
func Statements() ([]Statement, error) {
	names, err := fs.Glob(files, "*.sql")
	if err != nil {
		return nil, err
	}
	sort.Strings(names)

	var out []Statement
	for _, name := range names {
		b, err := files.ReadFile(name)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", name, err)
		}
		for _, stmt := range strings.Split(stripComments(string(b)), ";") {
			if s := strings.TrimSpace(stmt); s != "" {
				out = append(out, Statement{File: name, SQL: s})
			}
		}
	}
	return out, nil
}

// stripComments removes "--" line comments (whole-line and trailing) from SQL, keeping
// the code portion of each line. It does not attempt to honor "--" inside string
// literals, which the ingest DDL never contains.
func stripComments(sql string) string {
	lines := strings.Split(sql, "\n")
	for i, line := range lines {
		if idx := strings.Index(line, "--"); idx >= 0 {
			lines[i] = line[:idx]
		}
	}
	return strings.Join(lines, "\n")
}
