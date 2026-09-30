# Long-uptime result and recovery

## The degraded instance (not restarted since the soak's clean start)

- ClickHouse uptime 12.21 h, tracked memory 1.16 GiB against the 1.5 GiB cap, 161395 memory-limit errors since start.
- **Throughput with zero offered load: 0 spans stored in 5 min**, while the load writer restarted 6 times; backlog 425,913 records.
- Snapshot queries that themselves failed on memory: 0 (none).
- The long-uptime ramp was not run: this instance cannot drain even with no load, so every rate fails, and the ramp would have discarded the soak backlog.

## Recovery: restart ClickHouse only (Redpanda backlog kept)

- Retention check before restart: no risk (smallest consumed-but-retained cushion 9,742 records; the load receiver was stopped so nothing could write to the topic).
- Restarting ClickHouse alone: **no progress** in the 37.2 min before the intervention below.
- **Intervention at +37.2 min:** raised the load writer's memory limit 512 MiB -> 2 GiB (docker update; restored to 512 MiB afterwards) (the writer was OOM-killed (exit 137) about every 45 s while draining the backlog at 512 MiB). After it: first stored rows in **0.4 min**, backlog drained in **12.7 min**; peak writer memory **1012 MiB**.
- Records deleted by retention before being consumed, at any point: **0**.
