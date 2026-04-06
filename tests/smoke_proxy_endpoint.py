"""Quick smoke test for the standalone Cortex proxy endpoint.

Auth flow:
  - Authorization: Snowflake Token="<PAT>" → authenticates with SPCS ingress (stripped)
  - X-Cortex-Token: <PAT> → passes through to proxy, forwarded to Cortex as Bearer token
"""

import json
import sys
from pathlib import Path

import requests

PROXY_BASE_URL = "https://mrdbfdb-bqecbew-nyb92647.snowflakecomputing.app"
ENDPOINT = f"{PROXY_BASE_URL}/v1/chat/completions"

# Load PAT from .env
env_path = Path(__file__).parent / ".env"
pat = None
for line in env_path.read_text().splitlines():
    if line.startswith("SNOWFLAKE_PAT="):
        pat = line.split("=", 1)[1].strip()
        break

if not pat:
    print("SNOWFLAKE_PAT not found in .env")
    sys.exit(1)

payload = {
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}],
    "max_tokens": 64,
    "stream": False,
}

headers = {
    "Content-Type": "application/json",
    "Authorization": f'Snowflake Token="{pat}"',
    "X-Cortex-Token": pat,
}

print(f"POST {ENDPOINT}")
print(f"Model: {payload['model']}")
print()

try:
    resp = requests.post(ENDPOINT, headers=headers, json=payload, timeout=60)
except requests.ConnectionError as e:
    print(f"Connection failed: {e}")
    sys.exit(1)

print(f"Status: {resp.status_code}")
print(f"Content-Type: {resp.headers.get('content-type', 'unknown')}")

if "text/html" in resp.headers.get("content-type", ""):
    print("\nGot HTML login page — SPCS ingress auth failed.")
    sys.exit(1)

if resp.status_code != 200:
    print(f"\nError: {resp.text[:500]}")
    sys.exit(1)

body = resp.text.strip()
if not body:
    print("\nEmpty response body.")
    sys.exit(1)

data = resp.json()
print(f"\nResponse:")
print(json.dumps(data, indent=2))

choices = data.get("choices", [])
if choices:
    msg = choices[0].get("message", {}).get("content", "")
    print(f"\nAssistant: {msg}")
    print("\nProxy is working!")
else:
    print("\nNo choices in response — check proxy logs.")
    sys.exit(1)
