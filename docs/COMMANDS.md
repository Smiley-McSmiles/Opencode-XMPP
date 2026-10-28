# Chat commands

Commands are handled by the bridge itself. They never reach the model, never cost a
turn, and keep working when the model provider, the CLI or OMEMO is the thing that is
broken.

| Command | Who | Where | What it does |
| --- | --- | --- | --- |
| [`stop`](#stop) | anyone | DM | Interrupt the current answer |
| [`help`](#help) | owner | DM | Commands, current card, available cards |
| [`model`](#model) | owner | DM | Switch this chat to a model card |
| [`think-off` / `think-on`](#thinking) | owner | DM | Save reasoning on/off in the active card |
| [`clear`](#clear) | anyone | DM | Delete this contact's OpenCode sessions |
| [`repair`](#repair) | owner | DM | Model-free diagnostics |
| [`restart`](#restart) | owner | DM | Restart this bridge's unit |
| [`invite`](#invite) | owner | DM | Add a contact |
| [`join`](#join) | owner | DM | Join a room |
| [`approve` / `deny` / `pending`](#contact-requests) | owner | DM | Handle contact requests |

"DM" means a direct chat. In a room, every command except `stop` is ordinary text,
because room history is shared and a command there could affect other people.

Matching is forgiving about case and a trailing comma or colon (`Help:` works), but a
command is a whole message: `help me debug this` goes to the model.

## stop

```text
stop
```

Clears anything queued, stops the answer in flight and replies `Stopped.`. It does not
start a new question and does not delete the session; the next message continues where
the conversation was. Available to everyone, including guests.

Without `stop`, sending a new message while the agent is working counts as an
interrupt: the current answer is dropped and your new text is answered next, in the
same session. Set `[policy] on_new_message = "queue"` if you prefer answers to finish
first. A turn that carries a photo or voice note is never interrupted.

## help

Owner only. Lists:

- every command,
- the card (and base model) active in this chat,
- the model cards in `~/.opencode-xmpp/models/`, with their base model.

`help` lists **cards only** — it never dumps every model Ollama has. The default card is
marked `[default]`. If the bridge is restricted to a provider (from `[admin] model` or
`default.json`), cards on other providers are hidden.

If you have no cards yet, the list says so and points at the folder.

## model

```text
model <card-id>     # switch this chat to a model card
model default       # load default.json
```

Selection is **card-only**. Matching is exact on `id` or `name`, then a case-insensitive
substring, so `model ada` finds `Ada (Story)`. A raw model name such as
`ollama/gemma4:latest` is rejected with a hint to add a card whose `base_model_id` is
that model — the card is where the context and sampling settings live.

`model default` loads `~/.opencode-xmpp/models/default.json`. If that file does not
exist, the chat goes back to `[admin] model` and the reply says so.

Switching is per chat and per card: each selection gets its own OpenCode session, so
a context created for one model is never replayed into another. The selection is
forgotten when the bridge restarts, and every chat starts on `default.json`.

```text
Model switched to card Ada (Story) via ada for this chat (base: ollama/gemma4:26b-64k).
```

## think-off / think-on

```text
think-off
think-on
```

Disables or enables reasoning **in the card**, not in a copy of the model. The bridge:

1. rewrites `params.think` in the active card's JSON (`default.json` when no other card
   is selected),
2. applies the card's options to `~/.config/opencode/opencode.jsonc`,
3. answers with the card name and the file it saved.

That means the setting survives restarts, is visible in the JSON, and is per card: turn
thinking off for a chatty companion and leave it on for a coder. Edit the JSON by hand
instead if you prefer — `"params": { "think": false }` is the same thing.

There is no active card to edit only when the bridge runs without `default.json` and no
card has been selected; the reply says so.

## clear

```text
clear
```

The "start over" command. It:

1. stops any turn in flight,
2. deletes every OpenCode session it knows for **you** — the default model's, the
   active model's, the active card's, and any scoped session used earlier in this
   process,
3. leaves the chat ready for a new conversation.

It only ever touches the sender's own sessions. It cannot see or delete another
contact's context. `clear` also deletes the session for guests, who can use it to start
fresh.

## repair

```text
repair              # everything
repair logs         # one check
repair config
```

Runs fixed, model-free checks and returns one short report:

| Focus | Checks |
| --- | --- |
| `service` | the systemd unit's state |
| `logs` | recent journal lines for this unit |
| `omemo` | device identity and session files |
| `opencode` | the CLI, its version, auth and workspace |
| `storage` | config, secret permissions, spool, disk |
| `config` | parsed settings that affect behaviour |
| `pending` | contact requests waiting for a decision |
| `network` | XMPP connection and the upload service |

The report is redacted: contact JIDs become per-report aliases, home paths become
`~`, and URLs, tokens and stanza text are removed. It is encrypted when the command
arrived encrypted, and falls back to clearly marked plaintext when encryption is what
broke — otherwise you could never diagnose OMEMO. `[repair] plaintext = true` always
sends it in the clear; `[repair] redact_jids = false` keeps raw JIDs (only for a
deliberate private report).

`repair` never edits state and never restarts anything.

## restart

```text
restart
```

Acknowledges first, then asks systemd to restart the bridge's unit from a transient
`systemd-run` timer outside the service's cgroup — so the reply is delivered before the
connection drops and the restart does not kill its own request.

The unit name is `[policy] restart_unit`, or else derived from
`OPENCODE_XMPP_INSTANCE` (set by the generated unit) or the bot JID's local part:
`opencode@example.org` → `opencode-xmpp-opencode.service`.

## invite

```text
invite remote@example.org
```

Adds the JID to the roster and sends a presence subscription, without a model turn.
To stop being reachable, remove the contact on the other side; to keep the bot out of
a conversation entirely, add the JID to `[policy] ignore_jids`.

## join

```text
join room@conference.example.org
```

Joins the room now as `[rooms] nick`. To keep it after a restart, add it to
`[rooms] join` as well. The bot answers when addressed by its nick, or to everything if
`[rooms] respond = "always"`.

## Contact requests

With `[features] auto_accept_subscriptions = false` (the default) a stranger's request
is queued, not accepted, and every owner gets a chat message naming the requester:

```text
Contact request: remote@example.org wants to add opencode@example.org as a contact
(requested 2026-01-01 12:00; seen from remote@example.org/phone).
Approve: approve remote@example.org
Decline: deny remote@example.org
List: pending
```

| Command | Effect |
| --- | --- |
| `approve <jid>` | Accept; the JID is added to the roster and presence is exchanged |
| `deny <jid>` | Decline and drop the request |
| `pending` | List what is waiting |

If exactly one request is waiting, `approve` with no JID approves it. A bare "yes" is
never a command — only the explicit verbs count, so an ordinary conversation cannot
accidentally approve a stranger.
