"""Phase 1 smoke test. Run with the server running:
    python scripts/smoke_test.py
"""
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def chat(**overrides) -> httpx.Response:
    body = {
        "team_id": "search",
        "feature": "summarize",
        "messages": [{"role": "user", "content": "Hello gateway"}],
    }
    body.update(overrides)
    return httpx.post(f"{BASE}/v1/chat", json=body, timeout=120)


# (description, request overrides, expected status, expected error code)
CASES = [
    ("mock model returns a response", {}, 200, None),
    ("unknown model -> 400", {"model": "fake-model"}, 400, "unknown_model"),
    # Expects 503 only while OPENAI_API_KEY is unset in .env
    ("provider without API key -> 503", {"model": "gpt-4o-mini"}, 503, "provider_error"),
    ("empty messages -> 422 validation", {"messages": []}, 422, None),
    ("oversized prompt -> 400",
     {"messages": [{"role": "user", "content": "x" * 200_000}]}, 400, "context_too_long"),
]


def main() -> int:
    try:
        httpx.get(f"{BASE}/health", timeout=5).raise_for_status()
    except httpx.HTTPError:
        print(f"Server not reachable at {BASE}. Start it with: python -m uvicorn app.main:app --reload")
        return 1

    failures = 0
    for desc, overrides, want_status, want_code in CASES:
        resp = chat(**overrides)
        data = resp.json()
        got_code = data.get("error", {}).get("code") if isinstance(data.get("error"), dict) else None
        ok = resp.status_code == want_status and (want_code is None or got_code == want_code)
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {desc}: status={resp.status_code} code={got_code}")
        if not ok:
            print(f"        body: {data}")

    print("\nSample mock response:")
    print(chat().json())
    print(f"\n{len(CASES) - failures}/{len(CASES)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())