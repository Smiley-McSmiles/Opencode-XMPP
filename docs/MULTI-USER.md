# Owners, guests and several bots

opencode-xmpp is built for more than one person talking to it, without becoming a
remote shell for whoever finds the JID.

## Two tiers

Every message is classified **before** the model sees it:

```python
sender in [policy] owners  -> owner
anything else              -> guest
```

JIDs are compared in bare, case-folded form, so `local@example.org/phone` and
`LOCAL@example.org` are both the owner `local@example.org`.

| | owner | guest |
| --- | --- | --- |
| Agent | `xmpp-admin` | `xmpp-guest` |
| Tools | shell, files, projects, web | read, glob, grep, web |
| May edit files | yes | no |
| May run commands | yes | no |
| May use subagents | no (by agent rule) | no (by agent rule) |
| Default timeout | 600 s | 180 s |
| Bridge commands | all | `stop`, `help`, `model`, `clear` |

The tier is not a prompt. It selects a different agent, and the agent's permission
rules are enforced by opencode. A guest cannot talk its way into a shell command.

Guests can still choose **which card answers them**, but only from the guest shelf:

```
~/.opencode-xmpp/models/
├── *.json            root cards — owner only
├── admin/*.json      owner only
└── guest/*.json      visible and selectable by guests
```

`help` for a guest lists only `guest/`; `model <id>` refuses an admin card even if the
id is known. Guest defaults are `models/guest/default.json`, falling back to
`models/default.json`. See [MODEL-CARDS.md](MODEL-CARDS.md).

The shipped agents live in [`agents/`](../agents); the installer copies them to
`~/.config/opencode/agents/`. Edit them there if you want different rules — just
remember `xmpp-admin` already has everything, so its value is what it *denies*.

## Give the bot its own Unix user

**Do this.** The admin agent can run arbitrary commands as the user the bridge runs
as. If that user is your desktop account, one compromised owner JID (or one
prompt-injected file) is your whole home directory.

A dedicated user also makes the guest tier meaningful: `xmpp-guest` may read files, so
the files it can see should be a sandbox, not your documents.

```sh
sudo useradd --create-home --shell /usr/sbin/nologin opencode-bot
sudo -u opencode-bot -H bash -lc 'curl -fsSL https://opencode.ai/install | bash'
```

Then run the installer as that user, or move the deployment:

```sh
sudo -u opencode-bot -H ./install.sh \
    --jid opencode@example.org --owner local@example.org
loginctl enable-linger opencode-bot
```

The systemd unit is a **user** unit: it runs under `opencode-bot`'s own session, with
its own `HOME`, its own opencode config and its own sessions. Everything in
`~/.opencode-xmpp` belongs to that user.

## Several bots on one host

One XMPP account per bot, one Unix user per bot, one config directory per bot. They do
not share OMEMO identities, sessions, model cards or logs.

```
/home/llm-bot/.opencode-xmpp/      # local Ollama bot
    opencode.toml                  [admin] model = "ollama/gemma4:latest"
    models/                        local cards
/home/cloud-bot/.opencode-xmpp/    # hosted provider bot
    opencode.toml                  [admin] model = "openrouter/..."
```

Each gets:

```sh
./install.sh --jid llm-bot@example.org   --owner local@example.org --dir ~/.opencode-xmpp
./install.sh --jid cloud-bot@example.org --owner local@example.org --dir ~/.opencode-xmpp
```

The unit name comes from the bot JID's local part, so they are
`opencode-xmpp-llm-bot.service` and `opencode-xmpp-cloud-bot.service`.

The provider of each bot's `default.json` (or of `[admin] model` when it has no default
card) acts as a **provider fence**: an Ollama bot cannot switch to a hosted model and a
hosted bot cannot silently fall back to a local one. That matters when the promise is
"this conversation stays on this machine".

If two bots should not answer each other, put their JIDs in `[policy] ignore_jids`:

```toml
[policy]
ignore_jids = ["cloud-bot@example.org"]
```

Without that, two bots that are both in a room can loop.

## Guests in rooms

Room messages carry a real JID only if the room is non-anonymous. The bridge uses it:

- non-anonymous room: an owner JID gets the owner tier, everyone else guest;
- anonymous room: everyone is a guest, unless `[rooms] trust_nicks = true` and the
  nick is in `owner_nicks` — which is spoofable, so it is off by default.

`[rooms] respond = "mention"` (default) answers only when addressed by the bot's nick;
`"always"` answers every message.

For an encrypted room (`[rooms] encrypt = true`) the room must be non-anonymous, and
every member needs an OMEMO-capable client. The bridge will say it cannot encrypt
rather than post plaintext, unless you explicitly set `plaintext_fallback = true`.

## Sessions and privacy between people

- One OpenCode session per (bot, bare JID), derived, not stored by the bridge.
  Switching models or cards keeps that session; it does not create another.
- Older builds created extra model/card-scoped sessions. `sessions` finds them,
  `session <n|id>` continues one and `close <n|id>` deletes one. The chosen session is
  remembered across restarts, and the first message after upgrading adopts the most
  recent old session automatically.
- Your phone and your laptop share one transcript; another person has a different one.
- `clear` deletes only the sender's sessions.
- Bumping `[policy] session_id_version` (then restarting the bridge) gives **everyone**
  a fresh conversation without deleting anything:

  ```toml
  [policy]
  session_id_version = "2"
  ```

  Bump it after a model change if old sessions keep failing to load.

Sessions themselves live in opencode's storage (`~/.local/share/opencode`), not in
`~/.opencode-xmpp`; the bridge only derives their IDs.
