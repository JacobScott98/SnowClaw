#!/usr/bin/env python3
"""Phase 1 follow-up: test max_completion_tokens, response_format with json hint, and other edge cases."""

import json
import tomllib
from pathlib import Path

import httpx

CONNECTIONS_PATH = Path.home() / ".snowflake" / "connections.toml"
ACCOUNT = "XAB68032"
BASE_URL = f"https://{ACCOUNT}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"

def load_token() -> str:
    with open(CONNECTIONS_PATH, "rb") as f:
        cfg = tomllib.load(f)
    return cfg["main"]["token"]


def send(client, payload, label):
    print(f"\n{'='*80}")
    print(f"TEST: {label}")
    print(f"{'='*80}")
    print(f"Payload keys: {list(payload.keys())}")
    resp = client.post(BASE_URL, json=payload, timeout=60)
    print(f"Status: {resp.status_code}")
    try:
        body = resp.json()
        print(json.dumps(body, indent=2)[:1500])
    except Exception:
        print(resp.text[:1500])
    return resp.status_code


def main():
    token = load_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with httpx.Client(headers=headers) as c:
        # max_completion_tokens (the non-deprecated form)
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Say hello in 5 words."}],
            "max_completion_tokens": 50,
        }, "Claude + max_completion_tokens=50")

        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Say hello in 5 words."}],
            "max_completion_tokens": 50,
        }, "GPT-4.1 + max_completion_tokens=50")

        # response_format json_object with "json" in the prompt
        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Return a json object with key 'greeting' and value 'hello'."}],
            "response_format": {"type": "json_object"},
        }, "GPT-4.1 + response_format=json_object (json in prompt)")

        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Return a json object with key 'greeting' and value 'hello'."}],
            "response_format": {"type": "json_object"},
        }, "Claude + response_format=json_object (json in prompt)")

        # top_p
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "top_p": 0.9,
        }, "Claude + top_p=0.9")

        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "top_p": 0.9,
        }, "GPT-4.1 + top_p=0.9")

        # frequency_penalty / presence_penalty
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "frequency_penalty": 0.5,
            "presence_penalty": 0.5,
        }, "Claude + frequency_penalty + presence_penalty")

        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "frequency_penalty": 0.5,
            "presence_penalty": 0.5,
        }, "GPT-4.1 + frequency_penalty + presence_penalty")

        # seed
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "seed": 42,
        }, "Claude + seed=42")

        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "seed": 42,
        }, "GPT-4.1 + seed=42")

        # stop sequences
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Count to 10"}],
            "stop": ["5"],
        }, "Claude + stop=['5']")

        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Count to 10"}],
            "stop": ["5"],
        }, "GPT-4.1 + stop=['5']")

        # n (multiple completions)
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "n": 2,
        }, "Claude + n=2")

        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "n": 2,
        }, "GPT-4.1 + n=2")

        # logprobs
        send(c, {
            "model": "openai-gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "logprobs": True,
        }, "GPT-4.1 + logprobs=true")

        # user param
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "user": "test-user-123",
        }, "Claude + user param")

        # unknown param (should it be rejected?)
        send(c, {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "foo_bar_unknown": True,
        }, "Claude + unknown param foo_bar_unknown")


if __name__ == "__main__":
    main()
