# Model cards

A model card bundles everything that makes a model *yours*: a base Ollama model, a
persona, and the sampling and context settings to run it with. Cards are switched
per chat with `model <card-id>`, so one bot can be a coding assistant in one
conversation and a character in another.

- [The format](#the-format)
- [Where cards live](#where-cards-live)
- [The default card](#the-default-card)
- [Fields](#fields)
- [Examples](#examples)
- [Switching cards](#switching-cards)
- [How it is applied](#how-it-is-applied)
- [Troubleshooting](#troubleshooting)

## The format

The bridge reads **Open WebUI model cards**. That is Open WebUI's own export for a
model, which looks like this:

```json
{
  "id": "gemma-local",
  "name": "Gemma (Local)",
  "base_model_id": "gemma4:latest",
  "params": {
    "system": "You are {{char}}...",
    "temperature": 0.7
  },
  "meta": { "description": null, "profile_image_url": null }
}
```

> **This is not an Ollama format.** Ollama's native JSON is what `ollama show --json`
> prints (model details, template, parameters), and a Modelfile is text. Neither is
> read by the bridge. The card is the Open WebUI object above, and the bridge maps its
> `params` onto an Ollama **request**, which is what Open WebUI does too.

The file may contain one object or an array of objects:

```json
[ { "id": "a", "base_model_id": "gemma4:latest", "params": {} },
  { "id": "b", "base_model_id": "qwen3:14b",     "params": {} } ]
```

Any other JSON in the folder is skipped silently, so an unrelated file cannot break
startup.

## Where cards live

```
~/.opencode-xmpp/models/*.json
```

The folder is fixed (it is not a config setting). Create it and drop files in:

```sh
mkdir -p ~/.opencode-xmpp/models
cp examples/models/default.json ~/.opencode-xmpp/models/default.json
cp examples/models/gemma-local.json ~/.opencode-xmpp/models/
chmod 600 ~/.opencode-xmpp/models/*.json
```

Then send `help` from an owner JID; the card list appears at the bottom.

You do **not** need to restart the bridge: cards are read on every `help` and every
`model` command.

## The default card

`~/.opencode-xmpp/models/default.json` is special:

- every chat that has not selected another card starts with it;
- `model default` loads it;
- `help` marks it `[default]`;
- `think-off` / `think-on` edit it when no other card is active.

It uses the same format as any card. A minimal one:

```json
{
  "id": "gemma-default",
  "name": "Gemma (Default)",
  "base_model_id": "gemma4:latest",
  "params": {
    "system": "You are {{char}}, a concise assistant talking to {{user}} over XMPP.",
    "think": false,
    "num_ctx": 32768
  }
}
```

If `default.json` is missing, chats fall back to `[admin] model` (or `[guest] model`
for guests). `model default` then clears the selection and reports which model is being
used instead.

The default card is also the last word on the provider restriction when `[admin] model`
is empty: a `default.json` whose base is `ollama/...` keeps the bridge on Ollama cards.

Add or edit `default.json` at any time. The bridge applies it at startup and whenever
`model default` is sent; restart the bridge after editing it to apply it to every chat.

## Fields

### Identity

| Field | Required | Meaning |
| --- | --- | --- |
| `base_model_id` | yes* | Ollama model to run, e.g. `gemma4:latest` or `ollama/gemma4:latest`. |
| `id` | yes* | The name you type after `model`. Falls back to `slug` or `name`. |
| `name` | yes* | Display name in `help`. Falls back to `id`. |

\* At least one of `id`/`name` must exist; the card is skipped otherwise.

**`base_model_id: null`** is what Open WebUI writes when the model *is* a base model.
In that case the card's `id` is the Ollama model name:

```json
{ "id": "gemma4:26b", "name": "Gemma 4 26B", "base_model_id": null,
  "params": { "num_ctx": 65536 } }
```

runs `ollama/gemma4:26b`. The extra Open WebUI fields in an export (`version_id`,
`user_id`, `access_grants`, the base64 image inside `meta`, ...) are preserved in the
file but ignored by the bridge.

`base_model_id` must be a model opencode can see (`opencode models`). If the bridge is
restricted to the `ollama` provider, only Ollama bases are accepted.

### Persona

| Field | Meaning |
| --- | --- |
| `params.system` | System prompt for the chat. |
| `{{char}}` / `{char}` | Replaced with the card name, minus a trailing parenthetical: `Ada (Local)` → `Ada`. |
| `{{user}}` / `{user}` | Replaced with the sender's JID local part: `local@example.org` → `local`. |

The persona is sent as a prefix on the user's message for the active card, not
written into the session. Change the card, change the persona.

### Thinking

| Field | Meaning |
| --- | --- |
| `params.think` | `true` or `false`. `false` disables Ollama reasoning for this model. |

`think-off` and `think-on` write this field in the **active card** (or in
`default.json` when no card is selected), so the JSON is the single source of truth.

Anything other than a JSON boolean is ignored.

### Context

| Field | Meaning |
| --- | --- |
| `params.num_ctx` | Context window in tokens. Becomes both `limit.context` (opencode's budget) and `body.options.num_ctx` (what Ollama actually allocates). |

### Sampling and request options

Every key below is copied into `body.options`. Values are coerced to the right type;
a value that cannot be coerced is skipped instead of failing the card.

| Key | Type | Notes |
| --- | --- | --- |
| `temperature` | float | |
| `top_p` | float | |
| `top_k` | int | |
| `min_p` | float | |
| `typical_p` | float | |
| `tfs_z` | float | |
| `repeat_last_n` | int | |
| `repeat_penalty` | float | |
| `presence_penalty` | float | |
| `frequency_penalty` | float | |
| `mirostat` | int | `0`, `1` or `2` |
| `mirostat_tau` | float | |
| `mirostat_eta` | float | |
| `num_keep` | int | |
| `num_predict` | int | Maximum generated tokens |
| `max_tokens` | int | OpenAI spelling; used as `num_predict` when that is absent |
| `num_batch` | int | |
| `seed` | int | |
| `stop` | string or list of strings | Sent as a list |

Ignored on purpose: everything under `meta` (base64 images, descriptions, tags), and
Open WebUI-only keys the bridge does not map.

## Examples

### Local assistant

```json
{
  "id": "gemma-local",
  "name": "Gemma (Local)",
  "base_model_id": "gemma4:latest",
  "params": {
    "system": "You are {{char}}, a concise assistant talking to {{user}} over XMPP. Keep replies short.",
    "think": false,
    "num_ctx": 32768,
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "repeat_penalty": 1.1
  }
}
```

### Character with a bigger window

```json
{
  "id": "ada",
  "name": "Ada (Story)",
  "base_model_id": "gemma4:26b-64k",
  "params": {
    "system": "You are {{char}}. Stay in character with {{user}} at all times.",
    "num_ctx": 65536,
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 64,
    "repeat_penalty": 1.05,
    "max_tokens": 1024
  }
}
```

Ready-to-copy cards are in [`examples/models/`](../examples/models):

- `default.json` — a base-model export (`base_model_id: null`) for `gemma4:26b`
- `gemma-local.json` — a persona with an explicit `base_model_id`

## Switching cards

From an owner JID, in a direct chat:

```text
model ada
```

The reply names the card ID, its display name and the base model. `help` lists every
card the bridge can see. Matching is exact on `id` or `name` first, then a
case-insensitive substring, so `model ada` finds `Ada (Story)`.

```text
model default
```

loads `default.json` (or, if there is none, resets the chat to `[admin] model`).

Model and card selections are **per chat** and are forgotten when the bridge
restarts. Each selection also gets its own OpenCode session, so switching models does
not try to continue a context created for another model.

## How it is applied

When you select a card, the bridge updates the opencode config of the user running
the bridge (`~/.config/opencode/opencode.jsonc`, or `opencode.json` if that exists):

```jsonc
{
  "providers": {
    "ollama": {
      "models": {
        "gemma4:latest": {
          "limit": { "context": 32768 },
          "capabilities": { "reasoning": { "supported": false } },
          "body": {
            "think": false,
            "options": {
              "num_ctx": 32768,
              "temperature": 0.7,
              "top_p": 0.9,
              "top_k": 40,
              "repeat_penalty": 1.1
            }
          }
        }
      }
    }
  }
}
```

Things to know:

- **The file must exist.** If neither `opencode.jsonc` nor `opencode.json` is present,
  the card's persona and model still apply, but there is nowhere to write the model
  options. Create an empty one:

  ```sh
  mkdir -p ~/.config/opencode
  printf '{"$schema": "https://opencode.ai/config.json"}\n' \
      > ~/.config/opencode/opencode.jsonc
  ```

- **The file is rewritten as plain JSON.** Comments (`//`, `/* */`) are understood
  when reading, so a commented file does not break selection, but they are not
  preserved. Keep a copy if you care about them.
- **Settings are global to that model ID.** Two cards on the same base model share
  one entry; the last card selected wins for the shared options.
- **Only the model entry is touched.** Other providers and settings are kept.
- **`think-off` / `think-on` update the card first.** The bridge rewrites
  `params.think` in the card's JSON, then applies the card again, so the file and
  opencode never disagree.
- **The JSON is written for humans.** Card edits are re-serialised with two-space
  indentation, one field per line, and a trailing newline — never a single line. Open
  WebUI's own fields and `meta` are kept.

## Troubleshooting

**The card does not appear in `help`.**
Check that it is a `.json` file directly in `~/.opencode-xmpp/models/`, that it has
`base_model_id` and `id`/`name`, and that the base model is visible in
`opencode models`. A card whose base is not an allowed provider is hidden.

**`model <id>` says "No card matches".**
Selection is card-only; a raw model name (`ollama/gemma4:latest`) is not accepted.
Use the exact card ID from `help`; `help` prints `id — name` followed by
`base: ollama/...`. To run a model that has no card, add one with that
`base_model_id`.

**"I couldn't apply the card settings".**
The config file is not valid JSON, or is not writable by the bridge user. The reply
includes the parse error; fix the file and try again.

**The context did not change.**
`params.num_ctx` only takes effect when *that card* is active. If you edited
`default.json` while a chat had already selected another card, send `model default`.
Also confirm you edited the config of the user the bridge runs as, not your desktop
user's.
