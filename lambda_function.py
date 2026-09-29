"""Firehose transform Lambda: sample API events before Moesif.

Static rules from SAMPLING_CONFIG or sampling_config.json; nothing fetched at runtime.

    rate   = first matching rule, else default_sample_rate
    keep   = random() * 100 < rate
    weight = floor(100 / rate)   stamped on sampled events for extrapolation

Handler: lambda_function.lambda_handler
"""

import base64
import json
import logging
import math
import os
import random
import re

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

MISSING = object()  # Distinguishes an absent field from a JSON null
_CONFIG = None


# --- config -------------------------------------------------------------------

def load_config():
    """Read the config. Falls back to keeping everything."""
    keep_all = {"default_sample_rate": 100.0, "rules": [], "valid": True}
    try:
        inline = os.environ.get("SAMPLING_CONFIG", "").strip()
        if inline:
            raw, source = json.loads(inline), "env:SAMPLING_CONFIG"
        else:
            path = os.environ.get("SAMPLING_CONFIG_PATH") or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "sampling_config.json"
            )
            with open(path, encoding="utf-8") as handle:
                raw, source = json.load(handle), path

        config = {
            "default_sample_rate": _rate(raw.get("default_sample_rate", 100)),
            "rules": [_parse_rule(r, i) for i, r in enumerate(raw.get("rules", []))],
            "source": source,
            "valid": True,
        }
        logger.info("Loaded sampling config from %s: %s", source,
                    [(r["name"], r["sample_rate"]) for r in config["rules"]])
        return config
    except Exception as exc:
        logger.error("Bad or missing sampling config (%s); keeping all events", exc)
        keep_all["valid"] = False
        return keep_all


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("sample_rate must be a number 0-100, got %r" % (value,))
    return float(value)


def _parse_rule(raw, index):
    conditions = raw.get("conditions") or []
    if not conditions:
        # A rule with no conditions would match every event and shadow the rest.
        raise ValueError("rule[%d] has no conditions; use default_sample_rate instead" % index)
    return {
        "name": raw.get("name") or "rule[%d]" % index,
        "sample_rate": _rate(raw.get("sample_rate")),
        "conditions": [_parse_condition(c) for c in conditions],
    }


def _parse_condition(raw):
    operator = raw.get("operator", "equals")
    if operator not in OPERATORS:
        raise ValueError("unknown operator %r" % (operator,))
    if not raw.get("path"):
        raise ValueError("condition needs a 'path'")
    condition = {
        "path": raw["path"],
        "operator": operator,
        "value": raw.get("value"),
        "ignore_case": bool(raw.get("ignore_case")),
    }
    if operator == "regex":  # Compiled at load rather than on every event
        condition["regex"] = re.compile(str(raw.get("value")),
                                        re.IGNORECASE if condition["ignore_case"] else 0)
    return condition


# --- rule matching ------------------------------------------------------------

def get_path(payload, path):
    """Resolve a dotted path. Keys match exactly first, then case-insensitively."""
    current = payload
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment in current:
                current = current[segment]
                continue
            key = next((k for k in current
                        if isinstance(k, str) and k.casefold() == segment.casefold()), MISSING)
            if key is MISSING:
                return MISSING
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
    return current


def _number(value):
    """Float, or None if not numeric. Booleans are not treated as numbers."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value, ignore_case):
    if value is True or value is False or value is None:
        value = json.dumps(value)  # Compare using JSON spelling: true/false/null
    text = value if isinstance(value, str) else str(value)
    return text.casefold() if ignore_case else text


def _equals(actual, expected, ignore_case):
    left, right = _number(actual), _number(expected)
    if left is not None and right is not None:
        return left == right  # Numeric when both sides are numeric: 200 matches "200"
    return _text(actual, ignore_case) == _text(expected, ignore_case)


def _compare(actual, expected, test):
    left, right = _number(actual), _number(expected)
    return left is not None and right is not None and test(left, right)


def _between(actual, bounds):
    value, low, high = _number(actual), _number(bounds[0]), _number(bounds[1])
    if value is None or low is None or high is None:
        return False
    low, high = min(low, high), max(low, high)
    return low <= value <= high  # Inclusive, so [200, 299] covers all 2xx


OPERATORS = {
    "equals": lambda c, a: _equals(a, c["value"], c["ignore_case"]),
    "not_equals": lambda c, a: not _equals(a, c["value"], c["ignore_case"]),
    "in": lambda c, a: any(_equals(a, v, c["ignore_case"]) for v in c["value"]),
    "not_in": lambda c, a: not any(_equals(a, v, c["ignore_case"]) for v in c["value"]),
    "regex": lambda c, a: c["regex"].search(_text(a, False)) is not None,
    "contains": lambda c, a: _text(c["value"], c["ignore_case"]) in _text(a, c["ignore_case"]),
    "gt": lambda c, a: _compare(a, c["value"], lambda x, y: x > y),
    "gte": lambda c, a: _compare(a, c["value"], lambda x, y: x >= y),
    "lt": lambda c, a: _compare(a, c["value"], lambda x, y: x < y),
    "lte": lambda c, a: _compare(a, c["value"], lambda x, y: x <= y),
    "between": lambda c, a: _between(a, c["value"]),
    "exists": lambda c, a: True,   # Presence is checked before dispatch
    "not_exists": lambda c, a: False,
}


def _route(event):
    """The URL path only, without scheme, host or query string."""
    uri = get_path(event, "request.uri")
    if not isinstance(uri, str):
        return MISSING
    extracted = re.match(r"http[s]*://[^/]+(/[^?]+)", uri)
    return extracted.group(1) if extracted else "/"


# Paths computed from the event rather than read off it. request.route is
# derived the same way the Moesif SDKs derive it, so a rule written against
# either matches identically.
DERIVED = {"request.route": _route}


def _matches(condition, event):
    actual = get_path(event, condition["path"])
    if actual is MISSING and condition["path"] in DERIVED:
        actual = DERIVED[condition["path"]](event)
    present = actual is not MISSING and actual is not None
    if condition["operator"] == "exists":
        return present
    if condition["operator"] == "not_exists":
        return not present
    if actual is MISSING:
        return False  # An absent field satisfies no value comparison
    return OPERATORS[condition["operator"]](condition, actual)


def resolve_rate(event, config):
    """First rule whose conditions all match wins; otherwise the default."""
    for rule in config["rules"]:
        try:
            if all(_matches(c, event) for c in rule["conditions"]):
                return rule["sample_rate"], rule["name"]
        except Exception:
            logger.warning("Rule %r failed to match; skipping", rule["name"], exc_info=True)
    return config["default_sample_rate"], None


# --- keep / drop / weight -----------------------------------------------------

def should_keep(sample_rate):
    """True for approximately `sample_rate` percent of calls."""
    if sample_rate >= 100:
        return True
    if sample_rate <= 0:
        return False
    return random.random() * 100 < sample_rate


def weight_for(sample_rate):
    """How many original events each sampled event represents."""
    # Rates that do not divide 100 evenly are floored, which under-reports
    # volume: rate 30 yields weight 3, extrapolating to 90% of the true count.
    # Use 50, 25, 20, 10, 5, 4, 2 or 1 for exact extrapolation.
    return 1 if sample_rate <= 0 else max(1, math.floor(100 / sample_rate))


def stamp_weight(event, sample_rate):
    """Set the event's weight. Returns True if the event was modified."""
    weight = weight_for(sample_rate)
    existing = event.get("weight")
    if weight == existing:
        return False
    # Moesif treats an absent weight as 1, so writing it adds payload for nothing.
    if weight == 1 and existing is None:
        return False
    event["weight"] = weight
    return True


def sample(event, config):
    """Decide one event. Returns (keep, modified, rule_name)."""
    rate, rule_name = resolve_rate(event, config)
    if not should_keep(rate):
        return False, False, rule_name
    modified = isinstance(event, dict) and stamp_weight(event, rate)
    return True, modified, rule_name


# --- Firehose handler ---------------------------------------------------------

def lambda_handler(event, context=None):
    """One result per record, in order. Sampled-out records are marked Dropped.

    ProcessingFailed is never returned: it would route records to the delivery
    stream's error output. Records that cannot be read pass through unchanged.
    """
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    config = _CONFIG

    records, stats = [], {"in": 0, "kept": 0, "dropped": 0, "records_dropped": 0}
    for record in event.get("records") or []:
        try:
            records.append(_process(record, config, stats))
        except Exception:
            logger.exception("Error sampling record; passing it through")
            records.append(_passthrough(record))

    logger.info(json.dumps({
        "msg": "firehose_sampling_summary",
        "invocationId": event.get("invocationId"),
        "config_valid": config["valid"],
        "records_in": len(records),
        "records_dropped": stats["records_dropped"],
        "events_in": stats["in"],
        "events_kept": stats["kept"],
        "events_dropped": stats["dropped"],
    }))
    return {"records": records}


def _process(record, config, stats):
    # Nothing can be dropped, so skip decoding entirely.
    if config["default_sample_rate"] >= 100 and not config["rules"]:
        return _passthrough(record)

    try:
        body = base64.b64decode(record["data"]).decode("utf-8")
    except Exception:
        return _passthrough(record)

    items, kind = _split(body)
    if not items:
        return _passthrough(record)  # Not JSON; pass through unchanged

    kept, changed = [], False
    for raw, parsed in items:
        if parsed is None:          # Unparseable line within a valid record
            kept.append((raw, None, False))
            continue
        stats["in"] += 1
        keep, modified, _ = sample(parsed, config)
        if keep:
            stats["kept"] += 1
            changed = changed or modified
            kept.append((raw, parsed, modified))
        else:
            stats["dropped"] += 1

    if not kept:
        stats["records_dropped"] += 1
        return {"recordId": record["recordId"], "result": "Dropped"}
    if len(kept) == len(items) and not changed:
        return _passthrough(record)

    out = _join(kept, kind, body.endswith("\n"))
    return {
        "recordId": record["recordId"],
        "result": "Ok",
        "data": base64.b64encode(out.encode("utf-8")).decode("ascii"),
    }


def _split(body):
    """Into (raw, parsed_or_None) events. Handles one object, an array, or NDJSON."""
    whole = _try_json(body.strip())
    if isinstance(whole, list):
        return [(None, item) for item in whole], "array"
    if isinstance(whole, dict):
        return [(body, whole)], "lines"
    return [(line, _try_json(line.strip()))
            for line in body.split("\n") if line.strip()], "lines"


def _try_json(text):
    # Only objects and arrays are events; a bare scalar has no fields to match.
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _join(kept, kind, trailing_newline):
    if kind == "array":
        return json.dumps([parsed for _, parsed, _ in kept], separators=(",", ":"))
    # Only modified events are re-serialised; the rest stay byte-identical.
    lines = [json.dumps(parsed, separators=(",", ":")) if modified else raw
             for raw, parsed, modified in kept]
    text = "\n".join(lines)
    return text + "\n" if trailing_newline and text else text


def _passthrough(record):
    return {"recordId": record.get("recordId"), "result": "Ok", "data": record.get("data")}
