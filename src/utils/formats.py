from __future__ import annotations

from typing import Optional, Sequence


class plural:
    def __init__(self, value: int, return_count: Optional[bool] = True):
        self.value: int = value
        self.return_count = return_count

    def __format__(self, format_spec: str) -> str:
        v = self.value
        singular, _, plural = format_spec.partition("|")
        plural = plural or f"{singular}s"
        if abs(v) != 1:
            return f"{v} {plural}" if self.return_count else plural
        return f"{v} {singular}" if self.return_count else singular


def human_join(seq: Sequence[str], delim: str = ", ", final: str = "or") -> str:
    size = len(seq)
    if size == 0:
        return ""

    if size == 1:
        return seq[0]

    if size == 2:
        return f"{seq[0]} {final} {seq[1]}"

    return delim.join(seq[:-1]) + f" {final} {seq[-1]}"
