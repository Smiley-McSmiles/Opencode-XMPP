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

Several different "context" settings exist, and only one of them decides what Ollama
actually allocates:

| Setting | Enforced by | What it does |
| --- | --- | --- |
| `OLLAMA_CONTEXT_LENGTH` | the Ollama service | default context for models whose Modelfile has no `num_ctx` (Ollama's default is **4096**) |
| `num_ctx` in a model's **Modelfile** | Ollama | per-model override (needs an `ollama create` alias) |
| `limit.context` in opencode | opencode | budgeting and the compaction threshold — **does not change Ollama** |
| `body.options.num_ctx` | the provider | a request option; opencode's built-in Ollama provider does **not** forward it |

A card's `params.num_ctx` writes the last two. It keeps opencode's budget in step with
the model, but it **cannot raise Ollama's runtime context**: the request still runs at
4096 tokens. This is why a photo can fail with

```text
request (4461 tokens) exceeds the available context size (4096 tokens)
```

even when the card says `"num_ctx": 65536`.

### Raise Ollama's context

For a bridge, a service-wide default is the simplest and it needs no per-model copies:

```sh
sudo systemctl edit ollama
```

Add:

```ini
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=32768"
Environment="OLLAMA_NUM_PARALLEL=1"
```

then:

```sh
sudo systemctl daemon-reload
sudo systemctl restart ollama
ollama ps        # after the next turn: context_length should be 32768
```

`OLLAMA_NUM_PARALLEL=1` matters: parallel slots can each reserve the context, which
multiplies the KV-cache memory.

Set the card's `params.num_ctx` to the same value so opencode's budget agrees with
Ollama:

```json
{ "base_model_id": "gemma4:26b", "params": { "num_ctx": 32768 } }
```

A blank context (4096) is enough for plain text but not for images or long chats. 32768
is a good default on a machine with 32 GB+ of RAM; 65536 if you have room.

### Per-model instead of global

If one model needs a different window, give it a Modelfile alias:

```sh
cat >/tmp/gemma-32k.Modelfile <<'MODELFILE'
FROM gemma4:26b
PARAMETER num_ctx 32768
MODELFILE
ollama create gemma4-32k -f /tmp/gemma-32k.Modelfile
```

then point the card's `base_model_id` at `gemma4-32k`. The global setting stays the
default for every other model.

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

   You can also do it on demand in chat with:

   ```text
   compact
   ```

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
vision problem: raise Ollama's context (`OLLAMA_CONTEXT_LENGTH`, 32768 or 65536) and
set the card's `num_ctx` to match it.

**Formats.** Ollama's vision input accepts JPEG and PNG. The bridge converts a
GIF/WebP to PNG automatically when ImageMagick (`magick` or `convert`) is installed;
without it, a GIF/WebP is not attached and the model is told the format is unsupported.
Send a JPEG/PNG if in doubt.

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
