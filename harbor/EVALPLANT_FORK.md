# EvalPlant Harbor Fork

This directory is the Harbor execution engine bundled with EvalPlant.

- Upstream repository: `https://github.com/harbor-framework/harbor`
- Fixed upstream base: `b37833221e27435a18d7acdd41d875cdc2831893`
- Integrated fork state: `ba962f4b59f4b0617b42f3e84049c0fdf6c607fd`
- Harbor release line: `0.22.0`

The integrated changes register `dsh-minimal`, pin DeepSeek Harness SDK
`0.1.0rc7`, keep credentials out of process arguments, and persist isolated
Trial lifecycle events with infrastructure-only retries. These changes are
maintained directly in this monorepo; patch files and a nested Git repository
are intentionally not used.

Generated state stays local: `harbor/.venv/`, `harbor/jobs/`, caches, task
downloads, and credentials are ignored by `harbor/.gitignore`.
