// Command rebuild re-derives trace_index and/or agent_version_stats_daily from the
// deduplicated raw spans (data-model §3.7), e.g. after a replay or a partially applied
// insert double-counted a rollup. See internal/rebuild for how the query is derived.
//
//	rebuild [-table trace_index|agent_version_stats_daily|all] [-partition 202609,...]
//
// It refuses to run while the writer's consumer group has members: rows the views insert
// during a rebuild would be replaced away by the partition swap. Stop the writer first.
// Prints one JSON result per rebuilt partition and exits non-zero unless every one matches
// spans FINAL exactly.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/JCHETAN26/vigil/ingest/internal/config"
	"github.com/JCHETAN26/vigil/ingest/internal/rebuild"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "rebuild:", err)
		os.Exit(1)
	}
}

func run() error {
	table := flag.String("table", "all", "trace_index, agent_version_stats_daily, or all")
	parts := flag.String("partition", "", "comma-separated partitions of the rollup (YYYYMM); default: all")
	skipCheck := flag.Bool("skip-writer-check", false, "do not require the writer's consumer group to be idle")
	flag.Parse()

	targets := rebuild.Targets
	if *table != "all" {
		t, ok := rebuild.TargetByName(*table)
		if !ok {
			return fmt.Errorf("unknown table %q", *table)
		}
		targets = []rebuild.Target{t}
	}
	var partitions []string
	if *parts != "" {
		partitions = strings.Split(*parts, ",")
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Hour)
	defer cancel()
	if !*skipCheck {
		if err := requireIdleWriter(ctx); err != nil {
			return err
		}
	}
	ch := config.ClickHouseFromEnv()
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{ch.Addr},
		Auth: clickhouse.Auth{Database: ch.Database, Username: ch.Username, Password: ch.Password},
	})
	if err != nil {
		return err
	}
	defer conn.Close()

	exact := true
	enc := json.NewEncoder(os.Stdout)
	for _, t := range targets {
		p := partitions
		if t.ScopeExpr == "" {
			p = nil // unpartitioned: always rebuilt whole
		}
		results, err := rebuild.Run(ctx, conn, ch.Database, t, p)
		for _, r := range results {
			_ = enc.Encode(struct {
				rebuild.Result
				Exact bool `json:"exact"`
			}{r, r.Exact()})
			exact = exact && r.Exact()
		}
		if err != nil {
			return err
		}
	}
	if !exact {
		return fmt.Errorf("a rebuilt partition does not match spans FINAL")
	}
	return nil
}

// requireIdleWriter checks that the writer's consumer group has no members.
func requireIdleWriter(ctx context.Context) error {
	group := config.WriterFromEnv().Group
	cl, err := kgo.NewClient(kgo.SeedBrokers(config.KafkaFromEnv().Brokers...))
	if err != nil {
		return err
	}
	defer cl.Close()
	groups, err := kadm.NewClient(cl).DescribeGroups(ctx, group)
	if err != nil {
		return fmt.Errorf("describe writer group %s: %w", group, err)
	}
	if g, ok := groups[group]; ok && len(g.Members) > 0 {
		return fmt.Errorf("writer group %s has %d live member(s) (state %s): stop the writer before rebuilding",
			group, len(g.Members), g.State)
	}
	return nil
}
