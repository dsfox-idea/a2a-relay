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
- **Long peer tasks.** `a2a_send(agent, message)` hands a task to a peer and returns a job id at
  once; the peer's answer arrives later in the same chat as a separate message (`a2a_task` shows
  the state). Use it instead of `a2a_call` for anything that may take more than a few minutes:
  Hermes cuts every tool call at `timeouts.tools.sequential_call` (420 s by default), so a blocking
  `a2a_call` loses the answer of a long task even though the peer finishes it.
- **Gateway restarts without you.** The `gateway_restart` tool restarts this machine's gateway the
  way the `/restart` chat command does: it waits for running turns (the calling one included), then
  a detached watcher or the service manager starts the new process. When asked from your chat, the
  new gateway posts a notice there, so the agent re-checks what it was waiting on. To restart
  another machine, an agent asks that machine's agent over A2A. (`hermes gateway restart` in an
  agent's terminal kills the gateway the agent runs in.)
- **Trust levels.** How much an inbound A2A task may do in this profile without you. The level belongs
  to the receiving profile (`$HERMES_HOME/a2a_relay/trust.json`); `0` is the most autonomous and
  every higher number is stricter, so `strict` always names the top level.

  | Level | Task start | Commands Hermes would ask about | Tools | Forward to a peer | Peer text framed as untrusted |
  |---|---|---|---|---|---|
  | 0 autonomous | at once | run on their own | all | yes | no |
  | 1 delegate (default) | at once | run on their own, except system changes: asked | all | yes | no |
  | 2 allow | at once | run when they match the allow-list, others asked | all | no | yes |
  | 3 read | at once | none | read tools + `git status/log/diff/show/branch`, `rg`, `ls`, `cat`, `head`, `tail`, `hermes ... doctor/status` | no | yes |
  | 4 observe | after your "да" | as level 2 | as level 2 | no | yes |

  System changes are global package installs (`pip install` outside a venv, `npm -g`, `brew`,
  `winget`, `apt`, `cargo install`, ...), `hermes update/plugins/config/tools/setup`, and writes to
  `config.yaml`, `.env`, `SOUL.md` or the relay's own state. Hermes' floors (hardline blocks,
  `approvals.deny`) stay in force at every level. A level-4 task with no open chat of yours, a "нет"
  or no answer within `approvals.timeout` is returned to the peer as rejected, with the reason.
  `/a2a-trust` shows the level, `/a2a-trust <0-4|name|strict>` sets it, `/a2a-trust allow <glob>` /
  `disallow <glob>` edit the allow-list. Only your chats can run it, never an A2A session.
- **Commands.** `/a2a-allow [id]`, `/a2a-deny [id] [reason]`, `/a2a-pending`, `/a2a-trust`. Any of them
  also makes the current chat the feed target.

## Install

On every machine (macOS, Linux, Windows), with A2A already set up between them:

```bash
hermes plugins install dsfox-idea/a2a-relay --enable
hermes a2a-relay setup      # shows the fixes, asks once, backs up config.yaml, applies them
hermes gateway restart      # from a separate terminal, not from inside an agent session
hermes a2a-relay doctor     # read-only; every failed check prints the command that fixes it
```

`doctor` checks: the plugin is enabled and may inject into your chat; the A2A platform is on; the
node has a name; inbound peers have tokens and are trusted; inbound A2A sessions can call other
peers (`a2a` toolset on the `a2a` platform); `A2A_REPLY_TIMEOUT` and each peer's
`a2a_agents.<peer>.timeout` outlast `approvals.timeout`, so a late human approval still reaches
the caller; each configured peer is reachable and accepts its token; the Hermes build can deliver
into chats the gateway serves itself (the Windows TUI); every profile has the plugin too. Run it
per profile with `hermes -p <profile> a2a-relay doctor`. Tokens are never printed.

## Pairing machines (fleet)

Every machine must hold a token for every other one. Instead of pairing each pair by hand, a fleet
shares one secret; the token machine A presents to machine B is `HMAC(secret, "A>B")`, so every
direction has its own token and nothing but the secret is ever exchanged.

```bash
# first machine (A2A_AGENT_NAME=win)
hermes a2a-relay fleet add win http://100.97.1.117:9900
hermes a2a-relay fleet add m4  http://100.96.62.85:9900
hermes a2a-relay fleet invite        # prints: hermes a2a-relay fleet join a2ar1....
# on m4, in a terminal (the code carries the secret: never paste it into a chat)
hermes a2a-relay fleet join a2ar1....
# then restart the gateway on every machine, and on each one:
hermes a2a-relay fleet sync          # switches outbound peers to fleet tokens once they accept them
```

- To add a machine later: `fleet add` it on any member, `fleet invite`, `fleet join` on the new
  machine, `fleet add` + `fleet sync` on the other members (a member list has no secret, so an
  agent may run these for you over A2A), then restart their gateways.
- Hermes reads inbound tokens only when the gateway starts, so every membership change needs a
  gateway restart on the machines that must accept the new peer.
- Migration from hand-made pairs is safe: `sync` adds fleet tokens next to the old ones and moves an
  outbound peer to its fleet token only once that peer accepts it. When `doctor` shows every
  machine on fleet tokens, `fleet prune` drops the old inbound tokens.
- Other profiles on a machine reuse its membership: `hermes -p growser a2a-relay fleet sync`
  points their peers (including routes like `m4-growser` at `<m4>/growser/`) at the fleet tokens.
- One secret means one leaked machine exposes the fleet; rotate by removing the machine and
  re-inviting the others.

For development, a clone linked into `~/.hermes/plugins/a2a-relay` works as well (on Windows use a
directory junction: a symlink needs admin rights).

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
