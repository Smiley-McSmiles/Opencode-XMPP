"""The XMPP side: 1:1 chats, MUC rooms, OMEMO, HTTP uploads.

Every turn is one ``opencode run`` subprocess in the contact's one derived
session. The bridge never stores the transcript: the session lives in opencode,
keyed by a deterministic id, so a restart or an upgrade cannot fork a person into
a second conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import slixmpp
from slixmpp import JID

from . import approvals, cards, diagnostics, omemo, opencode_cli as cli, reactions, uploads
from .chunk import split_message
from .config import Config, Tier, read_secret
from .policy import (GUEST, OWNER, RateLimiter, bare_jid, is_ignored,
                     session_id_for, tier_for)

log = logging.getLogger("opencode_xmpp.bot")

OMEMO2_NAMESPACE = "urn:xmpp:omemo:2"
LEGACY_NAMESPACE = "eu.siacs.conversations.axolotl"
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".gif": "image/gif", ".webp": "image/webp"}
AUDIO_MIME = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
              ".opus": "audio/ogg", ".wav": "audio/wav", ".webm": "audio/webm",
              ".aac": "audio/aac", ".flac": "audio/flac"}
MAX_IMAGES_PER_TURN = 4

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas from JSONC.

    opencode reads ``opencode.jsonc``, and people write comments in it. The
    bridge edits that file when a model card or ``think-off`` needs to apply
    model options, so it has to understand the same dialect.
    """
    out: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            following = text[index + 1]
            if following == "/":
                newline = text.find("\n", index)
                if newline < 0:
                    break
                index = newline
                continue
            if following == "*":
                end = text.find("*/", index + 2)
                index = len(text) if end < 0 else end + 2
                continue
        out.append(char)
        index += 1
    return _TRAILING_COMMA_RE.sub(r"\1", "".join(out))


def load_opencode_config(path: Path) -> dict:
    """Read an opencode config file, tolerating JSONC comments."""
    data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not contain a JSON object")
    return data


@dataclass
class _Chat:
    """Everything in flight for one conversation (a contact, or a room member).

    At most one ``opencode run`` exists per conversation at a time. A message
    that arrives while the agent is still working interrupts that process and is
    folded into the next one, so a person never gets two parallel answers and the
    opencode session never forks.
    """

    pending: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None
    proc: object | None = None          # subprocess.Popen while a turn runs
    stop_event: threading.Event | None = None
    settings: "cli.RunSettings | None" = None
    session_id: str = ""
    agent: str = ""
    interrupt_requested: bool = False
    suppress: bool = False   # drop the reply of the turn we just interrupted
    media_turn: bool = False  # this turn carries an image: do not interrupt it
    clearing: bool = False    # a ``clear`` is deleting this chat's session
    model_override: str = ""  # per-chat ``model`` selection
    card: cards.Card | None = None  # active model card for this chat
    session_ids: set[str] = field(default_factory=set)  # every session used here


MUC_USER_NS = "http://jabber.org/protocol/muc#user"
DIRECT_INVITE_NS = "jabber:iq:conference"
MENTION_STRIP = ".,:;!?()[]<>\"'@*_~"


def _mention_word(word: str) -> str:
    """A word with mention punctuation and decoration removed, lowercased."""
    return word.strip(MENTION_STRIP).lower()


def room_invite(xml) -> tuple[str, str] | None:
    """(room, inviter) from a mediated MUC invite or a direct XEP-0249 one.

    A MUC invitation is a message with no body, so the ordinary message path
    used to drop it. Both spellings are recognised: Prosody and most rooms use
    ``<x xmlns="...muc#user"><invite from="..."/></x>``, while some clients send
    a lighter ``<x xmlns="jabber:iq:conference" jid="room@conference.host"/>``.
    """
    x = xml.find(f"{{{MUC_USER_NS}}}x")
    if x is not None:
        invite = x.find(f"{{{MUC_USER_NS}}}invite")
        if invite is not None:
            room = bare_jid(str(xml.get("from") or ""))
            inviter = bare_jid(str(invite.get("from") or xml.get("from") or ""))
            if room:
                return room, inviter
    direct = xml.find(f"{{{DIRECT_INVITE_NS}}}x")
    if direct is not None:
        room = bare_jid(str(direct.get("jid") or ""))
        inviter = bare_jid(str(xml.get("from") or ""))
        if room:
            return room, inviter
    return None


def _is_stop_command(text: str) -> bool:
    """A bare ``stop`` never reaches the model; anything else is conversation."""
    return (text or "").strip().lower() == "stop"


def _turn_preamble(cfg: Config, who: str, room: str | None) -> str:
    """The dynamic facts prepended to every message.

    Agent Markdown files carry the static instructions (plain text, markers,
    tool policy). This only tells the model which account it is, who it is
    answering and when — facts that change per turn and cannot live in an agent.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    where = f"the group chat {room}" if room else "a direct chat"
    return (f"[XMPP bridge] You are the account {cfg.account.jid}, answering {who} in "
            f"{where} at {stamp}. Reply as a chat message.\n\n")


def _settings_for(cfg: Config, tier: Tier) -> cli.RunSettings:
    """The CLI invocation for one trust tier."""
    return cli.RunSettings(
        binary=cfg.opencode.binary,
        workspace=cfg.opencode.workspace,
        agent=tier.agent,
        model=tier.model,
        auto_approve=tier.auto_approve,
        timeout_s=tier.timeout_s,
        env=cfg.opencode.env,
    )


class Bridge(slixmpp.ClientXMPP):
    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg.account.jid, read_secret(cfg.account.password_file))
        self.cfg = cfg
        self.bot_bare = bare_jid(cfg.account.jid)
        self.limiter = RateLimiter(cfg.policy.rate_limit_per_minute)
        self._chats: dict[str, _Chat] = {}
        self._stopped = asyncio.Event()
        self._upload_host: str = ""
        # ``default.json`` is the card every chat starts with. None means the
        # bridge falls back to [admin]/[guest] model, as before.
        self._default_card: cards.Card | None = cards.load_default()
        _instance = bare_jid(cfg.account.jid).split('@', 1)[0]
        self._pending = approvals.PendingStore(
            Path(cfg.features.omemo_storage).expanduser().with_name(f"pending-{_instance}.json"))
        self._spool = Path(cfg.features.spool_dir).expanduser()
        # Last time we told the owners a room's OMEMO is unreadable (room -> epoch).
        self._room_warned: dict[str, float] = {}
        # Turn counters for the optional manual compaction safety net.
        self._turn_counts: dict[str, int] = {}
        # One deterministic diagnostic run at a time; it is outside the chat runner.
        self._repairing = False

        self.register_plugin("xep_0045")           # multi-user chat
        self.register_plugin("xep_0066")           # out-of-band file links
        self.register_plugin("xep_0085")           # chat states / typing
        # Delivery receipts: ack every incoming message that asks for one (the
        # operator's client then shows their message as delivered/read) and ask
        # for one on our own replies. Rooms are excluded by the plugin itself.
        self.register_plugin("xep_0184", pconfig={"auto_ack": True, "auto_request": True})
        self.register_plugin("xep_0280")           # message carbons (other resources)
        self.register_plugin("xep_0444")           # message reactions
        # Answer server pings and keep the stream warm; without this Prosody
        # drops an idle client after a few minutes.
        self.register_plugin("xep_0199", pconfig={"keepalive": True, "interval": 60, "timeout": 30})
        if cfg.features.uploads:
            # xep_0363 is not in slixmpp's default registry (its module exports the
            # class as XEP_0363, which load_plugin() does not look for), so register it.
            from slixmpp.plugins.base import register_plugin as register_plugin_class
            from slixmpp.plugins.xep_0363 import XEP_0363
            register_plugin_class(XEP_0363)
            self.register_plugin("xep_0363")       # HTTP file upload
        if cfg.features.omemo:
            omemo.install(self, self._omemo_storage_path())

        self.add_event_handler("session_start", self._on_session_start)
        # slixmpp's "message" event only fires for stanzas carrying <body>, so
        # OMEMO messages (which have <encrypted> and no body) would be dropped.
        # Match every message stanza instead and filter ourselves.
        from slixmpp.xmlstream.handler import Callback
        from slixmpp.xmlstream.matcher import MatchXPath
        self.register_handler(Callback(
            "opencode-messages",
            MatchXPath("{%s}message" % self.default_ns),
            self._on_message,
        ))
        self.add_event_handler("groupchat_message", self._on_groupchat)
        self.add_event_handler("presence_subscribe", self._on_subscribe)
        self.add_event_handler("carbon_received", self._on_carbon_received)
        self.add_event_handler("omemo_initialized", self._on_omemo_ready)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("stream_error", self._on_stream_error)
        self.add_event_handler("connection_failed", self._on_connection_failed)
        self.add_event_handler("failed_auth", self._on_failed_auth)

    def _omemo_storage_path(self) -> str:
        """One OMEMO store per account: switching JIDs must not reuse identity state."""
        configured = Path(self.cfg.features.omemo_storage).expanduser()
        node = bare_jid(self.cfg.account.jid).split("@", 1)[0]
        return str(configured.with_name(f"{configured.stem}-{node}{configured.suffix}"))

    # -- lifecycle ----------------------------------------------------------

    def _on_session_start(self, _event) -> None:
        self.send_presence(pstatus="opencode bridge")
        log.info("connected as %s", self.boundjid.bare)
        asyncio.create_task(self._after_start())

    async def _after_start(self) -> None:
        # A card's context and sampling options live in opencode's config, so the
        # default card has to be applied once before the first turn.
        if self._default_card is not None:
            path = self._opencode_config_path()
            if path.is_file():
                try:
                    changed = await asyncio.to_thread(
                        self._apply_card_config, path, self._default_card)
                except (OSError, ValueError) as exc:
                    log.warning("could not apply default card %s: %s",
                                self._default_card.id, exc)
                else:
                    if changed:
                        log.info("applied default card %s (%s) to %s",
                                 self._default_card.id, self._default_card.base_model, path)
        # Advertise presence to every contact that is subscribed to us; without a
        # direct presence after a fresh approval, clients can keep showing "offline".
        try:
            await self.get_roster()
        except Exception:  # noqa: BLE001
            log.exception("could not fetch roster")
        asyncio.create_task(self._presence_loop())
        asyncio.create_task(self._reaction_outbox_loop())
        for jid in list(self.client_roster.keys()):
            try:
                item = self.client_roster[jid]
                if item["subscription"] in ("from", "both"):
                    self.send_presence(pto=jid)
            except Exception:  # noqa: BLE001
                log.debug("presence to %s failed", jid, exc_info=True)

        if self.cfg.features.uploads:
            await self._discover_upload_service()
        node = bare_jid(self.cfg.account.jid).split("@", 1)[0]
        for room in self.cfg.rooms.join:
            for nick in dict.fromkeys((self.cfg.rooms.nick, node, f"{node}-bridge")):
                try:
                    await self.plugin["xep_0045"].join_muc_wait(JID(room), nick, maxstanzas=0, timeout=15)
                    log.info("joined room %s as %s", room, nick)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("room %s as %s failed (%s); trying another nick", room, nick, exc)

    async def _presence_loop(self) -> None:
        """Re-advertise presence to subscribed contacts every few minutes.

        Some servers do not re-broadcast presence after a subscription is
        approved while the client was offline; contacts then sit at "offline".
        """
        while True:
            await asyncio.sleep(240)
            try:
                for jid in list(self.client_roster.keys()):
                    if self.client_roster[jid]["subscription"] in ("from", "both"):
                        self.send_presence(pto=jid)
            except Exception:  # noqa: BLE001
                log.debug("presence refresh failed", exc_info=True)

    def _on_carbon_received(self, msg) -> None:
        forwarded = msg["carbon_received"]
        log.info("carbon copy of a message addressed to another resource")
        self._on_message(forwarded)

    def _on_omemo_ready(self, _event) -> None:
        asyncio.create_task(self._log_omemo_identity())

    async def _log_omemo_identity(self) -> None:
        try:
            plugin = self.plugin["xep_0384"]
            session_manager = await plugin.get_session_manager()
            device, _others = await session_manager.get_own_device_information()
            log.info("OMEMO ready: device id %s, fingerprint %s",
                     device.device_id, device.identity_key.hex()[:16])
        except Exception:  # noqa: BLE001
            log.exception("could not read own OMEMO device information")

    async def _discover_upload_service(self) -> None:
        plugin = self.plugin["xep_0363"]
        configured = self.cfg.features.upload_service
        try:
            if configured:
                plugin.upload_service = JID(configured)
                self._upload_host = configured
                log.info("upload service set to %s", configured)
                return
            domain = JID(self.cfg.account.jid).domain
            info = await plugin.find_upload_service(domain=JID(domain))
            if info is None:
                log.warning("no HTTP upload service found on %s", domain)
                return
            self._upload_host = str(info["from"]).split("/")[0]
            log.info("discovered upload service %s", self._upload_host)
        except Exception:  # noqa: BLE001
            log.exception("HTTP upload discovery failed")

    def _on_disconnected(self, _event) -> None:
        log.warning("disconnected from XMPP")
        self._stopped.set()

    def _on_stream_error(self, error) -> None:
        log.error("XMPP stream error: %s", error)

    def _on_connection_failed(self, event) -> None:
        log.error("XMPP connection failed: %s", event)

    def _on_failed_auth(self, _event) -> None:
        log.error("XMPP authentication failed for %s", self.bot_bare)
        self._stopped.set()

    async def run_forever(self) -> None:
        self.connect(host=self.cfg.account.host, port=self.cfg.account.port)
        await self._stopped.wait()

    # -- subscriptions ------------------------------------------------------

    def _accept_subscription(self, jid: str) -> None:
        """Authorize the contact and request their presence back."""
        self.send_presence_subscription(pto=jid, ptype="subscribed")
        self.send_presence_subscription(pto=jid, ptype="subscribe")
        self.send_presence(pto=jid)

    def _on_subscribe(self, presence) -> None:
        """Owners and auto-accept are immediate; anyone else waits for an owner decision."""
        jid = presence["from"].bare
        owner_jids = {o.strip().lower() for o in self.cfg.policy.owners}
        if jid.strip().lower() in owner_jids or self.cfg.features.auto_accept_subscriptions:
            log.info("auto-accepting subscription from %s", jid)
            self._accept_subscription(jid)
            return
        new = self._pending.add(jid, str(presence["from"]))
        log.info("contact request from %s (%s) - notified owners", jid,
                 "new" if new else "already pending")
        asyncio.create_task(self._notify_owners_of_request(jid, str(presence["from"])))

    async def _notify_owners(self, text: str) -> None:
        for owner in self.cfg.policy.owners:
            await self._notify(owner, text)

    async def _notify_owners_of_request(self, jid: str, sender: str) -> None:
        when = time.strftime("%Y-%m-%d %H:%M")
        text = (f"Contact request: {jid} wants to add {self.bot_bare} as a contact "
                f"(requested {when}; seen from {sender}).\n"
                f"Approve: approve {jid}\nDecline: deny {jid}\nList: pending")
        for owner in self.cfg.policy.owners:
            await self._notify(owner, text)

    async def _notify(self, to: str, text: str) -> None:
        """Encrypted when the contact has devices, plaintext otherwise."""
        if self.cfg.features.omemo and await self._send_encrypted(to, text):
            return
        self._send(to, text)

    # -- inbound: 1:1 -------------------------------------------------------

    def _on_message(self, msg) -> None:
        # An invitation has no body: catch it here, before the body checks below,
        # so the bridge never depends on a plugin event being wired correctly.
        invite = room_invite(msg.xml)
        if invite is not None:
            asyncio.create_task(self._handle_room_invite(*invite))
            return
        raw_type = msg["type"]
        # Message text is never logged: the bridge's logs are not a transcript store.
        log.debug("handler saw message type=%s from=%s", raw_type, msg["from"])
        if raw_type not in ("chat", "normal"):
            log.debug("ignoring message type %s", raw_type)
            return
        # Carbon wrappers are handled by the carbon event, not directly.
        # (Checking msg["carbon_received"] is unreliable: it returns an empty
        # element object instead of None for ordinary messages.)
        carbon_ns = "{urn:xmpp:carbons:2}"
        if msg.xml.find(carbon_ns + "received") is not None or msg.xml.find(carbon_ns + "sent") is not None:
            return
        raw_body = msg["body"] or ""
        encrypted = bool(self.plugin["xep_0384"].is_encrypted(msg)) if self.cfg.features.omemo else False
        if not encrypted and not raw_body:
            return  # chat-state / receipt noise
        log.info("inbound <message> type=%s from=%s body_len=%d encrypted=%s",
                 raw_type, msg["from"], len(raw_body), encrypted)
        sender = bare_jid(str(msg["from"]))
        if sender == self.bot_bare:
            return  # never answer ourselves
        if is_ignored(sender, self.cfg.policy.ignore_jids):
            log.debug("ignoring message from %s (do-not-answer list)", sender)
            return
        if self.cfg.policy.quiet_unknown_senders and tier_for(sender, self.cfg.policy.owners) == GUEST:
            return
        if not self.limiter.allow(sender):
            log.warning("rate limited %s", sender)
            self.send_message(mto=sender, mbody="Slow down a moment — I'm still working through "
                                                "your last messages.", mtype="chat")
            return

        # The sender's stanza id is what a reaction has to address later; OMEMO
        # keeps it on the outer stanza, so read it before any decryption.
        stanza_id = str(msg["id"] or "")

        if encrypted:
            asyncio.create_task(self._handle_encrypted(sender, msg, stanza_id))
            return

        body = (msg["body"] or "").strip()
        if not body:
            return
        log.info("message from %s (%d chars, tier=%s)", sender, len(body),
                 tier_for(sender, self.cfg.policy.owners))
        self._dispatch(sender, body, encrypted=False, msg_id=stanza_id)

    async def _handle_encrypted(self, sender: str, msg, stanza_id: str = "") -> None:
        try:
            body, device = await omemo.decrypt_one_to_one(self, msg)
        except Exception as exc:  # noqa: BLE001 - not for us, or malformed
            log.warning("could not decrypt message from %s: %s", sender, exc)
            return
        body = (body or "").strip()
        if not body:
            return
        log.info("decrypted message from %s (device %s)", sender, device.device_id)
        self._dispatch(sender, body, encrypted=True, msg_id=stanza_id)

    # -- inbound: rooms -----------------------------------------------------

    def _on_groupchat(self, msg) -> None:
        room = str(msg["from"].bare)
        nick = str(msg["from"].resource)
        if nick == self.cfg.rooms.nick or nick == self.boundjid.resource:
            return
        # OMEMO in a room (XEP-0450) leaves only a fallback sentence as the body,
        # so a mention can never match it: decrypt before looking at the text.
        if self.cfg.features.omemo and self.plugin["xep_0384"].is_encrypted(msg):
            asyncio.create_task(self._handle_encrypted_groupchat(room, nick, msg))
            return
        body = (msg["body"] or "").strip()
        if not body:
            log.debug("groupchat message in %s from %s without a body", room, nick)
            return
        self._groupchat_body(room, nick, body, msg)

    async def _handle_encrypted_groupchat(self, room: str, nick: str, msg) -> None:
        """Read a room message that arrived OMEMO-encrypted (legacy or OMEMO 2)."""
        try:
            body = await omemo.decrypt_group(self, msg)
        except Exception as exc:  # noqa: BLE001 - device not in the room's list, not ours
            log.warning("could not decrypt an OMEMO room message from %s in %s: %s",
                        nick, room, exc)
            await self._warn_room_encryption(room)
            return
        body = (body or "").strip()
        if not body:
            return
        log.info("decrypted room message in %s from %s (%d chars)", room, nick, len(body))
        self._groupchat_body(room, nick, body, msg)

    async def _warn_room_encryption(self, room: str) -> None:
        """Say once an hour that a room's OMEMO excludes this account's device."""
        now = time.time()
        if now - self._room_warned.get(room, 0.0) < 3600:
            return
        self._room_warned[room] = now
        await self._notify_owners(
            f"Messages in {room} are OMEMO-encrypted and this account's device is not in "
            f"that room's device list, so they cannot be read. Either turn OMEMO off for "
            f"the room in your client, or add this device to it (its id is in the startup "
            f"line 'OMEMO ready: device id ...').")

    def _groupchat_body(self, room: str, nick: str, body: str, msg) -> None:
        addressed, text = self._room_addressed(body)
        # Every groupchat message is logged (length only): a silent room is then
        # distinguishable from a message that arrived but was not addressed to us.
        log.info("groupchat in %s from %s: addressed=%s, %d chars",
                 room, nick, addressed, len(body))
        if not addressed:
            return
        try:
            real_jid = str(msg["muc"]["item"]["jid"] or "")
        except Exception:  # noqa: BLE001 - anonymous room or no item element
            real_jid = ""
        tier = self._room_tier(nick, real_jid)
        if real_jid and is_ignored(real_jid, self.cfg.policy.ignore_jids):
            return
        key = f"{room}/{nick}"
        if not self.limiter.allow(key):
            return
        log.info("room message in %s from %s (tier=%s)", room, nick, tier)
        self._dispatch(key, text, encrypted=False, room=room, nick=nick, tier=tier,
                       real_jid=real_jid, msg_id=str(msg["id"] or ""))

    async def _handle_room_invite(self, room: str, inviter: str) -> None:
        """Owner invites are accepted; anyone else's becomes a question for the owners."""
        if not room:
            return
        log.info("room invite to %s from %s", room, inviter)
        if not self.cfg.rooms.accept_invites:
            await self._notify_owners(
                f"{inviter or 'Someone'} invited {self.bot_bare} to {room}. "
                f"Room invites are off; add the room to [rooms] join to answer there.")
            return
        if not inviter or tier_for(inviter, self.cfg.policy.owners) != OWNER:
            await self._notify_owners(
                f"Room invite: {inviter or 'unknown'} invited {self.bot_bare} to {room}. "
                f"Not an owner, so it was not joined — add it to [rooms] join to accept.")
            return
        nick = self.cfg.rooms.nick or "opencode"
        try:
            await self.plugin["xep_0045"].join_muc_wait(JID(room), nick, maxstanzas=0, timeout=15)
        except Exception as exc:  # noqa: BLE001 - bans, nicks in use, room gone
            log.warning("join of invited room %s as %s failed: %s", room, nick, exc)
            await self._notify(inviter, f"I could not join {room} as {nick}: {exc}")
            return
        log.info("joined room %s on %s's invite", room, inviter)
        await self._notify(inviter, f"Joined {room} as {nick}. Address me as \"{nick}: ...\" there.")

    def _room_addressed(self, body: str) -> tuple[bool, str]:
        """Whether a room message is aimed at us, and the text minus the address.

        People type the nick, the JID's local part, or a client-side mention of
        whichever name they saw in the occupant list, so all of them count.
        """
        lowered = body.lower()
        names = [self.cfg.rooms.nick.lower()]
        account = getattr(self.cfg, "account", None)
        jid = str(getattr(account, "jid", "") or "")
        node = bare_jid(jid).split("@", 1)[0].lower() if jid else ""
        for name in (node, "opencode"):
            if name not in names:
                names.append(name)
        for name in names:
            for prefix in (f"{name}:", f"{name},", f"@{name}"):
                if lowered.startswith(prefix):
                    return True, body[len(prefix):].strip()
        # A mention can sit anywhere and carries punctuation: "@bot,", "bot?" and
        # a client-inserted "@name" all have to count. Compare bare words.
        words = body.split()
        if any(_mention_word(word) in names for word in words):
            rest = " ".join(w for w in words if _mention_word(w) not in names).strip(" ,:;")
            # A bare "ollama?" leaves nothing behind, and an empty prompt is
            # rejected by the runs API (HTTP 400) instead of answered: keep the
            # original text so the model sees what was actually said.
            return True, rest or body
        return self.cfg.rooms.respond == "always", body

    def _room_tier(self, nick: str, real_jid: str) -> str:
        if real_jid:
            try:
                if tier_for(real_jid, self.cfg.policy.owners) == OWNER:
                    return OWNER
            except ValueError:
                pass
        if self.cfg.rooms.trust_nicks and nick in self.cfg.rooms.owner_nicks:
            return OWNER
        return GUEST

    # -- turn plumbing ------------------------------------------------------

    def _dispatch(self, key: str, body: str, encrypted: bool, room: str | None = None,
                  nick: str | None = None, tier: str | None = None, real_jid: str = "",
                  msg_id: str = "") -> None:
        # Route one inbound message: commands first, then the per-chat runner."""
        text = (body or "").strip()
        item = {"body": text, "encrypted": encrypted, "room": room,
                "nick": nick, "tier": tier, "real_jid": real_jid, "id": msg_id}
        if room is None and tier is None and approvals.clear_command(text):
            asyncio.create_task(self._handle_clear(key, item))
            return
        if room is None and tier is None and tier_for(key, self.cfg.policy.owners) == OWNER:
            think = approvals.think_command(text)
            if think is not None:
                asyncio.create_task(self._handle_think(key, item, think))
                return
            if approvals.help_command(text):
                asyncio.create_task(self._handle_help(key, item))
                return
            model_arg = approvals.model_command(text)
            if model_arg is not None:
                asyncio.create_task(self._handle_model(key, item, model_arg))
                return
            if approvals.restart_command(text):
                asyncio.create_task(self._handle_restart(key, item))
                return
            focus = approvals.repair_command(text)
            if focus is not None:
                asyncio.create_task(self._handle_repair(key, item, focus))
                return
            command = approvals.parse_command(text)
            if command is not None:
                asyncio.create_task(self._handle_approval(key, *command))
                return
            invite = approvals.invite_command(text)
            if invite is not None:
                asyncio.create_task(self._handle_invite(key, item, key, invite))
                return
            room_to_join = approvals.join_command(text)
            if room_to_join is not None:
                asyncio.create_task(self._handle_join(key, item, key, room_to_join))
                return
        chat = self._chats.setdefault(key, _Chat())
        if chat.clearing:
            # Hold anything that arrives during a hard clear; it must use the
            # new session, which only exists once the delete has finished.
            chat.pending.append(item)
            return
        if _is_stop_command(text):
            self._stop_chat(key, chat, item)
            return
        chat.pending.append(item)
        if chat.task is None or chat.task.done():
            chat.task = asyncio.create_task(self._run_chat(key, chat))
        elif self.cfg.policy.on_new_message == "queue" or chat.media_turn:
            # Queue mode, or a turn that carries a photo: never throw media away
            # by interrupting. This message is answered as soon as it settles.
            log.info("queued a message for %s behind the running turn", key)
        else:
            # The model is still working on the previous message: interrupt it
            # now; the new text becomes the next turn as soon as it settles.
            log.info("interrupting the running turn for %s (new message)", key)
            self._interrupt(chat)

    def _interrupt(self, chat: _Chat) -> None:
        """Drop the current answer and stop the opencode process running it."""
        chat.suppress = True
        if chat.proc is None or chat.interrupt_requested or chat.settings is None:
            return
        chat.interrupt_requested = True
        proc, settings, sid = chat.proc, chat.settings, chat.session_id
        if chat.stop_event is not None:
            chat.stop_event.set()
        asyncio.create_task(asyncio.to_thread(cli.interrupt, settings, sid, proc))

    def _stop_chat(self, key: str, chat: _Chat, item: dict) -> None:
        """``stop``: forget queued text and interrupt the run in flight."""
        chat.pending.clear()
        chat.suppress = True
        self._interrupt(chat)
        log.info("stop requested by %s", key)
        asyncio.create_task(self._reply(key, item, "Stopped."))

    async def _handle_approval(self, owner: str, action: str, target: str) -> None:
        """Owner said approve/deny/pending in chat. Nothing here reaches the model."""
        if action == "list":
            await self._notify(owner, self._pending.describe())
            return
        if not target:
            if len(self._pending) == 1:
                target = next(iter(self._pending.all()))
            else:
                await self._notify(owner, "Which contact? " + self._pending.describe())
                return
        if action == "approve":
            if not self._pending.remove(target):
                await self._notify(owner, f"No pending request from {target}.")
                return
            self._accept_subscription(target)
            log.info("owner %s approved contact %s", owner, target)
            await self._notify(owner, f"Approved {target}. They can now exchange presence; "
                                      f"their messages still use the guest tier.")
        else:
            if not self._pending.remove(target):
                await self._notify(owner, f"No pending request from {target}.")
                return
            self.send_presence_subscription(pto=target, ptype="unsubscribed")
            log.info("owner %s denied contact %s", owner, target)
            await self._notify(owner, f"Declined {target}.")

    async def _handle_invite(self, key: str, item: dict, owner: str, target: str) -> None:
        """``invite <jid>``: send the presence subscription the bridge owns.

        A forced command — nothing here reaches the model, so it works even when
        the account's model cannot call tools.
        """
        if not target:
            await self._reply(key, item, "Usage: invite user@example.org")
            return
        try:
            parsed = JID(target)
        except Exception:  # noqa: BLE001 - malformed JID from chat
            parsed = None
        if "@" not in target or parsed is None or not parsed.node or not parsed.domain:
            await self._reply(key, item, f"{target} is not a JID. Usage: invite user@example.org")
            return
        jid = bare_jid(target)
        if jid == self.bot_bare:
            await self._reply(key, item, "That JID is me.")
            return
        if is_ignored(jid, self.cfg.policy.ignore_jids):
            await self._reply(key, item, f"{jid} is on the do-not-contact list.")
            return
        try:
            # Add the contact to the roster so the account keeps it after a restart.
            self.update_roster(jid, name=str(parsed.node))
        except Exception:  # noqa: BLE001 - the subscription request still counts
            log.debug("could not add %s to the roster", jid, exc_info=True)
        self.send_presence_subscription(pto=jid, ptype="subscribe")
        self.send_presence(pto=jid)
        log.info("owner %s invited %s", owner, jid)
        await self._reply(
            key, item,
            f"Invite sent to {jid}. They approve it in their client; they stay guest "
            f"tier unless they are in [policy] owners.")

    async def _handle_join(self, key: str, item: dict, owner: str, target: str) -> None:
        """``join <room>``: an owner asks the bridge into a room, no model involved."""
        if not target:
            await self._reply(key, item, "Usage: join room@conference.example.org")
            return
        try:
            parsed = JID(target)
        except Exception:  # noqa: BLE001 - malformed JID from chat
            parsed = None
        if "@" not in target or parsed is None or not parsed.node or not parsed.domain:
            await self._reply(key, item,
                              f"{target} is not a room JID. Usage: join room@conference.example.org")
            return
        room = str(parsed.bare)
        nick = self.cfg.rooms.nick or "opencode"
        try:
            await self.plugin["xep_0045"].join_muc_wait(JID(room), nick, maxstanzas=0, timeout=15)
        except Exception as exc:  # noqa: BLE001 - bans, nick taken, room gone
            log.warning("join of %s as %s failed: %s", room, nick, exc)
            await self._reply(key, item, f"I could not join {room} as {nick}: {exc}")
            return
        log.info("owner %s asked me into %s (joined as %s)", owner, room, nick)
        await self._reply(
            key, item,
            f"Joined {room} as {nick}. Address me there as \"{nick}: ...\". Add it to "
            f"[rooms] join in this instance's opencode.toml to keep it after a restart.")

    # -- operator commands --------------------------------------------------

    def _restart_unit(self) -> str:
        """The systemd unit ``restart`` acts on.

        Prefer the explicit config value; otherwise derive it from the instance
        env var the shipped unit sets, then from the JID's local part, which is
        what ``opencode-xmpp unit --name`` uses.
        """
        configured = (self.cfg.policy.restart_unit or "").strip()
        if configured:
            return configured
        instance = os.environ.get("OPENCODE_XMPP_INSTANCE", "").strip()
        if not instance:
            instance = self.bot_bare.split("@", 1)[0]
        return f"opencode-xmpp-{instance}.service"

    @staticmethod
    def _schedule_restart(unit: str, delay_s: int = 2) -> tuple[bool, str]:
        """Queue a restart outside this service's cgroup.

        Running ``systemctl restart`` directly from a turn would ask systemd to
        stop the unit that is executing the command. ``systemd-run --on-active``
        creates an independent transient timer, so the request survives the unit
        going away. A detached shell is the fallback for systems without
        systemd-run.
        """
        name = f"opencode-xmpp-restart-{os.getpid()}-{time.time_ns()}"
        argv = ["systemd-run", "--user", "--collect", "--quiet",
                "--on-active", f"{int(delay_s)}s", "--unit", name,
                "systemctl", "--user", "restart", unit]
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True, name
        except FileNotFoundError:
            pass
        except OSError as exc:
            return False, str(exc)

        script = (f"sleep {int(delay_s)}; "
                  f"exec systemctl --user restart {shlex.quote(unit)}")
        try:
            subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            return False, str(exc)
        return True, "detached shell fallback"

    async def _handle_restart(self, key: str, item: dict) -> None:
        """Owner said ``restart``: acknowledge, then restart this unit off-turn."""
        unit = self._restart_unit()
        if not re.fullmatch(r"[A-Za-z0-9@_.:\\-]+\.service", unit):
            await self._reply(key, item, f"{unit!r} is not a systemd service name.")
            return
        try:
            check = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "show", "-p", "LoadState", "--value", unit],
                capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            await self._reply(key, item, f"I could not ask systemd about {unit}: {exc}")
            return
        state = (check.stdout or "").strip()
        if check.returncode != 0 or state != "loaded":
            detail = (check.stderr or check.stdout or "").strip().splitlines()
            reason = detail[0] if detail else (state or f"exit {check.returncode}")
            await self._reply(key, item, f"I can't restart {unit}: {reason}")
            return

        # The acknowledgement has to be on the wire before the process goes
        # away; the timer only starts once the reply has been sent.
        await self._reply(key, item,
                          f"Restarting {unit} now. This connection will drop and I'll be back "
                          f"automatically.")
        ok, detail = self._schedule_restart(unit)
        if ok:
            log.info("owner %s requested restart of %s (%s)", key, unit, detail)
        else:
            log.error("could not schedule restart of %s: %s", unit, detail)
            await self._reply(key, item, f"I could not schedule the restart: {detail}")

    @staticmethod
    def _opencode_config_path() -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        for name in ("opencode.jsonc", "opencode.json"):
            candidate = base / "opencode" / name
            if candidate.is_file():
                return candidate
        return base / "opencode" / "opencode.jsonc"

    async def _handle_think(self, key: str, item: dict, state: str) -> None:
        """Owner said ``think-off``/``think-on``: save it to the active card."""
        chat = self._chats.setdefault(key, _Chat())
        card = self._active_card(chat)
        if card is None:
            await self._reply(
                key, item,
                f"No model card is active. Add {cards.DEFAULT_NAME} to "
                f"{cards.default_dir()} or select a card with model <card-id>.")
            return
        enabled = state == "on"
        try:
            updated = await asyncio.to_thread(cards.set_param, card, "think", enabled)
        except (OSError, ValueError) as exc:
            log.warning("could not update thinking for %s: %s", card.source, exc)
            await self._reply(key, item, f"I couldn't update {card.source.name}: {exc}")
            return
        chat.card = updated
        chat.model_override = updated.model
        if (self._default_card is not None
                and updated.id == self._default_card.id
                and updated.source == self._default_card.source):
            self._default_card = updated
        path = self._opencode_config_path()
        if path.is_file():
            try:
                await asyncio.to_thread(self._apply_card_config, path, updated)
            except (OSError, ValueError) as exc:
                log.warning("could not apply thinking for %s: %s", updated.id, exc)
                await self._reply(
                    key, item,
                    f"Saved thinking {'on' if enabled else 'off'} to "
                    f"{updated.source.name}, but I couldn't apply it to opencode: {exc}")
                return
        verb = "on" if enabled else "off"
        log.info("owner %s turned thinking %s for card %s", key, verb, updated.id)
        await self._reply(
            key, item,
            f"Thinking is {verb} for card {updated.name} and saved to "
            f"{updated.source.name}. The next turn uses it.")

    def _allowed_provider(self) -> str:
        """The provider this bridge instance is meant to use.

        OpenCode exposes its own hosted free models to every account.  A local
        Ollama bot must not quietly offer them (private chats stay local), and a
        cloud bot should not silently switch to a local/other provider.
        """
        for model in (self.cfg.admin.model, self.cfg.guest.model):
            model = (model or "").strip()
            if "/" in model:
                return model.split("/", 1)[0].strip().lower()
        # A card-only deployment has no [admin] model; the default card then
        # decides which provider this bridge is allowed to use.
        if self._default_card is not None and "/" in self._default_card.model:
            return self._default_card.model.split("/", 1)[0].strip().lower()
        return ""

    def _model_allowed(self, model: str) -> bool:
        provider = self._allowed_provider()
        if not provider:
            return True
        return "/" in model and model.split("/", 1)[0].strip().lower() == provider

    @staticmethod
    def _write_opencode_config(path: Path, data: dict) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    @staticmethod
    def _apply_card_config(path: Path, card: cards.Card) -> list[str]:
        data = load_opencode_config(path)
        changed = cards.apply_params(data, card)
        if changed:
            Bridge._write_opencode_config(path, data)
        return changed

    # -- model cards --------------------------------------------------------

    def _active_card(self, chat: _Chat) -> cards.Card | None:
        """The card this chat uses: an explicit choice, else ``default.json``."""
        return chat.card if chat.card is not None else self._default_card

    def _active_model(self, chat: _Chat, tier: str) -> str:
        """The opencode model id for this chat, before any per-chat override."""
        if chat.model_override:
            return chat.model_override
        card = self._active_card(chat)
        if card is not None:
            return card.model
        return self.cfg.admin.model if tier == OWNER else self.cfg.guest.model

    def _reload_default_card(self) -> cards.Card | None:
        self._default_card = cards.load_default()
        return self._default_card

    async def _select_card(self, key: str, item: dict, chat: _Chat,
                           card: cards.Card) -> None:
        """Make ``card`` this chat's model and apply its options."""
        path = self._opencode_config_path()
        # Set the chat state before the (possibly slow) config write, so a
        # help command arriving immediately afterwards already sees the card.
        old_model, old_card = chat.model_override, chat.card
        chat.model_override = card.model
        chat.card = card
        if path.is_file():
            try:
                await asyncio.to_thread(self._apply_card_config, path, card)
            except (OSError, ValueError) as exc:
                chat.model_override, chat.card = old_model, old_card
                await self._reply(key, item, f"I couldn't apply the card settings: {exc}")
                return
        log.info("owner %s selected model card %s (%s)", key, card.id, card.model)
        await self._reply(
            key, item,
            f"Model switched to card {card.name} via {card.id} for this chat "
            f"(base: {card.base_model}). Its persona and model parameters apply "
            "on the next turn; model default goes back to default.json.")

    async def _handle_help(self, key: str, item: dict) -> None:
        """Owner said ``help``: commands, current card and the card list."""
        chat = self._chats.get(key) or _Chat()
        card = self._active_card(chat)
        current = self._active_model(chat, OWNER) or "(default)"
        if card is not None:
            current += f" (card: {card.name})"
        lines = [
            "Commands (owner):",
            "  stop",
            "  help",
            "  model <card-id>",
            "  model default",
            "  think-off / think-on  (saved to the active card)",
            "  restart",
            "  repair [focus]",
            "  clear",
            "  invite <jid>",
            "  join <room@conference.host>",
            "  approve <jid> / deny <jid> / pending",
            "",
            f"Current chat model: {current}",
        ]
        card_dir = cards.default_dir()
        lines.extend(["", f"Cards ({card_dir}):"])
        try:
            found = [entry for entry in cards.load_cards(card_dir)
                     if self._model_allowed(entry.base_model)]
        except OSError as exc:
            lines.append(f"  (unreadable: {exc})")
        else:
            default = self._default_card
            if found:
                for entry in found:
                    is_default = (default is not None
                                  and entry.id == default.id
                                  and entry.source == default.source)
                    lines.append(f"  {entry.id} — {entry.name}"
                                 + ("  [default]" if is_default else ""))
                    lines.append(f"      base: {entry.base_model}")
                lines.append("  select a card with: model <card-id>")
            else:
                lines.append(f"  (none — add {cards.DEFAULT_NAME} or any card to that directory)")
        await self._reply(key, item, "\n".join(lines))

    async def _handle_model(self, key: str, item: dict, argument: str) -> None:
        """Owner said ``model``: pick one of the model cards."""
        chat = self._chats.setdefault(key, _Chat())
        argument = argument.strip()
        if not argument:
            await self._reply(
                key, item,
                "Usage: model <card-id> or model default. Send help to list the cards.")
            return
        if argument.lower() in ("default", "reset", "off"):
            card = self._reload_default_card()
            if card is None:
                chat.model_override, chat.card = "", None
                fallback = (self.cfg.admin.model or "(the bridge default)")
                await self._reply(
                    key, item,
                    f"There is no {cards.DEFAULT_NAME} in {cards.default_dir()}, so this "
                    f"chat now uses {fallback}. Add a default.json to make it explicit.")
                return
            if not self._model_allowed(card.base_model):
                await self._reply(
                    key, item,
                    f"The default card uses {card.base_model}, but this bridge is "
                    f"restricted to {self._allowed_provider()} models.")
                return
            await self._select_card(key, item, chat, card)
            return

        card = cards.find_card(cards.load_cards(cards.default_dir()), argument)
        if card is None:
            hint = ""
            if "/" in argument or ":" in argument:
                base = argument.split("#", 1)[0]
                hint = (f" To use {argument!r}, add a card whose \"base_model_id\" is "
                        f"{(base.split('/', 1)[-1] or base)!r}.")
            await self._reply(
                key, item,
                f"No card matches {argument!r}. Models are chosen through cards in "
                f"{cards.default_dir()}.{hint} Send help to list them.")
            return
        if not self._model_allowed(card.base_model):
            await self._reply(
                key, item,
                f"Card {card.name} uses {card.base_model}, but this bridge is restricted "
                f"to {self._allowed_provider()} models.")
            return
        await self._select_card(key, item, chat, card)

    async def _handle_clear(self, key: str, item: dict) -> None:
        """Hard-clear the sender's derived OpenCode session."""
        chat = self._chats.setdefault(key, _Chat())
        if chat.clearing:
            await self._reply(key, item, "A clear is already in progress.")
            return
        chat.clearing = True
        chat.pending.clear()
        chat.suppress = True
        self._interrupt(chat)
        try:
            task = chat.task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=25)
                except asyncio.TimeoutError:
                    await self._reply(
                        key, item,
                        "I couldn't stop the current turn, so I left the context alone. Try again.")
                    return
                except Exception:  # noqa: BLE001 - the old turn may have failed
                    log.exception("turn failed while clearing context for %s", key)
                    await self._reply(
                        key, item,
                        "I couldn't stop the current turn cleanly, so I left the context alone.")
                    return
            tier = tier_for(key, self.cfg.policy.owners)
            settings = _settings_for(self.cfg, self.cfg.admin if tier == OWNER else self.cfg.guest)
            card = self._active_card(chat)
            default_model = (card.model if card is not None
                             else (self.cfg.admin.model if tier == OWNER else self.cfg.guest.model))
            models = {default_model, self.cfg.admin.model, self.cfg.guest.model,
                      chat.model_override}
            if chat.card is not None:
                models.add(chat.card.model)
            session_ids = set(chat.session_ids)
            for model in models:
                if model:
                    session_ids.add(self._scoped_session_id(key, model))
            if card is not None:
                session_ids.add(self._scoped_session_id(key, card.model, card.id))
            # Migration from the pre-scope bridge: the unscoped session.
            session_ids.add(session_id_for(
                self.cfg.account.jid, key, self.cfg.policy.session_id_version))

            deleted = 0
            failures: list[str] = []
            for sid in sorted(session_ids):
                ok, detail = await asyncio.to_thread(cli.delete_session, settings, sid)
                if ok:
                    deleted += 1
                elif "not found" not in (detail or "").lower():
                    failures.append(f"{sid}: {detail}")
            if failures:
                log.warning("clear for %s deleted %d session(s), failures: %s",
                            key, deleted, "; ".join(failures))
                await self._reply(
                    key, item,
                    f"Deleted {deleted} OpenCode session(s), but {len(failures)} failed: "
                    + failures[0])
            else:
                chat.session_ids.clear()
                log.info("cleared context for %s (%d session(s))", key, deleted)
                await self._reply(
                    key, item,
                    f"Context cleared: deleted {deleted} OpenCode session(s) for this contact. "
                    "The next message starts a fresh session.")
        finally:
            chat.clearing = False
            chat.suppress = False
            if chat.pending:
                chat.task = asyncio.create_task(self._run_chat(key, chat))

    async def _handle_repair(self, key: str, item: dict, focus: str) -> None:
        """Owner said ``repair``: run deterministic, model-free diagnostics."""
        if self._repairing:
            await self._reply(key, item,
                              "A repair run is already in progress; I'll send its report when it finishes.")
            return
        self._repairing = True
        try:
            await self._reply(key, item, "Running diagnostics…")
            report = await diagnostics.run_checks(self.cfg, self, focus)
            log.info("repair report for %s ready: %d chars", key, len(report))
            await self._send_diagnostics_report(key, item, report)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not kill the bridge
            log.exception("repair diagnostics failed")
            await self._reply(key, item, f"Diagnostics failed: {exc}")
        finally:
            self._repairing = False

    async def _send_diagnostics_report(self, key: str, item: dict, text: str) -> None:
        """Send a diagnostics report, preferring encryption but never hiding it.

        If the command arrived encrypted but OMEMO is the broken thing, the
        report still gets through as clearly marked plaintext. ``[repair]
        plaintext = true`` forces that path from the start.
        """
        chunks = split_message(text, self.cfg.policy.max_chunk_chars)
        encrypted = bool(self.cfg.features.omemo and item.get("encrypted")
                         and not self.cfg.repair.plaintext)
        first = True
        for chunk in chunks:
            prefix = ("[repair]\n" if encrypted else "[repair, unencrypted]\n") if first else ""
            first = False
            if encrypted and await self._send_encrypted(key, prefix + chunk):
                continue
            if encrypted:
                log.warning("repair report for %s could not be encrypted; sending it in the clear", key)
                encrypted = False
                prefix = "[repair, unencrypted]\n"
            self._send(key, prefix + chunk)

    async def _run_chat(self, key: str, chat: _Chat) -> None:
        """One runner per conversation: never two answers in flight for a user."""
        while chat.pending:
            items = chat.pending
            chat.pending = []
            item = self._merge_items(items)
            chat.suppress = False
            chat.interrupt_requested = False
            try:
                await self._handle_turn(key, chat, item)
            except Exception:  # noqa: BLE001 - one bad turn must not kill the chat
                log.exception("turn failed for %s", key)
                if not chat.suppress:
                    try:
                        await self._reply(key, item,
                                          "Something went wrong on my side. Try again in a moment.")
                    except Exception:  # noqa: BLE001
                        log.exception("could not report failure to %s", key)
            finally:
                chat.proc = None
                chat.stop_event = None
                chat.settings = None

    @staticmethod
    def _merge_items(items: list[dict]) -> dict:
        """Fold messages that arrived during a run into one turn."""
        if len(items) == 1:
            return items[0]
        merged = dict(items[-1])
        merged["body"] = "\n\n".join(i["body"] for i in items if i.get("body"))
        merged["encrypted"] = any(i.get("encrypted") for i in items)
        return merged

    def _scoped_session_id(self, key: str, model: str, card_id: str = "") -> str:
        scope = f"model:{model}" if model else ""
        if card_id:
            scope = f"{scope}|card:{card_id}" if scope else f"card:{card_id}"
        return session_id_for(self.cfg.account.jid, key,
                              self.cfg.policy.session_id_version, scope)

    def _session_id(self, key: str, chat: _Chat, tier: str) -> str:
        """One session per contact and selected model/card.

        OpenCode stores a session's selected model. Reusing the default session
        after a switch makes the CLI report "model is not available"; a scope
        keeps each model's transcript separate and starts cleanly.
        """
        card = self._active_card(chat)
        model = self._active_model(chat, tier)
        return self._scoped_session_id(key, model, card.id if card else "")

    async def _handle_turn(self, key: str, chat: _Chat, item: dict) -> None:
        room = item.get("room")
        if room:
            sender = item.get("real_jid") or f"{key.split('/')[0]}/{item.get('nick')}"
            tier = item.get("tier") or GUEST
        else:
            sender = key
            tier = tier_for(sender, self.cfg.policy.owners)

        settings = _settings_for(self.cfg, self.cfg.admin if tier == OWNER else self.cfg.guest)
        model = self._active_model(chat, tier)
        if model:
            settings = replace(settings, model=model)
        who = f"{item.get('nick')} in the group chat {room}" if room else sender
        preamble = _turn_preamble(self.cfg, who, room)
        body = preamble + item["body"]
        card = self._active_card(chat)
        if card is not None:
            card_system = cards.render_system(card, sender)
            if card_system:
                body = (preamble + f"[Active model card: {card.name}]\n"
                        f"{card_system}\n\n" + item["body"])

        notes: list[str] = []
        files: list[str] = []
        saved_paths: list[Path] = []
        if self.cfg.features.uploads and self._upload_host:
            for url in uploads.find_links(body, self._upload_host):
                try:
                    saved = await uploads.download(url, self._spool / bare_jid(sender),
                                                   self.cfg.features.max_upload_mb)
                    saved_paths.append(saved)
                    log.info("downloaded attachment from %s to %s", sender, saved)
                except Exception as exc:  # noqa: BLE001
                    log.warning("attachment download failed: %s", exc)
                    notes.append(f"[attachment {url} could not be downloaded]")
                    continue
                # Attach images and audio so a multimodal model actually sees
                # or hears them; hand the path of anything else to the owner
                # tier only. Guests get no host paths.
                suffix = saved.suffix.lower()
                is_image = suffix in IMAGE_MIME
                is_audio = suffix in AUDIO_MIME
                if (is_image or is_audio) and self.cfg.features.inline_images:
                    kind = "image" if is_image else "audio"
                    try:
                        if saved.stat().st_size <= self.cfg.features.max_inline_image_mb * 1024 * 1024:
                            files.append(str(saved))
                            notes.append(f"[{kind} attached: {saved.name}]" if tier == OWNER
                                         else f"[{kind} attached]")
                        else:
                            notes.append(f"[{kind} too large to attach]")
                    except OSError:
                        notes.append(f"[{kind} could not be read]")
                elif tier == OWNER:
                    notes.append(f"[attachment downloaded to {saved}]")
                else:
                    notes.append("[attachment received]")
        if notes:
            body = body + "\n\n" + "\n".join(notes)

        # An attachment must not be thrown away by a follow-up message.
        chat.media_turn = bool(saved_paths)
        # One session id per (bot, person), derived — never remembered on disk —
        # so every device of a user shares one transcript and nothing can fork it.
        sid = self._session_id(key, chat, tier)
        chat.session_ids.add(sid)
        turn = cli.Turn(user=body, session_id=sid, agent=settings.agent,
                        files=tuple(files[:MAX_IMAGES_PER_TURN]))
        chat.session_id, chat.agent, chat.settings = sid, settings.agent, settings
        chat.stop_event = threading.Event()

        keepalive: asyncio.Task | None = None
        if not room:
            self._chat_state(key, "composing")
            # Chat states expire client-side (~30s), so refresh while the turn runs.
            keepalive = asyncio.create_task(self._composing_keepalive(key))

        # The CLI's JSONL events arrive from the reader thread; forward the ones
        # we care about to the event loop.
        loop = asyncio.get_running_loop()
        notices = [0]

        def _on_event(event: dict) -> None:
            etype = str(event.get("type") or "")
            if etype == "tool_use" and self.cfg.features.interim_messages and not room:
                if notices[0] >= self.cfg.features.max_interim_messages:
                    return
                part = event.get("part") if isinstance(event.get("part"), dict) else {}
                tool = str(part.get("tool") or "tool")
                notices[0] += 1
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._reply(key, item, f"🔧 {tool}")))
            if not room:
                loop.call_soon_threadsafe(self._chat_state, key, "composing")

        def _on_start(proc) -> None:
            chat.proc = proc

        try:
            try:
                answer = await asyncio.to_thread(
                    cli.run, settings, turn, _on_event, _on_start, chat.stop_event)
            except cli.CLIError as exc:
                # ``exc.detail`` can quote paths or provider errors, so only the
                # fixed user-facing line reaches the log.
                log.error("agent error for %s: %s", key, exc.user_message)
                if not chat.suppress:
                    await self._reply(key, item, exc.user_message)
                return
            finally:
                chat.proc = None
                chat.stop_event = None

            if chat.suppress or answer.status == "cancelled":
                log.info("answer for %s discarded (interrupted)", key)
                return
            reply = answer.text
            if not reply.strip():
                return

            # Optional belt-and-braces compaction. opencode compacts
            # automatically; this just asks it to do so at a predictable point.
            count = self._turn_counts.get(sid, 0) + 1
            self._turn_counts[sid] = count
            if self.cfg.opencode.compact_after_turns and count % self.cfg.opencode.compact_after_turns == 0:
                asyncio.create_task(asyncio.to_thread(cli.compact, settings, sid))

            clean, files = uploads.extract_markers(reply)
            # A reaction is sent before the text: it is the acknowledgement, and
            # it must not wait behind an upload or a second chunk.
            clean, emoji = reactions.extract_markers(clean)
            if emoji:
                await self._send_reaction(item.get("id") or "", emoji,
                                          to=room or key,
                                          mtype="groupchat" if room else "chat")
                if not clean and not files:
                    log.info("reaction-only reply for %s", key)
                    return
            attachment_urls: list[str] = []
            if files and self.cfg.features.uploads and tier == OWNER:
                for path in files:
                    try:
                        attachment_urls.append(
                            await uploads.upload_encrypted(
                                self.plugin["xep_0363"], path,
                                self.cfg.features.max_upload_mb))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("upload of %s failed: %s", path, exc)
                        clean += f"\n(could not upload {path.name}: {exc})"

            log.info("reply for %s ready: %d chars, %d attachment(s), %d chunk(s)",
                     key, len(clean), len(attachment_urls), len(split_message(clean, self.cfg.policy.max_chunk_chars)))
            for chunk in split_message(clean, self.cfg.policy.max_chunk_chars):
                if chat.suppress:
                    log.info("stopped sending to %s (interrupted mid-reply)", key)
                    return
                await self._reply(key, item, chunk)
                await asyncio.sleep(0.2)
            for url in attachment_urls:
                await self._send_attachment(key, item, url)
        finally:
            chat.media_turn = False
            chat.proc = None
            chat.stop_event = None
            if keepalive is not None:
                keepalive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keepalive
            if not room and not chat.suppress:
                self._chat_state(key, "active")
            # Nothing the contact sent stays on disk: the spool is not an archive.
            for path in saved_paths:
                with contextlib.suppress(OSError):
                    path.unlink()

    # -- outbound -----------------------------------------------------------

    async def _reply(self, key: str, item: dict, text: str) -> None:
        room = item.get("room")
        if room:
            await self._send_room(item, text)
            return
        if item.get("encrypted") and self.cfg.features.omemo:
            if await self._send_encrypted(key, text):
                return
            mode = self.cfg.features.omemo_mode
            hint = "" if mode == "auto" else f" This bridge is set to OMEMO {mode} only."
            self.send_message(mto=key, mtype="chat",
                              mbody=f"(I couldn't encrypt that reply for your device.{hint})")
            return
        self._send(key, text)

    async def _encrypt_room(self, room: str, text: str):
        """Build an OMEMO 2 groupchat stanza for ``room``, or raise."""
        return await asyncio.wait_for(
            omemo.encrypt_group(self, room, text, self._room_recipients(room),
                                self.bot_bare),
            timeout=45)

    async def _send_room(self, item: dict, text: str) -> None:
        """A reply in a room: encrypted with OMEMO 2 when that room asks for it.

        A room that is meant to be encrypted never gets a plaintext fallback: if the
        message cannot be encrypted (a member without an OMEMO device, an anonymous
        room with no member JIDs) the bridge says so in the room instead of leaking
        the content.
        """
        room = item["room"]
        if self.cfg.rooms.encrypt and self.cfg.features.omemo:
            stanza = None
            try:
                stanza = await self._encrypt_room(room, text)
            except Exception as exc:  # noqa: BLE001 - trust may just be missing
                log.warning("could not encrypt a room reply for %s: %s", room, exc)
                try:
                    # A member whose devices were only ever seen on the other
                    # OMEMO backend has no trusted device, which refuses the
                    # whole message; trusting what we know fixes that.
                    await omemo.trust_room_devices(self, self._room_recipients(room))
                    stanza = await self._encrypt_room(room, text)
                except Exception as retry:  # noqa: BLE001 - give up on encryption
                    log.warning("still cannot encrypt for %s after forcing trust: %s",
                                room, retry)
            if stanza is None:
                if not self.cfg.rooms.plaintext_fallback:
                    self.send_message(
                        mto=room, mtype="groupchat",
                        mbody="I can't encrypt to this room right now, so I won't send that. "
                              "Everyone here needs an OMEMO-capable client, and the room must "
                              "not hide member addresses.")
                    return
                # Explicitly opted in: an answer in the clear beats no answer.
                log.warning("sending an unencrypted reply to %s", room)
                self.send_message(mto=room, mtype="groupchat",
                                  mbody="[unencrypted] " + text)
                return
            stanza.send()
            log.info("sent encrypted room reply to %s (%d chars)", room, len(text))
            return
        self.send_message(mto=room, mbody=text, mtype="groupchat")

    def _room_recipients(self, room: str) -> set[str]:
        """Bare JIDs of everyone in the room (empty for anonymous rooms).

        XEP-0045 keeps ``rooms[pfrom][room][nick]``; pfrom is None unless the client
        uses multi-from addressing, so look through every bucket.
        """
        plugin = self.plugin["xep_0045"]
        roster: dict = {}
        for bucket in plugin.rooms.values():
            found = bucket.get(JID(room)) or bucket.get(room)
            if found:
                roster = found
                break
        out: set[str] = set()
        for nick in list(roster):
            try:
                jid = plugin.get_jid_property(JID(room), nick, "jid")
            except Exception:  # noqa: BLE001 - the nick left between the two calls
                jid = None
            if jid:
                out.add(str(JID(jid).bare))
        return out

    async def _send_encrypted(self, to: str, text: str, stanza=None,
                              what: str = "reply") -> bool:
        plugin = self.plugin["xep_0384"]
        mode = self.cfg.features.omemo_mode
        if mode in ("2", "legacy"):
            # Refuse a silent downgrade: if the contact's devices exist but none
            # speaks the pinned version, say so instead of encrypting with the other.
            wanted = OMEMO2_NAMESPACE if mode == "2" else LEGACY_NAMESPACE
            try:
                manager = await plugin.get_session_manager()
                devices = await manager.get_device_information(JID(to).bare)
            except Exception:  # noqa: BLE001 - an unreadable list must not wedge the reply
                devices = frozenset()
            if devices and not any(wanted in device.namespaces for device in devices):
                log.warning("omemo_mode=%s: %s has no device for %s", mode, to, wanted)
                return False
        try:
            if stanza is None:
                stanza = self.make_message(mto=to, mtype="chat", mbody=text)
            # A bundle fetch for a stale device must never wedge the conversation.
            encrypted, errors = await asyncio.wait_for(
                plugin.encrypt_message(stanza, JID(to), identifier=to), timeout=45)
            if encrypted is None:
                log.warning("nothing to encrypt for %s (errors=%s)", to, errors)
                return False
            encrypted.send()
            log.info("sent encrypted %s to %s (%d chars, %d device errors)",
                     what, to, len(text), len(errors))
            return True
        except asyncio.TimeoutError:
            log.error("OMEMO encryption for %s timed out after 45s", to)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("OMEMO encryption for %s failed: %s", to, exc)
            return False

    def _send(self, to: str, text: str) -> None:
        try:
            self.send_message(mto=to, mbody=text, mtype="chat")
            log.info("sent reply to %s (%d chars)", to, len(text))
        except Exception:  # noqa: BLE001
            log.exception("failed to send to %s", to)

    async def _send_attachment(self, key: str, item: dict, url: str) -> None:
        room = item.get("room")
        if not room and item.get("encrypted") and self.cfg.features.omemo:
            # The URL contains the AES-GCM key, so it must travel inside OMEMO.
            if await self._send_encrypted(key, url, what="attachment link"):
                return
            log.warning("could not encrypt an attachment link for %s; not sending it", key)
            self._send(key, "I couldn't encrypt the attachment link, so I didn't send it.")
            return
        if room and self.cfg.rooms.encrypt and self.cfg.features.omemo:
            log.warning("refusing to send an attachment link in the encrypted room %s", room)
            self._send(room, "I can't send attachments securely into this room.")
            return
        try:
            msg = self.make_message(mto=room or key,
                                    mtype="groupchat" if room else "chat",
                                    mbody=url)
            msg["oob"]["url"] = url
            msg.send()
        except Exception:  # noqa: BLE001
            log.exception("failed to send attachment link %s", url)

    async def _send_reaction(self, target_id: str, emojis: list[str], *, to: str,
                             mtype: str = "chat") -> bool:
        """React to the message ``target_id`` (XEP-0444), encrypted when possible."""
        if not target_id:
            log.debug("no stanza id to react to for %s", to)
            return False
        if not emojis:
            return False
        try:
            msg = self.make_message(mto=to, mtype=mtype)
            self.plugin["xep_0444"].set_reactions(msg, target_id, emojis)
            msg.enable("store")  # keep the reaction in the contact's archive
        except Exception:  # noqa: BLE001 - a cosmetic feature must not break a turn
            log.exception("could not build a reaction for %s", to)
            return False
        if self.cfg.features.omemo and mtype == "chat":
            if await self._send_encrypted(to, "", stanza=msg, what="reaction"):
                return True
            mode = self.cfg.features.omemo_mode
            if mode != "auto":
                log.warning("reaction to %s dropped: omemo_mode=%s forbids a plaintext stanza",
                            to, mode)
                return False
            # Legacy OMEMO 0.3 encrypts the body only, and a reaction has none:
            # the stanza goes out in the clear rather than not at all.
            log.info("reaction to %s on id %s goes out unencrypted (no ciphertext for a "
                     "body-less stanza)", to, target_id)
        try:
            msg.send()
            log.info("sent reaction %s to %s on id %s", "".join(emojis), to, target_id)
            return True
        except Exception:  # noqa: BLE001
            log.exception("failed to send a reaction to %s", to)
            return False

    async def _reaction_outbox_loop(self, interval: float = 2.0) -> None:
        """Send reactions queued by the operator CLI (``opencode-xmpp react``).

        File-based on purpose: the running bridge owns the OMEMO sessions, so it
        has to be the one encrypting. A job is deleted only after it is handled.
        """
        await asyncio.sleep(5.0)  # let OMEMO finish loading its store first
        outbox = self._spool / reactions.OUTBOX_DIR
        while True:
            try:
                for path in sorted(outbox.glob("*.json")):
                    await self._drain_outbox(path)
            except Exception:  # noqa: BLE001 - the loop must survive a bad pass
                log.exception("reaction outbox pass failed")
            await asyncio.sleep(interval)

    async def _drain_outbox(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            to = bare_jid(str(data["to"]))
            text = data.get("text")
            if text:
                await self._notify(to, str(text))
                log.info("delivered queued message to %s", to)
                with contextlib.suppress(OSError):
                    path.unlink()
                return
            target_id = str(data["id"])
            emojis = [str(item) for item in data.get("reactions") or []]
        except Exception as exc:  # noqa: BLE001 - a poison job must not wedge the queue
            log.warning("discarding unreadable reaction job %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return
        await self._send_reaction(target_id, emojis, to=to)
        with contextlib.suppress(OSError):
            path.unlink()

    async def _composing_keepalive(self, to: str, interval: float = 15.0) -> None:
        """Re-send ``composing`` until cancelled: clients drop the state after ~30s."""
        while True:
            await asyncio.sleep(interval)
            self._chat_state(to, "composing")

    def _chat_state(self, to: str, state: str) -> None:
        try:
            msg = self.make_message(mto=to, mtype="chat")
            msg["chat_state"] = state
            msg.send()
        except Exception:  # noqa: BLE001 - cosmetic only
            log.debug("chat state %s failed", state, exc_info=True)
