#!/usr/bin/env python3
"""
Run the transform function locally and show what your rules do.

    python3 tests/run_local.py                      # Run with sampling_config.json on sample_event.json
    python3 tests/run_local.py --payload mine.json  # Run with sampling_config.json on your own captured records
    python3 tests/run_local.py --repeatable         # Same result on every run

Reads files and prints. Nothing is sent anywhere.
"""

import argparse
import base64
import collections
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_PAYLOAD = Path(__file__).resolve().parent / "sample_event.json"


def describe(record):
    """One readable line per record: what it was before the rules ran."""
    note = record.get("note", "")
    try:
        body = base64.b64decode(record["data"]).decode("utf-8")
    except Exception:
        return note, "(undecodable)"

    import lambda_function as lf
    items, _ = lf._split(body)
    events = [parsed for _, parsed in items if isinstance(parsed, dict)]
    if not events:
        return note, "(not JSON)"

    parts = []
    for e in events:
        verb = lf.get_path(e, "request.verb")
        status = lf.get_path(e, "response.status")
        route = lf._route(e)
        company = lf.get_path(e, "company_id")
        parts.append("%s %s %s %s" % (company, verb, status, route))
    return note, "  +  ".join(str(p) for p in parts)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--payload", type=Path, default=DEFAULT_PAYLOAD,
                        help="Firehose payload to run (default: tests/sample_event.json)")
    parser.add_argument("--repeatable", action="store_true",
                        help="freeze the sampling rolls so repeated runs give the same result")
    args = parser.parse_args()

    raw = json.loads((ROOT / "sampling_config.json").read_text(encoding="utf-8"))
    config = {k: v for k, v in raw.items() if not k.startswith("_")}
    os.environ["SAMPLING_CONFIG"] = json.dumps(config)

    import lambda_function as lf
    if args.repeatable:
        # Any fixed starting point works; the value itself carries no meaning.
        random.seed(0)

    print("\nrules in effect")
    print("  %5s  %s" % ("rate", "name"))
    for rule in config.get("rules", []):
        print("  %4g%%  %s" % (rule["sample_rate"], rule["name"]))
    print("  %4g%%  (everything else)" % config.get("default_sample_rate", 100))
    if "REPLACE_ME" in json.dumps(config.get("rules", [])):
        print("\n  !! REPLACE_ME is still in sampling_config.json;"
              " that rule will never match.")

    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    results = lf.lambda_handler(payload)["records"]

    print("\n  %-9s %-46s %s" % ("VERDICT", "EVENT", "WHY IT IS HERE"))
    print("  " + "-" * 104)
    for record, result in zip(payload["records"], results):
        note, summary = describe(record)
        verdict = "DROPPED" if result["result"] == "Dropped" else "kept"
        print("  %-9s %-46s %s" % (verdict, summary[:46], note))

    kept = []
    for result in results:
        if result["result"] != "Ok" or not result.get("data"):
            continue
        body = base64.b64decode(result["data"]).decode("utf-8", "replace")
        items, _ = lf._split(body)
        kept.extend(p for _, p in items if isinstance(p, dict))

    total_in = sum(len([p for _, p in lf._split(base64.b64decode(r["data"]).decode("utf-8", "replace"))[0]
                        if isinstance(p, dict)])
                   for r in payload["records"])
    weights = collections.Counter(e.get("weight", 1) for e in kept)

    print("\n  records in       %d" % len(payload["records"]))
    print("  records dropped  %d" % sum(1 for r in results if r["result"] == "Dropped"))
    print("  events in        %d" % total_in)
    print("  events kept      %d  (%.0f%%)" % (len(kept), 100.0 * len(kept) / max(total_in, 1)))
    print("  events dropped   %d" % (total_in - len(kept)))
    print("  weights stamped  " + ", ".join("%d x weight %s" % (c, w) for w, c in sorted(weights.items())))
    print("  Moesif counts    %d events after extrapolation\n"
          % sum(e.get("weight", 1) for e in kept))


if __name__ == "__main__":
    main()
