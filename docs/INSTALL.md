# Installing opencode-xmpp

This guide takes you from a bare machine to a bot you can message from your phone.
It assumes a Linux server, a domain you control, and a local Ollama. If you already
have a working XMPP account and opencode, jump to [Install](#4-install).

- [1. What you need](#1-what-you-need)
- [2. An XMPP account](#2-an-xmpp-account)
- [3. Ollama and opencode](#3-ollama-and-opencode)
- [4. Install](#4-install)
- [5. Configuration](#5-configuration)
- [6. Run it as a service](#6-run-it-as-a-service)
- [7. Verify](#7-verify)
- [8. Upgrade](#8-upgrade)
- [9. Uninstall](#9-uninstall)

## 1. What you need

| Thing | Why |
| --- | --- |
| Python 3.11 or newer | the bridge |
| [opencode](https://opencode.ai) v2 | runs the agent turns |
| An XMPP account for the bot | the bridge signs in as this JID |
| An XMPP server with HTTP upload (XEP-0363) | attachment transfers |
| [Ollama](https://ollama.com) (optional but assumed here) | local models |
| A dedicated Unix user (strongly recommended) | the admin agent has shell access |

Two example identities are used throughout:

- `local@example.org` — **you**, the owner.
- `remote@example.org` — a **guest**, someone else.
- `opencode@example.org` — the bot itself.

## 2. An XMPP account

Any modern server works. With [Prosody](https://prosody.im):

```sh
prosodyctl register opencode example.org 'a-long-random-password'
```

Then make sure these modules are enabled in `/etc/prosody/prosody.cfg.lua`:

```lua
modules_enabled = {
    -- ...
    "http_upload";      -- XEP-0363, required for attachments
    "muc";              -- group chats, optional
}

http_upload_file_size_limit = 40 * 1024 * 1024
http_upload_expire_after = 60 * 60 * 24 * 7
```

Restart Prosody and check that the upload service is advertised:

```sh
prosodyctl about
# or, from another machine:
# xmpp disco: upload.example.org
```

The bridge discovers the upload service automatically through XEP-0363. If your
server advertises it under a different host, set `[features] upload_service` in the
config; the bridge only ever downloads links from that host.

> **Encrypted rooms:** OMEMO 2 in a group needs member addresses, so the room must
> **not** be anonymous. Set the room's *whois* to `moderators` or `anyone` in your
> client (`/config room whois moderators` in Conversations) before enabling
> `[rooms] encrypt = true`.

## 3. Ollama and opencode

```sh
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4
```

Install opencode for the **same Unix user** that will run the bridge, then confirm
that it sees Ollama:

```sh
opencode models | grep ollama
# ollama/gemma4:latest
```

OpenCode v2 detects a local Ollama on `http://127.0.0.1:11434` by itself. If your
Ollama listens elsewhere, point the provider at it:

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

Then a quick sanity check, without the bridge:

```sh
opencode run --model ollama/gemma4:latest "say hello in five words"
```

See [OLLAMA.md](OLLAMA.md) for context windows, compaction and vision.

## 4. Install

```sh
git clone https://github.com/Smiley-McSmiles/Opencode-XMPP.git
cd Opencode-XMPP
./install.sh --jid opencode@example.org --owner local@example.org --service
```

The installer asks for the bot's XMPP password once, stores it as
`~/.opencode-xmpp/secrets/account.secret` (mode `0600`), installs the four agents
into `~/.config/opencode/agents/`, and enables a systemd **user** unit named after
the JID's local part (`opencode-xmpp-opencode.service`).

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--jid`, `--owner` | bot JID and owner JID (repeat `--owner` for more) |
| `--host`, `--port` | skip SRV discovery and use this host |
| `--workspace` | directory opencode runs in (default `$HOME`) |
| `--room`, `--nick` | join a MUC on startup |
| `--upload-service` | pin the XEP-0363 host instead of discovering it |
| `--service` / `--skip-init` / `--no-check` | service, config, and check toggles |
| `--force` | overwrite existing agents instead of keeping them |

### Manual install

The installer is a thin wrapper. The same thing by hand:

```sh
python3 -m venv ~/.opencode-xmpp/venv
~/.opencode-xmpp/venv/bin/pip install .
~/.opencode-xmpp/venv/bin/opencode-xmpp init \
    --jid opencode@example.org --owner local@example.org
~/.opencode-xmpp/venv/bin/opencode-xmpp check
```

`init` writes the config, the secret and the agents. It refuses to overwrite an
existing `opencode.toml` unless you pass `--force`.

For the tests:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

## 5. Configuration

Everything lives in `~/.opencode-xmpp/opencode.toml`. The commented reference is
[`opencode.toml.example`](../opencode.toml.example). The three settings that matter
most:

```toml
[account]
jid = "opencode@example.org"
password_file = "secrets/account.secret"   # relative to this file

[admin]
agent = "xmpp-admin"
model = "ollama/gemma4:latest"

[policy]
owners = ["local@example.org"]
```

Notes:

- `password_file` and every other path are resolved **relative to the config file**,
  so the directory can be moved as a unit.
- `[admin] model` also decides the bridge's allowed provider: the first segment
  (`ollama`) is the only provider `help` lists and `model` accepts. Leave it empty
  to allow every provider opencode knows.
- `[policy] owners` is an **access-control list for shell access**. Matching ignores
  case and the `/resource` part, so `local@example.org/phone` counts as
  `local@example.org`.
- `[guest]` applies to everyone not in `owners`.

Reload after editing:

```sh
systemctl --user restart opencode-xmpp-opencode
```

### Same machine, two bots

Run each bot as its own Unix user with its own `--dir`. This is the recommended way
to keep a local Ollama bot and a cloud bot apart; see
[MULTI-USER.md](MULTI-USER.md).

## 6. Run it as a service

`--service` already installed and started the unit. To generate it yourself:

```sh
~/.opencode-xmpp/venv/bin/opencode-xmpp unit \
    --config ~/.opencode-xmpp/opencode.toml --name opencode \
    > ~/.config/systemd/user/opencode-xmpp-opencode.service
systemctl --user daemon-reload
systemctl --user enable --now opencode-xmpp-opencode
```

For the unit to start **at boot**, with nobody logged in, enable lingering once:

```sh
loginctl enable-linger "$USER"
```

Check it:

```sh
systemctl --user status opencode-xmpp-opencode
journalctl --user -u opencode-xmpp-opencode -f
```

The unit runs the bridge in the foreground with `Restart=always`, so systemd keeps it
alive. It does **not** set `ProtectHome`: the admin agent is supposed to edit files in
your home, which is exactly why the bot should get its own user.

## 7. Verify

1. **The bridge's own check:**

   ```sh
   ~/.opencode-xmpp/venv/bin/opencode-xmpp check --config ~/.opencode-xmpp/opencode.toml
   ```

   It validates the config, the secret's permissions, the agents, the opencode
   binary and XMPP reachability.

2. **The log:**

   ```sh
   journalctl --user -u opencode-xmpp-opencode -n 50 --no-pager
   ```

   You want `OMEMO ready: device id …` and `discovered upload service …`.

3. **From your own client:** add `opencode@example.org` as a contact, accept the
   subscription, and send `help`. The first command reply also proves the owner tier
   is recognised — if `help` is ignored, the JID in `owners` does not match yours.

4. **From a guest:** add `remote@example.org` as a contact and ask a question. It
   should answer, but refuse anything that needs a shell.

## 8. Upgrade

```sh
cd Opencode-XMPP
git pull
./install.sh --skip-init
systemctl --user restart opencode-xmpp-opencode
```

`--skip-init` keeps your config, secret and agents. If a release changes the agent
files and you want the new ones, add `--force` to overwrite them.

Your `omemo.json` survives upgrades; **back it up** before moving machines, or your
contacts will see a new OMEMO identity.

## 9. Uninstall

```sh
systemctl --user disable --now opencode-xmpp-opencode
rm -rf ~/.opencode-xmpp
rm -f ~/.config/systemd/user/opencode-xmpp-opencode.service
systemctl --user daemon-reload
```

Remove the agents from `~/.config/opencode/agents/xmpp-*.md` only if nothing else
uses them.
