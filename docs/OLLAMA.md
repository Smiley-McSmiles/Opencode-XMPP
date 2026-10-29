# Running on Ollama

opencode-xmpp does not talk to Ollama itself. It starts `opencode run`, and opencode
talks to Ollama. That means everything you configure for opencode applies to the bot
unchanged — and everything you learn here applies to your terminal too.

## 1. Point opencode at Ollama

OpenCode v2 detects a local Ollama automatically:

```sh
ollama serve          # usually already a system service
ollama pull gemma4
opencode models | grep ollama
# ollama/gemma4:latest
```

If nothing appears, declare the provider in the config file of the user that runs
the bridge:

```jsonc
// ~/.config/opencode/opencode.jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "ollama": {
      "options": { "baseURL": "http://127.0.0.1:11434/v1" },
      "models": {}
    }
  }
}
```

Check it without the bridge:

```sh
opencode run --model ollama/gemma4:latest "reply with the single word: ready"
```

## 2. Choose the bot's default card

Models are chosen through **cards**, not from a list of everything Ollama has. The
default card is:

```
~/.opencode-xmpp/models/default.json
```

```json
{
  "id": "gemma4:26b",
  "name": "Gemma 4 26B",
  "base_model_id": null,
  "params": { "think": false, "num_ctx": 65536, "temperature": 1.0 }
}
```

(`base_model_id: null` is how an Open WebUI export says "this id is the base model" —
see [MODEL-CARDS.md](MODEL-CARDS.md).)

`help` then lists your cards, and `model default` goes back to this one.

`[admin] model` is a fallback only, used when there is no `default.json`:

```toml
[admin]
agent = "xmpp-admin"
# model = "ollama/gemma4:latest"
```

The provider of `default.json` (or of `[admin] model` when it is set) is the only
provider this instance uses: an Ollama bot never shows or accepts hosted models, so a
private local chat cannot leave the host by accident.

## 3. Context window

A model's context has two independent settings, and getting them out of sync is the
usual cause of "it worked, then it froze":

| Setting | Who enforces it |
| --- | --- |
| `num_ctx` in the model's **Modelfile** | Ollama's default for that model |
| `limit.context` in opencode | opencode's budgeting and compaction threshold |
| `options.num_ctx` in the request body | Ollama, per request — overrides the Modelfile |

A card's `params.num_ctx` sets **both** of the latter two:

```json
{ "base_model_id": "gemma4:latest", "params": { "num_ctx": 65536 } }
```

That is why you do **not** need `ollama create` to raise the context of a model whose
Modelfile does not set it. The bridge writes:

```jsonc
{
  "providers": {
    "ollama": {
      "models": {
        "gemma4:latest": {
          "limit": { "context": 65536 },
          "body": { "options": { "num_ctx": 65536 } }
        }
      }
    }
  }
}
```

Two caveats:

- The value must fit in memory. A 64k context costs VRAM/RAM even when the
  conversation is short.
- If you set `num_ctx` on a card, select that card (`model <card-id>`); a plain
  `model ollama/gemma4:latest` uses the model's own default.

Check what a model declares:

```sh
ollama show gemma4:latest --modelfile | grep -i num_ctx
```

## 4. Long conversations and compaction

A local model that "hangs" after a long chat is usually one of three things:
the context is full and generation crawls, the model is being reloaded, or the turn
exceeded the bridge's timeout.

The bridge uses two layers:

1. **opencode's own compaction**, which summarises the session before it overflows.
2. **A periodic safety net:**

   ```toml
   [opencode]
   compact_after_turns = 8
   ```

   Every eighth answered turn, the bridge asks opencode to compact the session. `0`
   (the default) leaves everything to opencode.

Time limits are per tier:

```toml
[admin]
timeout_s = 600

[guest]
timeout_s = 180
```

If a turn is cut off at the timeout, the bridge says so instead of waiting forever.
Raise `timeout_s` for a slow large model; keep it low for guests.

Useful checks:

```sh
ollama ps                      # what is loaded, how much memory, how long it stays
journalctl --user -u opencode-xmpp-opencode -n 100 --no-pager
```

For a single-user bridge, keeping one model loaded and one request at a time avoids
constant reloads:

```sh
sudo systemctl edit ollama
# [Service]
# Environment="OLLAMA_KEEP_ALIVE=30m"
# Environment="OLLAMA_NUM_PARALLEL=1"
```

`OLLAMA_NUM_PARALLEL=1` matters: with parallel slots, one model can reserve several
times the context memory.

## 5. Thinking

Reasoning models like Gemma 4 can "think" before answering. Thinking is often what
makes a local model feel slow, and it is invisible over XMPP, so the bridge lets you
switch it off per model:

```text
think-off
```

That writes, for the configured Ollama models in `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "capabilities": { "reasoning": { "supported": false } },
  "body": { "think": false }
}
```

`think-on` removes the override. The same can be set per card with
`"params": { "think": false }`, which is the better choice: it applies only to that
card's model and survives `think-on`.

## 6. Vision

Images sent to the bot are downloaded, sniffed and passed to the model with
`opencode run --file`. That only does something if the model advertises vision:

```sh
ollama show gemma4:latest --modelfile | head -5
# capabilities: completion, vision, tools, thinking
```

Controls:

```toml
[features]
inline_images = true
max_inline_image_mb = 8     # per image
```

An image larger than `max_inline_image_mb` is not attached; the model is told the
image was too large. Guest senders get the image attached too, but never the host
path.

**Images and context.** A photo adds roughly 1,000–2,000 tokens. A card that leaves
`num_ctx` unset runs on Ollama's 4096 default and will fail with
`request (…) exceeds the available context size (4096 tokens)`. That is not a
vision problem: raise `params.num_ctx` on the card (8192 minimum, 32768 or 65536
comfortable) and reselect it (`model <card-id>`).

**Formats.** Ollama's vision input accepts JPEG and PNG. A GIF/WebP is passed through
by the bridge but rejected by Ollama with `invalid image input`; send a JPEG/PNG
instead.

## 7. Audio and voice notes

The bridge recognises `.m4a`, `.mp3`, `.ogg`, `.opus`, `.wav`, `.flac`, `.webm` and
`.aac`, including files whose URL has no extension (it sniffs the magic bytes), and
attaches them to the turn exactly like images.

**Whether the model can hear them is up to Ollama.** Check the capabilities:

```sh
curl -s http://127.0.0.1:11434/api/show \
  -d '{"model":"gemma4:latest"}' | jq .capabilities
```

If `audio` is not in the list, the model will ignore or reject the attachment. Current
Gemma builds advertise `completion`, `vision`, `tools` and `thinking` — not `audio`.
For reliable voice notes, either:

- use an Ollama model that advertises `audio`, or
- transcribe before the turn (for example `whisper.cpp`) and send the text.

There is no built-in transcription step yet; that is tracked as future work.

## 8. Several models

Add a card per model and switch per chat:

```text
model gemma-local
model qwen-coder
model default
```

Each selection gets its own OpenCode session, so switching never replays a context
into a model that cannot load it. Selections are per chat and reset when the bridge
restarts, which starts every chat on `default.json`. Edit `default.json` to change the
default for everyone.
