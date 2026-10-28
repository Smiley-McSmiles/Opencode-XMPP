# Security

This page states what the bridge protects, what it does not, and how to deploy it
without turning a chat account into a remote shell.

## The trust boundary

```
untrusted                    endpoint                    trusted
─────────                    ────────                    ───────
anyone who can               XMPP server                 the bot's Unix user
message the JID      ──▶     (TLS, OMEMO)      ──▶       opencode + its tools
                                                         │
                                                         ▼
                                                   the host
```

The bridge itself does not execute anything. It starts `opencode run` as the user that
runs the bridge, and **that user's authority is the real limit**. Every mitigation
below follows from this.

## `[policy] owners` is shell access

An owner message runs the `xmpp-admin` agent, which has the terminal, file and project
tools. Anyone who can send a stanza as an owner JID can run commands as the bot's user.

Treat the list like an SSH `authorized_keys`:

- Keep it to your own JIDs.
- Enable OMEMO on every device you use.
- Do not put the same JID on a shared or public account.
- Remember that a compromised phone is a compromised owner.

Guests run `xmpp-guest`, whose rules deny `execute`, `edit`, `subagent` and `skill`.
That is enforced by opencode, not by the prompt.

## Give the bot its own user

The single most effective control. The admin agent is *supposed* to have the user's
authority; make that user small.

```sh
sudo useradd --create-home --shell /usr/sbin/nologin opencode-bot
```

Then:

- the bot's home contains only the deployment and whatever you let it work on;
- `~/.ssh`, browser profiles and your documents are not readable;
- `opencode auth login` for that user stores provider keys in *its*
  `~/.local/share/opencode/auth.json`, not yours;
- the guest agent's `read`/`grep` can only reach that home (plus world-readable files).

Do not run the bridge as your desktop account "just for now".

## Prompt injection

The agent reads things an attacker may control: web pages, files in the workspace,
email exported into the workspace, other people's messages in a room. A local model is
not immune, and an owner-tier agent that follows a malicious page can do real work.

Mitigations that work:

- a dedicated Unix user with nothing sensitive (above);
- no long-lived credentials on that account beyond what the agent needs;
- `[policy] owners` limited to people you trust with that user's files;
- `[rooms] respond = "mention"` so a busy room cannot drive the bot by accident;
- `[policy] ignore_jids` for other bots, to stop bot-to-bot loops.

There is no "sandbox mode" in the bridge. If a guest should never influence files,
give them a bot whose workspace is empty.

## Message privacy

- **No transcript on disk from the bridge.** Message text is never written to the log
  — only lengths and JIDs. Sessions belong to opencode.
- **Derived session ids.** The id is `sha256(bot JID | sender bare JID | version)`, so
  the JID itself is not used as the storage key.
- **Spool is transient.** Inbound attachments are written to
  `~/.opencode-xmpp/spool/<contact>/` and deleted after the turn.
- **Only your upload host is fetched.** A link in a message is downloaded only if it
  points at the configured XEP-0363 host; no arbitrary URL is ever requested.

## OMEMO

- **Replies mirror the incoming message.** A message that arrived encrypted gets an
  encrypted reply. A message sent in plaintext gets a plaintext reply. There is no
  silent encryption upgrade, and that is deliberate: a contact without OMEMO can still
  talk to the bot.

  If you require encryption, use a client that always encrypts (Conversations and
  Monal do by default) and check the lock icon. `[features] omemo_mode = "2"` (or
  `"legacy"`) refuses to send when the contact's devices lack that version; it does not
  turn a plaintext message into an encrypted one.

- **If encryption fails, the reply is replaced with a notice**, not sent in the clear.
  Same for attachment links.
- **Devices are trusted on first use (BTBV).** New devices are logged at `WARNING`:
  `auto-trusted new OMEMO device <id> of <jid> (fingerprint …)`. Read those lines. A
  device you do not recognise is a problem.
- **Rooms** with `[rooms] encrypt = true` use OMEMO 2 (XEP-0420). They never fall back
  to plaintext unless `plaintext_fallback = true`, and they need a non-anonymous room.
- **`omemo.json` is the identity.** Back it up. Losing it makes every contact see a new
  identity; leaking it lets someone impersonate the bot to contacts who trust it.

## Files

- **Outbound files are encrypted before upload.** The bridge reads the file, encrypts
  it with AES-256-GCM using a fresh key and IV, uploads only the ciphertext, and sends
  an `aesgcm://` URL whose fragment carries `iv + key` (XEP-0454).
- The upload service stores ciphertext and can never decrypt it.
- In an OMEMO chat the link is sent inside OMEMO; if that fails, the link is **not**
  sent. In an encrypted room the bridge refuses to send attachments at all rather than
  post a keyless link into the room.
- Only the owner tier can send files (`[[attach: …]]`); guest replies never carry
  attachments.
- The agent chooses the path, so a prompt-injected agent can try to attach a file it
  can read. The same Unix-user boundary applies.

## Secrets and backups

| File | Secret? | Back it up |
| --- | --- | --- |
| `~/.opencode-xmpp/secrets/account.secret` | XMPP password | yes (or re-enter it) |
| `~/.opencode-xmpp/omemo.json` | OMEMO identity and sessions | **yes** |
| `~/.local/share/opencode/auth.json` | provider API keys | yes |
| `~/.config/opencode/opencode.jsonc` | no (but may name endpoints) | yes |
| `~/.opencode-xmpp/opencode.toml` | no | yes |
| `~/.opencode-xmpp/models/*.json` | no | yes |
| `~/.opencode-xmpp/spool/` | transient | no |

Permissions: `account.secret` is `0600`, the `secrets/` directory is `0700`, and the
unit runs with `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` and a
`ReadWritePaths` allowlist. It deliberately does **not** set `ProtectHome`, because the
admin agent must edit files in the home.

The bridge never needs provider API keys itself. If a guide ever tells you to copy a key
into `opencode.toml`, the guide is wrong.

## Hardening checklist

- [ ] Dedicated Unix user for the bridge; `loginctl enable-linger` for boot start.
- [ ] `[policy] owners` contains only JIDs you control.
- [ ] OMEMO enabled on every owner device; check the `new device` log lines.
- [ ] XMPP server uses TLS and a real certificate (`starttls = true`).
- [ ] `chmod 700 ~/.opencode-xmpp/secrets`, `chmod 600` every file in it.
- [ ] `omemo.json` backed up somewhere private.
- [ ] Guest tier tested from a second account: it must refuse shell requests.
- [ ] `[policy] ignore_jids` lists any other bridges in the same rooms.
- [ ] `repair` run once, and the report looks clean.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Contact the maintainer through
the address on the GitHub profile linked in [README](../README.md), or use GitHub's
private vulnerability reporting on the repository.
