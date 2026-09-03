# Recorded worker event streams

Each `*.jsonl` file is the machine-readable event stream of one real invocation,
captured while validating the launch matrix. They exist so every per-backend
translation is tested without spawning a process or reaching a network, and so a
harness changing its event vocabulary shows up as a failing test rather than as a
run that silently reads as complete.

| Fixture | Recorded from | Shows |
|---|---|---|
| `codex-turn.jsonl` | `codex exec --json --sandbox read-only -C <dir> -o <file> -` | thread id capture, agent message, completed turn with token usage |
| `codex-failed-turn.jsonl` | the same, with a model identifier the account cannot use | error event and failed turn carrying a nested message |
| `claude-turn.jsonl` | `claude -p --output-format stream-json --verbose --add-dir <dir>` | session id from init, rate-limit event with utilisation and reset, successful result |
| `claude-failed-turn.jsonl` | the same, with a model identifier that does not exist | a result whose `is_error` is true while its `subtype` reads `success` |
| `claude-worked-turn.jsonl` | a real multi-hour node's stream | throughput: 62,888 generated tokens, an inference span and a wall span that differ by the node's own tool wait, a peak prompt of 242,912 against a declared window |
| `codex-usage-limit.jsonl` | a dispatch made while the account was spent | a refusal: `turn.failed` whose message states the usage limit and names the moment it resets |
| `codex-account-limits.jsonl` | `codex app-server`, fed an `initialize` handshake then `account/rateLimits/read` | headroom off the non-interactive path: used percentages and reset times for two metered windows, plus an unrelated notification interleaved with the answers |

The last row is the exchange behind `budget_probe`. It records that the harness
whose run stream reports no headroom does publish it elsewhere — the limits are
answered over the app server's line protocol, by a read that runs no model. Its
figures, window identifiers and account metadata are replaced with neutral
values: what is under test is the shape of the answer and which window binds,
and an account's real utilisation is not a constant anything should record.

`codex-usage-limit.jsonl` is verbatim, all four lines of it. It is the shape that
was folded to "headroom unknown" for six days while every pre-flight reported the
backend clear, which is why a recognised refusal is now read as a measurement.
The reset moment it names is written for a person — abbreviated month, ordinal
day, twelve-hour clock, no zone — so it is read as local wall clock; a test
asserting on it must assert the wall clock, never a fixed offset.

`claude-worked-turn.jsonl` is a real run reduced to the events a translation
reads. Every `usage`, `modelUsage` and duration is verbatim; the assistant
messages' text and tool-use blocks are replaced by a placeholder, the
tool-result events are dropped, and the session id and working directory are
neutralised. It is the fixture behind the rule that generated totals come from
the result and never from summing the assistant events: those carry a message's
opening usage and are emitted once per content block, so summing them yields 941
where the run generated 62,888.

Two elisions, both in the `claude` streams:

- Events emitted by this workstation's own hook configuration were dropped. They
  carry the hook's whole injected text and say nothing about the harness.
- The `init` event's inventory lists — the available agents, MCP servers, slash
  commands and tools — were removed. They are long, they are specific to one
  machine's configuration, and no translation reads them.

Everything load-bearing is verbatim, including the field spellings, so the
`utilization` on the wire and the `utilisation_pct` in the normalised record are
deliberately different words.

The last failed-turn row is why a verdict is read from `is_error` and never from
`subtype`: keying off the label would report that failure as a success.
