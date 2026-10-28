# Troubleshooting

Start with the log and the built-in check:

```sh
journalctl --user -u opencode-xmpp-opencode -n 100 --no-pager
~/.opencode-xmpp/venv/bin/opencode-xmpp check
```

From chat, `repair` gives a redacted snapshot of all of it without a model turn.

---

## The bot does not answer at all

```sh
systemctl --user status opencode-xmpp-opencode
journalctl --user -u opencode-xmpp-opencode -n 50 --no-pager
```

Look for `starting as <jid>`, `OMEMO ready` and `discovered upload service`.

| Symptom in the log | Cause |
| --- | --- |
| `connection failed` / `host not found` | wrong `[account] host`/`port`, or no SRV record |
| `not-authorized` | wrong password in `secrets/account.secret` |
| `disconnected` in a loop | the same JID is connected twice (two bridges, same account) |
| nothing after `starting` | the process is blocked before login: check DNS and port 5222 |

If the service never starts: `systemctl --user status` shows the unit's exit reason.
A common one is a path in the config that the service user cannot read.

## It answers guests but ignores me

`help` is owner-only. If the bot ignores `help`:

- the JID you are writing from is not in `[policy] owners` (resource and case are
  ignored, the rest must match);
- you are writing in a room, where commands are disabled;
- you hit the rate limit (`rate_limit_per_minute`, default 8/min) — the bot replies
  "Slow down a moment" for that.

Confirm the bot sees you as the owner:

```sh
journalctl --user -u opencode-xmpp-opencode | grep -i "owner"
```

## "I couldn't encrypt that reply for your device"

The bridge replies in the same mode it received: an encrypted message gets an
encrypted answer, and this notice means encryption failed while answering.

- The contact has no OMEMO device published (the client is offline, or OMEMO is off).
- `[features] omemo_mode` is pinned to `2` or `legacy` and the device speaks the other
  version. Set it back to `auto`, or update the client.
- The device list is stale: restart both clients once and resend.

The bridge will not fall back to plaintext here, by design.

## Attachments never arrive

Outbound (`[[attach: /path]]`):

- only the **owner** tier can send files;
- `[features] uploads` must be `true`;
- the log must contain `discovered upload service <host>`; if it says
  `no HTTP upload service found`, enable `http_upload` on the server or set
  `[features] upload_service = "upload.example.org"`;
- files larger than `max_upload_mb` (default 40) are refused;
- in an encrypted room the bridge refuses to send an attachment at all, because the
  link would carry the key in the clear;
- `could not upload <name>: …` in the reply names the real error.

Inbound:

- the link must point at the configured upload host, otherwise it is ignored on
  purpose (no arbitrary URL fetching);
- `aesgcm://` links are decrypted; a broken key shows as a download failure in the log.

## Photos are ignored by the model

```sh
ollama show <model> --modelfile | head -5
```

The model must advertise `vision`. Also check:

```toml
[features]
inline_images = true
max_inline_image_mb = 8
```

An image over the limit is not attached and the model is told so.

## A voice note is ignored

The bridge attaches `.m4a`, `.mp3`, `.ogg`, `.opus`, `.wav`, `.flac`, `.webm` and
`.aac`. The model must advertise `audio`:

```sh
curl -s http://127.0.0.1:11434/api/show -d '{"model":"<model>"}' | jq .capabilities
```

If `audio` is missing, the model cannot hear it; transcribe first or use an
audio-capable model. See [OLLAMA.md](OLLAMA.md).

## The local model freezes after a long chat

1. Check the context the card asks for:

   ```sh
   grep -A6 '"options"' ~/.config/opencode/opencode.jsonc
   ```

2. Lower `params.num_ctx` if it does not fit in memory. A 64k context is expensive
   even when the conversation is short.
3. Enable the compaction safety net:

   ```toml
   [opencode]
   compact_after_turns = 8
   ```

4. Watch memory and reloads:

   ```sh
   ollama ps
   journalctl -u ollama -n 50 --no-pager
   ```

5. If the turn simply takes longer than the tier's `timeout_s`, the bridge cuts it off
   and says so. Raise `[admin] timeout_s` for a slow model.

A request that started with a small context and later needed more can also hit a model
reload; keeping `OLLAMA_NUM_PARALLEL=1` and a long `OLLAMA_KEEP_ALIVE` avoids it.

## "No card matches" or "restricted to ollama models"

`model` only accepts **card IDs**; a raw model name is not a selection any more. Add a
card whose `base_model_id` is that model, then select the card.

The provider fence comes from `[admin] model`, or from `default.json` when that is
unset. That is intentional: a private local bot must not quietly answer through a
hosted model. A card whose base is on another provider is hidden and rejected.

`model ollama/my-card` is neither a card ID nor a model — cards are selected by `id`
or `name` without the provider prefix.

## A card does not change anything

- The card must be selected (`model <card-id>` or `model default`); a chat that has
  never selected one uses `[admin] model` and that model's own defaults.
- `~/.config/opencode/opencode.jsonc` must exist, or there is nowhere to write the
  options.
- If the reply is `I couldn't apply the card settings`, the file is not valid JSON or
  not writable. Comments are accepted now, but the file is rewritten as plain JSON.
- Two cards on the same base model overwrite each other's options.
- `think-off` / `think-on` edit the **card** and re-apply it; if the reply says it saved
  the file but could not apply it, check the opencode config path and permissions.

## A card edited on disk did not take effect

Cards are read by `help`, `model` and `think-off`. `default.json` is also applied at
startup, but a running chat keeps the card it selected. After editing `default.json`,
send `model default` (or restart the bridge) to apply it; after editing another card,
select it again.

Details: [MODEL-CARDS.md](MODEL-CARDS.md).

## The bot answers itself, or two bots talk forever

Put the other bot in `[policy] ignore_jids`:

```toml
[policy]
ignore_jids = ["other-bot@example.org"]
```

Also check `[rooms] respond`: `always` makes a bot answer every message, including
another bot's.

## Sessions keep failing after changing models

Old sessions were created for a different model. Use:

```text
clear
```

or, to reset **everyone** without deleting anything:

```toml
[policy]
session_id_version = "2"
```

then restart the bridge.

## `opencode models` fails from the service

The CLI needs a readable working directory and `HOME`. The generated unit sets
`HOME=%h` and `WorkingDirectory` to the config directory. If you wrote your own unit,
add both. Running `opencode` from a directory the service user cannot read (for example
another user's home) returns a 500/empty list.

## Where to look next

- [INSTALL.md](INSTALL.md) — setup and service
- [OLLAMA.md](OLLAMA.md) — models and context
- [MODEL-CARDS.md](MODEL-CARDS.md) — cards
- [COMMANDS.md](COMMANDS.md) — every command
- [SECURITY.md](SECURITY.md) — owners, guests, encryption
