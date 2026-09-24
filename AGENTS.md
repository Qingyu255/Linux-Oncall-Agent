# Implementation direction

- Read `docs/README.md` and the current phase in `docs/implementation-plan.md` before changing code.
- Build a small, evidence-driven Linux diagnosis MVP. Keep the agent sandbox separate from the target; target access is through typed, bounded probes only.
- Use Python with explicit types, small cohesive classes, dependency injection, and composition. Use protocols at I/O boundaries; pure functions for parsing. Avoid speculative frameworks and deep inheritance.
- Keep domain models independent of MCP, AWS, and harness SDKs. DeepSeek Harness is the intended runtime; native extensions need a demonstrated use case.
- Enforce authorization, scope, deadlines, output limits, and concurrency outside the model. Never execute model-supplied target shell commands. Fault injection is a separate operator-only path.
- Preserve evidence provenance, uncertainty, partial failures, and raw-artifact references. Never manufacture measurements or evaluation results.
- Add meaningful parser, policy, lifecycle, and integration tests with each capability. Run relevant checks; report what could not be tested.
- Update affected docs and decision records when behavior changes. Use Mermaid for diagrams. Keep secrets, Terraform state, and incident data out of Git.
