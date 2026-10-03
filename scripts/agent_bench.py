"""Compare models on recorded browser-agent prompts (from FORMLINE_AGENT_TRACE).

    uv run python scripts/agent_bench.py trace.jsonl --pick 2,3 --models gpt-4.1-mini,gpt-4.1 --repeat 3

For each picked prompt, prints each model's decision and latency, so prompt or model changes can be
judged on the exact situations that went wrong, not on a whole live run.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.agent.decision import Decision  # noqa: E402
from app.agent.prompts import system_prompt  # noqa: E402
from app.llm import client as llm  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("trace")
    p.add_argument("--pick", default="", help="comma-separated row numbers (default: all)")
    p.add_argument("--models", default="gpt-4.1-mini")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--temperature", type=float, default=0)
    args = p.parse_args()
    rows = [json.loads(line) for line in open(args.trace, encoding="utf-8")]
    picks = [int(x) for x in args.pick.split(",")] if args.pick else range(len(rows))
    for i in picks:
        prompt = rows[i]["prompt"]
        page = prompt.split("CURRENT PAGE:", 1)[1].strip().splitlines()[1]
        print(f"\n### row {i}: {page}")
        for model in args.models.split(","):
            for _ in range(args.repeat):
                t = time.perf_counter()
                try:
                    d = llm.structured(Decision, system=system_prompt("en"),
                                       messages=[{"role": "user", "content": prompt}], model=model,
                                       max_tokens=900, temperature=args.temperature)
                    out = f"{d.kind} {[(s.action, s.element_id, s.value) for s in d.steps]} | {d.say[:70]}"
                except Exception as e:
                    out = f"ERROR {str(e)[:90]}"
                print(f"  {model:14} {round((time.perf_counter() - t) * 1000):5}ms  {out}")


if __name__ == "__main__":
    main()
