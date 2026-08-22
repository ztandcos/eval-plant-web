# EvalPlant

EvalPlant imports failed mini-SWE-agent trajectories, locates the earliest pivotal error with a two-stage DeepSeek judge, and compares the attribution with human labels. BugsInPy supplies reproducible Python bugs and tests.

## Install

```bash
uv sync
uv run evalplant --help
```

Set `DEEPSEEK_API_KEY` before running an agent or the judge. The default models are `deepseek/deepseek-v4-flash` for mini-SWE-agent and `deepseek-v4-pro` for attribution; both can be overridden with CLI flags.

## First vertical slice

Clone BugsInPy once:

```bash
git clone https://github.com/soarsmu/BugsInPy.git .benchmarks/BugsInPy
```

Build the clean Agent image once. It contains EvalPlant's executor and
mini-SWE-agent, but no benchmark repository, task, database, oracle, or unrelated
`/testbed`:

```bash
docker build --platform linux/amd64 -t evalplant-agent:0.2 .
```

Prepare exactly one task through the host orchestrator:

```bash
uv run evalplant docker-prepare \
  --bugsinpy-root .benchmarks/BugsInPy \
  --project fastapi --bug 11 \
  --workspace .workspaces/fastapi-11 \
  --oracle-dir data/oracle
```

The trusted preparation container sees the benchmark as `/bench`, the single
empty host workspace as `/task`, and oracle output as `/oracle`. BugsInPy's own
checkout script needs temporary write access to `/bench`; the later Agent
container never mounts it. Preparation checks that the buggy revision fails and
the fixed revision passes, then sanitizes the buggy task. Building its Python
environment at `/task` makes it reusable by later Agent containers without
host-path-dependent virtual environments.

Run the prepared task through the host orchestrator:

```bash
uv run evalplant docker-run .workspaces/fastapi-11 \
  --experiment fastapi-11-flash \
  --step-limit 20
```

`docker-run` bind-mounts only that task at `/task` and the selected local output
directory at `/output`. The container cannot see sibling workspaces, the
EvalPlant database, oracle data, or host source. After it exits, the host imports
the artifacts into SQLite. The API key is inherited by name and is not stored in
the image or repository.

Attribution stays on the host: `analyze`, `inspect`, `annotate`, and `report`
read the local artifacts and SQLite database. A separate Judge image is optional
and is not required by this version.

Analyze and inspect failures:

```bash
uv run evalplant analyze --experiment smoke-1
uv run evalplant inspect TRAJECTORY_ID
```

Attributions are cached. Pass `--force` to analyze the same trajectories again.

Add a human label and report the blind-test metrics:

```bash
uv run evalplant annotate TRAJECTORY_ID \
  --split test \
  --step 12 \
  --stage fault_localization \
  --mechanism wrong_assumption \
  --evidence 10,12,14 \
  --oracle-used

uv run evalplant report --experiment smoke-1 --split test
```

Existing mini-SWE-agent run directories can be imported with:

```bash
uv run evalplant import /path/to/run --experiment exp-001
```

Raw trajectories and logs stay on disk. SQLite stores their paths, hashes, normalized step previews, attributions, and annotations.
