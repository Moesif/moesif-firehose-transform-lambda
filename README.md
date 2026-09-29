# Moesif Firehose Transform Lambda

A transform function for Amazon Data Firehose delivery streams that send API
events directly to Moesif.

When you integrate through Firehose, there is no Moesif SDK in your request
path, which means none of the capabilities an SDK normally provides. Events go
straight from your producer to Moesif, with no opportunity to shape them on the
way. **This function is that opportunity.** It runs inside the delivery stream,
sees every event before Moesif does, and can act on each one.

```
your API ──▶ Firehose ──▶ [ transform Lambda ] ──▶ Moesif
```

Your integration does not change. The delivery stream, its destination and your
producers stay exactly as they are - Firehose simply invokes this function on
each batch before delivering it.

## Why a transform Lambda

If you are already on Firehose and want a capability the SDKs give you, this is
where it goes.

## What it does today: sampling

Control how many events reach Moesif. Drop traffic with no analytical value,
and sample high-volume traffic down to a representative fraction while keeping
your volume metrics accurate.

Rules are **static and config-driven** and they live in `sampling_config.json` or
the `SAMPLING_CONFIG` environment variable and are read once per cold start.
Nothing is fetched at runtime, so there is no network dependency in the hot path.

---

## How it works

Every event gets checked against your rules. The first rule that fits decides
what share of that kind of traffic to keep - all of it, none of it, or something
in between.

### Your numbers stay right

Storing 1 event in 10 would normally make your traffic look like it collapsed.
It doesn't, because every event that survives is marked with how many it stands
for.

| you sent | kept at | stored in Moesif | each counts as | Moesif reports |
|---|---|---|---|---|
| 1,000 | 100% | 1,000 | 1 | 1,000 |
| 1,000 | 50% | ~500 | 2 | ~1,000 |
| 1,000 | 10% | ~100 | 10 | ~1,000 |

Counts, volume charts and trends stay accurate. You are storing less, not
reporting less.

Use a rate that divides 100 evenly such as 50, 25, 20, 10, 5, 4, 2 or 1. Anything else
reports a little under the true count.

### What you give up

Detail about individual requests. If a specific call was sampled out, it is not
in Moesif and cannot be looked up later.

Sampling is also decided per event rather than per user, so a single user's
requests are not kept or dropped together.

---

## Dependencies

No dependencies, Python standard library only.

---

## Configuring rules

Rules are evaluated **in order, and the first rule whose conditions all match
wins**. Conditions within a rule are ANDed. Events matching no rule get
`default_sample_rate`.

```json
{
  "default_sample_rate": 100,
  "rules": [
    { "name": "keep all errors", "sample_rate": 100,
      "conditions": [ { "path": "response.status", "operator": "gte", "value": 400 } ] },

    { "name": "drop health checks", "sample_rate": 0,
      "conditions": [ { "path": "request.route", "operator": "regex", "value": "^/health/?$" } ] },

    { "name": "high-volume client, successful reads", "sample_rate": 10,
      "conditions": [ { "path": "company_id", "value": "acme-corp" },
                      { "path": "response.status", "operator": "between", "value": [200, 399] } ] }
  ]
}
```

> **Order matters.** Rules that protect traffic (`sample_rate: 100`) and rules
> that remove it (`sample_rate: 0`) must come before the rules that sample, or
> the sampling rule will match first and win.

### `path`

Dotted path into the event: `response.status`, `request.verb`,
`request.headers.x-client-id`. Keys match exactly first, then case-insensitively,
so header casing does not matter.

Match the field names your stream actually carries. A raw API Gateway access log
is flat (`status`, `httpMethod`); a Moesif event model is nested
(`response.status`, `request.verb`).

`request.route` is derived when the event has no such field: the URL path alone,
without scheme, host or query string. Prefer it over `request.uri` for path
rules, since `request.uri` holds the full URL and `^/v1/items$` will not match
`https://api.example.com/v1/items?page=2`.

### `operator`

| operator | `value` | matches when |
|---|---|---|
| `equals` *(default)* | string, number, bool | equal, compared numerically when both sides are numbers, so `200` matches `"200"` |
| `not_equals` | string, number, bool | not equal |
| `in` | list | equal to any entry |
| `not_in` | list | equal to no entry |
| `regex` | pattern string | pattern found anywhere in the value |
| `contains` | string | substring found anywhere |
| `gt` `gte` `lt` `lte` | number | numeric comparison |
| `between` | `[low, high]` | within range, both ends included |
| `exists` | none | field present and not null |
| `not_exists` | none | field absent or null |

Add `"ignore_case": true` to any string comparison.

### `sample_rate`

A percentage from 0 to 100.

| value | effect |
|---|---|
| `100` | keep everything, so use it to protect traffic from later rules |
| `10` | keep about 1 in 10, each stamped `weight: 10` |
| `0` | drop unconditionally, **no weight stamped** |

Rate `0` removes events from Moesif entirely rather than sampling them, so
reserve it for traffic you never want counted, such as health checks and CORS
preflight.

Prefer rates that divide 100 evenly: **50, 25, 20, 10, 5, 4, 2, 1**. Weight is
`floor(100 / rate)`, so other values round down: rate 30 gives weight 3 and
extrapolates to 90% of true volume.

### Where the config comes from

Checked in this order:

| source | use when |
|---|---|
| `SAMPLING_CONFIG` env var (inline JSON) | rules change often, no redeploy needed |
| `SAMPLING_CONFIG_PATH` env var (file path) | several environments, or a Lambda layer |
| `sampling_config.json` beside the code | rules are stable and belong in version control |

Note that Lambda caps all environment variables at 4 KB combined, so large rule
sets belong in the bundled file.

---

## Deploying

1. Package the function:
   ```bash
   zip function.zip lambda_function.py sampling_config.json
   ```
2. Create the Lambda with Python 3.12 or later, handler
   `lambda_function.lambda_handler`, timeout **60 seconds** (the 3-second default
   is not enough for a full buffer), memory 512 MB.
3. On the delivery stream, enable **Transform source records with AWS Lambda**
   and point it at the function. Buffer hints of 1 MB / 60 s are a reasonable
   starting point.
4. Grant the delivery stream's IAM role `lambda:InvokeFunction` on the function.

To change rules later, update the `SAMPLING_CONFIG` environment variable. Saving
it recycles the execution environment, and the next invocation picks up the new
rules. No redeploy is needed.

---

## Monitoring

Each invocation emits one structured log line:

```json
{"msg": "firehose_sampling_summary", "records_in": 500, "events_in": 500,
 "events_kept": 61, "events_dropped": 439, "records_dropped": 439,
 "config_valid": true}
```

```
fields @timestamp, events_in, events_kept, events_dropped
| filter msg = "firehose_sampling_summary"
| stats sum(events_in) as sent, sum(events_dropped) as dropped by bin(1h)
```

Two things to watch:

- **`config_valid: false`**: the config failed to parse and every event is being
  kept. Worth an alarm.
- **`events_dropped: 0`** when you expect otherwise, usually a rule referencing a
  field name your stream does not carry.

---

## Behaviour and limitations

**Fails open.** An invalid config, an unreadable record, non-JSON content, or an
unexpected error passes the record through unchanged. The function never returns
`ProcessingFailed`, which would route records to the delivery stream's error
output.

**Batched records.** A record containing newline-delimited JSON or a JSON array is
sampled per event; the record is marked `Dropped` only when every event inside it
is dropped. Events that were not modified are re-emitted byte for byte.

**Sampling is independent per event.** Events are not grouped by user or session,
so a sampled trace may contain gaps. Deterministic sampling keyed on a user or
request id would avoid this and is a change to `should_keep()`.

**Weight handling depends on your ingestion path.** Confirm that sampled volume
extrapolates as you expect in Moesif before relying on these numbers for
reporting or billing.

**Static rules only, for now.** Rules change when you change the configuration.
There is no fetch from the Moesif API, no ETag handling, and no governance rules.
Dynamic sampling is the intended next capability for this function; see
*What it does today* above.

---

## License

Apache 2.0. See [LICENSE](LICENSE).
