# Memory and cgroup OOM diagnosis

1. Separate host memory/PSI from the configured cgroup scope.
2. Treat `memory.events` as cumulative; use an incident-interval delta with a stable boot identity.
3. Require a new `oom` or `oom_kill` event, or bounded kernel/service evidence. Exit 137 alone is
   insufficient.
4. Preserve missing PSI, swap or journal access as unknown or unsupported. Never replace it with zero.
