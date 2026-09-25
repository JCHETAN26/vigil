package consumer

import "fmt"

// DedupToken builds the insert_deduplication_token for a batch from its source location:
// topic, partition, and the first/last Redpanda offset of the contiguous range. Because
// it is derived only from the offset range (never a wall-clock or size value), a replay
// that reconstructs the same range reproduces the same token, and ClickHouse drops the
// re-sent batch before any rows or dependent MV rows are written (§3.5). The topic guards
// against token reuse across topics.
func DedupToken(topic string, partition int32, firstOffset, lastOffset int64) string {
	return fmt.Sprintf("%s:%d:%d:%d", topic, partition, firstOffset, lastOffset)
}
