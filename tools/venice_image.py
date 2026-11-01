#!/usr/bin/env python3
"""Generate or edit an image with Venice AI.

Run as the bot user. The Venice key is read from that user's OpenCode
credential store (or VENICE_API_KEY), never printed and never put on a
command line.

Examples:
  venice_image.py --prompt "a red fox in snow" --out "$OPENCODE_XMPP_WORK/img/fox.png"
  venice_image.py --image "$OPENCODE_XMPP_WORK/img/src.png" --prompt "make it 3D" \
      --out "$OPENCODE_XMPP_WORK/img/edit.png" --no-safe-mode

The XMPP bridge sets ``OPENCODE_XMPP_WORK`` to the chat's private scratch
directory, which `clear` deletes. Run by hand, substitute any writable path.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.venice.ai/api/v1"
DEFAULT_GENERATE = "chroma"
DEFAULT_EDIT = "qwen-edit-uncensored"


def opencode_binary() -> str | None:
    local = Path.home() / ".opencode" / "bin" / "opencode"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("opencode")


def venice_key() -> str:
    key = os.environ.get("VENICE_API_KEY", "").strip()
    if key:
        return key
    binary = opencode_binary()
    if not binary:
        raise SystemExit("opencode is not installed; set VENICE_API_KEY instead")
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    try:
        result = subprocess.run(
            [binary, "--standalone", "auth", "export", "venice"],
            cwd=str(Path.home()), env=env, capture_output=True, text=True,
            timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"could not read the Venice credential: {exc}") from exc
    if result.returncode != 0:
        raise SystemExit("no Venice credential for this user; run opencode auth login venice")
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse the Venice credential: {exc}") from exc
    for item in data if isinstance(data, list) else []:
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, dict) and value.get("type") == "key" and value.get("key"):
            return str(value["key"])
    raise SystemExit("the stored Venice credential has no API key")


def post(path: str, body: dict, key: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "image/png,image/jpeg,image/webp,application/json",
            "User-Agent": "opencode-xmpp-image/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:600].decode("utf-8", "replace")
        raise SystemExit(f"Venice returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"could not reach Venice: {exc}") from exc


def image_bytes(raw: bytes, content_type: str, key: str) -> bytes:
    if raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8\xff"):
        return raw
    if "json" in content_type.lower() or raw[:1] in (b"{", b"["):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Venice returned an unreadable response: {exc}") from exc
        images = data.get("images") if isinstance(data, dict) else None
        if not images:
            raise SystemExit("Venice returned no image")
        try:
            return base64.b64decode(str(images[0]), validate=True)
        except (ValueError, TypeError) as exc:
            raise SystemExit(f"Venice returned an unreadable image: {exc}") from exc
    raise SystemExit(f"Venice returned an unexpected {content_type or 'response'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--image", help="source image path or URL (enables edit)")
    parser.add_argument("--model", help="Venice image model id")
    parser.add_argument("--aspect-ratio", default=None)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--safe-mode", dest="safe_mode", action="store_true")
    parser.add_argument("--no-safe-mode", dest="safe_mode", action="store_false")
    parser.add_argument("--hide-watermark", dest="hide_watermark", action="store_true")
    parser.add_argument("--no-hide-watermark", dest="hide_watermark", action="store_false")
    parser.set_defaults(safe_mode=False, hide_watermark=True)
    args = parser.parse_args()

    key = venice_key()
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.image:
        model = args.model or DEFAULT_EDIT
        image = args.image
        source = Path(image).expanduser()
        if source.is_file():
            image = base64.b64encode(source.read_bytes()).decode("ascii")
        elif not image.startswith(("http://", "https://")):
            raise SystemExit(f"source image not found: {args.image}")
        body = {
            "model": model,
            "prompt": args.prompt,
            "image": image,
            "output_format": "png",
            "safe_mode": args.safe_mode,
        }
        if args.aspect_ratio:
            body["aspect_ratio"] = args.aspect_ratio
        raw, content_type = post("/image/edit", body, key, args.timeout)
    else:
        model = args.model or DEFAULT_GENERATE
        body = {
            "model": model,
            "prompt": args.prompt,
            "format": "png",
            "return_binary": True,
            "safe_mode": args.safe_mode,
            "hide_watermark": args.hide_watermark,
        }
        if args.aspect_ratio:
            body["aspect_ratio"] = args.aspect_ratio
        raw, content_type = post("/image/generate", body, key, args.timeout)

    blob = image_bytes(raw, content_type, key)
    out.write_bytes(blob)
    out.chmod(0o600)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
