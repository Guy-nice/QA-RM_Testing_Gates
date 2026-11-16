# -*- coding: utf-8 -*-
"""
Main orchestrator -- polyglot, lightweight mutation testing runner.
No API key required. On a gate failure it writes a prompt.txt for the agent,
but only when remediation was explicitly requested (see --remediate below).

Polyglot in one sentence: every changed file is mapped to its OWN language by
extension, files are grouped per language, each group is mutated by that
language's engine (pitest / stryker / stryker.net / mutmut / gremlins), and the
normalised results are merged into ONE mutation score. There is no single "repo
language" that shadows the others.

Diff-scoped, not whole-file: no engine mutates surgically by line, so each
engine still runs over the whole changed file/class, but its raw output is
then filtered down to only mutants on a line the diff actually touched
(git diff -U0 hunks) before anything is scored. A mutant outside those lines
never counts toward the score, same as an arid or equivalent mutant.

Usage:
    python run.py                          # auto-detect changed files
    python run.py --file src/a.ts --file b/c.py   # explicit, repeatable, mixed languages
    python run.py --module impl            # Maven module (Java only)
    python run.py --base origin/develop    # diff against a different base branch
    python run.py --remediate              # on gate failure, write prompt.txt for the agent
    python run.py --verify --run-id run-1  # re-run engines after adding tests
    python run.py --languages ts,python    # restrict a polyglot run to some languages
    python run.py --regen-prompt --allow-new-files --run-id run-1
                                            # after a human approves creating a new
                                            # test file, rewrite prompt.txt to include it

Exit codes (stable contract for CI scripts):
    0   PASS           gate passed, or nothing in scope to mutate
    1   ERROR          missing engine, engine crash, no language detected
    2   FAIL           gate failed (score below threshold); llmRequired in
                       gate.json is true only when --remediate was passed
    3   MANUAL_REVIEW  gate still failed after round 2 -- no round 3
    5   ALREADY_VERIFIED  --verify refused: that run already used its round 2
    6   SONAR_UPLOAD_FAILED  the final-state Sonar report could not be uploaded
                       (token missing or rejected, plugin not installed, scan
                       failed, ...). The mutation verdict is in gate.json and is
                       NOT changed; gate.json.incomplete is true. --skip-sonar
                       opts out of the upload entirely.
    # 130 USER_DECLINED and 255 INTERNAL are defined at the wizard wrapper level
    # (mutation_wizard.py) and are never emitted by run.py directly.

Requirements:
    Python 3.9+ (stdlib only -- no pip installs needed)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# A Windows console defaults to cp1252, which cannot encode the box-drawing and
# replacement (\ufffd) glyphs that engines like Stryker.NET emit in their banner
# and that get captured with errors="replace". Without this, printing an engine
# ERROR message that happens to contain such a character raises
# UnicodeEncodeError and takes the whole orchestrator down BEFORE gate.json is
# written -- turning a clean, reportable engine failure into an unexplained
# crash. Switch to UTF-8 when possible; never let this be fatal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):  # pragma: no cover - old/odd streams
        pass

# run.py may live anywhere the skill is installed: a project's
# .claude/skills/, ~/.claude/plugins/<plugin>/skills/, or a marketplace
# checkout. Nothing below may derive the project root from this file's path.
_HERE = Path(__file__).parent
_SKILLS = _HERE.parent.parent  # skills/ dir -- sibling skills (e.g. jira-updater) live here


def _module_for(args, prof, source_files) -> str:
    """The build module to hand the engine. --module wins; for Java it is the Maven module the
    files live in (empty for a single-module build, where `-pl` is an error) or, for a Gradle
    build, the Gradle project path (":" or ":core"); other languages use their profile default."""
    if args.module is not None:
        return args.module
    if prof.language == "java":
        return build_module_for(PROJECT_ROOT, source_files)
    return prof.default_module or ""


def _modules_by_file(args, prof, files) -> dict:
    """{module: [files]} for one language group. --module puts every file in that module. Java
    otherwise follows the build: each file belongs to the module (Maven) or Gradle project of its
    nearest build file, so a change that spans business-logic and impl runs the engine in both,
    not just in the one with most files. Other languages have a single module."""
    files = list(files)
    if args.module is not None or prof.language != "java":
        return {_module_for(args, prof, files): files}
    by_module = {}
    for f in files:
        by_module.setdefault(build_module_for(PROJECT_ROOT, [f]), []).append(f)
    return by_module


def _module_slug(module: str) -> str:
    """A module name as a directory name for its own reports (':core:api' -> 'core-api')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", module).strip("-") or "root"


def _relative_to_root(path: str) -> str:
    """An explicit --file as a posix path relative to PROJECT_ROOT.

    The caller may name it relative to the repo root or the working directory while the
    package root is a subdirectory; an existing path under PROJECT_ROOT is rewritten, any
    other spelling is kept as given (the pipeline reports it as missing)."""
    root = Path(PROJECT_ROOT).resolve()
    p = Path(path)
    if not p.is_absolute() and (root / p).exists():
        return p.as_posix()
    cand = p if p.is_absolute() else Path.cwd() / p
    if cand.exists():
        try:
            return cand.resolve().relative_to(root).as_posix()
        except ValueError:
            pass
    return p.as_posix()


def _print_repo_context() -> None:
    """Print which repo and path this run will operate on, before any other work."""
    remote = ""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        remote = (result.stdout or "").strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        pass
    print(f"  Repo : {Path(PROJECT_ROOT).name}  ({remote or 'no remote configured'})")
    print(f"  Path : {PROJECT_ROOT}")


try:  # package-relative imports -- resolve at any install location
    from .language_detector import detect, detect_for_files, missing_engines
    from .lang_profiles import get_profile, profile_for_file, profile_for_root, build_module_for
    from .changed_files_scanner import (
        scan, group_by_language, resolve_line_ranges_for_files, resolve_project_root,
        changes_outside_root,
    )
    from .arid_filter import filter_file, is_suppressed
    from .pit_parser import (
        parse_survivors, summarize, summarize_by, partition_suppressed, mutation_key,
        _file_keys, scored_total,
    )
    from . import mutation_runners
    from .report import generate
    from . import sonar_report
    from . import sonar_upload
except ImportError:  # direct invocation: py .../run.py
    sys.path.insert(0, str(_HERE))
    from language_detector import detect, detect_for_files, missing_engines
    from lang_profiles import get_profile, profile_for_file, profile_for_root, build_module_for
    from changed_files_scanner import (
        scan, group_by_language, resolve_line_ranges_for_files, resolve_project_root,
        changes_outside_root,
    )
    from arid_filter import filter_file, is_suppressed
    from pit_parser import (
        parse_survivors, summarize, summarize_by, partition_suppressed, mutation_key,
        _file_keys, scored_total,
    )
    import mutation_runners
    from report import generate
    import sonar_report
    import sonar_upload

# Root of the repo under test -- resolved from the invocation context (env var,
# git toplevel, cwd), never from where this script happens to be installed.
PROJECT_ROOT = resolve_project_root()

GATE_METRIC = "mutation_score"  # the ONLY gate. test_strength is informational.

# The run's advisory root language: used for Maven module defaults and prompt
# fencing only. Per-file resolution decides what each engine actually mutates.
_PROFILE = None

# Optional adoption tracking. All logic (auth, retry, config-reading, the
# non-blocking guarantee) lives in the sibling jira-updater skill's
# scripts/jira_updater.py -- this file only ever shells out to it. See
# that skill's README.md.
JIRA_UPDATER = str(_SKILLS / "jira-updater" / "scripts" / "jira_updater.py")
JIRA_CONFIG = str(_HERE.parent / "jira.config.json")


# --- helpers -----------------------------------------------------------------

def _profile_of(source_file: str):
    """The language profile that owns this file, chosen by its OWN extension.

    This is the crux of polyglot support: resolution is per-file, so a Java
    file, a TypeScript file and a Python file in one changeset each map to
    their correct engine instead of every non-primary-language file being
    discarded as 'not mutatable source'."""
    return profile_for_file(source_file) or _PROFILE


def _find_test_file(source_file: str) -> str | None:
    prof = _profile_of(source_file)
    return prof.test_file(PROJECT_ROOT, source_file) if prof is not None else None


def _resolve_target(source_file: str) -> str | None:
    prof = _profile_of(source_file)
    return prof.target_id(PROJECT_ROOT, source_file) if prof is not None else None


def _update_jira(jira_id: str) -> None:
    """Best-effort adoption update on a terminal PASS/NOTHING_TO_MUTATE verdict.

    Never raises -- a Jira outage, auth failure or rate limit must never fail
    the gate. Relays the updater's human-readable result line into run.py's
    own stdout so it lands in mutation-wizard.log (the wizard captures this
    process's stdout+stderr verbatim) -- no separate log file needed."""
    try:
        result = subprocess.run(
            [sys.executable, JIRA_UPDATER, "--issue", jira_id, "--config", JIRA_CONFIG],
            capture_output=True, text=True, timeout=30,
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.startswith("[jira-updater]"):
                print(f"  {line}")
    except Exception as exc:  # belt-and-suspenders: must never propagate
        print(f"  [jira-updater] skipping update on {jira_id} -- {exc}")


def _telemetry_write(artifacts_dir: Path, telemetry: dict) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "telemetry.json").write_text(
        json.dumps(telemetry, indent=2), encoding="utf-8"
    )


def _step(telemetry: dict, name: str, started: float, **extra) -> None:
    entry = {
        "wallClockMs": round((time.time() - started) * 1000),
        "tokensIn": 0, "tokensOut": 0, "tokensTotal": 0,
        "apiCalls": 0, "toolCalls": 1,
    }
    entry.update(extra)
    telemetry["steps"][name] = entry


def _write_gate(artifacts_dir: Path, gate: dict) -> None:
    """Persist gate.json.

    ALWAYS called before any exit, including the nothing-to-do and error paths.
    The wizard reads gate.json to build its MUTATION_RESULT line; when the file
    is absent it reports verdict UNKNOWN, which is indistinguishable from a
    crash and tells the agent nothing about what happened."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")


def _agent_version() -> str:
    for parent in _HERE.parents:
        candidate = parent / ".claude-plugin" / "plugin.json"
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8")).get("version", "unknown")
            except (OSError, ValueError):
                break
    return "unknown"


def _sonar_finish(args, run_id, artifacts_dir, *, round_no, verdict, exit_code,
                  all_mutations, diff_map, arid_map, equivalent_map, productive,
                  targets, tools_run, score_before=None, run_started=None) -> int:
    """Step 7, FINAL state only: write the Sonar report, then upload it.

    `score_before` is round 1's new-code score and `run_started` the epoch second round 1
    began (telemetry's runStartMs): both ride in the report so the dashboard can show the
    run's before -> after and its total time, whichever round uploads.

    Returns the process exit code: the gate's own `exit_code`, or 6 when the
    upload failed (the mutation verdict in gate.json is unaffected). Upload is
    a mandatory part of the run; `--skip-sonar` is the explicit opt-out."""
    if args.skip_sonar:
        print("  Sonar upload skipped (--skip-sonar).")
        return exit_code
    print("\n[Step 7] Sonar report + upload...")
    status = {"attempted": True, "ok": False}
    try:
        report = sonar_report.build_report(
            run_id=run_id, threshold=args.threshold, round_no=round_no, verdict=verdict,
            tools=tools_run, agent_version=_agent_version(), project_root=Path(PROJECT_ROOT),
            all_mutations=all_mutations, scope_filter=_filter_to_diff_scope, diff_map=diff_map,
            arid_map=arid_map, equivalent_map=equivalent_map,
            target_files=[f for f, _ in targets], productive=productive,
            score_before=score_before,
            duration_sec=None if run_started is None else time.time() - run_started)
        path = sonar_report.write_report(Path(PROJECT_ROOT), artifacts_dir, report)
        print(f"  report: {path}  "
              f"(overall {report['summary']['overall']['total']} mutants, "
              f"new {report['summary']['new']['total']})")
        pr_override = None
        if args.sonar_pr:
            branch = args.sonar_pr_branch
            if not branch:
                head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=PROJECT_ROOT,
                                      capture_output=True, text=True, check=False)
                branch = head.stdout.strip()
            pr_override = (args.sonar_pr, branch, args.sonar_pr_base or "main")
        result = sonar_upload.upload(Path(PROJECT_ROOT), use_pr=not args.sonar_no_pr,
                                     project_key=args.sonar_project_key, host=args.sonar_host,
                                     pr_override=pr_override)
    except Exception as exc:  # the gate verdict must survive any export problem
        result = {"ok": False, "stage": "report", "message": f"could not build the report: {exc}"}
    status.update(result)
    gate_path = Path(artifacts_dir) / "gate.json"
    if gate_path.exists():
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["sonarUpload"] = status
        gate["incomplete"] = not result["ok"]
        _write_gate(Path(artifacts_dir), gate)
    if not result["ok"]:
        sonar_upload.report_failure(result)
        return sonar_upload.EXIT_UPLOAD_FAILED
    return exit_code


def _exit_no_work(artifacts_dir: Path, telemetry: dict, run_id: str,
                  reason: str, detail: dict = None, jira_id: str = None) -> None:
    print(f"\n  {reason}")
    _write_gate(artifacts_dir, {
        "runId": run_id,
        "verdict": "NOTHING_TO_MUTATE",
        "gateMetric": GATE_METRIC,
        "score": None,
        "threshold": None,
        "llmRequired": False,
        "reason": reason,
        **(detail or {}),
        "artifactsDir": str(artifacts_dir),
    })
    _finalise_telemetry(telemetry, artifacts_dir)
    if jira_id:
        _update_jira(jira_id)
    sys.exit(0)


def _exit_error(artifacts_dir: Path, telemetry: dict, run_id: str, message: str,
                detail: dict = None) -> None:
    print(f"\nERROR: {message}")
    _write_gate(artifacts_dir, {
        "runId": run_id,
        "verdict": "ERROR",
        "gateMetric": GATE_METRIC,
        "score": None,
        "threshold": None,
        "llmRequired": False,
        "error": message,
        # Which language/engine failed matters: in a polyglot run "the engine
        # crashed" is not actionable until you know WHICH engine.
        **(detail or {}),
        "artifactsDir": str(artifacts_dir),
    })
    _finalise_telemetry(telemetry, artifacts_dir)
    sys.exit(1)


def _wanted_languages(raw: str) -> set:
    """Parse --languages into canonical profile names. Accepts ts/js/py aliases."""
    if not raw:
        return set()
    alias = {"ts": "typescript", "js": "javascript", "py": "python",
             "cs": "csharp", "c#": "csharp", "kt": "java", "kotlin": "java",
             "golang": "go", "node": "javascript"}
    out = set()
    for token in raw.replace(";", ",").split(","):
        token = token.strip().lower()
        if token:
            out.add(alias.get(token, token))
    return out


# --- Steps 2 & 3 shared by main() and verify() --------------------------------

def _resolve_targets(target_files, include_untested, wanted_langs):
    """Map each file to (profile, target_id, test_file). Returns (targets, skipped, prof_by_file).

    `targets` is [(source_file, target_id)]; grouping into engines happens later.
    """
    targets, skipped, prof_by_file = [], [], {}
    for f in target_files:
        prof = profile_for_file(f)
        if prof is None:
            skipped.append((f, "not mutatable source for any supported language"))
            continue
        if wanted_langs and prof.language not in wanted_langs:
            skipped.append((f, f"language {prof.language} excluded by --languages"))
            continue
        target_id = prof.target_id(PROJECT_ROOT, f)
        if not target_id:
            skipped.append((f, f"no {prof.tool} target could be derived"))
            continue
        if not os.path.exists(os.path.join(PROJECT_ROOT, f)):
            skipped.append((f, "no longer on disk"))
            continue
        test_file = prof.test_file(PROJECT_ROOT, f)
        if not include_untested and not (test_file and os.path.exists(test_file)):
            skipped.append((f, f"no test file (looked for {', '.join(prof.test_patterns)})"))
            continue
        targets.append((f, target_id))
        prof_by_file[f] = prof
    return targets, skipped, prof_by_file


def _run_arid(targets, prof_by_file):
    """Step 3 over every target. Returns (arid_map, equivalent_map, productive, detail).

    Maps are keyed by BOTH the repo-relative path and the basename, because
    engines disagree about which they report: Stryker and gremlins give a full
    path, PIT gives only `PolicyUtils.java`. Keying on both means the lookup
    succeeds either way instead of silently suppressing nothing."""
    arid_map, equivalent_map, productive, detail = {}, {}, {}, {}
    for source_file, _ in targets:
        prof = prof_by_file[source_file]
        result = filter_file(os.path.join(PROJECT_ROOT, source_file), prof.language)
        equivalents = {int(k): v for k, v in result["equivalent_operators"].items()}
        for key in {source_file, Path(source_file).name}:
            arid_map[key] = set(result["arid_lines"])
            equivalent_map[key] = equivalents
        productive[source_file] = result["productive_lines"]
        detail[source_file] = {
            "language": prof.language,
            "totalLines": result["total_lines"],
            "aridLines": result["arid_lines"],
            "equivalentLines": result["equivalent_lines"],
            "aridPct": result["arid_pct"],
            "error": result["error"],
        }
        if result["error"]:
            print(f"         {Path(source_file).name}: SKIPPED -- {result['error']}")
        else:
            print(f"         [{prof.language}] {Path(source_file).name}: "
                  f"{result['arid_pct']}% arid "
                  f"({len(result['arid_lines'])}/{result['total_lines']} lines), "
                  f"{len(result['equivalent_lines'])} equivalent-mutant line(s)")
    return arid_map, equivalent_map, productive, detail


def _run_engines(targets, prof_by_file, args, reports_root, label="Step 4",
                 productive=None, test_files=None):
    """Run one engine per language group and merge the normalised records.

    A polyglot changeset runs several tools; their outputs are concatenated into
    one mutation list, so the gate, summarize and report downstream stay
    language-agnostic. Returns (mutations, tools_run, error)."""
    groups = {}
    for source_file, tid in targets:
        prof = prof_by_file[source_file]
        groups.setdefault(prof.language, {"profile": prof, "targets": []})
        groups[prof.language]["targets"].append((source_file, tid))

    mutations, tools_run = [], []
    for lang, grp in sorted(groups.items()):
        prof = grp["profile"]
        by_module = _modules_by_file(args, prof, [f for f, _ in grp["targets"]])
        # One report dir per tool when polyglot, so their outputs don't collide.
        grp_reports = reports_root if len(groups) == 1 else reports_root / lang
        for grp_module, module_files in sorted(by_module.items()):
            module_targets = [(f, t) for f, t in grp["targets"] if f in module_files]
            scope = f"module: {grp_module}" if grp_module else f"{len(module_targets)} file(s)"
            if len(by_module) > 1:
                scope += f"; {len(by_module)} modules changed, one run each"
            # One report dir per module, so two modules' mutations.xml never collide.
            mod_reports = grp_reports / _module_slug(grp_module) if len(by_module) > 1 else grp_reports
            mod_reports.mkdir(parents=True, exist_ok=True)
            print(f"\n[{label}] Running {prof.tool} for {lang} ({scope})...")
            rc, muts, err = mutation_runners.execute(
                prof, PROJECT_ROOT, grp_module, module_targets,
                reports_dir=str(mod_reports),
                timeout=args.timeout,
                quiet=not args.verbose,
                log_path=str(mod_reports / f"{prof.tool}.log"),
                productive_lines=productive or {},
                skip_install_check=getattr(args, "skip_engine_check", False),
                # Lets the engine run only the specs that could kill these mutants,
                # instead of the whole suite, when it supports that.
                test_files=test_files or {},
                narrow_tests=not getattr(args, "full_suite", False),
                exclude_tests=getattr(args, "exclude_tests", None) or [],
            )
            if rc != 0:
                where = f" in module {grp_module}" if len(by_module) > 1 else ""
                return mutations, tools_run, f"{prof.tool} ({lang}) run failed{where}: {err}"
            mutations.extend(muts)
        tools_run.append(prof.tool)
    return mutations, tools_run, None


def _filter_to_diff_scope(mutations, diff_map):
    """Keep only mutants on a line the diff actually changed.

    Every engine still mutates the whole file/class -- none of the five
    support surgical line-level mutation -- so this filters the RESULT by
    exact changed-line number instead. Excluded mutants are dropped from BOTH
    the numerator and the denominator, the same treatment already given to
    arid/equivalent suppression, so a mutant outside the diff never drags the
    score down or shows up as work to do.

    `diff_map` maps a file key to the diff's changed line numbers, or None
    meaning "whole file" (untracked/new file, or no diff context could be
    established) -- a fail-open so a file is never silently excluded from
    scoring entirely just because its ranges are unknown.
    """
    if not diff_map:
        return mutations, []
    kept, dropped = [], []
    for m in mutations:
        ranges, matched = None, False
        for key in _file_keys(m):
            if key in diff_map:
                matched = True
                r = diff_map[key]
                if r is None:
                    ranges = None
                    break
                ranges = (ranges or set()) | r
        if not matched or ranges is None:
            kept.append(m)
            continue
        (kept if m["line"] in ranges else dropped).append(m)
    return kept, dropped


def _score(mutations, arid_map, equivalent_map, include_no_coverage):
    """Apply suppression, then summarize.

    Returns (summary, survivors, per_language, suppressed).
    """
    counted, suppressed = partition_suppressed(
        mutations, arid_map, equivalent_map, is_suppressed=is_suppressed
    )
    summary = summarize(counted)
    summary["suppressed"] = len(suppressed)
    survivors = parse_survivors(counted, arid_map=None,
                                include_no_coverage=include_no_coverage)
    return summary, survivors, summarize_by(counted, "language"), suppressed


def _print_score(summary, per_language, suppressed):
    print(f"\n         Mutation score : {summary['mutation_score']}% "
          f"({summary['killed']}/{scored_total(summary)})")
    print(f"         Test strength  : {summary['test_strength']}%  (informational only)")
    print(f"         SURVIVED {summary['survived']} | "
          f"NO_COVERAGE {summary['no_coverage']} | "
          f"ERRORS {summary['errors']} | "
          + (f"NON_VIABLE {summary['non_viable']} (not scored) | " if summary.get("non_viable") else "")
          + f"suppressed (arid/equivalent) {len(suppressed)}")
    if len(per_language) > 1:
        print("\n         Per language:")
        for lang, s in per_language.items():
            print(f"           {lang:<12} {s['mutation_score']:>6}%  "
                  f"({s['killed']}/{scored_total(s)})  survived {s['survived']}, "
                  f"no-coverage {s['no_coverage']}")


# --- main --------------------------------------------------------------------

def main(args, run_id: str, start: float) -> None:
    global _PROFILE
    artifacts_dir = Path(PROJECT_ROOT) / "mutation-reports" / run_id
    telemetry = {"runStartMs": round(start * 1000), "steps": {}, "totals": {}}
    wanted_langs = _wanted_languages(args.languages)

    print(f"\n{'='*60}")
    print("  MUTATION RUNNER -- polyglot lightweight edition")
    print(f"{'='*60}\n")
    _print_repo_context()

    # --- Step 2 FIRST: scope decides which languages matter ------------------
    # Detecting the repo's languages before knowing the scope wastes a full
    # filesystem walk and, worse, invites a single "repo language" to decide
    # what gets mutated. Scope first, then detect only what the scope needs.
    t = time.time()
    print("[Step 2] Resolving target files...")
    if args.file:
        target_files = [_relative_to_root(f) for f in args.file]
        scope_label, scope_detail = "explicit", f"{len(target_files)} file(s) via --file"
        skipped_by_scanner = []
        diff_map = resolve_line_ranges_for_files(PROJECT_ROOT, target_files, base=args.base)
    else:
        try:
            scanned = scan(PROJECT_ROOT, base=args.base)
        except RuntimeError as exc:
            _exit_error(artifacts_dir, telemetry, run_id, str(exc))
            return
        target_files = scanned["files"]
        scope_label, scope_detail = scanned["scope"], scanned["detail"]
        skipped_by_scanner = scanned["skipped"]
        diff_map = scanned.get("lineRanges") or {}
    print(f"         scope: {scope_label} ({scope_detail})")
    outside = changes_outside_root(PROJECT_ROOT)
    if outside:
        print(f"         NOTE: {len(outside)} changed file(s) outside {Path(PROJECT_ROOT).name}/ "
              "belong to another package and are not part of this run "
              f"(e.g. {outside[0]}). Run from that package, or set MUTATION_PROJECT_ROOT.")

    if not target_files:
        _exit_no_work(artifacts_dir, telemetry, run_id,
                      "No changed source files in scope. Nothing to mutate.",
                      {"scope": scope_label, "scopeDetail": scope_detail,
                       "skipped": skipped_by_scanner}, jira_id=args.jira_id)
        return

    by_lang = group_by_language(target_files)
    print(f"         {len(target_files)} file(s) changed across "
          f"{len(by_lang)} language(s):")
    for lang, group in by_lang.items():
        print(f"           [{lang}] " + ", ".join(Path(f).name for f in group))

    # --- Step 1: detect languages + verify each engine, scoped to the files ---
    t1 = time.time()
    print("\n[Step 1] Detecting languages and engines for the files in scope...")
    try:
        lang_info = detect_for_files(PROJECT_ROOT, target_files)
    except RuntimeError:
        # Every file in scope belongs to no supported language. Fall back to the
        # repo-wide view purely so the message names what the repo actually is.
        try:
            lang_info = detect(PROJECT_ROOT)
        except RuntimeError as exc:
            _exit_error(artifacts_dir, telemetry, run_id, str(exc))
            return
        _exit_no_work(artifacts_dir, telemetry, run_id,
                      "No file in scope is mutatable source for a supported language.",
                      {"detectedLanguages": lang_info["languages"]}, jira_id=args.jira_id)
        return

    for entry in lang_info["languages"]:
        flag = {True: "ok", False: "MISSING", None: "?"}[entry["installed"]]
        print(f"         {entry['language']:<12} -> {entry['tool']:<15} "
              f"engine: {flag}")
    _PROFILE = get_profile(lang_info["language"])
    # One module: remembered for the prompt and the verify hint. Several (a change spanning modules):
    # "" so the verify run infers them per file again instead of being pinned to one.
    mods = _modules_by_file(args, _PROFILE, target_files)
    module = next(iter(mods)) if len(mods) == 1 else ""

    # A missing engine for ONE language in a polyglot run is still fatal: running
    # the rest would report a score that silently omits a language.
    missing = [e for e in missing_engines(lang_info)
               if not wanted_langs or e["language"] in wanted_langs]
    if missing and args.skip_engine_check:
        print("         --skip-engine-check: proceeding despite "
              + ", ".join(f"{e['tool']} ({e['language']})" for e in missing))
        missing = []
    if missing:
        _exit_error(artifacts_dir, telemetry, run_id, "; ".join(
            f"{e['tool']} for {e['language']} is not installed -- {e['installHint']}"
            for e in missing
        ), {"languages": [e["language"] for e in lang_info["languages"]],
            "tools": lang_info["tools"],
            "missingEngines": [{"language": e["language"], "tool": e["tool"],
                                "installHint": e["installHint"]} for e in missing]})
        return
    _step(telemetry, "step1", t1,
          languages=[e["language"] for e in lang_info["languages"]],
          tools=lang_info["tools"], polyglot=lang_info["polyglot"])
    # Step 1 runs inside step 2's span; add its time back so the Timing table doesn't count it twice.
    t += time.time() - t1

    # --- back to Step 2: which of those files are actually worth mutating ----
    # Mutating a class with no test file yields nothing but NO_COVERAGE noise --
    # that is a coverage gap, and Sonar reports it far more cheaply than a
    # mutation engine does.
    targets, skipped, prof_by_file = _resolve_targets(
        target_files, args.include_untested, wanted_langs
    )

    if skipped:
        print("\n         Skipped (no mutation value):")
        for f, why in skipped:
            print(f"           {Path(f).name}  --  {why}")
        if not args.include_untested:
            print("         Use --include-untested to mutate these anyway.")

    if not targets:
        _exit_no_work(artifacts_dir, telemetry, run_id,
                      "No target files with existing tests. Nothing to mutate.",
                      {"scope": scope_label, "skipped": [f for f, _ in skipped]},
                      jira_id=args.jira_id)
        return

    print(f"\n         {len(targets)} target(s) will be mutated:")
    for src, tid in targets:
        print(f"           [{prof_by_file[src].language}] {tid}")
    _step(telemetry, "step2", t,
          scope=scope_label, scopeDetail=scope_detail,
          changedFiles=target_files,
          targetClasses=[c for _, c in targets],
          languages=sorted({p.language for p in prof_by_file.values()}),
          skipped=[f for f, _ in skipped])

    # --- Step 3 -- arid filter, per file, in that file's own language --------
    t = time.time()
    print("\n[Step 3] Running arid filter...")
    arid_map, equivalent_map, productive, arid_detail = _run_arid(targets, prof_by_file)
    _step(telemetry, "step3", t, files=arid_detail)

    # Scope productive lines to diff hunks so the engine only generates mutants
    # on lines the diff actually touched. Without this the engine mutates the
    # entire file (400+ mutants) even though only ~12 lines changed, turning a
    # 5-minute run into a 90-minute one. When diff_map is absent (new/untracked
    # file with no base to diff against) we fall back to the full productive set.
    productive_full = productive  # unscoped, for the Sonar report's density
    if diff_map:
        diff_scoped = {}
        for source_file, prod_lines in productive.items():
            matched_ranges = None
            for key in (source_file, Path(source_file).name):
                if key in diff_map:
                    matched_ranges = diff_map[key]
                    break
            if matched_ranges is not None:
                diff_scoped[source_file] = [l for l in prod_lines if l in matched_ranges]
            else:
                diff_scoped[source_file] = prod_lines
        productive = diff_scoped

    # --- Step 4 -- one engine per language, merged into one score -----------
    # Resolved before the engines run, not after: an engine that can narrow its
    # own test suite needs to know which specs cover these targets.
    test_files = {}
    for source_file, _ in targets:
        tf = _find_test_file(source_file)
        if tf and os.path.exists(tf):
            test_files[source_file] = os.path.relpath(tf, PROJECT_ROOT)

    t = time.time()
    mutations, tools_run, err = _run_engines(
        targets, prof_by_file, args, artifacts_dir / "pit",
        productive=productive, test_files=test_files,
    )
    if err:
        _exit_error(artifacts_dir, telemetry, run_id, err,
                    {"languages": sorted({p.language for p in prof_by_file.values()}),
                     "tools": tools_run,
                     "targetFiles": [f for f, _ in targets]})
        return
    if not mutations:
        _exit_no_work(artifacts_dir, telemetry, run_id,
                      f"{', '.join(tools_run)} generated 0 mutants for the files in "
                      "scope. Nothing to score.",
                      {"tools": tools_run, "targets": [c for _, c in targets]},
                      jira_id=args.jira_id)
        return

    all_mutations = mutations  # whole-file scope, kept for the Sonar report
    mutations, out_of_diff_scope = _filter_to_diff_scope(mutations, diff_map)
    if out_of_diff_scope:
        print(f"         {len(out_of_diff_scope)} mutant(s) outside the diff's "
              "changed lines -- excluded from scoring.")
    if not mutations:
        _exit_no_work(artifacts_dir, telemetry, run_id,
                      "Every mutant produced fell outside the diff's changed lines. "
                      "Nothing to score.",
                      {"tools": tools_run, "targets": [c for _, c in targets],
                       "outOfDiffScope": len(out_of_diff_scope)}, jira_id=args.jira_id)
        return

    summary_before, survivors_before, per_language, suppressed = _score(
        mutations, arid_map, equivalent_map, not args.no_coverage_excluded
    )
    _print_score(summary_before, per_language, suppressed)
    if summary_before["total"] and scored_total(summary_before) == 0:
        # Nothing ran a test, so there is no score to gate on and no test that could change that.
        _exit_no_work(artifacts_dir, telemetry, run_id,
                      f"All {summary_before['total']} in-scope mutant(s) were non-viable (they did "
                      "not compile). Nothing to score.",
                      {"tools": tools_run, "nonViable": summary_before["non_viable"]},
                      jira_id=args.jira_id)
        return
    _step(telemetry, "step4", t, tools=tools_run, summary=summary_before,
          perLanguage=per_language, survivors=len(survivors_before),
          suppressed=len(suppressed), outOfDiffScope=len(out_of_diff_scope))
    _telemetry_write(artifacts_dir, telemetry)

    # Persist pre-verify state so --verify can pick it up later.
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "results.json").write_text(json.dumps({
        "run_id": run_id,
        "file": targets[0][0],
        "target_files": [f for f, _ in targets],
        "languages": sorted({p.language for p in prof_by_file.values()}),
        "summary_before": summary_before,
        "per_language_before": per_language,
        "kill_ratio_before": summary_before["mutation_score"],
        "survivors_before": survivors_before,
        "include_untested": args.include_untested,
        "exclude_tests": args.exclude_tests,
        "module": module,
        # Persisted so --verify filters round 2's mutants against the SAME
        # changed-line set as round 1, rather than recomputing a diff that
        # could drift if working-tree state changed between rounds.
        "diff_map": {f: (sorted(r) if r is not None else None) for f, r in diff_map.items()},
        # Checked by --regen-prompt: remediation must be asked for, never enabled after the fact.
        "remediate": bool(args.remediate),
    }, indent=2), encoding="utf-8")

    # --- THE GATE ------------------------------------------------------------
    # This is the decision point. Everything above is deterministic and free.
    # Only a FAIL verdict justifies spending tokens.
    # mutation_score = killed / total, which correctly penalises untested new
    # code (NO_COVERAGE) and not just weak assertions.
    score = summary_before[GATE_METRIC]
    gate_pass = score >= args.threshold

    survived_only = [s for s in survivors_before if s["category"] == "survived"]
    no_coverage = [s for s in survivors_before if s["category"] == "no-coverage"]

    gate = {
        "runId": run_id,
        "verdict": "PASS" if gate_pass else "FAIL",
        "gateMetric": GATE_METRIC,
        "threshold": args.threshold,
        "score": score,
        "mutationScore": summary_before["mutation_score"],
        "testStrength": summary_before["test_strength"],
        "total": summary_before["total"],
        "killed": summary_before["killed"],
        "survived": summary_before["survived"],
        "noCoverage": summary_before["no_coverage"],
        "errors": summary_before["errors"],
        "nonViable": summary_before.get("non_viable", 0),
        "suppressed": len(suppressed),
        "outOfDiffScope": len(out_of_diff_scope),
        "llmRequired": not gate_pass and args.remediate,
        "remediationEnabled": args.remediate,
        "languages": sorted({p.language for p in prof_by_file.values()}),
        "tools": tools_run,
        "perLanguage": per_language,
        "targetFiles": [f for f, _ in targets],
        "testFiles": test_files,
        "survivorsForLlm": survived_only,
        "noCoverageMutants": no_coverage,
        "artifactsDir": str(artifacts_dir),
        "module": module,
    }
    _write_gate(artifacts_dir, gate)

    print(f"\n{'='*60}")
    print(f"  GATE: {gate['verdict']}   "
          f"(mutation score {score}% vs threshold {args.threshold}%)")
    print(f"{'='*60}")
    print(f"  mutation score : {summary_before['mutation_score']}%")
    print(f"  test strength  : {summary_before['test_strength']}%  (not the gate)")
    print(f"  survived       : {summary_before['survived']}")
    print(f"  no coverage    : {summary_before['no_coverage']}")

    telemetry["gate"] = {"verdict": gate["verdict"], "score": score,
                         "threshold": args.threshold, "metric": GATE_METRIC}

    # --- PASS: report immediately, no LLM ------------------------------------
    if gate_pass:
        print("\n  Threshold met -- skipping test generation entirely.")
        print(f"  0 script tokens spent.\n{'='*60}\n")
        _finalise_telemetry(telemetry, artifacts_dir)
        generate(PROJECT_ROOT, targets[0][0], survivors_before, survivors_before, [],
                 summary_before, summary_before, run_id, time.time() - start,
                 threshold=args.threshold, gate_metric=GATE_METRIC,
                 target_files=[f for f, _ in targets],
                 per_language=per_language, tools=tools_run)
        code = _sonar_finish(args, run_id, artifacts_dir, round_no=1, verdict="PASS",
            exit_code=0, all_mutations=all_mutations, diff_map=diff_map, arid_map=arid_map,
            equivalent_map=equivalent_map, productive=productive_full, targets=targets,
            tools_run=tools_run, score_before=score, run_started=start)
        if args.jira_id:
            _update_jira(args.jira_id)
        sys.exit(code)

    # --- FAIL: hand off to the agent, but only if remediation was requested --
    # Remediation is opt-in (default off): invoking the pipeline at all is not
    # informed consent for AI to start editing test files on every failing
    # run. Without --remediate this is a deterministic-only report and stop.
    t = time.time()
    print(f"\n  Threshold NOT met -- {len(survived_only)} survivor(s) and "
          f"{len(no_coverage)} uncovered mutant(s) need new tests.")

    if not args.remediate:
        print("  Remediation was not requested -- this run is complete.")
        print("  No tests are written for it. Only a user can ask for a separate remediation run.")
        _step(telemetry, "step5_handoff", t, promptPath=None, skipped="remediation not enabled")
        _finalise_telemetry(telemetry, artifacts_dir)
        code = _sonar_finish(args, run_id, artifacts_dir, round_no=1, verdict="FAIL",
            exit_code=2, all_mutations=all_mutations, diff_map=diff_map, arid_map=arid_map,
            equivalent_map=equivalent_map, productive=productive_full, targets=targets,
            tools_run=tools_run, score_before=score, run_started=start)
        print(f"{'='*60}")
        print(f"  gate.json  : {artifacts_dir / 'gate.json'}")
        print(f"{'='*60}\n")
        sys.exit(code)

    prompt_path, new_file_needed = _write_prompt(run_id, targets, survivors_before, module,
                                                 allow_new_files=args.allow_new_files)
    gate["newFileNeeded"] = new_file_needed
    _write_gate(artifacts_dir, gate)
    if new_file_needed and not args.allow_new_files:
        print(f"  {len(new_file_needed)} mutant(s) need a brand-new test file that "
              "doesn't exist yet -- held back from this round (see gate.json."
              "newFileNeeded). A human must approve creation first: re-run with\n"
              f"    python run.py --regen-prompt --allow-new-files --run-id {run_id}")
    _step(telemetry, "step5_handoff", t, promptPath=str(prompt_path),
          survivorsForLlm=len(survived_only), noCoverageMutants=len(no_coverage),
          newFileNeeded=len(new_file_needed))
    _finalise_telemetry(telemetry, artifacts_dir)

    print(f"{'='*60}")
    print(f"  gate.json  : {artifacts_dir / 'gate.json'}")
    print(f"  prompt.txt : {prompt_path}")
    print("  After adding tests, run:")
    print(f"    python run.py --verify --run-id {run_id} --threshold {args.threshold}")
    print(f"{'='*60}\n")
    sys.exit(2)  # exit 2 == gate failed, LLM step required


def _finalise_telemetry(telemetry: dict, artifacts_dir: Path) -> None:
    telemetry["runEndMs"] = round(time.time() * 1000)
    telemetry["totalWallClockMs"] = telemetry["runEndMs"] - telemetry["runStartMs"]
    telemetry["totals"] = {
        "scriptTokensIn": 0, "scriptTokensOut": 0, "scriptTokensTotal": 0,
        "scriptApiCalls": 0,
        "toolCalls": sum(s.get("toolCalls", 0) for s in telemetry["steps"].values()),
        "note": (
            "These zeros cover the SCRIPTED steps only -- they are Python and make "
            "no API calls. They do NOT include the orchestrating agent's own token "
            "usage (its system prompt, this script's stdout read back into context, "
            "and its reasoning). Report that number separately from the client."
        ),
    }
    _telemetry_write(artifacts_dir, telemetry)


def _record_verify_outcome(artifacts_dir: Path, round_no: int, verdict: str, summary: dict) -> None:
    """Append this verify's outcome to results.json["verify_history"]."""
    path = artifacts_dir / "results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data.setdefault("verify_history", []).append({
        "round": round_no, "verdict": verdict,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        **{k: summary.get(k) for k in ("mutation_score", "total", "killed", "survived",
                                       "no_coverage", "errors")},
    })
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def verify(args, run_id: str, start: float) -> None:
    """Re-run the engines after tests have been added, then report."""
    global _PROFILE
    _print_repo_context()
    artifacts_dir = Path(PROJECT_ROOT) / "mutation-reports" / run_id
    # One verify per run: a second would overwrite the first round-2 outcome in place.
    # Refused before anything is touched.
    gate_path0 = artifacts_dir / "gate.json"
    if gate_path0.exists():
        try:
            prior = json.loads(gate_path0.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
        if prior.get("phase") == "verify":
            print(f"Refusing --verify for {run_id}: round 2 was already run for this "
                  f"run (verdict {prior.get('verdict')}, score {prior.get('score')}%) and "
                  "there is no round 3. Start a new run to score again.")
            sys.exit(5)
    # Merge onto round 1's telemetry instead of starting fresh, so its timings and token data survive.
    telemetry_path = artifacts_dir / "telemetry.json"
    telemetry = {"runStartMs": round(start * 1000), "steps": {}, "totals": {}}
    if telemetry_path.exists():
        try:
            telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    telemetry.setdefault("runStartMs", round(start * 1000))
    telemetry.setdefault("steps", {})
    telemetry.setdefault("totals", {})
    results_path = artifacts_dir / "results.json"
    if not results_path.exists():
        print(f"No pre-verify results at {results_path}. Run without --verify first.")
        sys.exit(1)

    pre = json.loads(results_path.read_text(encoding="utf-8"))
    survivors_before = pre.get("survivors_before", [])
    summary_before = pre.get("summary_before") or {
        "mutation_score": pre.get("kill_ratio_before", 0),
        "test_strength": 0, "killed": 0, "total": 0,
        "survived": 0, "no_coverage": 0, "errors": 0,
    }
    # Keep round 1's commit; recapturing HEAD here would include any remediation commit.
    target_files = [f for f in (pre.get("target_files") or [pre.get("file")]) if f]
    # Round 2 runs the same tests as round 1: the exclusions are inherited unless given again.
    if not args.exclude_tests:
        args.exclude_tests = pre.get("exclude_tests") or []
    wanted_langs = _wanted_languages(args.languages)
    # Same changed-line set as round 1 (persisted, not recomputed) so round 2
    # is filtered identically -- a like-for-like comparison, not a diff that
    # could have drifted from re-running git against a since-changed tree.
    diff_map = {f: (set(r) if r is not None else None)
                for f, r in (pre.get("diff_map") or {}).items()}

    # Re-resolve each file against its own profile so verify is polyglot exactly
    # like round 1. `include_untested` is inherited from the original run: a file
    # admitted in round 1 must not be dropped in round 2, or the two scores
    # would be measured over different populations and the comparison is a lie.
    try:
        _PROFILE = profile_for_root(PROJECT_ROOT)
    except RuntimeError:
        _PROFILE = None
    targets, skipped, prof_by_file = _resolve_targets(
        target_files, pre.get("include_untested", args.include_untested), wanted_langs
    )
    if not targets:
        _exit_error(artifacts_dir, telemetry, run_id,
                    "None of the round-1 target files are still resolvable. "
                    f"Skipped: {skipped}")
        return

    print("[Step 3] Re-running arid filter...")
    arid_map, equivalent_map, productive, _ = _run_arid(targets, prof_by_file)

    verify_test_files = {}
    for source_file, _ in targets:
        tf = _find_test_file(source_file)
        if tf and os.path.exists(tf):
            verify_test_files[source_file] = os.path.relpath(tf, PROJECT_ROOT)

    mutations, tools_run, err = _run_engines(
        targets, prof_by_file, args, artifacts_dir / "pit-verify",
        label="Step 4b", productive=productive, test_files=verify_test_files,
    )
    if err:
        _exit_error(artifacts_dir, telemetry, run_id, err,
                    {"phase": "verify", "round": args.round,
                     "languages": sorted({p.language for p in prof_by_file.values()}),
                     "tools": tools_run})
        return
    if not mutations:
        _exit_error(artifacts_dir, telemetry, run_id,
                    f"{', '.join(tools_run)} generated 0 mutants on the verify run. "
                    "Round 1 produced mutants, so this is an engine or build problem, "
                    "not a score of 100%.")
        return

    all_mutations = mutations  # whole-file scope, kept for the Sonar report
    mutations, out_of_diff_scope = _filter_to_diff_scope(mutations, diff_map)
    if out_of_diff_scope:
        print(f"          {len(out_of_diff_scope)} mutant(s) outside the diff's "
              "changed lines -- excluded from scoring.")
    if not mutations:
        _exit_error(artifacts_dir, telemetry, run_id,
                    "Every mutant on the verify run fell outside the diff's changed "
                    "lines. Round 1 produced in-scope mutants, so this is an engine "
                    "or build problem, not a score of 100%.")
        return

    summary_after, survivors_after, per_language, suppressed = _score(
        mutations, arid_map, equivalent_map, not args.no_coverage_excluded
    )
    _print_score(summary_after, per_language, suppressed)
    if summary_after["total"] and scored_total(summary_after) == 0:
        _exit_error(artifacts_dir, telemetry, run_id,
                    f"All {summary_after['total']} mutant(s) on the verify run were non-viable "
                    "(they did not compile). Round 1 scored, so this is an engine or build "
                    "problem, not a score.")
        return

    score_after = summary_after[GATE_METRIC]
    # Compare like with like: round 1's mutation score, never its test strength.
    score_before = summary_before.get(GATE_METRIC, summary_before.get("kill_ratio", 0))
    # Only what round 1 really measured goes to Sonar: a legacy results.json without the
    # gate metric makes `score_before` a default, not a score.
    round1_score = summary_before.get(GATE_METRIC)
    # Round 1's start, kept in its telemetry: the whole run, not just this verify.
    run_started = telemetry["runStartMs"] / 1000
    gate_pass = score_after >= args.threshold
    remaining = [s for s in survivors_after if s.get("category") == "survived"]
    no_coverage_after = [s for s in survivors_after if s.get("category") == "no-coverage"]
    final_round = args.round >= 2

    print(f"\n          Mutation score : {score_before}%  ->  {score_after}%")
    print(f"          GATE: {'PASS' if gate_pass else 'FAIL'} "
          f"(mutation score {score_after}% vs {args.threshold}%)")

    # Round 2 is the last attempt. A mutant that survives two rounds of targeted
    # tests is usually equivalent -- unkillable by construction. Flag, don't loop.
    exhausted = (not gate_pass) and final_round
    verdict = "PASS" if gate_pass else ("MANUAL_REVIEW" if exhausted else "FAIL")

    print("[Step 6] Generating report...")
    generate(PROJECT_ROOT, target_files[0], survivors_before, survivors_after, [],
             summary_before, summary_after, run_id, time.time() - start,
             threshold=args.threshold, gate_metric=GATE_METRIC,
             target_files=target_files, per_language=per_language,
             tools=tools_run, tests_added=args.tests_added)

    _write_gate(artifacts_dir, {
        "runId": run_id,
        "phase": "verify",
        "round": args.round,
        "verdict": verdict,
        "gateMetric": GATE_METRIC,
        "threshold": args.threshold,
        "score": score_after,
        "scoreBefore": score_before,
        "mutationScore": summary_after["mutation_score"],
        "testStrength": summary_after["test_strength"],
        "total": summary_after["total"],
        "killed": summary_after["killed"],
        "survived": summary_after["survived"],
        "noCoverage": summary_after["no_coverage"],
        "errors": summary_after["errors"],
        "nonViable": summary_after.get("non_viable", 0),
        "suppressed": len(suppressed),
        "outOfDiffScope": len(out_of_diff_scope),
        "languages": sorted({p.language for p in prof_by_file.values()}),
        "tools": tools_run,
        "perLanguage": per_language,
        "targetFiles": target_files,
        "testFiles": verify_test_files,
        "roundsExhausted": exhausted,
        "llmRequired": not gate_pass and not exhausted,
        "survivorsRemaining": remaining,
        "artifactsDir": str(artifacts_dir),
    })
    _step(telemetry, "step4b", start, tools=tools_run, summary=summary_after,
          perLanguage=per_language)
    _record_verify_outcome(artifacts_dir, args.round, verdict, summary_after)
    _finalise_telemetry(telemetry, artifacts_dir)

    if gate_pass:
        print(f"\n{'='*60}")
        print(f"  GATE PASS after round {args.round} -- report generated.")
        print(f"{'='*60}\n")
        code = _sonar_finish(args, run_id, artifacts_dir, round_no=args.round, verdict="PASS",
            exit_code=0, all_mutations=all_mutations, diff_map=diff_map, arid_map=arid_map,
            equivalent_map=equivalent_map, productive=productive, targets=targets,
            tools_run=tools_run, score_before=round1_score, run_started=run_started)
        if args.jira_id:
            _update_jira(args.jira_id)
        sys.exit(code)

    if exhausted:
        print(f"\n{'='*60}")
        print("  MANUAL REVIEW REQUIRED")
        print(f"{'='*60}")
        print(f"  Mutation score {score_after}% is still below {args.threshold}% "
              f"after {args.round} rounds.")
        print(f"  {len(remaining)} mutant(s) survived targeted tests -- likely equivalent.")
        for s in remaining:
            print(f"    [{s.get('language','?')}] {mutation_key(s)}:{s.get('line')} "
                  f"| {s.get('operator')}")
        print("\n  STOPPING. Do not attempt another round.")
        print(f"  gate.json: {artifacts_dir / 'gate.json'}")
        print(f"{'='*60}\n")
        sys.exit(_sonar_finish(args, run_id, artifacts_dir, round_no=args.round,
            verdict="MANUAL_REVIEW", exit_code=3, all_mutations=all_mutations, diff_map=diff_map, arid_map=arid_map,
            equivalent_map=equivalent_map, productive=productive, targets=targets,
            tools_run=tools_run, score_before=round1_score, run_started=run_started))

    # Not the final round: a later --verify will upload the final state.
    sys.exit(2)


def _write_prompt(run_id, targets, survivors, module, allow_new_files=False) -> tuple:
    """
    Both SURVIVED and NO_COVERAGE mutants lower the mutation score, so both are
    listed as work. They need different treatment and are labelled accordingly:
    a survivor means a test runs the line but asserts too weakly; no-coverage
    means no test reaches the line at all.

    File creation is a bigger, riskier action than appending to a file that
    already exists, so it needs an explicit human "yes" first. By default
    (`allow_new_files=False`) any mutant whose file has no existing test file
    is held BACK out of the prompt entirely and returned as `new_file_needed`
    instead -- never created unattended. A human approves via
    `--regen-prompt --allow-new-files`, which rewrites this same prompt to
    include them, without re-running the mutation engines.

    Returns (prompt_path, new_file_needed).
    """
    survived = [s for s in survivors if s["category"] == "survived"]
    no_cover = [s for s in survivors if s["category"] == "no-coverage"]

    test_file_by_source = {}
    for source_file, _ in targets:
        tf = _find_test_file(source_file)
        test_file_by_source[source_file] = tf

    def _needs_new_file(s):
        tf = test_file_by_source.get(mutation_key(s))
        return bool(tf) and not os.path.exists(tf)

    new_file_needed = []
    if not allow_new_files:
        held_survived = [s for s in survived if _needs_new_file(s)]
        held_no_cover = [s for s in no_cover if _needs_new_file(s)]
        new_file_needed = held_survived + held_no_cover
        survived = [s for s in survived if s not in held_survived]
        no_cover = [s for s in no_cover if s not in held_no_cover]

    sections = []
    langs_seen = set()
    included_files = {mutation_key(s) for s in survived + no_cover}
    for source_file, _ in targets:
        if source_file not in included_files:
            continue
        prof = profile_for_file(source_file)
        fence = prof.language if prof is not None else ""
        langs_seen.add(fence)
        source_code = Path(os.path.join(PROJECT_ROOT, source_file)).read_text(
            encoding="utf-8", errors="replace")
        test_file = test_file_by_source.get(source_file)
        has_test = test_file and os.path.exists(test_file)
        existing = (
            Path(test_file).read_text(encoding="utf-8", errors="replace")
            if has_test else f"(no existing test file; create {test_file})"
        )
        sections.append(
            f"SOURCE FILE ({source_file})  [{fence}]:\n```{fence}\n{source_code}\n```\n\n"
            f"EXISTING TESTS ({test_file}) -- do not repeat or edit these:\n"
            f"```{fence}\n{existing}\n```"
        )

    def _fmt(items):
        return "\n".join(
            f"  #{i+1}  [{s.get('language','?')}] {mutation_key(s)}:{s['line']} | "
            f"{s['operator']} | {s['description']}"
            for i, s in enumerate(items)
        ) or "  (none)"

    langs_label = ", ".join(sorted(l for l in langs_seen if l)) or "the source"

    create_note = (
        "- Do NOT create any new file. Every mutant listed above already has an "
        "existing test file to append to"
        if not allow_new_files else
        "- Some source files above have no existing test file yet (marked "
        "'no existing test file; create ...') -- creating them was explicitly "
        "approved for this run. Follow the repo's existing test-placement and "
        "naming convention when choosing where to put the new file"
    )

    held_back_note = ""
    if new_file_needed:
        held_back_note = (
            f"\nHELD BACK -- {len(new_file_needed)} mutant(s) need a brand-new test "
            "file that doesn't exist yet. Not included this round; creating a file "
            "requires human approval first:\n"
            f"{_fmt(new_file_needed)}\n"
        )

    prompt = f"""You are a test engineer specializing in mutation testing.

This is a POLYGLOT run covering: {langs_label}. Each mutant below names its own
language and file -- write each test in that file's own language and framework.

{chr(10).join(sections)}

SURVIVED -- a test executes the line but does not assert strongly enough ({len(survived)}):
{_fmt(survived)}

NO_COVERAGE -- no test reaches the line at all ({len(no_cover)}):
{_fmt(no_cover)}
{held_back_note}
INSTRUCTIONS:
- Both lists lower the mutation score and both need tests
- Use the SAME test framework and assertion style already present in each
  existing test file (JUnit for Java, Jasmine/Jest/Vitest for TS/JS, pytest for
  Python, xUnit for C#, testing.T for Go)
- Add new test cases only -- never modify or delete an existing test
{create_note}
- Name each test descriptively: <unit>_<scenario>_<expectedResult>
- Each test must fail if and only if its target mutant is applied
- Return ONLY the new test code

Then re-run verification:
  python run.py --verify --run-id {run_id}{f" --module {module}" if module else ""}
"""

    out_dir = Path(PROJECT_ROOT) / "mutation-reports" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return str(prompt_path), new_file_needed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polyglot lightweight mutation testing runner")
    parser.add_argument("--file", action="append",
                        help="Specific source file (relative to project root). "
                             "Repeatable, and may mix languages.")
    parser.add_argument("--module", default=None,
                        help="Build module to mutate: for Java a Maven module ('impl') or a Gradle "
                             "project ('core', ':core:api'); inferred from the changed files when "
                             "omitted. Ignored by engines that mutate by file path.")
    parser.add_argument("--languages", default=None,
                        help="Comma-separated allow-list, e.g. 'typescript,python'. "
                             "Restricts a polyglot run to some languages only.")
    parser.add_argument("--base", default=None,
                        help="Base branch/ref. Omit to scope to local uncommitted "
                             "changes, falling back to the last commit.")
    parser.add_argument("--jira-id", default=None,
                        help="Optional Jira issue key (e.g. PROJ-1234). Validated "
                             "before this run proceeds; the adoption label from "
                             "jira.config.json is applied on a PASS or "
                             "NOTHING_TO_MUTATE verdict. See "
                             ".claude/skills/jira-updater/README.md.")
    parser.add_argument("--verify", action="store_true",
                        help="Re-run the engines after tests were added, then report")
    parser.add_argument("--run-id", help="Run ID to verify (used with --verify)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Per-engine timeout in seconds (default: 1800)")
    parser.add_argument("--threshold", type=int, default=80,
                        help="Gate threshold percentage (default: 80)")
    parser.add_argument("--verbose", action="store_true",
                        help="Stream full engine output instead of writing it to a log")
    parser.add_argument("--round", type=int, default=2,
                        help="Which attempt this verify represents (default: 2)")
    parser.add_argument("--tests-added", type=int, default=0,
                        help="Self-reported count of test cases added this round")
    parser.add_argument("--include-untested", action="store_true",
                        help="Mutate files that have no test file "
                             "(produces NO_COVERAGE noise)")
    parser.add_argument("--no-coverage-excluded", action="store_true",
                        help="Report only SURVIVED mutants, excluding NO_COVERAGE")
    parser.add_argument("--full-suite", action="store_true",
                        help="Run the project's ENTIRE test suite instead of only "
                             "the specs that cover the mutated files. Slower (on a "
                             "large Angular app, dramatically so), but immune to a "
                             "spec that covers the target without importing it by "
                             "name and so is not auto-detected.")
    parser.add_argument("--exclude-tests", action="append", default=[], metavar="GLOB",
                        help="Java/Kotlin: test class globs PIT must not run, comma separated or "
                             "repeated (e.g. '*IntegrationTest,com.acme.slow.*'); also read from "
                             "MUTATION_EXCLUDED_TESTS. Tests that need Docker (Testcontainers) are "
                             "left out on their own when Docker is not reachable. Round 2 inherits "
                             "the value of round 1.")
    parser.add_argument("--skip-engine-check", action="store_true",
                        help="Do not treat a failed engine --version probe as fatal. "
                             "For CI images where the probe is unreliable, and for "
                             "the self-test, which stubs the engines out. The engine "
                             "still has to exist when Step 4 actually invokes it.")
    parser.add_argument("--remediate", action="store_true",
                        help="Allow the LLM hand-off (prompt.txt + Round 2) when the "
                             "gate fails. Off by default: invoking the pipeline is "
                             "not consent for AI to start editing test files -- "
                             "without this flag a failed gate just reports and stops. "
                             "Pass it only when the user's own words ask for tests to "
                             "be written or survivors fixed.")
    parser.add_argument("--allow-new-files", action="store_true",
                        help="Allow the hand-off prompt to include mutants that have "
                             "no existing test file (would require creating one). "
                             "Off by default -- a human must approve file creation "
                             "explicitly, normally via --regen-prompt.")
    parser.add_argument("--skip-sonar", action="store_true",
                        help="Do not write the Sonar report or upload it. Upload is a "
                             "mandatory step by default (exit 6 if it fails); this is "
                             "the explicit opt-out for repos without Sonar.")
    parser.add_argument("--sonar-project-key", default="", metavar="KEY",
                        help="Sonar project key, for a repo with no sonar-project.properties "
                             "(its Sonar setup lives in the Gradle build or a CI template). "
                             "Also read from SONAR_PROJECT_KEY.")
    parser.add_argument("--sonar-host", default="", metavar="URL",
                        help="Sonar server URL, same case. Also read from SONAR_HOST_URL.")
    parser.add_argument("--sonar-no-pr", action="store_true",
                        help="Always run a Sonar branch analysis, even on a PR branch "
                             "(servers without pull request analysis).")
    parser.add_argument("--sonar-pr", default="", metavar="NUMBER",
                        help="Pull request number to upload to, without needing the GitHub "
                             "CLI. Use with --sonar-pr-branch and --sonar-pr-base.")
    parser.add_argument("--sonar-pr-branch", default="", metavar="BRANCH",
                        help="Head branch of the pull request (default: the current branch).")
    parser.add_argument("--sonar-pr-base", default="", metavar="BRANCH",
                        help="Base branch of the pull request (default: main).")
    parser.add_argument("--regen-prompt", action="store_true",
                        help="Write prompt.txt from the already-computed round-1 "
                             "results (results.json) without re-running the mutation "
                             "engines. Used to add --allow-new-files approval after "
                             "the fact, once a human has confirmed it interactively, "
                             "and -- with --remediate -- to continue a finished run "
                             "that was started without it, once the USER asks for it "
                             "to be remediated (keeps one run id and one report).")
    return parser.parse_args()


def regen_prompt(args, run_id: str) -> None:
    """Write prompt.txt for a finished round 1 without re-running any engine.

    Used to approve new test files (--allow-new-files) or, with --remediate, to continue a
    report-only run the user now wants remediated (same run id, one report). Without
    --remediate it never enables remediation on a report-only run.
    """
    artifacts_dir = Path(PROJECT_ROOT) / "mutation-reports" / run_id
    results_path = artifacts_dir / "results.json"
    gate_path = artifacts_dir / "gate.json"
    if not results_path.exists():
        print(f"No results at {results_path}. Run without --regen-prompt first.")
        sys.exit(1)
    pre = json.loads(results_path.read_text(encoding="utf-8"))
    gate0 = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else {}
    survivors_before = pre.get("survivors_before", [])

    remediate = pre.get("remediate")
    if remediate is None:  # runs recorded before this flag existed
        remediate = gate0.get("phase") != "verify" and bool(gate0.get("remediationEnabled"))
    newly_enabled = False
    if not remediate:
        if not getattr(args, "remediate", False):
            print(f"Refusing --regen-prompt for {run_id}: this run was started WITHOUT "
                  "--remediate, so no tests may be written for it. If the USER has now "
                  "asked for this run to be remediated, re-run with "
                  f"--regen-prompt --remediate --run-id {run_id}; otherwise it is complete.")
            sys.exit(1)
        # Only valid while round 1 is the latest state and there is something tests can fix.
        if gate0.get("phase") == "verify":
            print(f"Refusing: {run_id} has already been verified (round 2 is used). "
                  "Start a new run to remediate again.")
            sys.exit(1)
        if gate0.get("verdict") != "FAIL":
            print(f"Refusing: {run_id} finished with verdict {gate0.get('verdict')}, "
                  "so there is nothing to remediate.")
            sys.exit(1)
        actionable = [s for s in survivors_before if s.get("category") in ("survived", "no-coverage")]
        if not actionable:
            print(f"Refusing: {run_id} failed with no surviving or uncovered mutants -- the "
                  "shortfall comes from engine errors (timeouts or crashes), which new tests "
                  "cannot fix. Re-run the workflow instead.")
            sys.exit(1)
        newly_enabled = True

    target_files = [f for f in (pre.get("target_files") or [pre.get("file")]) if f]
    targets = [(f, f) for f in target_files]  # target_id unused by _write_prompt
    module = pre.get("module", "")
    prompt_path, new_file_needed = _write_prompt(
        run_id, targets, survivors_before, module, allow_new_files=args.allow_new_files
    )
    if gate_path.exists():
        gate0["newFileNeeded"] = new_file_needed
        gate0["remediationEnabled"] = True
        gate0["llmRequired"] = True
        _write_gate(artifacts_dir, gate0)
    if newly_enabled:
        # Record the decision and the skipped hand-off so verify and token attribution see a remediation run.
        pre["remediate"] = True
        results_path.write_text(json.dumps(pre, indent=2, ensure_ascii=False), encoding="utf-8")
        tel_path = artifacts_dir / "telemetry.json"
        if tel_path.exists():
            tel = json.loads(tel_path.read_text(encoding="utf-8"))
            survived = [s for s in survivors_before if s.get("category") == "survived"]
            uncovered = [s for s in survivors_before if s.get("category") == "no-coverage"]
            tel.setdefault("steps", {})["step5_handoff"] = {
                "wallClockMs": 0, "toolCalls": 1, "promptPath": str(prompt_path),
                "survivorsForLlm": len(survived), "noCoverageMutants": len(uncovered),
                "newFileNeeded": len(new_file_needed), "continuedFromReportOnlyRun": True,
            }
            tel_path.write_text(json.dumps(tel, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"prompt.txt written: {prompt_path}"
          + ("  (remediation enabled for this run at the user's request)" if newly_enabled else ""))
    if new_file_needed:
        print(f"  {len(new_file_needed)} mutant(s) still held back (new-file creation "
              "not approved).")
    sys.exit(0)


if __name__ == "__main__":
    _args = _parse_args()
    _start = time.time()
    _run_id = _args.run_id or f"run-{int(_start)}"

    if _args.jira_id:
        _validation = subprocess.run(
            [sys.executable, JIRA_UPDATER, "--issue", _args.jira_id, "--validate-only"],
            capture_output=True, text=True, timeout=30,
        )
        if _validation.returncode != 0:
            print(f"\nERROR: Jira ID '{_args.jira_id}' failed validation.")
            print((_validation.stdout + _validation.stderr).strip())
            sys.exit(1)

    if _args.regen_prompt:
        regen_prompt(_args, _run_id)
    elif _args.verify:
        verify(_args, _run_id, _start)
    else:
        main(_args, _run_id, _start)
