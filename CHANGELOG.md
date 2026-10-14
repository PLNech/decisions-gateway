# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-14

### Added

- HTTP gateway on `127.0.0.1:8017` that proxies `POST /v1/...` and `GET /v1/models` to a configured backend.
- Bearer auth on every route except `/health`, with the key read in-process from a key file
  (`key_file`, `key_var`, or the `DECISIONS_GATEWAY_KEY_FILE` environment variable).
- `Host` header check against DNS rebinding, JSON content type, `Content-Length` and body size caps.
- Question, option and questions x options caps, checked before forwarding.
- Admission control: per-backend in-flight cap (`503`), per-client concurrency cap (`429`) and optional
  `X-Decision-Deadline-Ms` wait for a slot.
- One JSON error envelope for gateway errors and backend 4xx responses, whose bodies are replaced.
- `Server-Timing` with backend entries, gateway overhead and gateway total.
- Prometheus `/metrics` and JSON `/v1/stats` behind the key, with per-minute aggregates flushed to SQLite
  under retention and a hard row cap.
- Labels only from the `known_clients` and `known_models` allow-lists or closed enums; no request or
  response text is logged or stored, proven by an end-to-end canary test.
- Hardened systemd unit template, an example backend unit, a render script, an example config and an
  install, cutover and rollback runbook.
- CI on Python 3.11, 3.12 and 3.13, and a release workflow that attaches the sdist and wheel.

[Unreleased]: ../../compare/v0.1.0...HEAD
[0.1.0]: ../../releases/tag/v0.1.0
