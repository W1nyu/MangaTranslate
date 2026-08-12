"""Monthly spend guard for the paid translation fallback.

A free tier stops on its own: DeepL Free answers 456 and Azure F0 answers 403,
and the reader simply sees that translation stopped. A paid key has no such
edge. Once the fallback is allowed, an ordinary evening of scrolling can spend
real money without anything in the flow saying so.

This ledger is that missing edge. It counts the characters actually handed to a
paid engine, refuses the request that would cross the configured monthly limit,
and keeps the count on disk so restarting the server does not hand the reader a
fresh budget.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable

from .runtime import LOCAL_SERVER_ROOT


#: Enough for a long reading month on either paid plan while still being a
#: recognisable ceiling rather than an open tab.
DEFAULT_MONTHLY_CHARACTER_LIMIT = 500_000
USAGE_FILE_NAME = ".paid-usage.json"


def configured_monthly_limit() -> int:
    """Read ``PAID_MONTHLY_CHAR_LIMIT``; ``0`` means the reader removed the cap."""
    raw = str(os.environ.get("PAID_MONTHLY_CHAR_LIMIT", "")).strip()
    if not raw:
        return DEFAULT_MONTHLY_CHARACTER_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        # A typo in .env must not silently become "unlimited".
        return DEFAULT_MONTHLY_CHARACTER_LIMIT


def _month_key(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


class PaidUsageLedger:
    """Characters sent to a paid engine in the current calendar month."""

    def __init__(
        self,
        path: Path | str | None = None,
        monthly_limit: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else LOCAL_SERVER_ROOT / USAGE_FILE_NAME
        self._monthly_limit = configured_monthly_limit() if monthly_limit is None else max(0, int(monthly_limit))
        # Local time, because the month the reader is billed for is the month
        # they see on their own calendar.
        self._clock = clock or datetime.now
        self._lock = Lock()
        self._loaded = False
        self._month = ""
        self._used = 0
        self._by_provider: dict[str, int] = {}

    @property
    def monthly_limit(self) -> int:
        return self._monthly_limit

    @property
    def limited(self) -> bool:
        return self._monthly_limit > 0

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # A missing or hand-edited ledger starts the month at zero rather
            # than preventing translation entirely.
            stored = {}
        if not isinstance(stored, dict):
            return
        self._month = str(stored.get("month", ""))
        try:
            self._used = max(0, int(stored.get("usedCharacters", 0)))
        except (TypeError, ValueError):
            self._used = 0
        by_provider = stored.get("byProvider")
        if isinstance(by_provider, dict):
            for code, value in by_provider.items():
                try:
                    self._by_provider[str(code)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue

    def _rollover_locked(self) -> None:
        month = _month_key(self._clock())
        if month != self._month:
            self._month = month
            self._used = 0
            self._by_provider = {}

    def _save_locked(self) -> None:
        payload = {
            "month": self._month,
            "usedCharacters": self._used,
            "byProvider": dict(self._by_provider),
        }
        try:
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            # Losing the on-disk copy is worth a slightly generous budget; it is
            # not worth failing a translation the reader is waiting for.
            pass

    def _ready_locked(self) -> None:
        self._load_locked()
        self._rollover_locked()

    def remaining(self) -> int | None:
        """Characters still allowed this month, or ``None`` when uncapped."""
        if not self.limited:
            return None
        with self._lock:
            self._ready_locked()
            return max(0, self._monthly_limit - self._used)

    def can_spend(self, characters: int) -> bool:
        if not self.limited:
            return True
        with self._lock:
            self._ready_locked()
            return self._used + max(0, int(characters)) <= self._monthly_limit

    def record(self, provider: str, characters: int) -> None:
        amount = max(0, int(characters))
        if not amount:
            return
        with self._lock:
            self._ready_locked()
            self._used += amount
            self._by_provider[str(provider)] = self._by_provider.get(str(provider), 0) + amount
            self._save_locked()

    def snapshot(self) -> dict[str, object]:
        """Usage figures for the popup. No key material is involved."""
        with self._lock:
            self._ready_locked()
            remaining = max(0, self._monthly_limit - self._used) if self.limited else None
            return {
                "month": self._month,
                "usedCharacters": self._used,
                "monthlyLimit": self._monthly_limit,
                "limited": self.limited,
                "remainingCharacters": remaining,
                "byProvider": dict(self._by_provider),
            }
