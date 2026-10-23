# acpx 0.20.0 transcripts (#14, phase 0)

Recorded on win (Windows 11, Node 24.19.0) on 2026-10-23 with `claude`
(`npx -y @agentclientprotocol/claude-agent-acp@^0.81.2`, Claude Max subscription), in a throwaway git repo,
one acpx session `spike` reused for every run. Account data, user paths and the long command list are
replaced (`owner@example.com`, `<home>`, `<repo>`, one `example-command`); nothing else is edited.

Common flags: `--format json --json-strict --suppress-reads --non-interactive-permissions deny`, prompt on
stdin via `claude -s spike --file -`. `--json-strict` kept stderr empty in every run.

| File | Extra flags | Session mode | Exit | Final | Outcome |
|---|---|---|---|---|---|
| `ok.ndjson` | `--approve-all --timeout 300` | auto | 0 | result `stopReason: end_turn` | file written, reply `done` |
| `auto-mode-ignores-deny-all.ndjson` | `--deny-all` | **auto** | 0 | `end_turn` | **file written anyway**: in mode `auto` claude-agent-acp never asks, so acpx has nothing to deny |
| `denied.ndjson` | `--deny-all` | default | **5** | `end_turn` | write refused: `tool_call_update` `status: failed`, text `User refused permission to run tool`; reply says the write was denied |
| `escalated.ndjson` | `--permission-policy '{"escalate":["execute"],"defaultAction":"approve"}'` | default | **5** | `end_turn` | one `session/request_permission`; its response carries `_meta.acpx.permissionEscalation` (`toolKind: execute`, `toolInput.command`, `matchedRule`, `action: escalate`) and `optionId: reject` |
| `timeout.ndjson` | `--approve-all --timeout 5` | auto | **3** | JSON-RPC `error` `-32070` `data.acpxCode: TIMEOUT`, then `session/cancel`, `stopReason: cancelled` | partial reply text kept |
| `cancelled.ndjson` | `--approve-all`, `acpx claude cancel -s spike` from a second process after 12 s | default | **0** | `stopReason: cancelled` | `cancel` printed `cancel requested`, exit 0 |

Facts the parser relies on (from these files):
- Reply text: `session/update` with `update.sessionUpdate == "agent_message_chunk"`, `update.content.text`.
- Final: a JSON-RPC response whose `result.stopReason` is `end_turn` or `cancelled`; a timeout adds an
  `error` object with `data.acpxCode`.
- Tool calls: `tool_call` (`kind`: `edit`, `execute`, ...) and `tool_call_update` (`status`: `completed` /
  `failed`).
- Escalation: `_meta.acpx.permissionEscalation` inside the response to `session/request_permission`.
- Exit 0 with `stopReason: cancelled` means cancelled, not done.

Findings that change the plan:
1. **The permission flags only work when the agent asks.** claude-agent-acp starts sessions in mode `auto`
   (`currentModeId: "auto"` in `session/new`), where it never sends `session/request_permission`;
   `--deny-all` then denies nothing. After `acpx claude set-mode default -s <session>` the same flags work.
   The executor must set the mode explicitly on every session it creates (or pass it at creation) and
   doctor/tests must cover it; `approve-reads` / `deny-all` / escalation are otherwise silently void.
2. A denied or escalated permission ends the run with exit 5 even though the turn finished (`end_turn`).
3. Windows: `shutil.which("acpx")` resolves to `C:\Users\<user>\AppData\Roaming\npm\acpx.CMD`.
4. The `_auth/status_update` notification carries the account e-mail: transcripts written by the executor
   contain personal data and must stay under `$HERMES_HOME`, never be published.

Not recorded yet: `codex` (not installed on win); a `no_session` case (exit 4); Ollama-backed agents
(the owner runs Ollama on a Mac, reached over Tailscale).
