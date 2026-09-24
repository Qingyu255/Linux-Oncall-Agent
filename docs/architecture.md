# Architecture

## Deployment and trust boundaries

One AWS EC2 target is enough for the MVP. The developer workstation hosts a trusted controller/broker and an isolated Linux container running the harness. The target runs a small deterministic Python service; it contains no model runtime. On macOS, use a Linux container VM such as the existing Docker environment, not macOS `/proc` substitutes.

```mermaid
flowchart TB
    User[Engineer CLI] --> Controller
    subgraph Local[Developer workstation]
        subgraph Trusted[Trusted controller container]
            Controller[Investigation service]
            Broker[Capability broker and MCP endpoint]
            Store[SQLite state and artifact files]
            Tunnel[SSM port-forward subprocess]
            Relay[Restricted model API relay]
        end
        subgraph Sandbox[Untrusted agent container]
            Harness[DeepSeek Harness]
            Shell[Local shell and Python]
            Copies[Sanitized evidence copies]
        end
        Controller --> Harness
        Harness --> Broker
        Harness --> Relay
        Harness --> Shell
        Shell --> Copies
        Broker --> Store
        Broker --> Tunnel
    end
    Relay --> Provider[Configured model provider]
    Tunnel --> SSM[AWS Session Manager]
    subgraph Target[Disposable EC2 target]
        Agent[SSM agent] --> Probe[Loopback-only probe service]
        Probe --> Kernel[procfs and cgroup v2]
        Probe --> Journal[Bounded service journal]
        Probe --> FS[Approved filesystem mounts]
        Lab[Operator-only fault runner] --> Workload[Synthetic workload]
    end
    SSM --> Agent
    User -. separate administrative path .-> Lab
```

The diagram shows logical components, not a requirement for a microservice per box. Controller, policy, storage and MCP live in one Python application. The relay is a small fixed-destination service or a proven equivalent, validated in Phase 0. No generic HTTP proxy or destination URL supplied by the model is allowed.

The agent container joins only an internal network shared with the broker. The broker has a separate egress interface for SSM and the configured model provider. Disable forwarding between interfaces; publish no administrative endpoints to the agent. The broker starts the SSM tunnel on its own loopback, so the agent cannot reach it. Local CLI administration uses a socket or host-only port not exposed on the agent network.

The agent receives only a short-lived investigation-scoped token. AWS credentials, target API credentials, provider keys, Terraform state and authoritative evidence storage belong to the trusted environment. The agent can write a temporary workspace, not controller files. Neither container gets a Docker socket through the application configuration; the operator launches containers outside the agent's control.

A thin runner inside the agent container launches the DSH SDK and its child runtime there. The trusted controller dispatches jobs and cancellation to that runner through the scoped control channel; it does not launch a harness subprocess in the credential-bearing controller container. The runner's results remain untrusted proposals until broker validation. For the first release, the operator starts the container pair and stops/replaces the agent container if graceful cancellation fails.

The runner also consumes DSH's session notification callback and projects it onto a versioned JSONL
progress channel for the operator CLI. Only lifecycle state, allowlisted oncall tool names, success or
failure, retries, and the turn finish reason cross this channel. Prompts, reasoning blocks, tool
arguments, tool results, and unknown tool names are discarded inside the agent container. This stream
is presentation data; persisted broker events and admitted evidence remain authoritative.

## Remote request flow

MCP is the harness-facing adapter. The target uses a simpler versioned HTTPS API over an SSM tunnel. This keeps MCP and harness lifecycle concerns off the degraded machine. Pin a target certificate in the broker and authenticate requests with a rotated per-target token; SSM is transport access control, not a substitute for application authorization.

```mermaid
sequenceDiagram
    participant H as Harness
    participant B as Python broker
    participant P as Target probe service
    participant E as Evidence store
    H->>B: Typed probe request
    B->>B: Validate session, scope and budget
    B->>E: Persist request and policy decision
    B->>P: Authenticated request with deadline and request ID
    P->>P: Validate capability, scope and hard limits
    P->>P: Execute fixed collector and bound output
    P-->>B: Observation, quality and raw output
    B->>B: Normalize and sanitize
    B->>E: Commit evidence and artifact metadata
    B-->>H: Facts, limitations and evidence IDs
    H->>B: Update hypothesis or submit report
    B->>E: Validate references and persist interpretation
```

Target authorization is enforced even if a caller bypasses MCP. The broker controls per-investigation budgets; the target independently controls per-host maxima. Harness policy hooks can improve behavior but do not grant access or override these controls.

## Lifecycle

```mermaid
stateDiagram-v2
    [*] --> created
    created --> preflight
    preflight --> running: target and runtime ready
    preflight --> failed: configuration or connection failure
    running --> completed: validated report
    running --> inconclusive: insufficient evidence or budget exhausted
    running --> cancelling: operator cancel
    cancelling --> cancelled: workers stopped
    running --> failed: unrecoverable service error
    running --> interrupted: controller lost
    interrupted --> running: explicit resume and fresh preflight
    interrupted --> cancelled: operator closes run
    completed --> [*]
    inconclusive --> [*]
    cancelled --> [*]
    failed --> [*]
```

The lifecycle controls resources, not diagnostic ordering. The model chooses which question to investigate next. No hardcoded CPU-to-memory-to-disk reasoning graph is required. A report validation failure returns structured feedback; allow at most one correction within the original budget, then export an inconclusive report.

Resume retains old evidence with timestamps and rechecks boot ID and process identity; it never silently treats old samples as current. Core release persists state and reports after crashes; seamless model-session resume is a follow-on.

## Data ownership

| Data | Owner | Agent access |
|---|---|---|
| Raw captured output | Trusted broker, restricted files | Sanitized copies only |
| Immutable parsed evidence | Trusted repository | Read through scoped API |
| Hypotheses and findings | Investigation service | Propose validated updates |
| Authoritative audit events | Broker and target | Read summaries, cannot overwrite |
| Temporary analysis scripts | Agent workspace | Read/write, disposable |
| Fault truth and reset handles | Operator/evaluator | Never mounted into agent |
| Credentials and Terraform state | Operator/trusted services | None |

## Extension path

Keep the same domain contracts when replacing the harness, moving the controller to a separate EC2 instance, or adding a remote artifact store. Add multi-user identity, mTLS lifecycle, production log handling, fleet scheduling and stronger sandbox isolation only after the single-host MVP is working. A container separates resources and credentials but shares a kernel with its Linux host; do not describe it as a formally verified hostile-code boundary.
