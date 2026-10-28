"""Open WebUI-style model cards stored as JSON files.

A card lives in ``~/.opencode-xmpp/models/`` and is either a single object or a
list of objects.  ``models/guest/`` is for cards guests may pick, ``models/admin/``
for admin-only cards, and the root holds shared/legacy cards (admin-only, plus the
fallback ``default.json``).  Only the fields the bridge needs are read:
``id``/``name``, ``base_model_id``, ``params.system``, ``params.think`` and the
Ollama request options (``params.num_ctx``, ``params.temperature``, ``top_p``,
``top_k``, ``repeat_penalty``, ...).  The large ``meta`` blobs (including
base64 images) are ignored.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NAME = "default.json"

# Sampling options Ollama accepts in a request body.  Open WebUI stores the same
# names under ``params``; ``max_tokens`` is the OpenAI spelling of ``num_predict``.
_OPTIONS: dict[str, type] = {
    "temperature": float,
    "top_p": float,
    "top_k": int,
    "min_p": float,
    "typical_p": float,
    "tfs_z": float,
    "repeat_last_n": int,
    "repeat_penalty": float,
    "presence_penalty": float,
    "frequency_penalty": float,
    "mirostat": int,
    "mirostat_tau": float,
    "mirostat_eta": float,
    "num_keep": int,
    "num_predict": int,
    "num_batch": int,
    "seed": int,
}


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    base_model: str          # OpenCode model ID, e.g. ollama/acme/gemma4:latest
    base_id: str             # provider model ID, e.g. acme/gemma4:latest
    system: str
    think: bool | None
    num_ctx: int | None
    source: Path
    options: dict[str, object] = field(default_factory=dict)

    @property
    def model(self) -> str:
        """Alias used by the bridge for the OpenCode model ID."""
        return self.base_model


def default_dir() -> Path:
    return Path.home() / ".opencode-xmpp" / "models"


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _options_from(params: dict) -> dict[str, object]:
    """Typed Ollama request options from an Open WebUI ``params`` object."""
    options: dict[str, object] = {}
    for key, kind in _OPTIONS.items():
        value = params.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            options[key] = kind(value)
        except (TypeError, ValueError):
            continue
    if "num_predict" not in options:
        try:
            options["num_predict"] = int(params["max_tokens"])
        except (KeyError, TypeError, ValueError):
            pass
    stop = params.get("stop")
    if isinstance(stop, str) and stop:
        options["stop"] = [stop]
    elif isinstance(stop, list) and stop and all(isinstance(item, str) for item in stop):
        options["stop"] = list(stop)
    return options


def _card(entry: dict, source: Path) -> Card | None:
    if not isinstance(entry, dict):
        return None
    cid = _text(entry.get("id") or entry.get("slug") or entry.get("name"))
    name = _text(entry.get("name") or cid)
    if not cid or not name:
        return None
    base = _text(entry.get("base_model_id") or entry.get("baseModelId"))
    if not base:
        # Open WebUI exports a model that *is* a base model with
        # base_model_id: null, and its id is the Ollama model name
        # ("gemma4:26b"). Treat that as the base instead of dropping the card.
        base = cid
    if base.startswith("ollama/"):
        base = base.split("/", 1)[1]
    base = base.strip()
    if not base:
        return None

    params = entry.get("params")
    if not isinstance(params, dict):
        params = {}
    system = _text(params.get("system"))
    think = params.get("think")
    think = bool(think) if isinstance(think, bool) else None
    num_ctx = params.get("num_ctx")
    try:
        num_ctx = int(num_ctx) if num_ctx is not None else None
    except (TypeError, ValueError):
        num_ctx = None

    return Card(id=cid, name=name, base_model=f"ollama/{base}", base_id=base,
                system=system, think=think, num_ctx=num_ctx, source=source,
                options=_options_from(params))


def default_path(directory: Path | None = None) -> Path:
    """``default.json``: the card a chat starts with and ``model default`` uses."""
    root = Path(directory) if directory is not None else default_dir()
    return root / DEFAULT_NAME


def _read_default(path: Path) -> Card | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        card = _card(entry, path)
        if card is not None:
            return card
    return None


def load_default(directory: Path | None = None) -> Card | None:
    """The root ``default.json`` card, or None when it is missing or invalid."""
    return _read_default(default_path(directory))


def load_default_for(role: str, directory: Path | None = None) -> Card | None:
    """The role's default card: ``models/<role>/default.json``, else the root one."""
    root = Path(directory) if directory is not None else default_dir()
    card = _read_default(root / role / DEFAULT_NAME)
    if card is not None:
        return card
    return _read_default(root / DEFAULT_NAME)


def _matching_entry(data: object, card: Card) -> dict | None:
    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        found = _card(entry, card.source)
        if found is not None and found.id == card.id and found.base_id == card.base_id:
            return entry
    return None


def set_param(card: Card, key: str, value: object) -> Card:
    """Write ``params[key]`` back to the card's file and return the new card.

    Only the matching entry is touched, so a file holding several cards keeps the
    rest intact. ``meta`` and unknown fields survive.
    """
    try:
        data = json.loads(card.source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {card.source.name}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"{card.source.name} is not valid JSON: {exc}") from exc
    entry = _matching_entry(data, card)
    if entry is None:
        raise ValueError(f"{card.source.name} no longer contains card {card.id!r}")
    params = entry.get("params")
    if not isinstance(params, dict):
        params = entry["params"] = {}
    params[key] = value
    tmp = card.source.with_name(card.source.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, card.source)
    updated = _card(entry, card.source)
    if updated is None:  # pragma: no cover - the entry was valid a moment ago
        raise ValueError(f"{card.source.name} produced an invalid card")
    return updated


def _read_cards(root: Path) -> list[Card]:
    if not root.is_dir():
        return []
    cards: list[Card] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            card = _card(entry, path)
            if card is not None:
                cards.append(card)
    return cards


def load_cards(directory: Path | None = None) -> list[Card]:
    """Read every ``*.json`` card in one directory, skipping non-card files."""
    root = Path(directory) if directory is not None else default_dir()
    return _read_cards(root)


def cards_for_role(role: str, directory: Path | None = None) -> list[Card]:
    """The cards one trust tier may see and select.

    Guests get ``models/guest/`` only. Admins get the root (shared/legacy cards),
    ``models/admin/`` and ``models/guest/``, in that order, without duplicates.
    """
    root = Path(directory) if directory is not None else default_dir()
    if role == "guest":
        directories = [root / "guest"]
    else:
        directories = [root, root / "admin", root / "guest"]
    cards: list[Card] = []
    seen: set[tuple[str, str]] = set()
    for folder in directories:
        for card in _read_cards(folder):
            key = (card.id.casefold(), card.base_id.casefold())
            if key in seen:
                continue
            seen.add(key)
            cards.append(card)
    return cards


def find_card(cards: list[Card], query: str) -> Card | None:
    wanted = query.strip().casefold()
    if not wanted:
        return None
    for card in cards:
        if wanted in (card.id.casefold(), card.name.casefold()):
            return card
    for card in cards:
        if wanted in card.id.casefold() or wanted in card.name.casefold():
            return card
    return None


def _char_name(card: Card) -> str:
    # "Ada (Local)" -> "Ada"; the parenthetical is a card label, not a name.
    return re.sub(r"\s*\([^)]*\)\s*$", "", card.name).strip() or card.name


def render_system(card: Card, user: str) -> str:
    """The card's system prompt with the Open WebUI placeholders filled."""
    text = card.system
    if not text:
        return ""
    sender = (user or "").split("@", 1)[0] or "the user"
    return (text.replace("{{char}}", _char_name(card))
                .replace("{{user}}", sender)
                .replace("{char}", _char_name(card))
                .replace("{user}", sender))


def apply_params(config: dict, card: Card) -> list[str]:
    """Apply a card's thinking and Ollama options to its base model.

    Everything lands under ``providers.ollama.models.<base>.body.options``, which
    OpenCode sends with the request.  That way the card -- not the base model's
    Modelfile -- decides the context window and the sampling parameters.
    """
    from . import thinking

    changed: list[str] = []
    if card.think is not None:
        changed.extend(thinking.apply_thinking(config, [card.base_model], card.think))

    options = dict(card.options)
    if card.num_ctx is not None:
        options["num_ctx"] = card.num_ctx
    if options:
        models = (config.setdefault("providers", {})
                        .setdefault("ollama", {})
                        .setdefault("models", {}))
        model = models.setdefault(card.base_id, {})
        if card.num_ctx is not None:
            model.setdefault("limit", {})["context"] = card.num_ctx
        body = model.setdefault("body", {})
        if not isinstance(body, dict):
            body = model["body"] = {}
        request_options = body.get("options")
        if not isinstance(request_options, dict):
            request_options = body["options"] = {}
        request_options.update(options)
        if card.base_model not in changed:
            changed.append(card.base_model)
    return changed
