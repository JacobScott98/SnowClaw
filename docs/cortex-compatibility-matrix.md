# Snowflake Cortex Chat Completions API — Compatibility Matrix

**Endpoint:** `https://<account>.snowflakecomputing.com/api/v2/cortex/v1/chat/completions`
**Tested:** 2026-03-30
**Models tested:** `claude-3-5-sonnet`, `openai-gpt-4.1`

## Parameter Compatibility

| Parameter | Claude (Anthropic) | OpenAI (GPT) | Notes |
|---|---|---|---|
| `messages` | OK | OK | Required |
| `model` | OK | OK | Required |
| `tools` | OK | OK | Standard function-calling format |
| `parallel_tool_calls` | OK (silently accepted) | OK | See [analysis below](#parallel_tool_calls-analysis) |
| `stream` | OK | OK | SSE format, standard chunks |
| `temperature` | OK | OK | |
| `top_p` | OK | OK | |
| `max_completion_tokens` | OK | OK | **Use this**, not `max_tokens` |
| `max_tokens` | **400** | **400** | `"max_tokens is deprecated in favor of max_completion_tokens"` |
| `stop` | OK | OK | Works correctly (stops at sequence) |
| `seed` | OK (accepted) | OK | Likely silently ignored for Claude |
| `n` | OK (returns 1) | OK (returns n) | Claude ignores n, always returns 1 choice |
| `frequency_penalty` | OK (accepted) | OK | Likely silently ignored for Claude |
| `presence_penalty` | OK (accepted) | OK | Likely silently ignored for Claude |
| `response_format` (`json_object`) | **400** | OK* | Claude: `"json object response format is not supported"`. GPT: requires "json" in messages |
| `logprobs` | Not tested | OK | GPT returns token-level logprobs |
| `user` | OK (accepted) | OK (accepted) | Silently accepted |
| Unknown params | OK (ignored) | OK (ignored) | Cortex silently drops unknown fields |

**Legend:** OK = HTTP 200, **400** = HTTP 400 error, OK* = conditional success

## parallel_tool_calls Analysis

### Cortex behavior

Cortex **silently accepts** `parallel_tool_calls` for all model families — it does not reject the parameter or return an error. This means:

- `parallel_tool_calls: true` → HTTP 200 (Claude and GPT)
- `parallel_tool_calls: false` → HTTP 200 (Claude and GPT)
- `parallel_tool_calls` omitted → HTTP 200 (Claude and GPT)

The parameter is either **passed through to the upstream provider** or **silently ignored**. There is no HTTP-level failure to detect.

### OpenClaw root cause

OpenClaw's Pi agent injects `parallel_tool_calls` via `createParallelToolCallsWrapper` in `src/agents/pi-embedded-runner/extra-params.ts`. Key behavior:

1. The wrapper **only applies** to `openai-completions` and `openai-responses` API types
2. It patches the outbound JSON payload with `parallel_tool_calls = <boolean>`
3. The value is configurable per-model in `openclaw.json` under `agents.defaults.models.<provider/model>.params`

Since Snowflake Cortex uses the `openai-completions` API type, the wrapper fires for all Cortex models. For Claude models routed through Cortex, this means `parallel_tool_calls` is injected into the payload — but since Cortex silently accepts it, **the issue is not an HTTP error but potentially incorrect behavior** (e.g., Claude not actually honoring the parallel tool calls constraint).

### Proxy implication

The cortex-proxy should:
- **Strip `parallel_tool_calls`** from requests to Claude models on Cortex (parameter is meaningless for Anthropic's API)
- **Pass through `parallel_tool_calls`** for OpenAI models (native support)
- **Rewrite `max_tokens` → `max_completion_tokens`** for all models (Cortex rejects the deprecated form)
- **Strip `response_format: json_object`** for Claude models (Cortex rejects it)

## Multi-Turn Tool Calling (Parallel Tool Results)

**Tested:** 2026-03-30 — Reproduction script: `phase1_parallel_repro.py`

### The bug

When Claude returns **multiple parallel tool_calls** in a single response, sending the tool results back in the follow-up request causes a **400 error** on Cortex. The same flow works perfectly with OpenAI models.

### Reproduction flow

1. Send request with tools + prompt designed to trigger 2+ parallel tool calls → **HTTP 200** (both Claude and GPT)
2. Send follow-up: `[user, assistant(tool_calls), tool, tool]` → **HTTP 400** (Claude) / **HTTP 200** (GPT)

### Error message

```
"invalid request parameters: Each 'toolUse' block must be accompanied with a matching 'toolResult' block."
```

This is an **Anthropic API error message** leaking through Cortex's translation layer. It means Cortex is converting OpenAI-format `tool` role messages to Anthropic-format `tool_result` content blocks, but the conversion fails when there are multiple parallel tool calls/results.

### Test matrix

| Scenario | Claude | GPT-4.1 |
|---|---|---|
| Single tool call → single tool result (multi-turn) | OK | OK |
| Parallel tool_calls → all tool results at once | **400** | OK |
| Parallel tool_calls → only first tool result | **400** | OK |
| Parallel tool_calls → all results (assistant content=null) | **400** | OK |
| Parallel tool_calls → all results (no assistant content key) | **400** | OK |

### Root cause analysis

Cortex translates OpenAI Chat Completions format to Anthropic Messages API format internally. The Anthropic API uses a different structure for tool calls:

- **OpenAI format:** Assistant message has `tool_calls` array; each tool result is a separate `role: "tool"` message with `tool_call_id`
- **Anthropic format:** Assistant message has `content` array with `tool_use` blocks; tool results are `tool_result` content blocks in the next `user` message

Cortex's translation layer appears to fail when mapping multiple OpenAI-format `tool` messages back to the Anthropic `tool_result` content block structure. The error "Each toolUse block must be accompanied with a matching toolResult block" confirms that Cortex is reaching the Anthropic API validation but with a malformed request.

### Proxy implication

The cortex-proxy **must** handle this at the multi-turn message level:

1. **Detect** when a Claude model's assistant response contains 2+ `tool_calls`
2. **Serialize** the parallel tool calls into sequential single-tool-call turns before sending the follow-up to Cortex
3. Alternatively, **intercept** the outbound messages and restructure the tool result messages into a format Cortex can translate correctly

This is the **highest-priority proxy rewrite rule** since it causes hard failures in any agentic workflow that uses parallel tool calls with Claude on Cortex.

## Error Messages Reference

| Condition | HTTP Status | Error Message |
|---|---|---|
| `max_tokens` used | 400 | `"max_tokens is deprecated in favor of max_completion_tokens"` |
| `response_format: json_object` on Claude | 400 | `"json object response format is not supported"` |
| `response_format: json_object` on GPT without "json" in messages | 400 | `"invalid request parameters: 'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json'."` |
| Parallel tool results on Claude (multi-turn) | 400 | `"invalid request parameters: Each 'toolUse' block must be accompanied with a matching 'toolResult' block."` |

## Response Shape Notes

- Claude responses have empty `finish_reason: ""` (GPT returns `"stop"` or `"tool_calls"`)
- Claude responses have empty `id: ""` (GPT returns `chatcmpl-*` IDs)
- Claude responses have empty `model: "claude-3-5-sonnet"` in the response (GPT returns empty `model: ""`)
- Both include zero-value `audio` and `function_call` fields in message objects (Cortex standardization)
- Tool call IDs: Claude uses `tooluse_*` format, GPT uses `call_*` format
