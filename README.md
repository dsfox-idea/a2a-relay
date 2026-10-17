# a2a-relay

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that brings inbound
A2A (agent-to-agent) traffic into your live TUI chat and lets you approve or deny commands
that inbound A2A sessions need approval for — by simply answering "да" / "нет" (yes / no).

## Why

Inbound A2A tasks run inside the messaging gateway, in their own sessions. Without this plugin
you do not see them in your chat, and when such a task hits a dangerous-command approval the
prompt goes nowhere: the A2A adapter drops non-final sends, and the command is denied after
`approvals.timeout` with nobody ever asked.

## What it does

- **Feed.** Inbound tasks, the replies sent back, A2A calls made from other sessions, and approval
  prompts are injected as messages into the most recently active TUI chat on this machine.
  A task and its reply are merged into one message; an approval prompt is delivered immediately.
  History is not replayed: the feed starts at the moment the plugin first runs.
- **Approvals.** When an inbound A2A session raises an approval prompt, the gateway side publishes
  it to `$HERMES_HOME/a2a_relay/approvals.jsonl`; your decision is applied to the waiting agent
  through Hermes' own approval queue (`tools.approval.resolve_gateway_approval`).
- **Plain answers.** Your first message after an approval prompt, if it is a bare yes/no
  ("да", "ок", "нет", "нет, не сейчас" …), is parsed by plugin code — never by the model — and
  applied as the decision. A peer's text therefore cannot talk the agent into approving.
  Anything else closes the window; then use the commands below.
- **Commands.** `/a2a-allow [id]`, `/a2a-deny [id] [reason]`, `/a2a-pending`. Any of them also makes
  the current chat the feed target.

## Install

```bash
git clone https://github.com/dsfox-idea/a2a-relay ~/dev/a2a-relay
ln -s ~/dev/a2a-relay ~/.hermes/plugins/a2a-relay
hermes plugins enable a2a-relay --no-allow-tool-override
hermes config set plugins.entries.a2a-relay.allow_gateway_injection true
```

On Windows a symlink needs Developer Mode or admin rights; a directory junction does not
(PowerShell): `New-Item -ItemType Junction -Path "$env:LOCALAPPDATA\hermes\plugins\a2a-relay" -Target <clone>`.

Then restart the TUI chat that should receive the feed. The running gateway reloads plugins on
`enable`; if it does not, restart it.

Recommended: give peers time to receive a reply after a late approval. Set
`A2A_REPLY_TIMEOUT=900` in `~/.hermes/.env` (gateway restart required) and
`a2a_agents.<this-node>.timeout: 900` on each calling peer, so the A2A wait outlasts
`approvals.timeout` (300 s by default).

## Requirements and limits

- macOS, Linux and Windows. The cross-process lock is `fcntl.flock` on POSIX and
  `msvcrt.locking` on Windows; on Windows the owner-liveness check uses `OpenProcess`, never
  `os.kill(pid, 0)` (signal 0 is `CTRL_C_EVENT` there and would interrupt the process).
- Requires the bundled `a2a` platform plugin and the Ink TUI (`hermes --tui`).
- Each machine sees only its own inbound traffic.
- Only prompts that reach a human are relayed: with `approvals.mode: smart` the guardian model
  approves or denies many commands itself, and those never become prompts.
- A plain "да"/"нет" works only when exactly one shown request is pending.

## Tests

```bash
cd ~/.hermes/hermes-agent   # tests import Hermes modules
hermes --run-module unittest discover -s ~/dev/a2a-relay/tests -v
```

`hermes --run-module` uses Hermes' own interpreter and dependencies; a bare bundled `python3`
lacks them and fails on import.

## License

MIT
