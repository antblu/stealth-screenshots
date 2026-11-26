#!/usr/bin/env python3
"""Local mock LLM endpoint for verifying Stealth Screenshots on Windows.

Logs only timestamps, HTTP status codes, and payload byte sizes (never image data
or credentials).
"""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time


class MockState:
    def __init__(self, mode: str, expected_token: str, slow_seconds: float) -> None:
        self.mode = mode
        self.expected_token = expected_token
        self.slow_seconds = slow_seconds
        self.request_count = 0


def make_handler(state: MockState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_POST(self) -> None:
            state.request_count += 1
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b""

            auth = self.headers.get("Authorization", "")
            status = 200
            extra_headers = {}

            if state.expected_token and auth != f"Bearer {state.expected_token}":
                status = 401
            elif state.mode == "401":
                status = 401
            elif state.mode == "429":
                status = 429
                extra_headers["Retry-After"] = "2"
            elif state.mode == "500":
                status = 500
            elif state.mode == "slow":
                time.sleep(state.slow_seconds)
                status = 200
            elif state.mode == "mixed":
                if state.request_count % 3 == 2:
                    status = 429
                    extra_headers["Retry-After"] = "2"

            valid_json = False
            has_system = False
            has_image = False
            try:
                doc = json.loads(raw_body.decode("utf-8"))
                valid_json = True
                msgs = doc.get("messages", [])
                has_system = any(m.get("role") == "system" for m in msgs)
                has_image = any(
                    m.get("role") == "user"
                    and isinstance(m.get("content"), list)
                    and any(c.get("type") == "image_url" for c in m["content"])
                    for m in msgs
                )
            except Exception:
                pass

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(
                f"[{ts}] req={state.request_count} path={self.path} "
                f"bytes={length} valid_json={valid_json} "
                f"system={has_system} image={has_image} status={status}",
                flush=True,
            )

            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in extra_headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LLM endpoint for testing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--mode",
        choices=["ok", "slow", "401", "429", "500", "mixed"],
        default="ok",
    )
    parser.add_argument("--token", default="", help="Expected Bearer token")
    parser.add_argument("--slow-seconds", type=float, default=12.0)
    args = parser.parse_args()

    state = MockState(args.mode, args.token, args.slow_seconds)
    server = HTTPServer((args.host, args.port), make_handler(state))
    print(f"Listening on http://{args.host}:{args.port} (mode={args.mode})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
