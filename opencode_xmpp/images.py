"""Text-to-image generation through OpenRouter or Venice.

The bridge calls these APIs itself, so the agent never needs to see a provider
key. Keys come from the bot user's OpenCode credential store and stay in
memory.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VENICE_URL = "https://api.venice.ai/api/v1/image/generate"


class ImageError(Exception):
    """A failure with a message that is safe to send to the contact."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


def _post_json(url: str, body: dict, api_key: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "opencode-xmpp/0.4",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise ImageError(f"The image provider returned HTTP {exc.code}.", detail) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ImageError("I couldn't reach the image provider.", str(exc)) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageError("The image provider returned an unreadable response.",
                         str(exc)) from exc
    if not isinstance(data, dict):
        raise ImageError("The image provider returned an unexpected response.")
    return data


def _data_uri_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    header, separator, payload = value.partition(",")
    if not separator or ";base64" not in header:
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None


def _openrouter_image(message: dict) -> bytes | None:
    candidates: list[object] = []
    images = message.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    candidates.append(image_url.get("url"))
                elif isinstance(image_url, str):
                    candidates.append(image_url)
                candidates.append(item.get("url"))
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                candidates.append(image_url.get("url"))
            elif isinstance(image_url, str):
                candidates.append(image_url)
    for candidate in candidates:
        blob = _data_uri_bytes(candidate)
        if blob:
            return blob
    return None


def _openrouter(model: str, prompt: str, api_key: str, timeout: float) -> bytes:
    if not model:
        raise ImageError("No OpenRouter image model is configured.")
    data = _post_json(OPENROUTER_URL, {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }, api_key, timeout)
    if data.get("error"):
        raise ImageError("OpenRouter rejected the image request.",
                         str(data.get("error"))[:500])
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ImageError("OpenRouter returned no image.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    blob = _openrouter_image(message if isinstance(message, dict) else {})
    if not blob:
        raise ImageError("OpenRouter returned text but no image.")
    return blob


def _venice(model: str, prompt: str, api_key: str, timeout: float,
            safe_mode: bool) -> bytes:
    if not model:
        raise ImageError("No Venice image model is configured.")
    data = _post_json(VENICE_URL, {
        "model": model,
        "prompt": prompt,
        "format": "png",
        "safe_mode": safe_mode,
    }, api_key, timeout)
    images = data.get("images")
    if not isinstance(images, list) or not images:
        omitted = data.get("content_policy_violation_count")
        if omitted:
            raise ImageError("Venice omitted the image under its content policy.")
        raise ImageError("Venice returned no image.")
    try:
        blob = base64.b64decode(str(images[0]), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("Venice returned an unreadable image.", str(exc)) from exc
    return blob


def generate(provider: str, model: str, prompt: str, api_key: str,
             timeout: float = 180.0, safe_mode: bool = True) -> bytes:
    """Generate one image and return its bytes. Raises ImageError."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageError("Tell me what image to make.")
    if len(prompt) > 4000:
        raise ImageError("That image prompt is too long.")
    if provider == "openrouter":
        return _openrouter(model, prompt, api_key, timeout)
    if provider == "venice":
        return _venice(model, prompt, api_key, timeout, safe_mode)
    raise ImageError(f"Unknown image provider {provider!r}.")


def suffix(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return ".png"
