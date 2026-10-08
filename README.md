# Moesif Firehose Transform Lambda

A transform function for Amazon Data Firehose delivery streams that send API
events directly to Moesif.

When you integrate through Firehose, there is no Moesif SDK in your request
path, which means none of the capabilities an SDK normally provides. Events go
straight from your producer to Moesif, with no opportunity to shape them on the
way. **This function is that opportunity.** It runs inside the delivery stream,
sees every event before Moesif does, and can act on each one.

```
your API/AWS API Gateway ──▶ AWS Firehose ──▶ [ AWS transform Lambda ] ──▶ Moesif
```

Both setups are supported: API Gateway writing access logs to the stream, or your
own producer sending Moesif events to it.

Your integration does not change. The delivery stream, its destination and your
producers stay exactly as they are. Firehose simply invokes this function on
each batch before delivering it.

## Why a transform Lambda

If you are already on Firehose and want a capability the SDKs give you, this is
where it goes.

## What it does today: sampling

Control how many events reach Moesif. Drop traffic with no analytical value,
and sample high-volume traffic down to a representative fraction while keeping
your volume metrics accurate.

Rules are **managed in Moesif** and fetched at runtime, so rates change in the
Moesif UI without touching the function. The sampling decision itself is made
from a cached config, so no record waits on a network call.

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

Use a rate that divides 100 evenly, such as 50, 25, 20, 10, 5, 4, 2 or 1. Anything else
reports a little under the true count.

### What you give up

Detail about individual requests. If a specific call was sampled out, it is not
in Moesif and cannot be looked up later.

Sampling is also decided per event rather than per user, so a single user's
requests are not kept or dropped together.

---

## Dependencies

No dependencies. Python standard library only.

---

## Configuring rules

Rules come from Moesif. Set one environment variable and the function fetches
them:

```
MOESIF_APPLICATION_ID = <your Application Id>
```

It fetches the config and applies it. Change your sampling rates in Moesif and this
function picks them up on its next refresh. Nothing is redeployed, and no rules
live in the function.

### What the config contains

| field | what it sets |
|---|---|
| `sample_rate` | the default rate for anything unmatched |
| `user_sample_rate` | `{user_id: rate}` |
| `company_sample_rate` | `{company_id: rate}` |
| `regex_config` | rules matched on request and response fields |

Rates are resolved in this order, so the same config produces the same rate
wherever it is applied:

```
regex_config  ->  user_sample_rate  ->  company_sample_rate  ->  sample_rate
```

Within `regex_config`, rules are evaluated in order and the first whose
conditions all match wins. Put rules that protect traffic (`sample_rate: 100`)
and rules that remove it (`sample_rate: 0`) before the rules that sample.

### Works with either stream

Some delivery streams carry API Gateway access logs; others carry Moesif event
models. Rules are written the same way for both, and the function works out
which it is receiving. Nothing to configure.

### Settings

Every setting is an environment variable. Nothing is hardcoded.

| variable | default | |
|---|---|---|
| `MOESIF_APPLICATION_ID` | none | set it to enable sampling |
| `MOESIF_BASE_URI` | `https://api.moesif.net` | override for another region or a proxy |
| `CONFIG_REFRESH_SECONDS` | `60` | how often to re-check while warm |
| `CONFIG_FETCH_TIMEOUT_SECONDS` | `6` | total time allowed for a fetch, retries included |
| `DEBUG` | off | set to `true` for verbose logging |

### How refresh works

Since Lambda freezes the execution environment between invocations, the function
checks on the invocation path, and only once `CONFIG_REFRESH_SECONDS` has
elapsed. The ETag from the previous response goes back as `If-None-Match`, so an
unchanged config costs a `304` and no body.

With high Firehose concurrency, each warm instance refreshes independently, so
raise the interval if many instances run at once.

A failed fetch is retried with a short backoff until
`CONFIG_FETCH_TIMEOUT_SECONDS` is spent. Each attempt is given whatever is left
of that budget, so a fetch never delays a batch for longer than it allows.

### When there are no rules

**Without rules, nothing is sampled and every event reaches Moesif.** That is
the state when `MOESIF_APPLICATION_ID` is not set, and on a cold start when
Moesif could not be reached.

Failures never stop delivery. If Moesif is unreachable, returns an error, or
sends something unparseable, the function keeps using the config it already has
and retries on the next interval rather than on every invocation.

The summary log line reports the config in use via `config_source`, and
`config_valid: false` means no rules are in effect.

---

## Deploying

1. Package the function:
   ```bash
   zip function.zip lambda_function.py
   ```
2. Create the Lambda with Python 3.12 or later, handler
   `lambda_function.lambda_handler`, memory 512 MB, and a timeout of **at least
   30 seconds**. Sampling a full 6 MB buffer takes well under a second, so the
   timeout mainly needs to cover a config fetch, which `CONFIG_FETCH_TIMEOUT_SECONDS`
   already caps. Raise it if your buffers are large or you prefer more headroom.
3. On the delivery stream, enable **Transform source records with AWS Lambda**
   and point it at the function. Buffer hints of 1 MB / 60 s are a reasonable
   starting point.
4. Grant the delivery stream's IAM role `lambda:InvokeFunction` on the function.

Set `MOESIF_APPLICATION_ID` on the function. Rules are then managed in Moesif and
picked up on the next refresh, with no redeploy.

---

## Monitoring

Each invocation emits one structured log line:

```json
{"msg": "firehose_sampling_summary", "records_in": 500, "events_in": 500,
 "events_kept": 61, "events_dropped": 439, "records_dropped": 439,
 "config_source": "moesif:/v1/config", "config_valid": true}
```

```
fields @timestamp, events_in, events_kept, events_dropped
| filter msg = "firehose_sampling_summary"
| stats sum(events_in) as sent, sum(events_dropped) as dropped by bin(1h)
```

Two things to watch:

- **`config_valid: false`**: no rules are in effect and every event is being
  kept, either because `MOESIF_APPLICATION_ID` is unset or because Moesif could
  not be reached on the cold start. Worth an alarm.
- **`events_dropped: 0`** when you expect otherwise: usually a rule referencing a
  field name your stream does not carry.

---

## Behaviour and limitations

**Fails open.** Anything the function cannot handle passes through unchanged. It
never returns `ProcessingFailed`, which would route records to the delivery
stream's error output.

**Batched records.** A record containing newline-delimited JSON or a JSON array is
sampled per event; the record is marked `Dropped` only when every event inside it
is dropped. Events that were not modified are re-emitted byte for byte.

**Sampling is independent per event.** Events are not grouped by user or session,
so a sampled trace may contain gaps. Deterministic sampling keyed on a user or
request id would avoid this and is a change to `should_keep()`.

**Weight handling depends on your ingestion path.** Confirm that sampled volume
extrapolates as you expect in Moesif before relying on these numbers for
reporting or billing.

**No governance rules.** This function samples and filters; it does not block or
transform requests the way an SDK's governance rules can.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

Apache 2.0. See [LICENSE](LICENSE).
