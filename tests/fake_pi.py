"""Scripted RPC peer. It never contacts a model provider."""

import json
import sys

from harbor_pi_code_mode.values import record

for line in sys.stdin:
    message = record(json.loads(line))
    command = message["type"]
    if command == "exit":
        break
    if command == "malformed":
        print("not-json", flush=True)
        continue
    print(
        json.dumps(
            {
                "id": message["id"],
                "type": "response",
                "success": command != "reject",
                "data": {"value": "a\u2028b"},
            }
        ),
        flush=True,
    )
    if command == "prompt":
        print(json.dumps({"type": "agent_settled"}), flush=True)
