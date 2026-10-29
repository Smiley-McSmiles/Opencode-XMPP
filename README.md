<h1 align="center">opencode-xmpp</h1>

<p align="center">
  <img src="assets/icon.svg" alt="opencode-xmpp" width="128" height="128">
</p>

<p align="center">
  <a href="https://github.com/Smiley-McSmiles/Opencode-XMPP/releases"><img src="https://img.shields.io/badge/version-0.4.0-blue.svg?style=flat-square" alt="Version 0.4.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="MIT License"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg?style=flat-square" alt="Python 3.11+"></a>
  <a href="https://xmpp.org"><img src="https://img.shields.io/badge/protocol-XMPP%20%7C%20OMEMO-purple.svg?style=flat-square" alt="XMPP with OMEMO"></a>
  <a href="https://ollama.com"><img src="https://img.shields.io/badge/models-Ollama%20local-orange.svg?style=flat-square" alt="Ollama (local)"></a>
  <a href="https://github.com/Smiley-McSmiles/Opencode-XMPP/actions"><img src="https://github.com/Smiley-McSmiles/Opencode-XMPP/actions/workflows/tests.yml/badge.svg?style=flat-square" alt="Tests"></a>
</p>

<p align="center">
  <b>Talk to your opencode agents from any XMPP client.</b><br>
  A small bridge that logs into a Jabber account, forwards your messages to the
  <a href="https://opencode.ai">opencode</a> CLI, and sends the answers back — over
  OMEMO, in direct chats and group rooms, with encrypted file transfers.<br>
  Works with Conversations, Gajim, Dino, Monal, Siskin, Ward and anything else that speaks XMPP.
</p>

<p align="center">
  <a href="docs/INSTALL.md"><b>Install</b></a> ·
  <a href="docs/OLLAMA.md"><b>Ollama setup</b></a> ·
  <a href="docs/MODEL-CARDS.md"><b>Model cards</b></a> ·
  <a href="docs/COMMANDS.md"><b>Chat commands</b></a> ·
  <a href="docs/SECURITY.md"><b>Security</b></a>
</p>

---

## 👥 Developers & Attribution

opencode-xmpp is developed and maintained by:

- **WOOSAH** (Lead Architect & Maintainer)
- **DeepSeek** (Engineer)

Project GitHub: [https://github.com/Smiley-McSmiles/Opencode-XMPP](https://github.com/Smiley-McSmiles/Opencode-XMPP)

## What this is

Your opencode setup already has agents, tools, permissions, sessions and model
providers. This project gives all of that a chat interface:

```
 your phone / laptop                your server
┌───────────────────┐      ┌──────────────────────────────────────────────┐
│ Conversations     │      │  opencode-xmpp                               │
│ Gajim · Dino      │◀────▶│   ├─ slixmpp client (OMEMO, MUC, uploads)     │
│ Monal · Siskin     │ XMPP │   ├─ two trust tiers (owner / guest)         │
└───────────────────┘      │   └─ one `opencode run` per turn             │
                           │            │                                 │
                           │            ▼                                 │
                           │   opencode CLI ── agents · tools · sessions  │
                           │            │                                 │
                           │            ▼                                 │
                           │   Ollama at 127.0.0.1:11434                  │
                           └──────────────────────────────────────────────┘
```

No new agent framework, no second API server, no prompt plumbing to maintain. The
bridge is a **transport**: it turns an XMPP message into
`opencode run --session <derived-id> --agent <tier>` and returns the answer.

One conversation per person, not per device. The session id is *derived* from the
bot JID and the sender's bare JID — no model or card scope — so a restart, a model
switch, a reinstall or a lost state file cannot fork someone into a second
conversation, and a phone, a laptop and a desktop share one transcript. If an older
build left extra sessions, `sessions` lists them, `session <n>` continues one and
`close <n>` deletes one.

## ✨ Features

**Two trust tiers, enforced by routing.**
The operator's JIDs (in `[policy] owners`) run the `xmpp-admin` agent with the full
toolset — shell, files, projects, web. Everyone else runs `xmpp-guest`: read, glob,
grep, web, no shell and no edits. The tier is decided from the JID before the model
sees anything; it is not prompt wording and it cannot be talked around.

**OMEMO 0.3 and OMEMO 2 / 0.8**, in direct chats and rooms, handled by one plugin.
`omemo_mode` is `auto` by default; pin it to `2` or `legacy` to refuse sending to a
contact whose devices lack that version. Device identities live in `omemo.json`, and
new devices are trusted on first use and logged at `WARNING`.

**Encrypted files, both directions.** When an agent writes `[[attach: /path]]`, the
bridge encrypts the file with **AES-256-GCM before upload** and sends an
`aesgcm://` link (XEP-0454). The HTTP upload service only ever stores ciphertext, and
in an OMEMO chat the link itself is sent encrypted — if encryption fails, the bridge
refuses to send the link rather than leak it. Inbound `aesgcm://` photos and voice
notes are decrypted for the model; only links on your configured upload host are
fetched (SSRF guard).

**Photos and voice notes reach the model.** Images and audio (`.m4a`, `.mp3`, `.ogg`,
`.opus`, `.wav`, `.flac`, `.webm`, `.aac`) are downloaded, sniffed by magic bytes and
passed to `opencode run --file`, so a vision model actually sees the photo and an
audio-capable model hears the voice note. Audio support still depends on the model
advertising `audio`; see [docs/OLLAMA.md](docs/OLLAMA.md).

**Cards are the model menu.** Drop an Open WebUI export (`*.json`) in
`~/.opencode-xmpp/models/` and switch with `model my-card`. `help` lists **only your
cards** — an id and a name, never every model Ollama happens to have and never a base
model path. The card's persona applies to your chat, and its `num_ctx`, `temperature`,
`top_p`, `top_k`, `repeat_penalty`, `seed` and friends are written into the Ollama
model's request options — so the card, not the base model's Modelfile, decides the
context window. `default.json` is the card every chat starts with and what
`model default` loads. See [docs/MODEL-CARDS.md](docs/MODEL-CARDS.md).

**Separate card shelves for owners and guests.** `models/admin/` and `models/guest/`
hold role-specific cards; the root holds shared/legacy cards. Admins see everything,
guests see and can select only `models/guest/` — so an adult or heavy card stays out of
a guest's `help` menu.

**Group chats (MUC)** that answer when addressed by nick, or to every message.
Encrypted rooms use OMEMO 2 over the XEP-0420 stanza-content envelope; the bridge
builds and parses that envelope itself, reads both versions, and refuses to fall back
to plaintext.

**Interrupt, stop, continue.** A new message interrupts the answer in flight and is
answered next in the same session; a bare `stop` stops without asking a new question.
Replies longer than `max_chunk_chars` are split on line boundaries with code fences
closed and re-opened. Typing indicators, delivery receipts and XEP-0444 reactions work.

**Owner commands that still work when the model is broken.** `help`, `model`,
`think-off`/`think-on`, `clear`, `restart`, `repair`, `invite`, `join`, `approve` —
handled by the bridge, no model turn, no token cost.

**Contact requests stay under your control.** Strangers are queued, not accepted; you
get a chat message and answer `approve <jid>` or `deny <jid>`.

## 🚀 Quick start

You need **Python 3.11+**, the **opencode CLI**, an **XMPP account** for the bot, and
**Ollama** if you are going local. The full walkthrough is in
[docs/INSTALL.md](docs/INSTALL.md).

```sh
git clone https://github.com/Smiley-McSmiles/Opencode-XMPP.git
cd Opencode-XMPP
./install.sh --jid opencode@example.org --owner local@example.org --service
```

That installs the bridge into `~/.opencode-xmpp`, the agents into
`~/.config/opencode/agents/`, and enables a systemd user unit. Then:

```sh
opencode-xmpp check      # config, secret, agents, CLI, XMPP reachability
```

Message the bot from `local@example.org`:

```text
help
```

You should get the command list, the current card and the cards the bridge can see.
Add `remote@example.org` (or anyone) as a contact and they get the read-only guest
tier.

> **`[policy] owners` is shell access to the host.** Treat it like an SSH key list.
> Run the bridge as its own Unix user, not as your desktop account. See
> [docs/SECURITY.md](docs/SECURITY.md).

## 🧠 Model cards

A model card is an **Open WebUI export** — not an Ollama Modelfile and not the JSON
that `/api/show` returns. Open WebUI stores a model as `id`/`name`/`base_model_id`
plus a `params` object, and that is exactly what the bridge reads:

```json
{
  "id": "gemma-local",
  "name": "Gemma (Local)",
  "base_model_id": "gemma4:26b-64k",
  "params": {
    "system": "You are {{char}}, a concise assistant. The user is {{user}}.",
    "think": false,
    "num_ctx": 65536,
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "repeat_penalty": 1.1,
    "max_tokens": 2048
  }
}
```

| Field | Effect |
| --- | --- |
| `base_model_id` | Ollama model to run. `gemma4:26b-64k` or `ollama/gemma4:26b-64k` both work; `null` (Open WebUI's base-model export) uses the card `id`. |
| `params.system` | Persona for the chat. `{{char}}` → card name, `{{user}}` → sender's local part. |
| `params.think` | `true`/`false` → Ollama reasoning on/off for this model. |
| `params.num_ctx` | OpenCode's budget (`limit.context`). It does **not** change Ollama's context; raise that with `OLLAMA_CONTEXT_LENGTH`. |
| `params.temperature`, `top_p`, `top_k`, `min_p`, `typical_p`, `tfs_z`, `repeat_last_n`, `repeat_penalty`, `presence_penalty`, `frequency_penalty`, `mirostat*`, `num_keep`, `num_predict`, `max_tokens`, `num_batch`, `seed`, `stop` | Passed as Ollama request options. |

Cards are per-chat: `model gemma-local` switches this conversation, `model default`
loads `default.json`, and switching cards keeps the same conversation — opencode 2.2
accepts a new model on an existing session, so a contact has **one session**, not one
per card. `think-off` / `think-on` edit the active card's JSON — no `ollama create`, no
second copy of the model. The big `meta` blob Open WebUI adds (base64 images,
descriptions) is ignored. A ready-to-copy example
lives in [`examples/models/default.json`](examples/models/default.json) (a base-model
export) and [`examples/models/gemma-local.json`](examples/models/gemma-local.json) (a
persona). Card files are always written back with two-space indentation and one field
per line.

Full reference: [docs/MODEL-CARDS.md](docs/MODEL-CARDS.md).

## ⚙️ Ollama

opencode v2 detects a local Ollama automatically. Confirm it:

```sh
ollama pull gemma4
opencode models | grep ollama
# ollama/gemma4:latest
```

Then give the bridge its default card — `~/.opencode-xmpp/models/default.json`:

```json
{
  "id": "gemma-local",
  "name": "Gemma (Local)",
  "base_model_id": "gemma4:latest",
  "params": { "think": false, "num_ctx": 32768, "temperature": 0.7 }
}
```

Every chat starts with it, and `model default` goes back to it. `[admin] model` is
only a fallback for when there is no `default.json`:

```toml
[admin]
agent = "xmpp-admin"
# model = "ollama/gemma4:latest"
```

The provider of `[admin] model` — or of `default.json` when that is unset — is the
only provider this instance will use, so a local Ollama bot never falls back to a hosted
model by accident.

**Long conversations.** Two things matter for a local model that freezes after a long
chat:

```toml
[opencode]
compact_after_turns = 8     # ask opencode to compact every 8 answered turns
```

and a context window big enough for the job. Ollama's default is only 4096 tokens, so
raise it on the service:

```sh
sudo systemctl edit ollama
# [Service]
# Environment="OLLAMA_CONTEXT_LENGTH=32768"
# Environment="OLLAMA_NUM_PARALLEL=1"
```

A card's `params.num_ctx` sets OpenCode's budget; it does **not** raise Ollama's context.
Match it to the service value so both agree. More in
[docs/OLLAMA.md](docs/OLLAMA.md#3-context-window).

## 💬 Chat commands

Handled by the bridge, so they work even when the model cannot call tools and never
cost a turn. Full reference: [docs/COMMANDS.md](docs/COMMANDS.md).

| Command | Who | What it does |
| --- | --- | --- |
| `stop` | anyone | Interrupt the answer in progress (`Stopped.`) without a new question |
| `help` / `?` | anyone, DM | Commands, current card and the cards your tier may use |
| `model <card-id>` | anyone, DM | Switch this chat to a card (guests: guest cards only) |
| `model default` | anyone, DM | Load the role's `default.json` for this chat |
| `think-off` / `think-on` | owner, DM | Save the reasoning toggle to the active card's JSON |
| `clear` | anyone, DM | Stop the turn and delete every known OpenCode session for this contact |
| `sessions` | anyone, DM | List this contact's OpenCode sessions, numbered |
| `session <number\|id>` | anyone, DM | Continue one of those sessions |
| `close <number\|id>` | anyone, DM | Delete one of those sessions |
| `compact` | anyone, DM | Ask opencode to compact this chat's session now |
| `restart` | owner, DM | Acknowledge, then restart this bridge's systemd unit |
| `repair [focus]` | owner, DM | Model-free diagnostics, sent as a redacted report |
| `invite <jid>` | owner, DM | Add a contact and send a presence subscription |
| `join <room@conference.host>` | owner, DM | Join a room now as `rooms.nick` |
| `approve <jid>` / `deny <jid>` / `pending` | owner | Handle waiting contact requests |

Anything else goes to the model. In a room, only `stop` is special; everything else is
ordinary text.

## 🏗️ Layout

```
opencode_xmpp/config.py       TOML + 0600 secret files
opencode_xmpp/policy.py       JID normalisation, owner/guest tiering, derived session ids
opencode_xmpp/bot.py          slixmpp client, per-conversation runner, chat commands
opencode_xmpp/opencode_cli.py one `opencode run` per turn; interrupt, compact, delete
opencode_xmpp/cards.py        Open WebUI model cards → persona + Ollama options
opencode_xmpp/thinking.py     reasoning overrides applied to opencode.jsonc
opencode_xmpp/uploads.py      XEP-0363 uploads (AES-GCM), XEP-0454 downloads
opencode_xmpp/omemo.py        OMEMO device management
opencode_xmpp/sce.py          XEP-0420 stanza-content envelope for encrypted rooms
opencode_xmpp/chunk.py        fence-aware message splitting
opencode_xmpp/reactions.py    XEP-0444 reactions
opencode_xmpp/approvals.py    contact requests and owner commands
opencode_xmpp/diagnostics.py  the model-free `repair` report
opencode_xmpp/__main__.py     CLI: run | check | init | unit | react | handoff
agents/                       xmpp-admin, xmpp-chat, xmpp-guest, xmpp-ops
examples/models/              copyable cards (default + persona)
docs/                         how-to guides
```

A deployment lives in `~/.opencode-xmpp/`:

```
opencode.toml          bot JID, owners, agents, limits
secrets/account.secret XMPP password, 0600, git-ignored
omemo.json             OMEMO identity and sessions — back this up
spool/                 per-contact attachment spool, emptied after each turn
models/                cards: default.json, admin/, guest/
venv/                  the bridge's own virtualenv
```

## 📚 Documentation

| Guide | What it covers |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | Server and XMPP setup, the installer, systemd, verification |
| [docs/OLLAMA.md](docs/OLLAMA.md) | Local models, context windows, compaction, vision and audio |
| [docs/MODEL-CARDS.md](docs/MODEL-CARDS.md) | The card format, every supported field, examples |
| [docs/COMMANDS.md](docs/COMMANDS.md) | Every chat command and what it does to sessions |
| [docs/MULTI-USER.md](docs/MULTI-USER.md) | Owners, guests, several bots, one Unix user per bot |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, hardening, what to back up |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common failures and how to read the logs |

Prefer the commented file? [`opencode.toml.example`](opencode.toml.example) documents
every setting.

## 🔒 Security model

- **`[policy] owners` is the tool-access list.** An owner turn runs the `xmpp-admin`
  agent with shell access. Anyone who can send as that JID can run commands.
- **Guests are guests.** They run `xmpp-guest`, whose permission rules deny `execute`,
  `edit`, `subagent` and `skill`. It can read files in the bot's workspace, so give the
  bot a dedicated Unix user with nothing to leak. Guests may pick a card from
  `models/guest/`, but cannot see or select root/admin cards.
- **The bridge does not store transcripts.** It derives session ids; OpenCode owns the
  sessions. Message text is never written to the log — only lengths and JIDs.
- **Secrets are files, 0600, never in git.** `secrets/account.secret` is the XMPP
  password. Model provider credentials belong to opencode (`opencode auth login`), not
  to this bridge.
- **Reports are redacted.** `repair` replaces contact JIDs with per-report aliases and
  is encrypted when the command arrived encrypted.
- **No arbitrary recipients.** The bridge only ever addresses a reply to the bare JID
  that wrote in. There is deliberately no "send to any JID" tool.

Read [docs/SECURITY.md](docs/SECURITY.md) before exposing the bot to the internet,
especially if guests will use it.

## ✅ Status & limitations

- Built for and tested against **Prosody** with `slixmpp` 1.17 and OpenCode v2.
- **Local Ollama is the supported provider today.** Hosted providers are not covered
  by the guides yet; the bridge itself is provider-agnostic, but the model-card
  option mapping and the thinking toggles are Ollama-specific.
- **Audio needs an audio-capable model.** The bridge attaches a voice note to the
  turn, but Ollama has to advertise `audio` for the model; current Gemma builds
  advertise `completion`, `vision`, `tools` and `thinking`, not `audio`.
- **Cards are the only selectable unit.** A chat that has selected a card keeps it
  until `model default` or a restart; every chat starts on `default.json`.
- Interfaces may still move, but the wire protocol is XMPP: your chat client does not
  care what happens behind the bridge.

Bug reports and pull requests are welcome in the
[issue tracker](https://github.com/Smiley-McSmiles/Opencode-XMPP/issues). If it is a
vulnerability, please report it privately instead.

## 📄 License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it.
