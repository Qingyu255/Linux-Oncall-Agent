# Day 1 execution manifest

Captured 2026-09-23 in the local development lab. This is a reproducibility record for the verified
CPU vertical slice; it is not an AWS deployment manifest.

## Host and source

| Item | Observed value |
|---|---|
| Host | macOS 26.3, arm64 |
| Repository base commit | `e212ec8734b7b26e605d3654a04c0a692cb82c0e` plus the uncommitted Day 1 worktree |
| DeepSeek Harness source reviewed | `00102833dfaee1da9f48a3a8eae9d34005a75218` |
| Python | 3.12.12 |
| uv | 0.9.5 |
| Docker client/server | 27.3.1 / 27.3.1 |
| Docker Compose | v2.29.7-desktop.1 |

## Application dependencies

| Package | Pinned version |
|---|---:|
| deepseek-harness-sdk | 0.1.5rc1 |
| fastapi | 0.141.1 |
| httpx | 0.28.1 |
| mcp | 2.2.0 |
| pydantic | 2.13.5 |
| typer | 0.27.2 |
| uvicorn | 0.53.0 |

`uv.lock` is authoritative for the complete Python resolution. Both application images derive from
`python:3.12-slim-bookworm` by digest in the generated image metadata.

## Built images

| Image | Local image ID | Architecture |
|---|---|---|
| `linux-oncall-core:day1` | `sha256:6c406f64a795d9ff5e6b6e61e359f599a1cadf33546f2a17336219e4272158a1` | arm64 |
| `linux-oncall-agent:day1` | `sha256:54f33313dea0279687c0b38cdd28faea3e99528b191c0138964f9db98ff13a55` | arm64 |

Image IDs will change when source or dependencies change; tags name the local Day 1 build and are not
remote immutable release references.

## Runtime configuration

- Provider mode: deterministic fixture over the OpenAI Chat Completions wire format.
- Configured model identifier: `gpt-4.1-mini`; no provider call was made in fixture mode.
- DSH profile: `sdk-minimal` plus `harness/oncall.patch.yml`.
- DSH plugins: OpenAI-compatible Pi adapter, oncall MCP client, system prompt, persistent local bash.
- Disabled: DeepSeek provider, DeepSeek API extensions, session upload/log plugin, package inventory.
- Limits: 180-second investigation deadline, 20 probe calls, 2 concurrent probes, 10 MiB total
  admitted artifacts, 1 MiB per artifact, 2 MiB provider response, and 12 model-relay requests.

## Acceptance artifacts

| Check | Result |
|---|---|
| Bounded CPU fault | Run `bc005286bf8e41dc9ec58528d21b1708`; accepted, inconclusive fixture report |
| CPU evidence | 1.007 cores used / 1.0 quota; 2,549,414 µs throttling during the sample |
| Process attribution | Two workload PIDs at about 49% of one core each |
| Citation validation | Two investigation-owned evidence IDs accepted |
| Agent network | Broker reachable; target DNS and public socket blocked |
| Agent authority | No Docker socket, AWS/provider credentials, target token, or admin token |
| Cancellation | Active run persisted as `cancelled`; in-flight unit test admits no evidence |
| Automated checks | Ruff pass; strict core mypy pass; 16 pytest tests pass; sdist/wheel build pass |

The rendered reports and raw local evidence live under ignored `.local/`. See
[Day 1 status](day-1-status.md) for interpretation and limitations.
