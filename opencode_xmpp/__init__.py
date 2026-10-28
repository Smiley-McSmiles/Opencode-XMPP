"""XMPP <-> opencode bridge.

A small daemon that logs into an XMPP account, forwards 1:1 messages and group
mentions to the local ``opencode`` CLI, and sends the answer back as a chat
message. One ``opencode run`` subprocess per turn, one deterministic opencode
session per contact, so context survives restarts and upgrades without the
bridge ever storing a transcript.

Two trust tiers, enforced by the agents' permission rules rather than by prompt
wording:

* admin - the sender's JID is listed in ``policy.owners``; the run uses the
          full-tool agent (terminal, files, projects, web).
* guest - everyone else; the run uses a read-only agent (read, glob, grep and
          web only) whose rules deny shell, edits and subagents.

The bridge itself never executes a tool and never addresses a stanza to a JID
other than the one that wrote in.
"""

__version__ = "0.4.0"
