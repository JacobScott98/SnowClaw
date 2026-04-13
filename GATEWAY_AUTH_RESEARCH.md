# OpenClaw Gateway Authentication Research

**Date:** 2026-04-13
**Branch:** research/gateway-auth
**Source:** OpenClaw docs — `/gateway/configuration-reference.md`, `/gateway/configuration.md`, `/gateway/troubleshooting.md`

---

## 1. Available Auth Modes

The gateway supports four auth modes via `gateway.auth.mode`:

| Mode | Value | Description |
|------|-------|-------------|
| **Token** | `"token"` | Shared secret token. Default when onboarding wizard runs. |
| **Password** | `"password"` | Shared password (`gateway.auth.password` or `OPENCLAW_GATEWAY_PASSWORD` env var). |
| **Trusted Proxy** | `"trusted-proxy"` | Delegates auth to an identity-aware reverse proxy. Expects non-loopback proxy source. Trusts identity headers from IPs listed in `gateway.trustedProxies`. |
| **None** | `"none"` | Explicit no-auth mode. Only for trusted local loopback setups. |

Config location in `~/.openclaw/openclaw.json`:

```json5
{
  gateway: {
    auth: {
      mode: "token",  // none | token | password | trusted-proxy
      token: "your-token",
    },
  },
}
```

---

## 2. Default Auth Behavior

Key facts from the docs:

- **Auth is required by default.** The onboarding wizard (`openclaw onboard`) generates a token automatically.
- **Non-loopback binds always require auth.** If `bind` is `lan`, `tailnet`, or `custom`, you must have token, password, or trusted-proxy auth configured. The gateway refuses to start without it (`refusing to bind gateway ... without auth`).
- If both `gateway.auth.token` and `gateway.auth.password` are set, you **must** set `gateway.auth.mode` explicitly — startup fails otherwise.
- `gateway.auth.mode: "none"` is intentionally **not offered** by onboarding prompts. You must set it manually.

---

## 3. Disabling Auth for Local Development

### Set `gateway.auth.mode: "none"`

This is the correct approach for local dev on loopback:

```json5
// ~/.openclaw/openclaw.json
{
  gateway: {
    mode: "local",
    port: 18789,
    bind: "loopback",  // IMPORTANT: must be loopback for mode "none"
    auth: {
      mode: "none",
    },
  },
}
```

Or via CLI:

```bash
openclaw config set gateway.auth.mode "none"
```

**Constraints:**
- `mode: "none"` only works with `bind: "loopback"` (the default). Non-loopback binds will be rejected.
- After changing `gateway.auth`, a gateway restart is required (the `gateway.*` config section requires restart, though `hybrid` reload mode handles this automatically).

### Remove the token entirely

If you previously had a token set, also clear it:

```bash
openclaw config unset gateway.auth.token
openclaw config set gateway.auth.mode "none"
```

Or in config:

```json5
{
  gateway: {
    auth: {
      mode: "none",
      // no token or password fields
    },
  },
}
```

---

## 4. Why Auth Still Prompts in Local Dev

Common reasons the gateway still expects credentials locally:

### 4a. Onboarding wizard set a token

The `openclaw onboard` flow generates a token by default. If you ran onboarding, `gateway.auth.token` is set and mode defaults to `"token"`.

**Fix:** Set `gateway.auth.mode: "none"` and remove the token.

### 4b. `gateway.auth.mode` not explicitly set to `"none"`

If `gateway.auth.token` exists in config, the gateway infers `mode: "token"` even without an explicit mode field. Simply removing the mode doesn't disable auth if a token is still present.

**Fix:** Explicitly set `mode: "none"` AND remove token/password fields.

### 4c. Environment variable override

`OPENCLAW_GATEWAY_PASSWORD` or a token in `~/.openclaw/.env` can inject auth even if config says `"none"`.

**Fix:** Check `~/.openclaw/.env` and process env for auth-related vars.

### 4d. Non-loopback bind

If `gateway.bind` is anything other than `loopback` (e.g., `lan` for Docker), the gateway **requires** auth and will refuse to start with `mode: "none"`.

**Fix:** Use `bind: "loopback"` for local dev, or provide a token.

### 4e. SecretRef resolution

If `gateway.auth.token` or `gateway.auth.password` is configured via SecretRef and the ref can't be resolved, resolution fails closed (no silent fallback).

**Fix:** Remove the SecretRef or ensure it resolves.

### 4f. Control UI specific: device identity required

The Control UI (dashboard) may require device auth separately from gateway shared-secret auth. Error `device identity required` means the browser needs to complete device pairing.

**Fix:** Check `openclaw devices list` and approve pending devices, or set `controlUi.dangerouslyDisableDeviceAuth: false` for local dev.

---

## 5. When to Use Each Auth Mode

| Mode | Use Case |
|------|----------|
| `"none"` | Local development on loopback. No external access. Single-user trusted machine. |
| `"token"` | Default for most setups. Share the token with clients (Control UI, CLI, nodes). Good for single-user or small-team with loopback or LAN. |
| `"password"` | Alternative to token when you prefer a memorable passphrase. Same security profile as token. |
| `"trusted-proxy"` | Production behind a reverse proxy (nginx, Caddy, Cloudflare Access, etc.) that handles auth and injects identity headers. Requires non-loopback bind and `gateway.trustedProxies` config. |

### Additional auth features

- **`allowTailscale: true`** — When using `tailscale.mode: "serve"`, Tailscale identity headers can satisfy Control UI/WebSocket auth without a token. HTTP API endpoints still follow the normal auth mode.
- **Rate limiting** — `gateway.auth.rateLimit` with configurable `maxAttempts`, `windowMs`, `lockoutMs`. Loopback is exempt by default (`exemptLoopback: true`).

---

## 6. Diagnostic Commands

```bash
# Check current auth config
openclaw config get gateway.auth
openclaw config get gateway.auth.mode

# Check gateway status and connectivity
openclaw gateway status
openclaw doctor

# Watch for auth errors
openclaw logs --follow

# Check for env var overrides
env | grep OPENCLAW_GATEWAY
```

### Troubleshooting auth detail codes

| Code | Meaning | Action |
|------|---------|--------|
| `AUTH_TOKEN_MISSING` | Client didn't send a token | Set token in client, or switch to `mode: "none"` |
| `AUTH_TOKEN_MISMATCH` | Token doesn't match | Verify token with `openclaw config get gateway.auth.token` |
| `AUTH_DEVICE_TOKEN_MISMATCH` | Device token stale/revoked | `openclaw devices list` then rotate |
| `PAIRING_REQUIRED` | Device not approved | `openclaw devices approve <requestId>` |

---

## 7. Recommended Local Dev Config

Minimal config for pain-free local development:

```json5
// ~/.openclaw/openclaw.json
{
  gateway: {
    mode: "local",
    bind: "loopback",
    auth: {
      mode: "none",
    },
  },
  agents: {
    defaults: {
      workspace: "~/.openclaw/workspace",
    },
  },
}
```

This ensures:
- No auth prompts on localhost
- Gateway only listens on 127.0.0.1 (safe)
- Hot-reload works out of the box (hybrid mode is default)
