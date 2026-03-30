# Snowflake Cortex Chat Completions API — Compatibility Matrix

**Endpoint:** `https://<account>.snowflakecomputing.com/api/v2/cortex/v1/chat/completions`
**Tested:** 2026-03-29
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

## Error Messages Reference

| Condition | HTTP Status | Error Message |
|---|---|---|
| `max_tokens` used | 400 | `"max_tokens is deprecated in favor of max_completion_tokens"` |
| `response_format: json_object` on Claude | 400 | `"json object response format is not supported"` |
| `response_format: json_object` on GPT without "json" in messages | 400 | `"invalid request parameters: 'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json'."` |

## Response Shape Notes

- Claude responses have empty `finish_reason: ""` (GPT returns `"stop"` or `"tool_calls"`)
- Claude responses have empty `id: ""` (GPT returns `chatcmpl-*` IDs)
- Claude responses have empty `model: "claude-3-5-sonnet"` in the response (GPT returns empty `model: ""`)
- Both include zero-value `audio` and `function_call` fields in message objects (Cortex standardization)
- Tool call IDs: Claude uses `tooluse_*` format, GPT uses `call_*` format
