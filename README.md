# a2a-relay

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for people who run Hermes on
several machines of their own. The agents on those machines hand work to each other while you stay
in control from the chat you already have open:

- tasks between machines land in **chats you can see**, or show up there as a feed;
- whatever a peer's task needs your approval for is asked **in your chat**, and you answer with a
  plain "yes" / "no";
- a **trust level** per profile decides how much a peer may do without you.

## Quick start: two machines

You need Hermes with the bundled `a2a` platform on both machines, both on one private network (a LAN
or a tailnet such as Tailscale). Below the machines are `win` (100.64.0.1) and `mac` (100.64.0.2); A2A
listens on port 9900, a2a-relay on 9901. Allow TCP 9900 and 9901 between them in the OS firewall.

1. **Install** on both machines:

   ```bash
   hermes plugins install dsfox-idea/a2a-relay --enable
   hermes a2a-relay setup      # shows the config fixes, asks once, backs up config.yaml, applies them
   ```

2. **Pair them.** A fleet shares one secret; the token machine A presents to machine B is
   `HMAC(secret, "A>B")`, so every direction has its own token and only the secret is ever exchanged.

   ```bash
   # on win
   hermes a2a-relay fleet add win http://100.64.0.1:9900
   hermes a2a-relay fleet add mac http://100.64.0.2:9900
   hermes a2a-relay fleet invite        # prints: hermes a2a-relay fleet join a2ar1....
   # on mac, in a terminal (the code carries the secret: never paste it into a chat)
   hermes a2a-relay fleet join a2ar1....
   ```

3. **Restart the gateway** on both machines from a terminal (`hermes gateway restart`; never from
   inside an agent session), then on each:

   ```bash
   hermes a2a-relay fleet sync          # moves outbound peers to fleet tokens once they accept them
   hermes a2a-relay doctor              # read-only; every failed check prints the command that fixes it
   ```

4. **Open a service chat** on each machine and title it with the `hermes-` prefix (`/title hermes-win`).
   A2A notices and approval requests of that machine go only there.

5. **Try it** in a chat on win: "list my open chats on all machines" (`sessions_list`), then, from a
   chat of project X, "ask the chat of project X on mac to run its tests" (`session_send`). The task appears in that
   chat on mac, and its answer comes back to your chat on win.

## What you get

### Tasks into visible chats

| Tool | What it does |
|---|---|
| `sessions_list` | Open chats on every machine: machine, profile, idle/working, project@branch or folder, service chat mark, title, last message. Unreachable machines are listed too. |
| `session_send(project=..., text)` | Hands a task to the open chat of that project (or `session=<id>` for a given chat). It runs there as a normal turn you can watch. Returns at once. |
| `session_reply(id, text)` | The receiving chat's answer; it arrives in the sender's chat as a separate message. |

A closed target chat is reported as not delivered; a task that already passed 3 chats is not
forwarded again; with no answer in 60 min the sender is told once, and a late answer still arrives.
Only chats the gateway serves are listed (TUI and desktop chats), never one-off `hermes chat -q` runs.

### Long tasks to a peer agent

`a2a_send(agent, message)` hands a task to a peer's own A2A session and returns a job id at once; the
answer arrives later in your chat (`a2a_task` shows the state). Prefer it to `a2a_call` for anything
longer than a few minutes: Hermes cuts every tool call at `timeouts.tools.sequential_call` (420 s by
default), and a blocking `a2a_call` loses the answer. Those peer sessions are hidden from you, so
use `a2a_send` for machine tasks and `session_send(project=...)` for project work.

### Feed and approvals

Inbound A2A tasks, the replies sent back and calls made from other sessions are posted as one notice
into the machine's **service chat**: the open chat whose title starts with `hermes-` (set it with
`/title hermes-<machine>`; with several, the most recently active one). A task and its reply are merged;
history is not replayed. When a peer's task needs approval, the request is posted there at once.
Project chats never receive these notices. With no service chat open, everything waits (`/a2a-pending`
lists it) until you open one; a task that needs your "yes" is declined at once, since nobody can see it. Without this plugin
such a prompt goes nowhere: the A2A adapter drops it and the command is denied after
`approvals.timeout` with nobody asked.

Answer with a bare **yes / ok / no / no, not now** (Russian: **да / ок / нет / нет, не сейчас**). Plugin
code parses your message and applies it; the model never decides, so a peer's text cannot talk the
agent into approving. Other messages in between do not close the request; it stays open until answered
or expired. With several requests waiting, use the commands:

| Command | |
|---|---|
| `/a2a-pending` | requests waiting for you |
| `/a2a-allow [id]`, `/a2a-deny [id] [reason]` | decide one |
| `/a2a-trust` | show or set this profile's trust level and allow-list |

### Trust levels

How much a peer's task may do in this profile without you. Each profile has its own level
(`$HERMES_HOME/a2a_relay/trust.json`); `0` is the most autonomous, `strict` always names the strictest.

| Level | Task starts | Commands Hermes would ask about | Tools | Forward to a peer | Peer text framed as untrusted |
|---|---|---|---|---|---|
| 0 autonomous | at once | run | all | yes | no |
| 1 delegate (default) | at once | run, except system changes: asked | all | yes | no |
| 2 allow | at once | run if on the allow-list, others asked | all | no | yes |
| 3 read | at once | none | read tools + `git status/log/diff/show/branch`, `rg`, `ls`, `cat`, `head`, `tail`, `hermes ... doctor/status` | no | yes |
| 4 observe | after your "yes" | as level 2 | as level 2 | no | yes |

System changes: global package installs (`pip install` outside a venv, `npm -g`, `brew`, `winget`,
`apt`, `cargo install`, ...), `hermes update/plugins/config/tools/setup`, and writes to `config.yaml`,
`.env`, `SOUL.md` or the plugin's own `a2a_relay/` folder (reading that folder is fine). Hermes' own
floors (hardline blocks, `approvals.deny`) apply at every level.

The level applies the same way to a peer's A2A session, to a task sent to a profile route
(`http://host:9900/<profile>/`), and to a `session_send` task running in one of your chats; there
questions are allowed, because you are in that chat. A level-4 task with no open chat of yours, a "no",
or no answer in time goes back to the peer as rejected, with the reason.

`/a2a-trust 3` sets a level; `/a2a-trust allow git pull*` and `/a2a-trust disallow <glob>` edit the
allow-list. Only your own chats can change it, never a peer.

### Gateway restarts without you

`gateway_restart` restarts this machine's gateway the safe way: it waits for running turns, refuses
while a chat waits for your answer (a question or an approval) unless called with `force`, and posts a
notice in the calling chat when the new gateway is up. To restart another machine, an agent asks that
machine's agent. `hermes gateway restart` in an agent's own terminal kills the gateway the agent runs in.

### Coding agents (acpx)

`code_send(task, executor?, session?)` hands a coding task to Claude Code (or another agent
[acpx](https://github.com/openclaw/acpx) supports) in **the calling chat's own project**: the git repo of
the chat's working folder, inside the configured roots. Each job works in its own `git worktree` at
`<root>/.a2a-worktrees/<repo>-<job>` on branch `a2a/<job>`; your checkout is never touched and worktrees
are kept. The result (reply, branch, commits, `git diff --stat`) comes back to the chat; the full
transcript stays in `$HERMES_HOME/a2a_relay/code/`. `code_task`, `code_cancel` and `code_wait` manage
jobs. Peers at levels 0-2 may use it in their own project, level 3 may not, level 4 only after your "yes".

```bash
npm install -g acpx@0.20.0     # Node >= 22.13
hermes a2a-relay coder init    # writes a2a_relay/coder.json (claude, ~/dev); --root <dir>, --executor opencode
hermes a2a-relay doctor        # checks acpx, node, executors, roots; spends no tokens
```

`coder.json` keys: `executors`, `default_executor`, `roots`, `timeout_s` (default 3600), `permissions`
(`approve-all` | `approve-reads` | `deny-all`), `session_modes` (`claude` must not be `auto`: in that
mode the adapter never asks for permission).

The coding agent's commands do not go through Hermes' approvals: the worktree protects against mistakes,
not against a hostile agent. It cannot ask questions mid-job, so write a complete brief. Claude through
acpx uses your own subscription; transcripts contain account data and stay under `$HERMES_HOME`.

## Settings

In `config.yaml`, under `plugins.entries.a2a-relay`:

| Key | Default | |
|---|---|---|
| `language` | `display.language`, else English | Language of notices and command replies: `en` or `ru`. Yes/no words of both languages are always understood. |
| `allow_gateway_injection` | must be `true` | Lets the plugin post into your chats (`setup` sets it). |

The environment variable `A2A_RELAY_LANGUAGE` overrides `language`.

## Several profiles

Every profile has its own trust level and coding settings. Other profiles reuse the machine's fleet
membership: `hermes -p <profile> a2a-relay fleet sync` points their peers (including profile routes such
as `mac-work` at `http://100.64.0.2:9900/work/`) at fleet tokens. Run `hermes -p <profile> a2a-relay
doctor` per profile.

## Fleet maintenance

- Add a machine: `fleet add` it on any member, `fleet invite`, `fleet join` on the new machine, then
  `fleet add` + `fleet sync` on the others and restart their gateways (inbound tokens are read only when
  the gateway starts). A member list carries no secret, so an agent may run these for you.
- Moving from hand-made tokens is safe: `sync` adds fleet tokens next to the old ones and switches a peer
  only once it accepts them; when `doctor` shows every machine on fleet tokens, `fleet prune` drops the
  old ones.
- One secret means one leaked machine exposes the fleet; rotate by removing it and re-inviting the others.

### Which chat gets what

A chat's **project** is the git repo of its working folder (origin repo name, else the folder name;
worktrees count as their repo). The rules are enforced by plugin code on both machines:

| Message | Goes to |
|---|---|
| Project task (`session_send(project="growser", ...)`) | The open chat of that project. None open: "not delivered". Several: the sender picks one. |
| Task from a project chat | Only chats of the same project, or the service chat. |
| Machine task (update, logs, restart) | The service chat (`hermes-...`) or `a2a_send`. |
| A2A notices, approval requests | Only the service chat. |

A chat that is neither a project chat nor the service chat takes no tasks.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A peer's task waits and you never saw a request | Requests go only to the service chat. Open a chat and title it `hermes-<machine>` (`/title hermes-m5`); `/a2a-pending` lists what waits. |
| "yes" did nothing | A plain yes counts only in the service chat, and only while the request is open (5 min by default); with several waiting use `/a2a-allow <id>`. |
| `sessions_list` says a machine is not reachable | Its gateway is down, it runs an older plugin, or TCP 9901 is blocked. `curl http://<ip>:9901/sessions` should answer 401 (no token). |
| A chat is missing from `sessions_list` | Only chats the gateway serves are listed; `hermes chat -q` runs and closed chats are not. |
| The first `code_send` on a new machine fails with "acpx could not open session" | npm was still fetching the adapter. Retry, or warm the cache once: `npm exec --yes --package=@agentclientprotocol/claude-agent-acp@^0.81.2 -- node -e 0`. |
| `doctor` warns "trust levels on profile routes" | Your Hermes changed the internal method the plugin wraps for profile routes; update a2a-relay. Until then tasks to `/<profile>/` keep the untrusted framing and level 4 blocks them there. |
| A chat was cut by a restart | Restart with `gateway_restart`, never `hermes gateway restart` inside an agent. |

## Requirements and limits

- macOS, Linux and Windows; the bundled `a2a` platform; TUI or desktop chats. On Windows the
  cross-process lock is `msvcrt.locking` (`fcntl.flock` elsewhere) and the liveness check uses
  `OpenProcess`, never `os.kill(pid, 0)`, which would interrupt the process there.
- Each machine sees only its own inbound traffic.
- With `approvals.mode: smart` Hermes' guardian model settles many commands itself; only prompts that
  reach a human are relayed.
- For development, link a clone into `~/.hermes/plugins/a2a-relay` (on Windows a directory junction: a
  symlink needs admin rights).

## Tests

```bash
cd ~/.hermes/hermes-agent   # the tests import Hermes modules
hermes --run-module unittest discover -s ~/dev/a2a-relay/tests -v
```

`hermes --run-module` uses Hermes' own interpreter; a bare `python3` lacks its dependencies.

There is no hosted CI. A `pre-push` hook runs the whole suite before every push; enable it once
per clone with `git config core.hooksPath .githooks` (skip once with `git push --no-verify`).
After each release every fleet machine runs the suite on its own platform (Windows, macOS);
Linux is not covered until a Linux machine joins the fleet.

## License

MIT
