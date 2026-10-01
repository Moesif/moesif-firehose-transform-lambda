"""Firehose transform Lambda: sample API events before Moesif.

Rules are fetched from Moesif with MOESIF_APPLICATION_ID. Without them nothing is sampled.

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
import time
import urllib.error
import urllib.request

# --- settings -----------------------------------------------------------------
# Defaults for the environment variables named beside them.
# MOESIF_APPLICATION_ID has no default: without it no config is fetched and nothing is sampled.

DEFAULT_BASE_URI = "https://api.moesif.net"   # MOESIF_BASE_URI
DEFAULT_REFRESH_SECONDS = 60.0                # CONFIG_REFRESH_SECONDS
DEFAULT_FETCH_TIMEOUT_SECONDS = 6.0           # CONFIG_FETCH_TIMEOUT_SECONDS

# Fixed constants, not configurable.
DEFAULT_SAMPLE_RATE = 100.0         # the rate when Moesif supplies no rules
CONFIG_PATH = "/v1/config"          # appended to the base URI
TRUTHY = ("1", "true", "yes", "on") # values that turn DEBUG on
RETRY_BACKOFF_SECONDS = 0.5         # multiplied by the attempt number
MIN_FETCH_TIMEOUT_SECONDS = 0.1     # floor for a configured fetch budget


logger = logging.getLogger()
DEBUG = os.environ.get("DEBUG", "").strip().lower() in TRUTHY
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

MISSING = object()  # Distinguishes an absent field from a JSON null

_CONFIG = None      # the config in use
_ETAG = None        # ETag of the last config fetched from Moesif
_FETCHED_AT = 0.0   # monotonic time of the last fetch attempt


# --- config -------------------------------------------------------------------

def keep_everything(reason):
    """The config used when Moesif has not supplied one: sample nothing."""
    return {"default_sample_rate": DEFAULT_SAMPLE_RATE, "rules": [], "user_sample_rate": {},
            "company_sample_rate": {}, "source": reason, "valid": False}


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("sample_rate must be a number 0-100, got %r" % (value,))
    return float(value)


def _rate_map(raw):
    """Validate a {id: sample_rate} map, as used for users and companies."""
    if not isinstance(raw, dict):
        return {}
    return {str(key): _rate(value) for key, value in raw.items()}


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


# --- dynamic config from Moesif ----------------------------------------------

def dynamic_config_enabled():
    return bool(os.environ.get("MOESIF_APPLICATION_ID", "").strip())


def _refresh_seconds():
    try:
        return max(0.0, float(os.environ.get("CONFIG_REFRESH_SECONDS",
                                             DEFAULT_REFRESH_SECONDS)))
    except ValueError:
        return DEFAULT_REFRESH_SECONDS


def _fetch_timeout():
    """Total time allowed for a config fetch, retries included."""
    try:
        return max(MIN_FETCH_TIMEOUT_SECONDS,
                   float(os.environ.get("CONFIG_FETCH_TIMEOUT_SECONDS",
                                        DEFAULT_FETCH_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_FETCH_TIMEOUT_SECONDS


def fetch_remote_config():
    """GET /v1/config. Returns a config, or None to keep the current one.
    Retries until the time budget runs out. Each attempt is given whatever is
    left of it, so the call can never outlast CONFIG_FETCH_TIMEOUT_SECONDS.
    """
    app_id = os.environ.get("MOESIF_APPLICATION_ID", "").strip()
    if not app_id:
        return None

    url = os.environ.get("MOESIF_BASE_URI", DEFAULT_BASE_URI).rstrip("/") + CONFIG_PATH
    deadline = time.monotonic() + _fetch_timeout()
    attempt = 0

    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("Giving up on the Moesif config after %d attempt(s); "
                           "keeping the current config", attempt - 1)
            return None

        config, retry = _attempt_fetch(url, app_id, remaining)
        if config is not None or not retry:
            return config

        # Back off, but never past the deadline.
        delay = min(RETRY_BACKOFF_SECONDS * attempt,
                    max(0.0, deadline - time.monotonic()))
        if delay:
            time.sleep(delay)


def _attempt_fetch(url, app_id, timeout):
    """One request. Returns (config or None, whether to try again)."""
    global _ETAG

    headers = {"X-Moesif-Application-Id": app_id,
               "Content-Type": "application/json; charset=utf-8"}
    if _ETAG:
        headers["If-None-Match"] = _ETAG

    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            _ETAG = response.headers.get("X-Moesif-Config-ETag") or _ETAG
            return _from_moesif_config(json.loads(body)), False
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            # Not a failure: the config has not changed since the last fetch.
            logger.debug("Moesif config unchanged (304)")
            return None, False
        if 401 <= exc.code <= 403:
            logger.error("Unauthorized fetching Moesif config; check MOESIF_APPLICATION_ID")
        else:
            logger.warning("Moesif config fetch returned status %s", exc.code)
    except Exception as exc:
        logger.warning("Moesif config fetch failed (%s)", exc)
    return None, True


def _from_moesif_config(raw):
    """Translate a Moesif /v1/config document into this function's config shape."""
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")

    rules = []
    for index, entry in enumerate(raw.get("regex_config") or []):
        conditions = [{"path": c.get("path"), "operator": "regex", "value": c.get("value")}
                      for c in entry.get("conditions") or []]
        try:
            rules.append(_parse_rule({"name": "regex_config[%d]" % index,
                                      "sample_rate": entry.get("sample_rate"),
                                      "conditions": conditions}, index))
        except Exception as exc:
            # One unusable entry should not cost us the rest of the config.
            logger.warning("Skipping regex_config[%d] from Moesif: %s", index, exc)

    return {
        "default_sample_rate": _rate(raw.get("sample_rate", DEFAULT_SAMPLE_RATE)),
        "rules": rules,
        "user_sample_rate": _rate_map(raw.get("user_sample_rate")),
        "company_sample_rate": _rate_map(raw.get("company_sample_rate")),
        "source": "moesif:" + CONFIG_PATH,
        "valid": True,
    }


def samples_everything(config):
    """True when no rule can drop anything, so records need not be decoded."""
    return (config["default_sample_rate"] >= 100
            and not config["rules"]
            and not config.get("user_sample_rate")
            and not config.get("company_sample_rate"))


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


# Paths computed from the event rather than read off it.
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
    """Rules first, then per-user, then per-company, then the default."""
    for rule in config["rules"]:
        try:
            if all(_matches(c, event) for c in rule["conditions"]):
                return rule["sample_rate"], rule["name"]
        except Exception:
            logger.warning("Rule %r failed to match; skipping", rule["name"], exc_info=True)

    user_rates = config.get("user_sample_rate") or {}
    if user_rates:
        user_id = get_path(event, "user_id")
        if isinstance(user_id, str) and user_id in user_rates:
            return user_rates[user_id], "user:" + user_id

    company_rates = config.get("company_sample_rate") or {}
    if company_rates:
        company_id = get_path(event, "company_id")
        if isinstance(company_id, str) and company_id in company_rates:
            return company_rates[company_id], "company:" + company_id

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

def get_config():
    """The config in use, refreshed from Moesif when the interval has elapsed."""
    global _CONFIG, _FETCHED_AT
    now = time.monotonic()

    if _CONFIG is None:
        _FETCHED_AT = now
        _CONFIG = fetch_remote_config()
        if _CONFIG is None:
            # No rules yet, so nothing is sampled. The next interval tries again.
            reason = ("no MOESIF_APPLICATION_ID set" if not dynamic_config_enabled()
                      else "Moesif config unavailable")
            _CONFIG = keep_everything(reason)
            logger.warning("Keeping all events: %s", reason)
        else:
            logger.info("Sampling config loaded from Moesif: default %g%%, %d rule(s)",
                        _CONFIG["default_sample_rate"], len(_CONFIG["rules"]))
    elif dynamic_config_enabled() and now - _FETCHED_AT >= _refresh_seconds():
        # Stamp the attempt before making it, so a failing endpoint is retried
        # on the interval rather than on every invocation.
        _FETCHED_AT = now
        fresh = fetch_remote_config()
        if fresh:
            _CONFIG = fresh
            logger.info("Sampling config refreshed from Moesif: default %g%%, %d rule(s)",
                        fresh["default_sample_rate"], len(fresh["rules"]))

    return _CONFIG


def lambda_handler(event, context=None):
    """One result per record, in order. Sampled-out records are marked Dropped.

    ProcessingFailed is never returned: it would route records to the delivery
    stream's error output. Records that cannot be read pass through unchanged.
    """
    config = get_config()

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
        "config_source": config.get("source", "unknown"),
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
    if samples_everything(config):
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
