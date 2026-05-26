from config import OPENCODE_MAX_RETRIES, OPENCODE_SETUP_MODEL, OPENCODE_SPEC_MODEL
from src.file_utils import collect_file_names, is_file_ready
from src.verification import streaming_reasoner
from src.extract import run_extraction, EXT_TO_LANG
from src.generate_topdown_layers import generate_topdown_layers
import argparse
import os
import sys
import json
import time
import shutil
import subprocess
import logging

def _deduplicate_phases(phases_dir):
    """Ensure each source file appears in at most one phase; keep the earliest."""
    phases_path = os.path.join(phases_dir, "phases.json")
    with open(phases_path, "r") as f:
        data = json.load(f)

    seen = set()
    phases_to_remove = []
    for phase in sorted(data["phases"], key=lambda p: p["phase"]):
        for module in phase["modules"]:
            original = module["source_files"]
            deduped = []
            for sf in original:
                if sf not in seen:
                    seen.add(sf)
                    deduped.append(sf)
                else:
                    logging.info(
                        "Removed duplicate file '%s' from phase %d module '%s'",
                        sf, phase["phase"], module["name"],
                    )
            module["source_files"] = deduped
        total_files = sum(len(m["source_files"]) for m in phase["modules"])
        if total_files == 0:
            logging.info("Removing phase %d: no source files remain after deduplication", phase["phase"])
            phases_to_remove.append(phase)
    for phase in phases_to_remove:
        data["phases"].remove(phase)

    # Renumber phases sequentially and update depends_on_phases references
    old_to_new = {}
    for idx, phase in enumerate(sorted(data["phases"], key=lambda p: p["phase"]), start=1):
        old_to_new[phase["phase"]] = idx
        phase["phase"] = idx
    for phase in data["phases"]:
        phase["depends_on_phases"] = [
            old_to_new[dep] for dep in phase.get("depends_on_phases", [])
            if dep in old_to_new
        ]

    with open(phases_path, "w") as f:
        json.dump(data, f, indent=2)


def _normalize_rel_path(path):
    """Normalize a relative path to the slash-separated form used in phases.json."""
    return os.path.normpath(path).replace(os.sep, "/")


def _is_under_dir(path, parent):
    """Return True if path is inside parent or equal to parent."""
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:
        return False


def _is_supported_source_file(rel_path):
    ext = rel_path.rsplit(".", 1)[-1] if "." in rel_path else ""
    return ext in EXT_TO_LANG


def _run_git(git_dir, args):
    try:
        return subprocess.check_output(
            ["git", "-C", git_dir, *args],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except subprocess.CalledProcessError as e:
        msg = e.stderr.strip() if e.stderr else str(e)
        raise RuntimeError(msg) from e


def _build_commit_scope(proj_dir, requested_commit):
    """Resolve a git revision and collect changed source files under proj_dir."""
    proj_abs = os.path.abspath(proj_dir)
    git_root = _run_git(proj_abs, ["rev-parse", "--show-toplevel"])
    git_root = os.path.abspath(git_root)
    resolved_commit = _run_git(
        git_root,
        ["rev-parse", "--verify", f"{requested_commit}^{{commit}}"],
    )
    changed = _run_git(
        git_root,
        ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", resolved_commit],
    )

    included = []
    skipped = []
    seen = set()
    for git_rel in [line.strip() for line in changed.splitlines() if line.strip()]:
        git_abs = os.path.abspath(os.path.join(git_root, git_rel))
        if not _is_under_dir(git_abs, proj_abs):
            skipped.append({"path": git_rel, "reason": "outside_project_dir"})
            continue
        proj_rel = _normalize_rel_path(os.path.relpath(git_abs, proj_abs))
        if proj_rel.startswith("fm_agent/") or proj_rel.startswith("fm_agent_commit/"):
            skipped.append({"path": proj_rel, "reason": "fm_agent_workspace"})
            continue
        if not os.path.exists(git_abs):
            skipped.append({"path": proj_rel, "reason": "file_deleted_or_missing"})
            continue
        if not os.path.isfile(git_abs):
            skipped.append({"path": proj_rel, "reason": "not_a_file"})
            continue
        if not _is_supported_source_file(proj_rel):
            skipped.append({"path": proj_rel, "reason": "unsupported_extension"})
            continue
        if proj_rel not in seen:
            included.append(proj_rel)
            seen.add(proj_rel)

    return {
        "requested_commit": requested_commit,
        "resolved_commit": resolved_commit,
        "git_root": git_root,
        "project_dir": proj_abs,
        "included_source_files": included,
        "skipped_files": skipped,
    }


def _write_commit_scope(scope, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    scope_path = os.path.join(output_dir, "commit_scope.json")
    with open(scope_path, "w") as f:
        json.dump(scope, f, indent=2)
    return scope_path


def _print_commit_scope_summary(scope, scope_path=None):
    included = scope.get("included_source_files", [])
    skipped = scope.get("skipped_files", [])
    print("[Pipeline] Commit scope:")
    print(f"  requested_commit: {scope['requested_commit']}")
    print(f"  resolved_commit: {scope['resolved_commit']}")
    print(f"  included_source_files: {len(included)}")
    for rel in included:
        print(f"    - {rel}")
    print(f"  skipped_files: {len(skipped)}")
    if scope_path:
        print(f"  scope_file: {scope_path}")


def _filter_phases_by_commit_scope(phases_dir, included_source_files):
    """Keep only commit-scoped source files in phases.json. Return kept file count."""
    phases_path = os.path.join(phases_dir, "phases.json")
    with open(phases_path, "r") as f:
        data = json.load(f)

    allowed = set(_normalize_rel_path(path) for path in included_source_files)
    phases_to_remove = []
    kept_count = 0
    for phase in data.get("phases", []):
        filtered_modules = []
        for module in phase.get("modules", []):
            kept_files = [
                sf for sf in module.get("source_files", [])
                if _normalize_rel_path(sf) in allowed
            ]
            if kept_files:
                module["source_files"] = kept_files
                filtered_modules.append(module)
                kept_count += len(kept_files)
        phase["modules"] = filtered_modules
        if not filtered_modules:
            phases_to_remove.append(phase)

    for phase in phases_to_remove:
        data["phases"].remove(phase)

    old_to_new = {}
    for idx, phase in enumerate(sorted(data.get("phases", []), key=lambda p: p["phase"]), start=1):
        old_to_new[phase["phase"]] = idx
        phase["phase"] = idx
    for phase in data.get("phases", []):
        phase["depends_on_phases"] = [
            old_to_new[dep] for dep in phase.get("depends_on_phases", [])
            if dep in old_to_new
        ]

    with open(phases_path, "w") as f:
        json.dump(data, f, indent=2)
    return kept_count

def _setup_logging(work_dir):
    """Configure logging to write to a file in work_dir."""
    log_path = os.path.join(work_dir, "fm_agent.log")
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
    )


def _opencode_env(proj_dir):
    env = {**os.environ, "PWD": os.path.abspath(proj_dir)}
    if os.environ.get("OPENROUTER_API_KEY") and not env.get("DEEPSEEK_API_KEY"):
        env["DEEPSEEK_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    return env


def _detect_network_error(log_path):
    """Scan the log file tail for network/provider errors. Return a short reason or None."""
    try:
        with open(log_path, "r", errors="replace") as f:
            # Read only the last 8KB to avoid slow reads on large logs
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read()
        if "provider_unavailable" in tail or "Network connection lost" in tail:
            return "LLM provider returned 502 / network connection lost"
        if "rate_limit" in tail.lower() or "429" in tail:
            return "LLM provider rate-limited (429)"
        if "timeout" in tail.lower():
            return "request timeout"
    except OSError:
        pass
    return None


def _get_phase_files(phases_data, phase_num, input_dir):
    """Return relative paths of extracted function files for a given phase."""
    phase = next(p for p in phases_data["phases"] if p["phase"] == phase_num)
    phase_files = []
    for module in phase["modules"]:
        for src_file in module["source_files"]:
            dir_part = os.path.dirname(src_file)
            base = os.path.basename(src_file)
            dot_idx = base.rfind(".")
            if dot_idx >= 0:
                subdir = base[:dot_idx] + "-" + base[dot_idx + 1:]
            else:
                subdir = base
            extracted_dir = os.path.join(input_dir, dir_part, subdir)
            if os.path.isdir(extracted_dir):
                for fname in sorted(os.listdir(extracted_dir)):
                    fpath = os.path.join(extracted_dir, fname)
                    if os.path.isfile(fpath):
                        phase_files.append(os.path.relpath(fpath, input_dir))
    return phase_files


def _clean_previous_run(work_dir):
    """Remove the fm_agent working directory from the previous pipeline run."""
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)


def _get_pending_batches(batches, proj_dir):
    """Return batches that still have at least one function without specs."""
    pending = []
    for batch in batches:
        for func_rel in batch.get("functions", []):
            full_path = os.path.join(proj_dir, func_rel)
            if not is_file_ready(full_path):
                pending.append(batch)
                break
    return pending


def _run_opencode_step(proj_dir, work_dir, script_dir, log_file,
                       md_name, expected_file, stage_label, model):
    """Run a single opencode session for a workflow step markdown file.

    Copies the md file to work_dir, runs opencode against it, and retries
    until expected_file exists or retries are exhausted.
    """
    md_src = os.path.join(script_dir, "md", md_name)
    md_dst = os.path.join(work_dir, md_name)
    shutil.copy2(md_src, md_dst)

    fm_reminder = ("IMPORTANT: The fm_agent/ directory is NOT part of the project source code. "
                    "It is a workspace for storing your output files only. "
                    "Do NOT modify any existing project files.")
    for attempt in range(1, OPENCODE_MAX_RETRIES + 1):
        if attempt == 1:
            prompt = f"Follow the instructions in the attached file. {fm_reminder}"
        else:
            prompt = ("Continue where you left off. The previous run was interrupted by a network error. "
                      f"Check what has already been done and only complete the remaining steps. {fm_reminder}")
        try:
            subprocess.run(
                ["opencode", "run", "--model", f"deepseek/{model}",
                 "--file", os.path.join(proj_dir, "fm_agent", md_name), "--", prompt],
                cwd=proj_dir, env=_opencode_env(proj_dir), check=True, stdout=log_file, stderr=log_file,
            )
        except subprocess.CalledProcessError as e:
            logging.warning(f"{stage_label} attempt {attempt}: opencode exited with code {e.returncode}")

        if os.path.exists(expected_file):
            break

        if attempt < OPENCODE_MAX_RETRIES:
            log_file.flush()
            _reason = _detect_network_error(os.path.join(work_dir, "fm_agent.log"))
            reason_msg = f" ({_reason})" if _reason else ""
            delay = 10
            print(
                f"[Pipeline] {stage_label} failed to produce {os.path.basename(expected_file)} "
                f"(attempt {attempt}/{OPENCODE_MAX_RETRIES}){reason_msg}. Retrying in {delay}s..."
            )
            logging.warning(
                f"{stage_label} attempt {attempt} failed: {os.path.basename(expected_file)} missing{reason_msg}. "
                f"Retrying in {delay}s."
            )
            time.sleep(delay)
        else:
            log_file.flush()
            _reason = _detect_network_error(os.path.join(work_dir, "fm_agent.log"))
            reason_msg = f" Likely cause: {_reason}." if _reason else ""
            print(
                f"[Pipeline] ERROR: {stage_label} failed after {OPENCODE_MAX_RETRIES} attempts. "
                f"{os.path.basename(expected_file)} is missing.{reason_msg} "
                f"Check {os.path.basename(proj_dir)}/fm_agent/fm_agent.log for details."
            )
            log_file.close()
            sys.exit(1)


def _has_source_code(proj_dir):
    """Check whether proj_dir contains at least one source code file."""
    source_exts = set(EXT_TO_LANG.keys())
    for root, dirs, files in os.walk(proj_dir):
        # Skip hidden dirs and common non-source dirs
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                   {'node_modules', '__pycache__', 'venv', '.venv', 'fm_agent'}]
        for fname in files:
            ext = fname.rsplit('.', 1)[-1] if '.' in fname else ''
            if ext in source_exts:
                return True
    return False


def run_pipeline(proj_dir, commit=None, commit_dry_run=False):
    if not os.path.isdir(proj_dir):
        print(f"[Pipeline] ERROR: proj_dir does not exist or is not a directory: {proj_dir}")
        sys.exit(1)

    if not _has_source_code(proj_dir):
        print(f"[Pipeline] ERROR: No source code files found in {proj_dir}. "
              f"Supported extensions: {', '.join(sorted(EXT_TO_LANG.keys()))}")
        sys.exit(1)

    commit_scope = None
    if commit:
        try:
            commit_scope = _build_commit_scope(proj_dir, commit)
        except RuntimeError as e:
            print(f"[Pipeline] ERROR: failed to resolve commit '{commit}': {e}")
            sys.exit(1)

        if commit_dry_run:
            scope_path = _write_commit_scope(
                commit_scope,
                os.path.join(proj_dir, "fm_agent_commit"),
            )
            _print_commit_scope_summary(commit_scope, scope_path)
            return

        if not commit_scope["included_source_files"]:
            scope_path = _write_commit_scope(
                commit_scope,
                os.path.join(proj_dir, "fm_agent_commit"),
            )
            _print_commit_scope_summary(commit_scope, scope_path)
            print("[Pipeline] Commit mode: no supported source files under the project directory. Nothing to run.")
            return

    work_dir = os.path.join(proj_dir, "fm_agent")
    input_dir = os.path.join(work_dir, "extracted_functions")
    output_dir = os.path.join(work_dir, "logic_verification_results")

    # Clean files from the previous run
    _clean_previous_run(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    _setup_logging(work_dir)
    log_file = open(os.path.join(work_dir, "fm_agent.log"), "a")
    if commit_scope:
        scope_path = _write_commit_scope(commit_scope, work_dir)
        logging.info("Commit scope written to %s", scope_path)
        _print_commit_scope_summary(commit_scope, scope_path)

    # Initialize opencode in the project directory (skip if AGENTS.md already exists)
    agent_md = os.path.join(proj_dir, "AGENTS.md")
    if os.path.exists(agent_md):
        print("[Pipeline] Stage 1/5: AGENTS.md found, skipping opencode init.")
    else:
        print("[Pipeline] Stage 1/5: Initializing opencode...")
        subprocess.run(["opencode", "run", "--command", "init"], cwd=proj_dir, env=_opencode_env(proj_dir), check=True, stdout=log_file, stderr=log_file)

    # Copy workflow_setup_extract.md to proj_dir and run opencode against it
    print("[Pipeline] Stage 2/5: Understanding codebase and extracting functions ...")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    workflow_src = os.path.join(script_dir, "md", "workflow_setup_extract.md")
    workflow_dst = os.path.join(work_dir, "workflow_setup_extract.md")
    shutil.copy2(workflow_src, workflow_dst)
    _proj_dir_abs = os.path.abspath(proj_dir)
    _proj_dir_name = os.path.basename(_proj_dir_abs)
    with open(workflow_dst, "r") as _f:
        _md = _f.read()
    _old = ("- `phases[*].modules[*].source_files` — relative paths from repo root of all source files "
            "that belong to this module.")
    _new = (f"- `phases[*].modules[*].source_files` — relative paths from the project root "
            f"`{_proj_dir_abs}` of all source files that belong to this module. "
            f"For example, a file at `{_proj_dir_abs}/path/to/file.ext` must be recorded as "
            f"`path/to/file.ext`, NOT as `{_proj_dir_name}/path/to/file.ext`.")
    _md = _md.replace(_old, _new, 1)
    with open(workflow_dst, "w") as _f:
        _f.write(_md)
    fm_reminder = ("IMPORTANT: The fm_agent/ directory is NOT part of the project source code. "
                    "It is a workspace for storing your output files only. "
                    "Do NOT include fm_agent/ paths in phases.json. "
                    "Do NOT modify any existing project files.")
    for attempt in range(1, OPENCODE_MAX_RETRIES + 1):
        if attempt == 1:
            prompt = f"Follow the instructions in the attached file. {fm_reminder}"
        else:
            prompt = ("Continue where you left off. The previous run was interrupted by a network error. "
                      f"Check what has already been done and only complete the remaining steps. {fm_reminder}")
        try:
            subprocess.run(["opencode", "run", "--model", f"deepseek/{OPENCODE_SETUP_MODEL}", "--file", os.path.join(proj_dir, "fm_agent", "workflow_setup_extract.md"), "--", prompt], cwd=proj_dir, env=_opencode_env(proj_dir), check=True, stdout=log_file, stderr=log_file)
        except subprocess.CalledProcessError as e:
            logging.warning(f"Stage 2 attempt {attempt}: opencode exited with code {e.returncode}")

        # Validate that the agent produced phases.json
        phases_json = os.path.join(work_dir, "phases.json")
        if os.path.exists(phases_json):
            break

        if attempt < OPENCODE_MAX_RETRIES:
            # Check log for network errors to provide better diagnostics
            log_file.flush()
            _reason = _detect_network_error(os.path.join(work_dir, "fm_agent.log"))
            reason_msg = f" ({_reason})" if _reason else ""
            delay = 10
            print(
                f"[Pipeline] Stage 2 failed to produce phases.json (attempt {attempt}/{OPENCODE_MAX_RETRIES}){reason_msg}. "
                f"Retrying in {delay}s..."
            )
            logging.warning(f"Stage 2 attempt {attempt} failed: phases.json missing{reason_msg}. Retrying in {delay}s.")
            time.sleep(delay)
        else:
            log_file.flush()
            _reason = _detect_network_error(os.path.join(work_dir, "fm_agent.log"))
            reason_msg = f" Likely cause: {_reason}." if _reason else ""
            print(
                f"[Pipeline] ERROR: Stage 2 failed after {OPENCODE_MAX_RETRIES} attempts. "
                f"phases.json is missing.{reason_msg} "
                f"Check {os.path.basename(proj_dir)}/fm_agent/fm_agent.log for details."
            )
            log_file.close()
            sys.exit(1)

    # Deduplicate source files across phases
    _deduplicate_phases(work_dir)
    if commit_scope:
        kept_count = _filter_phases_by_commit_scope(
            work_dir,
            commit_scope["included_source_files"],
        )
        print(f"[Pipeline] Commit mode: retained {kept_count} source file(s) in phases.json.")
        logging.info("Commit mode retained %d source file(s) in phases.json", kept_count)
        if kept_count == 0:
            print("[Pipeline] Commit mode: phases.json does not contain changed source files. Nothing to run.")
            log_file.close()
            return

    # Run function extraction using extract.py
    print("[Pipeline] Extracting functions from source files...")
    run_extraction(proj_dir, work_dir=work_dir, force=True, verbose=True)

    # Copy system_prompt.md to spec_prompts/system_prompt.md
    spec_prompts_dir = os.path.join(work_dir, "spec_prompts")
    os.makedirs(spec_prompts_dir, exist_ok=True)
    shutil.copy2(
        os.path.join(script_dir, "md", "system_prompt.md"),
        os.path.join(spec_prompts_dir, "system_prompt.md"),
    )
    shutil.copy2(
        os.path.join(script_dir, "src", "generate_batch_prompts.py"),
        os.path.join(spec_prompts_dir, "generate_batch_prompts.py"),
    )
    shutil.copy2(
        os.path.join(script_dir, "src", "run_batch_gen.py"),
        os.path.join(spec_prompts_dir, "run_batch_gen.py"),
    )

    print("[Pipeline] Stage 3/5: Collecting file list...")
    file_list = collect_file_names(input_dir, os.path.join(work_dir, "fm_agent_file_list.json"))

    if not file_list:
        print("[Pipeline] No functions found to verify. Skipping spec generation.")
        log_file.close()
        return

    # --- Stage 4: Generate topdown layers ---
    print("[Pipeline] Stage 4/5: Generating topdown layers...")
    phases_data = json.load(open(os.path.join(work_dir, "phases.json")))
    generate_topdown_layers(work_dir)

    # --- Stage 5: Execute spec generation workflow (per phase, per layer) ---
    print("[Pipeline] Stage 5/5: Generating specs & verification...")
    batch_md_src = os.path.join(script_dir, "md", "workflow_spec_step4_batch.md")
    batch_md_dst = os.path.join(work_dir, "workflow_spec_step4_batch.md")
    shutil.copy2(batch_md_src, batch_md_dst)

    all_processed = set()
    num_phases = len(phases_data["phases"])
    project_name = phases_data.get("project", "project")

    for phase_info in sorted(phases_data["phases"], key=lambda p: p["phase"]):
        phase_num = phase_info["phase"]
        phase_name = phase_info["name"]
        phase_files = _get_phase_files(phases_data, phase_num, input_dir)

        if not phase_files:
            logging.info(f"Phase {phase_num} ({phase_name}): no extracted files, skipping.")
            continue

        # Determine how many layers this phase has
        layers_json_path = os.path.join(
            spec_prompts_dir, f"phase_{phase_num:02d}_topdown_layers.json"
        )
        if not os.path.exists(layers_json_path):
            generate_topdown_layers(work_dir, [phase_num])
        with open(layers_json_path, "r") as f:
            layers_data = json.load(f)
        total_layers = layers_data.get("total_layers", 1)

        batch_dir = os.path.join(
            spec_prompts_dir,
            f"batch_prompts_{project_name}_phase{phase_num:02d}",
        )

        for layer_idx in range(total_layers):
            print(f"[Pipeline] Stage 5/5: Phase {phase_num}/{num_phases} — {phase_name}, Layer {layer_idx}/{total_layers - 1}")

            # Generate batch prompts for this layer
            subprocess.run(
                ["python3", "fm_agent/spec_prompts/generate_batch_prompts.py",
                 "--phase", str(phase_num), "--layers", str(layer_idx)],
                cwd=proj_dir, check=True, stdout=log_file, stderr=log_file,
            )

            # Read manifest
            manifest_path = os.path.join(batch_dir, "manifest.json")
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            all_batches = manifest.get("batches", [])

            if not all_batches:
                logging.info(f"Phase {phase_num} Layer {layer_idx}: no batches, skipping.")
                continue

            batch_rel_dir = os.path.relpath(batch_dir, proj_dir)

            # Build file list for this layer from the manifest
            layer_files = []
            for batch_info in all_batches:
                for func_rel in batch_info.get("functions", []):
                    rel = os.path.relpath(os.path.join(proj_dir, func_rel), input_dir)
                    layer_files.append(rel)

            layer_processed = set()

            for attempt in range(1, OPENCODE_MAX_RETRIES + 1):
                # Find batches with unspecced functions
                pending_batches = _get_pending_batches(all_batches, proj_dir)
                if not pending_batches:
                    break

                # Spawn concurrent opencode processes (one per pending batch)
                spec_procs = []
                for batch_info in pending_batches:
                    batch_file = batch_info["file"]
                    batch_prompt_rel = os.path.join(batch_rel_dir, batch_file)
                    fm_reminder = ("IMPORTANT: fm_agent/ is your output workspace, not project source. "
                                    "Do NOT modify any existing project files.")
                    if attempt == 1:
                        prompt = (
                            f"Process the batch prompt file at {batch_prompt_rel}. "
                            f"Read it and fm_agent/spec_prompts/system_prompt.md, "
                            f"generate behavioral specs for each function listed, "
                            f"and write the complete specced files directly. {fm_reminder}"
                        )
                    else:
                        prompt = (
                            f"Continue processing the batch prompt file at {batch_prompt_rel}. "
                            f"Some functions may already have specs from a previous attempt. "
                            f"Check each function file — only generate specs for those "
                            f"that don't have [SPEC] blocks yet. "
                            f"Read fm_agent/spec_prompts/system_prompt.md for the format rules. {fm_reminder}"
                        )
                    proc = subprocess.Popen(
                        ["opencode", "run", "--model", f"deepseek/{OPENCODE_SPEC_MODEL}",
                         "--file", os.path.join(proj_dir, "fm_agent", "workflow_spec_step4_batch.md"),
                         "--", prompt],
                        cwd=proj_dir, env=_opencode_env(proj_dir), stdout=log_file, stderr=log_file,
                    )
                    spec_procs.append(proc)

                logging.info(
                    f"Phase {phase_num} Layer {layer_idx} attempt {attempt}: "
                    f"spawned {len(spec_procs)} opencode processes for {len(pending_batches)} batches"
                )

                newly_processed = streaming_reasoner(input_dir, output_dir, file_list=layer_files,
                                   proj_dir=proj_dir, work_dir=work_dir,
                                   spec_procs=spec_procs,
                                   already_processed=all_processed | layer_processed)
                layer_processed.update(newly_processed)

                for proc in spec_procs:
                    proc.wait()

                # Check if any files in this layer received specs
                specs_generated = sum(
                    1 for rel in layer_files
                    if is_file_ready(os.path.join(input_dir, rel))
                )
                if specs_generated > 0 and not _get_pending_batches(all_batches, proj_dir):
                    break

                if specs_generated > 0:
                    # Partial progress — retry remaining batches without delay
                    logging.info(
                        f"Phase {phase_num} Layer {layer_idx} attempt {attempt}: "
                        f"{specs_generated} specs generated, retrying remaining batches"
                    )
                    continue

                if attempt < OPENCODE_MAX_RETRIES:
                    log_file.flush()
                    _reason = _detect_network_error(os.path.join(work_dir, "fm_agent.log"))
                    reason_msg = f" ({_reason})" if _reason else ""
                    delay = 10
                    print(
                        f"[Pipeline] Stage 5 Phase {phase_num} Layer {layer_idx} produced no specs "
                        f"(attempt {attempt}/{OPENCODE_MAX_RETRIES}){reason_msg}. "
                        f"Retrying in {delay}s..."
                    )
                    logging.warning(
                        f"Stage 5 Phase {phase_num} Layer {layer_idx} attempt {attempt} failed: "
                        f"no specs generated{reason_msg}. Retrying in {delay}s."
                    )
                    time.sleep(delay)
                else:
                    log_file.flush()
                    _reason = _detect_network_error(os.path.join(work_dir, "fm_agent.log"))
                    reason_msg = f" Likely cause: {_reason}." if _reason else ""
                    print(
                        f"[Pipeline] ERROR: Stage 5 Phase {phase_num} Layer {layer_idx} failed "
                        f"after {OPENCODE_MAX_RETRIES} attempts. "
                        f"No specs were generated.{reason_msg} "
                        f"Check {os.path.basename(proj_dir)}/fm_agent/fm_agent.log for details."
                    )
                    log_file.close()
                    sys.exit(1)

        # Mark all files from this phase as processed for subsequent phases
        for rel in phase_files:
            all_processed.add(os.path.join(input_dir, rel))

    log_file.close()

    # Print confirmed bug count
    summary_path = os.path.join(work_dir, "bug_validation", "summary.json")
    if os.path.exists(summary_path):
        with open(summary_path, "r") as f:
            summary = json.load(f)
        confirmed = summary.get("total_confirmed", 0)
        print(f"[Pipeline] Confirmed bugs: {confirmed}")

    print("[Pipeline] Done.")


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run FM-Agent on a project directory.",
    )
    parser.add_argument("proj_dir", help="Target project directory to analyze.")
    parser.add_argument(
        "--commit",
        help="Analyze only source files changed by this single git commit-ish.",
    )
    parser.add_argument(
        "--commit-dry-run",
        action="store_true",
        help="Resolve --commit and write fm_agent_commit/commit_scope.json without running OpenCode.",
    )
    args = parser.parse_args(argv)
    if args.commit_dry_run and not args.commit:
        parser.error("--commit-dry-run requires --commit")
    return args


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])

    start_time = time.time()
    run_pipeline(
        os.path.abspath(args.proj_dir),
        commit=args.commit,
        commit_dry_run=args.commit_dry_run,
    )
    end_time = time.time()
    logging.info(f"Total time: {end_time - start_time:.2f} seconds")
