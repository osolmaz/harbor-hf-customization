"""Fail the optional mutation workflow when the recorded kill rate is too low."""

import argparse
import json
from pathlib import Path

from harbor_pi_code_mode.values import count, number, record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-kill-rate", type=float, required=True)
    args = parser.parse_args()
    minimum = number(args.min_kill_rate)
    stats = record(json.loads(Path("mutants/mutmut-cicd-stats.json").read_text()))
    total, killed = count(stats["total"]), count(stats["killed"])
    if not total or killed / total * 100 < minimum:
        raise SystemExit("Mutation kill rate is below the configured minimum")
    print(f"Mutation gate passed: {killed}/{total} killed")


if __name__ == "__main__":
    main()
