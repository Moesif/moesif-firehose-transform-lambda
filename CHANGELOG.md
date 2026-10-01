# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-01

First release.

### Added

- Firehose transform handler that samples API events before they reach Moesif.
- Sampling rules fetched from Moesif's `/v1/config`, enabled by setting
  `MOESIF_APPLICATION_ID`. Supports `sample_rate`, `user_sample_rate`,
  `company_sample_rate` and `regex_config`, applied in that precedence.
- `weight` stamped on sampled events so Moesif extrapolates back to true volume.
- Config refreshed on the invocation path once `CONFIG_REFRESH_SECONDS` has
  elapsed, with the previous ETag sent as `If-None-Match`.
- Failed fetches retried within the `CONFIG_FETCH_TIMEOUT_SECONDS` budget.
- Records containing newline-delimited JSON or a JSON array sampled per event.
- Settings read from the environment: `MOESIF_APPLICATION_ID`, `MOESIF_BASE_URI`,
  `CONFIG_REFRESH_SECONDS`, `CONFIG_FETCH_TIMEOUT_SECONDS`, `DEBUG`.
- One structured summary log line per invocation.

### Behaviour

- Fails open: anything the function cannot handle passes through unchanged, and
  `ProcessingFailed` is never returned.
- Without `MOESIF_APPLICATION_ID`, or until a config is fetched, every event is
  kept.

[1.0.0]: https://github.com/Moesif/moesif-firehose-transform-lambda/releases/tag/v1.0.0
