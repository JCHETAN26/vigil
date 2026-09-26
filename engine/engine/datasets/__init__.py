"""Dataset adapters: one interface so tau-bench, HotpotQA, and BFCL plug in identically
(design doc §5). Week 2 ships the interface plus a local JSON adapter for hand-written cases;
the real dataset adapters come later."""

from .base import Case, DatasetAdapter, get_adapter, register_adapter
from .local import LocalJSONAdapter

__all__ = ["Case", "DatasetAdapter", "get_adapter", "register_adapter", "LocalJSONAdapter"]
