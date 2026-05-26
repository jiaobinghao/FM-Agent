# FM-Agent — Agent Instructions

## What this repo is

FM-Agent is a framework for fully automated Hoare-style formal verification of large codebases using LLMs. It is **not** a library — it is a pipeline that operates ON other codebases, invoking `opencode` subprocesses to do the actual reasoning work.

**Entry point**: `python3 main.py <proj_dir>`

The pipeline is a **5-stage controller** that orchestrates LLM agents (via the `opencode` CLI) to understand a target codebase, extract functions, generate behavioral specs, reason about correctness, and validate bugs.

## Architecture (high-level pipeline)

1. **Stage 1** — `opencode init` → creates `AGENTS.md` in the *target* codebase (not this repo)
2. **Stage 2** — `opencode run workflow_setup_extract.md` → produces `fm_agent/phases.json` + domain context
3. **Extraction** — `src/extract.py` splits source files into one function per file under `fm_agent/extracted_functions/`
4. **Stage 3** — `src/file_utils.py` collects file list
5. **Stage 4** — `src/generate_topdown_layers.py` builds call graph + topological layers
6. **Stage 5** — `opencode run workflow_spec_step4_batch.md` (concurrent batches) → generates `[SPEC]` blocks, then `src/reasoner.py` + `src/prompts.py` do block-by-block Hoare reasoning, `src/verification.py` validates bugs

All output goes into `fm_agent/` *inside the target codebase*, not this repo.

## Project structure

```
config.py          # All LLM model settings, GRANULARITY, MAX_WORKERS, retries
main.py            # Pipeline controller (orchestrator only, no LLM logic)
src/
  extract.py       # Function extraction from source (11 languages, brace/indent parsing)
  file_utils.py     # collect_file_names(), is_file_ready() checks [SPEC]+[INFO] markers
  parser.py        # Parses [SPEC]/[INFO] sections from extracted function files
  reasoner.py      # Block-by-block Hoare-style reasoning engine
  prompts.py       # LLM prompt templates for post-condition gen + spec checking
  llm_client.py     # OpenRouter/OpenAI client wrapper with rate-limit retry
  verification.py   # Streaming watcher + ThreadPoolExecutor for reasoning+bughunting
  generate_topdown_layers.py   # Call graph builder + topological layer computation
  generate_batch_prompts.py    # Batch prompt generator for spec generation
  run_batch_gen.py             # Spec coverage verification utility
md/                 # Workflow markdown prompts that drive opencode subprocesses
  workflow_setup_extract.md     # Stage 2: codebase understanding + phases.json
  workflow_spec_step4_batch.md  # Stage 5: per-batch spec generation
  system_prompt.md              # Behavioral spec writing rules (WHAT not HOW)
  bug_validator.md              # Bug confirmation workflow (probe scripts)
  workflow_spec_step1_layers.md # Original spec for generate_topdown_layers.py
```

## Key conventions

### Function file format (extracted + specced)

Each extracted function file, once processed, has this structure:
```
// [SPEC]
// Unit: path/to/file.ext
// FunctionName(params) -> ReturnType
// Pre-condition:
//   - ...
// Post-condition:
//   - ...
// [SPEC]

// [INFO]
// callee_name(params) -> ReturnType
//   Pre-condition: ...
//   Post-condition: ...
// [SPLIT]
// ...
// [INFO]

<original function source code unchanged>
```

`is_file_ready()` considers a file ready when it has **≥2 `[SPEC]` lines AND ≥2 `[INFO]` lines**. The comment prefix varies by language.

### Config (config.py)

This repo's checked-in `config.py` uses **DeepSeek** as default, NOT the Anthropic defaults shown in README:

- `LLM_MODEL = "deepseek-v4-pro"` (fallback for all task-specific models)
- `LLM_OPENROUTER_API_BASE_URL = "https://api.deepseek.com"` (overrides README default of OpenRouter)
- `GRANULARITY = 40` — max lines per reasoning block
- `MAX_WORKERS = 10` — concurrent reasoning threads
- `MAX_SPC_ITER = 5` — max retries for spec-check LLM calls
- `OPENCODE_MAX_RETRIES = 5` — max retries for opencode subprocess steps

The pipeline passes models to opencode as `deepseek/<model_name>` (hardcoded prefix in main.py L157).

### Environment

- API key: `OPENROUTER_API_KEY` env var (also propagated as `DEEPSEEK_API_KEY`)
- Python: 3.12+, no virtualenv management in the repo itself
- Dependencies: `openai` pip package, `opencode` CLI, `bun`, `oh-my-openagent`

### oh-my-openagent requirement

The `comment-checker` hook in `oh-my-openagent` MUST be disabled (it will intercept every `[SPEC]` comment block FM-Agent writes). Add to `~/.config/opencode/oh-my-openagent.json`:
```json
{ "disabled_hooks": ["comment-checker"] }
```

## Running a single test / verification

There is no test suite for FM-Agent itself. To verify the pipeline works, run it against a target project:
```bash
export OPENROUTER_API_KEY="your-key"
python3 main.py /path/to/target/project
```

## Dangerous patterns to avoid

- **Do NOT modify `fm_agent/` files in the target project.** It is the pipeline's scratch space. `_clean_previous_run()` deletes it on each run.
- **Do NOT put source files in `fm_agent/`.** The pipeline expects only generated output there.
- **The `config.py` model settings are repo-local.** The README describes published defaults; the checked-in file has different values for deepseek.
- **`main.py` L157 hardcodes `deepseek/` prefix** when passing the model to opencode — changing the config model string won't change the prefix.
- **Function extraction relies on AST-level string parsing**, not real parsers — it has known limitations with complex template metaprogramming and nested lambdas.

## Supported languages

Rust, C, C++, Python, Java, Go, CUDA, JavaScript, TypeScript, ArkTS — mapped via `EXT_TO_LANG` dict in `src/extract.py` and `src/verification.py`.

## Workflow docs (md/)

The `md/` files contain prompts that drive opencode subprocesses. They are consumed by the pipeline, not read directly by users. When debugging Stage 2 or Stage 5 failures, inspect these files to understand what the LLM agent was asked to do. `bug_validator.md` can be customized per-project to specify how to build/run probe scripts for bug confirmation.
