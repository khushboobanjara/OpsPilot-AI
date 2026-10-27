"""Budgets for guest (public demo) sessions. In memory, per process.

A guest may ask the AI assistant and search the documents; both cost real resources (Groq tokens, CPU). So each guest session has a
small allowance, and all guests together share a hard daily ceiling on assistant questions. Starting a new guest session is itself
rate limited (see auth_routes.py), which is what stops someone from simply restarting to get a new allowance.

With several worker processes each one keeps its own counters (the limits become per worker): run one worker for a public demo,
or put a rate limit at the proxy as well. Everything here is a no-op for real users.
"""
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.config import settings

MAX_TRACKED_SESSIONS = 20_000


class GuestBudget:
    def __init__(self, clock=lambda: datetime.now(timezone.utc)):
        self.clock = clock
        self._lock = threading.Lock()
        self._used: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._day: str | None = None
        self._day_chat = 0

    def reset(self) -> None:
        with self._lock:
            self._used.clear()
            self._day, self._day_chat = None, 0

    def _seconds_to_midnight(self, now: datetime) -> int:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))

    def charge(self, name: str, kind: str) -> None:
        """Use one unit of `kind` ('chat' or 'search') for guest `name`, or raise HTTP 429."""
        per_session = settings.guest_chat_per_session if kind == "chat" else settings.guest_search_per_session
        now = self.clock()
        with self._lock:
            today = now.strftime("%Y-%m-%d")
            if self._day != today:
                self._day, self._day_chat = today, 0
            if kind == "chat" and self._day_chat >= settings.guest_chat_per_day_global:
                raise HTTPException(429, "The public demo has reached its daily limit for AI questions. Please come back tomorrow.",
                                    headers={"Retry-After": str(self._seconds_to_midnight(now))})
            key = (name, kind)
            used = self._used.get(key, 0)
            if used >= per_session:
                raise HTTPException(429, f"This demo session has used its {per_session} {'AI questions' if kind == 'chat' else 'document searches'}. "
                                         "You can still browse everything else.")
            self._used[key] = used + 1
            self._used.move_to_end(key)
            while len(self._used) > MAX_TRACKED_SESSIONS:          # bounded memory: forget the oldest sessions first
                self._used.popitem(last=False)
            if kind == "chat":
                self._day_chat += 1


budget = GuestBudget()


def charge(principal, kind: str) -> None:
    """Call before doing the expensive work. Does nothing unless the caller is a guest."""
    if getattr(principal, "guest", False):
        budget.charge(principal.username, kind)
