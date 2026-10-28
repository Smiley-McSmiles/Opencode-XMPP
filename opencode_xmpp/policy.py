"""Sender identity, tiering and the one-session-per-contact rule.

XMPP JIDs are compared in their bare, case-folded form (XEP-0165 style
simplification): no resource, node and domain lowercased. This is what stops
``owner@example.org/phone`` from being treated as a stranger while
``owner@example.org`` is the owner.

Session ids are *derived*, never remembered: ``opencode run --session <id>``
creates the session if it does not exist and continues it if it does. Deriving
the id from the bot's own JID and the contact's bare JID means a restart, a lost
state file or a reinstall cannot fork a person into a second conversation, and
every device of that person lands in the same transcript. Bumping
``policy.session_id_version`` gives every contact a clean conversation without
deleting anything.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

OWNER = "owner"
GUEST = "guest"

SESSION_PREFIX = "ses_"
SESSION_ID_CHARS = 26


def bare_jid(jid: str) -> str:
    """Return ``node@domain`` with the resource stripped and case folded."""
    text = (jid or "").strip()
    if not text:
        raise ValueError("empty JID")
    text = text.split("/", 1)[0]
    if "@" not in text:
        # A server JID (no node) is never a person; keep it distinct and folded.
        return text.lower()
    node, _, domain = text.rpartition("@")
    return f"{node.lower()}@{domain.lower()}"


def tier_for(jid: str, owners: tuple[str, ...]) -> str:
    """Classify a sender as OWNER or GUEST. Unknown senders are guests."""
    sender = bare_jid(jid)
    for owner in owners:
        try:
            if bare_jid(owner) == sender:
                return OWNER
        except ValueError:
            continue
    return GUEST


def session_id_for(bot_jid: str, sender_jid: str, version: str = "1",
                   scope: str = "") -> str:
    """The opencode session this contact uses with this bot.

    ``ses_`` plus 26 hex characters: the same shape as an opencode session id,
    and a pure function of the bot JID, the contact's *bare* JID, the configured
    version and an optional model/card scope. No state to lose. A scope keeps
    each selected model's session separate, so switching models does not try to
    continue a session whose stored model is unavailable.
    """
    material = f"{bare_jid(bot_jid)}|{bare_jid(sender_jid)}|v{version}"
    if scope:
        material += f"|{scope}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return SESSION_PREFIX + digest[:SESSION_ID_CHARS]


def is_ignored(jid: str, ignore_jids: tuple[str, ...]) -> bool:
    """True when this sender is on the do-not-answer list (other bridge bots)."""
    sender = bare_jid(jid)
    for entry in ignore_jids:
        try:
            if bare_jid(entry) == sender:
                return True
        except ValueError:
            continue
    return False


@dataclass
class RateLimiter:
    """Per-sender token bucket: ``limit`` messages per rolling minute."""

    limit: int = 8
    window_s: float = 60.0

    def __post_init__(self) -> None:
        self._sent: dict[str, list[float]] = {}

    def allow(self, jid: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        key = bare_jid(jid)
        stamps = [t for t in self._sent.get(key, []) if now - t < self.window_s]
        if len(stamps) >= self.limit:
            self._sent[key] = stamps
            return False
        stamps.append(now)
        self._sent[key] = stamps
        return True
