"""Execution context: stands in for n8n's $('Node name'), $input and the clock."""
from __future__ import annotations
import datetime as _dt
from typing import Any


class NodeRef:
    def __init__(self, name: str, items: list[dict] | None):
        if items is None:
            raise KeyError(f"node '{name}' has not run")
        self.name, self.items = name, items

    def first(self) -> dict:
        return self.items[0] if self.items else {}

    def all(self) -> list[dict]:
        return self.items

    def item_matching(self, i: int) -> dict | None:
        return self.items[i] if 0 <= i < len(self.items) else None


class Ctx:
    """outputs: node name -> list of JSON dicts (what each node returned)."""

    def __init__(self, now: _dt.datetime | None = None, outputs: dict[str, list[dict]] | None = None, config: dict | None = None):
        self.now = now or _dt.datetime.now(_dt.timezone.utc)
        self.outputs: dict[str, list[dict]] = outputs or {}
        self.config: dict[str, Any] = config or {}

    def node(self, name: str) -> NodeRef:
        return NodeRef(name, self.outputs.get(name))

    def has(self, name: str) -> bool:
        return name in self.outputs

    def set(self, name: str, items: list[dict]) -> list[dict]:
        self.outputs[name] = items
        return items

    def now_iso(self) -> str:
        return self.now.astimezone(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f"{self.now.microsecond // 1000:03d}Z"
