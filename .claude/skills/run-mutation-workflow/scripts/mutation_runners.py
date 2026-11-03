# -*- coding: utf-8 -*-
"""
Step 4 (generic): run the right mutation engine for each language and normalise
its output into ONE record schema, so every downstream step (summarize,
parse_survivors, the gate, report.py) is language-agnostic.

Supported engines:

    pitest         Java/Kotlin   -> mutations.xml                (via pit_parser)
    stryker        TS/JS         -> mutation.json                (Stryker schema)
    stryker-dotnet C#            -> mutation-report.json         (Stryker schema)
    mutmut         Python        -> junitxml (v2) / results (v3)
    gremlins       Go            -> gremlins JSON

Normalised record shape:
    status, detected, line, operator, description, class, method,
    source_file (basename), file (repo-relative when the engine reports it),
    language, tool, index

`file` exists because `source_file` alone is a basename, and a repo with
`a/index.ts` and `b/index.ts` collapses two different files onto one key --
which silently cross-applies one file's arid lines to the other. Downstream
lookups prefer `file` and fall back to `source_file`.

Statuses are normalised to PIT's taxonomy so the existing constants in
pit_parser (KILLED_STATUSES / ERROR_STATUSES / "SURVIVED" / "NO_COVERAGE")
apply unchanged:
    KILLED, SURVIVED, NO_COVERAGE, TIMED_OUT, RUN_ERROR, NON_VIABLE

Engine binaries are resolved through `profile.engine_command()` -- the SAME
function Step 1 uses for its installed check -- so "installed: True" can never
disagree with what actually runs, and a local project install always wins over
`npx`, which would otherwise reach for the network mid-run.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_HERE = Path(__file__).parent

try:  # package-relative import -- resolves at any install location
    from . import pit_parser  # (Java engine, reused directly)
    from . import lang_profiles
    from . import java_tests
except ImportError:  # direct invocation: py .../mutation_runners.py
    sys.path.insert(0, str(_HERE))
    import pit_parser  # noqa: E402  (Java engine, reused directly)
    import lang_profiles  # noqa: E402
    import java_tests  # noqa: E402

# How many discontiguous line ranges are worth passing to an engine before it is
# cheaper to just hand over the whole file. A heavily interleaved file produces
# dozens of tiny ranges and an unwieldy command line for no real saving.
MAX_MUTATION_RANGES = 40


# --- public entry point ------------------------------------------------------

def execute(profile, project_root, module, targets, reports_dir,
            timeout=1800, quiet=True, log_path=None, productive_lines=None,
            skip_install_check=False, test_files=None, narrow_tests=True, exclude_tests=()):
    """
    Run the mutation engine for `profile` and return (rc, mutations, err).

    rc == 0 and mutations is a list of normalised records on success.
    rc != 0 and err is a human-readable message on failure.

    `targets` is a list of (source_file, target_id) tuples produced by the
    orchestrator via profile.target_id(...).
    `productive_lines` optionally maps source_file -> sorted list of line
    numbers worth mutating; engines that accept a mutation range use it to skip
    arid lines at generation time instead of the pipeline discarding those
    mutants afterwards.
    `test_files` maps source_file -> its own test file. Used to narrow the test
    suite the engine executes (see `_related_specs`); `narrow_tests=False`
    forces the whole suite.
    `exclude_tests` are test class globs PIT must not run (Java/Kotlin only; see java_tests).
    """
    dispatch = {
        "pitest":         _run_pitest,
        "stryker":        _run_stryker,
        "stryker-dotnet": _run_stryker_dotnet,
        "mutmut":         _run_mutmut,
        "gremlins":       _run_gremlins,
    }.get(profile.tool)

    if dispatch is None:
        return 1, None, f"No mutation runner wired for tool '{profile.tool}'."
    if profile.tool == "pitest":
        dispatch = functools.partial(dispatch, exclude_tests=exclude_tests)

    if not skip_install_check and not profile.is_installed(project_root):
        return 127, None, (
            f"{profile.tool} for {profile.language} is not installed -- "
            f"{profile.install_hint}. This is an environment problem, not a "
            f"test-quality one; the pipeline does not install engines."
        )

    rc, mutations, err = dispatch(profile, project_root, module, targets,
                                  reports_dir, timeout, quiet, log_path,
                                  productive_lines or {},
                                  test_files or {}, narrow_tests)
    if rc == 0 and mutations is not None:
        for m in mutations:
            m.setdefault("language", profile.language)
            m.setdefault("tool", profile.tool)
    return rc, mutations, err


# --- helpers -----------------------------------------------------------------

def _record(status, line, operator, description, source_file,
            cls="", method="", index="", file_path="", tests_run=None, killing_test=""):
    return {
        "status": status,
        "detected": status == "KILLED",
        "line": int(line or 0),
        "operator": operator or "",
        "description": description or "",
        "class": cls,
        "method": method,
        "source_file": source_file or "",
        "file": file_path or "",
        "index": index,
        # Per-test data, best effort: None / "" when the engine does not report it.
        "tests_run": tests_run,
        "killing_test": killing_test or "",
    }


def _run(cmd, cwd, timeout, quiet, log_path, tolerate_nonzero=False, env=None):
    """Run a subprocess, stream output in real-time, tee to log_path. Returns
    (returncode, combined_output). Never raises."""
    import threading
    print(f"[MUT] {' '.join(str(c) for c in cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
    except (OSError, FileNotFoundError) as exc:
        return 127, f"could not execute {cmd[0]}: {exc}"

    lines = []

    def _drain():
        for line in proc.stdout:
            lines.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        t.join(timeout=5)
        return 124, f"mutation run exceeded {timeout}s and was killed."
    t.join()

    output = "".join(lines)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(output, encoding="utf-8")

    if proc.returncode != 0 and not tolerate_nonzero:
        print(f"[MUT] run failed (exit {proc.returncode}) -- last 40 lines:")
        for line in output.splitlines()[-40:]:
            print(f"  {line}")
    elif log_path:
        print(f"  (full output: {log_path})")

    return proc.returncode, output


def _tail(output, n=25):
    """The last few meaningful lines of engine output, for an error message.

    A bare 'produced no report' tells the caller nothing about WHY; the engine
    almost always said so itself a few lines earlier."""
    if not isinstance(output, str):
        return ""
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    return "\n    ".join(lines[-n:])


def _newest(roots, filename, min_mtime=None):
    """Newest `filename` under any of `roots` (each searched recursively).

    Roots are passed explicitly and kept narrow. The previous revision searched
    the entire project root, which on a Node repo means walking node_modules --
    minutes of I/O, and a real chance of matching some dependency's own report
    fixture instead of ours."""
    cands = []
    for root in roots:
        root = Path(root)
        if root.is_file() and root.name == filename:
            cands.append(root)
        elif root.is_dir():
            cands.extend(p for p in root.rglob(filename) if "node_modules" not in p.parts)
    if min_mtime is not None:
        cands = [c for c in cands if c.stat().st_mtime >= min_mtime - 2]
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _ranges(lines):
    """Coalesce a sorted line list into (start, end) inclusive ranges."""
    out = []
    for n in sorted(set(lines)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return [(a, b) for a, b in out]


def _stryker_mutate_spec(target_id, productive):
    """`file` or `file:start-end,file:start-end` for Stryker's mutation range.

    Passing productive ranges means arid lines are never mutated in the first
    place, rather than being generated, executed, and then discarded by the
    pipeline. That is both faster and honest: a mutant the pipeline intends to
    ignore should not have been counted in the denominator."""
    if not productive:
        return [target_id]
    rngs = _ranges(productive)
    if len(rngs) > MAX_MUTATION_RANGES:
        return [target_id]
    return [f"{target_id}:{a}-{b}" for a, b in rngs]


# --- pitest (Java) -----------------------------------------------------------

def _print_test_selection(sel):
    """Say which tests PIT runs and which it leaves out, so a score never hides that."""
    if sel["mode"] == "full":
        print("         tests: the whole suite (--full-suite)")
    elif sel["mode"] == "referencing":
        print(f"         tests: {len(sel['selected'])} test class(es) covering the changed class(es): "
              + ", ".join(n.rsplit(".", 1)[-1] for n in sel["selected"]))
    elif sel.get("all_covering_left_out"):
        print("         tests: every test covering the changed class(es) is left out; "
              "using the other tests of their package")
    else:
        print("         tests: no test names the changed class(es); using the tests of their package")
    if sel["container"]:
        print("         left out, Docker is not reachable: "
              + ", ".join(n.rsplit(".", 1)[-1] for n in sel["container"])
              + " (code only these cover is reported NO_COVERAGE)")
    if sel["user_excluded"]:
        print("         left out, --exclude-tests: "
              + ", ".join(n.rsplit(".", 1)[-1] for n in sel["user_excluded"]))


def _run_pitest(profile, project_root, module, targets, reports_dir,
                timeout, quiet, log_path, productive_lines,
                test_files=None, narrow_tests=True, exclude_tests=()):
    run_start = time.time()
    exclude = java_tests.split_globs(exclude_tests, java_tests.excluded_from_env())
    if lang_profiles.java_build_tool(project_root) == "gradle":
        gradle = lang_profiles.gradle_executable(project_root)
        if gradle is None:
            return 127, None, "Gradle build but no Gradle wrapper (gradlew) and no gradle on PATH."
        project_path = lang_profiles.gradle_project_path(module)
        module = project_path.strip(":").replace(":", "/")  # build/reports/pitest lives in its dir
        target_classes = [t for _, t in targets]
        selection = java_tests.select_tests(
            project_root, module, target_classes,
            own_tests=[test_files[f] for f, _ in targets if f in (test_files or {})],
            exclude=exclude, full_suite=not narrow_tests)
        _print_test_selection(selection)
        rc = pit_parser.run_pit_gradle(
            project_root, gradle, project_path=project_path,
            target_classes=target_classes, reports_dir=str(reports_dir),
            junit5=lang_profiles.uses_junit_platform(project_root),
            timeout=timeout, quiet=quiet, log_path=log_path,
            test_globs=selection["tests"], excluded_tests=selection["excluded"],
        )
    else:
        rc = pit_parser.run_pit(
            project_root, module=module or None,
            target_classes=[t for _, t in targets],
            reports_dir=str(reports_dir), timeout=timeout,
            quiet=quiet, log_path=log_path, excluded_tests=exclude,
        )
    if rc != 0:
        return rc, None, f"PIT run failed (exit {rc}). See {log_path}."
    try:
        xml_path = pit_parser.find_mutations_xml(
            project_root, module, str(reports_dir), min_mtime=run_start
        )
    except (FileNotFoundError, RuntimeError) as exc:
        return 1, None, str(exc)
    return 0, pit_parser.parse_mutations(xml_path), None


# --- Stryker (TS/JS) and Stryker.NET (C#): shared report schema --------------

_STRYKER_STATUS = {
    "Killed": "KILLED",
    "Survived": "SURVIVED",
    "NoCoverage": "NO_COVERAGE",
    "Timeout": "TIMED_OUT",
    "RuntimeError": "RUN_ERROR",
    "CompileError": "NON_VIABLE",
}


def _parse_stryker_json(path, project_root):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(project_root).resolve()
    mutations = []
    for file_path, entry in (data.get("files") or {}).items():
        rel = file_path
        # Stryker writes keys relative to the project it ran in, so a relative key must be
        # resolved against project_root, not the working directory of this process.
        candidate = Path(file_path) if Path(file_path).is_absolute() else root / file_path
        try:
            rel = candidate.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            rel = Path(file_path).as_posix()
        base = os.path.basename(file_path)
        for m in entry.get("mutants", []):
            raw = m.get("status", "")
            if raw == "Ignored":
                continue
            status = _STRYKER_STATUS.get(raw, "RUN_ERROR")
            line = ((m.get("location") or {}).get("start") or {}).get("line", 0)
            killed_by = m.get("killedBy") or []
            tests_done = m.get("testsCompleted")
            mutations.append(_record(
                status, line,
                m.get("mutatorName", ""),
                m.get("description") or m.get("replacement") or m.get("mutatorName", ""),
                base, index=str(m.get("id", "")), file_path=rel,
                tests_run=tests_done if isinstance(tests_done, int) else None,
                killing_test=str(killed_by[0]) if killed_by else "",
            ))
    return mutations


def _stryker_json_targets(project_root, reports_dir):
    """Where a Stryker JSON report might land, narrowest first.

    The configured `jsonReporter.fileName` is read from the project's own
    Stryker config so the pipeline looks where THIS repo actually writes,
    instead of assuming the default path."""
    root = Path(project_root)
    targets = [Path(reports_dir)]
    for name in ("stryker.conf.json", "stryker.config.json", ".stryker.conf.json"):
        conf = root / name
        if not conf.exists():
            continue
        try:
            data = json.loads(conf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        configured = ((data.get("jsonReporter") or {}).get("fileName"))
        if configured:
            targets.append(root / configured)
    targets += [root / "reports" / "mutation", root / "reports"]
    return targets


# --- narrowing the test suite Stryker executes -------------------------------
#
# Before testing a single mutant, Stryker runs the suite once unmutated (the
# "dry run") to learn which test covers which line. With `projectType:
# angular-cli` that means `ng test`, which by default compiles and runs EVERY
# `**/*.spec.ts` in the workspace. On a large Angular app that dry run alone can
# take 30+ minutes and time out before one mutant is tested -- and then every
# per-mutant run pays the same cost again.
#
# Nothing about mutation testing needs the whole suite. Only the specs that
# actually exercise the mutated file can kill its mutants. So the pipeline
# derives those specs per run -- from the files it already resolved in Step 2 --
# and passes them as `ng test --include=<glob>`.
#
# TRADE-OFF, stated plainly: if a spec covers the target without importing it by
# name (e.g. only through a barrel re-export), it will not be selected, and a
# mutant that spec would have killed is reported SURVIVED or NO_COVERAGE
# instead. That over-reports work; it never over-reports quality. `--full-suite`
# turns narrowing off.

_SPEC_SUFFIXES = (".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx",
                  ".spec.js", ".test.js")


def _all_spec_files(project_root):
    """Every spec file in the project, repo-relative."""
    root = Path(project_root)
    out = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_WALK_DIRS]
        for name in filenames:
            if name.endswith(_SPEC_SUFFIXES):
                out.append(Path(dirpath, name).relative_to(root).as_posix())
    return out


_SKIP_WALK_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "out", "coverage", "target",
    ".angular", ".stryker-tmp", "mutation-reports", "reports", ".venv",
    "test-results", ".claude", ".github", "e2e",
})


def _related_specs(project_root, targets, test_files):
    """Specs that could kill a mutant in `targets`, repo-relative and sorted.

    Two sources, unioned:
      1. Each target's own co-located spec (already resolved by Step 2).
      2. Any spec that imports the target module by name -- so a mutant killed
         by a *different* spec is still correctly reported as killed rather
         than appearing to survive because its killer was excluded.
    """
    wanted = set()
    for source_file, _ in targets:
        own = test_files.get(source_file)
        if own:
            own = Path(own).as_posix()
            if (Path(project_root) / own).exists():
                wanted.add(own)

    # An import of `.../timeoff-accrual.service` (extension omitted, as TS
    # requires) is the reliable signal that a spec exercises this file.
    stems = {Path(src).name.rsplit(".", 1)[0] for src, _ in targets}
    if stems:
        alternation = "|".join(re.escape(s) for s in sorted(stems))
        quote = "['\"]"
        pattern = re.compile(
            r"(?:from|import|require)\s*\(?\s*" + quote
            + r"[^'\"]*?/?(?:" + alternation + r")" + quote
        )
        for spec in _all_spec_files(project_root):
            if spec in wanted:
                continue
            try:
                text = (Path(project_root) / spec).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                wanted.add(spec)

    return sorted(wanted)


def _angular_source_root(project_root):
    """The Angular project's `sourceRoot` (e.g. `src`), or `src` if undetermined.

    Angular's karma `include` resolver only strips a leading `sourceRoot/`
    prefix off a pattern that itself starts with that prefix -- see
    `_include_glob` for why that matters for brace-expanded patterns.
    """
    path = Path(project_root) / "angular.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "src"
    projects = data.get("projects") or {}
    for project in projects.values():
        source_root = project.get("sourceRoot")
        if source_root:
            return source_root.rstrip("/")
    return "src"


def _include_glob(specs, source_root="src"):
    """One glob string matching exactly `specs`.

    `ng test --include` is an array option but Stryker can only forward a single
    string per key, so several paths are expressed with brace expansion --
    `{a.spec.ts,b.spec.ts}` -- which Angular's glob matcher expands. A single
    path needs no braces.

    Angular's `include` resolver strips a leading `sourceRoot/` (e.g. `src/`)
    off the pattern *string* before matching, so paths resolve relative to
    `sourceRoot`. That strip only fires when the pattern starts with
    `sourceRoot/` literally -- a brace-wrapped pattern starts with `{`, so the
    strip never happens and each alternative keeps its `sourceRoot/` prefix,
    which then gets resolved a second time against `cwd: sourceRoot` (e.g.
    `src/src/app/...`), matching nothing. So each spec must have its
    `sourceRoot/` prefix removed *before* being brace-joined.
    """
    if not specs:
        return None
    if len(specs) == 1:
        return specs[0]
    prefix = source_root + "/"
    stripped = [s[len(prefix):] if s.startswith(prefix) else s for s in specs]
    return "{" + ",".join(stripped) + "}"


_STRYKER_CONFIG_NAMES = ("stryker.conf.json", "stryker.config.json",
                         ".stryker.conf.json")

# A JS/TS project may equally ship its Stryker config as an ES or CommonJS
# module. Python cannot parse one, but it does not have to: Node can import the
# project's own config and the pipeline can then override the two keys it cares
# about in a generated wrapper (see `_scoped_js_stryker_config`).
_STRYKER_JS_CONFIG_NAMES = (
    "stryker.conf.mjs", "stryker.conf.js", "stryker.conf.cjs",
    "stryker.config.mjs", "stryker.config.js", "stryker.config.cjs",
    ".stryker.conf.mjs", ".stryker.conf.js", ".stryker.conf.cjs",
)

# Narrowing only means anything for the Angular CLI karma runner, and for a JS
# config that fact cannot be read off a parsed dict -- so it is confirmed
# textually instead. No match means "not provably angular-cli", which skips
# narrowing rather than guessing.
_ANGULAR_CLI_RE = re.compile(
    r"projectType\s*:\s*['\"]angular-cli['\"]")


def _load_stryker_config(project_root):
    """(path, parsed) for the project's JSON Stryker config, or (None, None)."""
    root = Path(project_root)
    for name in _STRYKER_CONFIG_NAMES:
        path = root / name
        if not path.exists():
            continue
        try:
            return path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return path, None
    return None, None


def _find_js_stryker_config(project_root):
    """Path of the project's `.mjs`/`.js`/`.cjs` Stryker config, or None."""
    root = Path(project_root)
    for name in _STRYKER_JS_CONFIG_NAMES:
        path = root / name
        if path.exists():
            return path
    return None


def _write_js_scoped_config(js_config_path, reports_dir, glob, rel_report):
    """Generate a `.mjs` wrapper around a JS Stryker config. Returns its path.

    Python cannot parse -- let alone safely rewrite -- an ES/CommonJS config, so
    it does not try. It emits a tiny module that lets NODE evaluate the
    project's own config, then shallow-overrides only the two keys narrowing
    needs. Everything else the project set (karma port, hostname, configFile,
    coverageAnalysis, and any env-var-driven logic) is preserved exactly,
    because it is the project's own code producing it.

    The project's config file is never modified.
    """
    reports_dir = Path(reports_dir)
    out_path = reports_dir / "stryker.scoped.conf.mjs"
    # An absolute path is not a valid ESM specifier on Windows; a file:// URL is.
    base_url = Path(js_config_path).resolve().as_uri()
    out_path.write_text(
        "// Generated per run by the mutation pipeline -- do not edit.\n"
        f"// Wraps {Path(js_config_path).name} to narrow the Angular test suite\n"
        "// and pin the JSON report path. The wrapped config is untouched.\n"
        f"const loaded = await import({json.dumps(base_url)});\n"
        "const base = { ...(loaded.default ?? loaded) };\n"
        "\n"
        "// A relative $schema would not resolve from this file's directory.\n"
        "delete base.$schema;\n"
        "\n"
        "const karma = { ...(base.karma ?? {}) };\n"
        "const ngConfig = { ...(karma.ngConfig ?? {}) };\n"
        "ngConfig.testArguments = {\n"
        "  ...(ngConfig.testArguments ?? {}),\n"
        f"  include: {json.dumps(glob)},\n"
        "};\n"
        "karma.ngConfig = ngConfig;\n"
        "base.karma = karma;\n"
        "\n"
        "base.jsonReporter = {\n"
        "  ...(base.jsonReporter ?? {}),\n"
        f"  fileName: {json.dumps(rel_report)},\n"
        "};\n"
        "\n"
        "export default base;\n",
        encoding="utf-8")
    return out_path


def _scoped_stryker_config(project_root, reports_dir, targets, test_files):
    """Write a per-run Stryker config that narrows the suite. Returns (path, note).

    The project's own config is reused verbatim and only two things are added:
    the `ng test --include` glob, and an exact `jsonReporter.fileName` inside
    this run's report directory (so the report is read from a known path instead
    of being guessed at by mtime). The project's config file is never modified.

    A JSON config is deep-copied and rewritten here; a `.mjs`/`.js`/`.cjs` one
    is wrapped by a generated module that Node evaluates instead.

    Returns (None, reason) when narrowing does not apply -- no config, a
    non-Angular runner, or no specs found. Running ZERO tests would report every
    mutant as survived, so "found nothing" always means "do not narrow".
    """
    config_path, config = _load_stryker_config(project_root)
    js_config_path = _find_js_stryker_config(project_root) if config is None else None

    if config is None and js_config_path is None:
        return None, ("no Stryker config found -- running the full suite"
                      if config_path is None else
                      f"{config_path.name} is not valid JSON -- running the full suite")

    if config is not None:
        karma = config.get("karma") or {}
        if karma.get("projectType") != "angular-cli":
            return None, (f"karma.projectType is {karma.get('projectType')!r}, not "
                          "'angular-cli' -- test narrowing not applicable")
    else:
        # The JS config is never executed here, only read, so the check is
        # textual. Not provably angular-cli means do not narrow.
        try:
            source = js_config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        if not _ANGULAR_CLI_RE.search(source):
            return None, (f"{js_config_path.name} does not declare karma.projectType "
                          "'angular-cli' -- test narrowing not applicable")

    specs = _related_specs(project_root, targets, test_files)
    glob = _include_glob(specs, _angular_source_root(project_root))
    if not glob:
        return None, ("no spec file found for the targets -- running the full "
                      "suite rather than zero tests")

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    # Stryker resolves relative paths against its cwd, which is project_root.
    rel_report = Path(
        os.path.relpath(reports_dir / "mutation.json", project_root)).as_posix()

    if config is not None:
        scoped = json.loads(json.dumps(config))  # deep copy
        scoped.pop("$schema", None)  # a relative $schema would not resolve from here
        scoped.setdefault("karma", {}).setdefault("ngConfig", {}) \
              .setdefault("testArguments", {})["include"] = glob
        scoped.setdefault("jsonReporter", {})["fileName"] = rel_report
        out_path = reports_dir / "stryker.scoped.conf.json"
        out_path.write_text(json.dumps(scoped, indent=2), encoding="utf-8")
    else:
        out_path = _write_js_scoped_config(
            js_config_path, reports_dir, glob, rel_report)

    return out_path, f"{len(specs)} spec file(s): " + ", ".join(specs)


def _run_stryker(profile, project_root, module, targets, reports_dir,
                 timeout, quiet, log_path, productive_lines,
                 test_files=None, narrow_tests=True):
    engine = profile.engine_command(project_root)
    if engine is None:
        return 127, None, "Stryker not found. npm i -D @stryker-mutator/core"

    # ONE --mutate, comma separated. `--mutate` takes a single value, so a
    # second --mutate silently REPLACES the first: passing one flag per target
    # mutated only the last file and reported a score for it as if it covered
    # every file in scope.
    specs = []
    for source_file, tid in targets:
        specs.extend(_stryker_mutate_spec(tid, productive_lines.get(source_file)))

    cmd = engine + ["run", "--reporters", "json,progress", "--mutate", ",".join(specs)]

    scoped_config = None
    if narrow_tests:
        scoped_config, note = _scoped_stryker_config(
            project_root, reports_dir, targets, test_files or {})
        if scoped_config is not None:
            print(f"[MUT] narrowing the test suite to {note}")
            # The config file is Stryker's last POSITIONAL argument.
            cmd.append(str(scoped_config))
        else:
            print(f"[MUT] test narrowing skipped: {note}")

    start = time.time()
    # Stryker exits nonzero when the mutation score is under a configured break
    # threshold. That is a normal result, not a tool failure -- the report is
    # still written, so judge on the report's presence instead of the exit code.
    rc, out = _run(cmd, project_root, timeout, quiet, log_path, tolerate_nonzero=True)

    search = [Path(reports_dir)] + _stryker_json_targets(project_root, reports_dir)
    report = _newest(search, "mutation.json", start)
    if report is None:
        return 1, None, (
            f"Stryker produced no mutation.json report (exit {rc}). Last output:\n"
            f"    {_tail(out)}"
        )
    return 0, _parse_stryker_json(report, project_root), None


def _dotnet_root(dotnet_dir):
    """The directory holding the SDK's `sdk/` and `host/fxr/` folders, or None.

    Asks the SDK itself (`dotnet --list-sdks` prints `<version> [<root>/sdk]`), which is right for
    wrapper scripts and symlinks (Homebrew keeps the real install under libexec/); falls back to
    the directory the `dotnet` executable resolves into."""
    def valid(d):
        return (d / "host" / "fxr").is_dir() or (d / "sdk").is_dir()

    exe = Path(dotnet_dir) / ("dotnet.exe" if os.name == "nt" else "dotnet")
    try:
        out = subprocess.run([str(exe), "--list-sdks"], capture_output=True, text=True,
                             timeout=30, encoding="utf-8", errors="replace").stdout
        for line in reversed(out.splitlines()):
            m = re.search(r"\[(.+)\]\s*$", line)
            if m and valid(Path(m.group(1)).parent):
                return Path(m.group(1)).parent
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        d = exe.resolve().parent
    except OSError:
        d = Path(dotnet_dir)
    return d if valid(d) else None


def _dotnet_env(project_root):
    """A process environment in which Stryker.NET can spawn `dotnet`.

    Stryker.NET is a global tool but it shells out to the `dotnet` SDK to
    restore, build and test the project. When the pipeline is launched from a
    shell whose PATH lacks the .NET SDK (a common CI / agent sandbox), the
    `dotnet-stryker` binary itself resolves via our tools-dir fallback, then
    dies mid-run with "An error occurred trying to start process 'dotnet' ...
    The system cannot find the file specified." We therefore hand the child a
    PATH that is guaranteed to contain the dotnet SDK directory, discovered from
    PATH first and then the standard install locations. Returns None when
    nothing needs adding, so the default inherited environment is used."""
    dotnet_dir = None
    found = shutil.which("dotnet")
    if found:
        dotnet_dir = str(Path(found).parent)
    else:
        exe = "dotnet.exe" if os.name == "nt" else "dotnet"
        candidates = []
        for var in ("DOTNET_ROOT", "DOTNET_ROOT(x86)"):
            val = os.environ.get(var)
            if val:
                candidates.append(Path(val))
        home = os.environ.get("DOTNET_CLI_HOME") or os.path.expanduser("~")
        candidates += [
            Path(home) / ".dotnet",
            Path(r"C:\Program Files\dotnet"),
            Path(r"C:\Program Files (x86)\dotnet"),
            Path("/usr/local/share/dotnet"),
            Path("/usr/share/dotnet"),
            Path("/usr/lib/dotnet"),
        ]
        for d in candidates:
            if (d / exe).is_file():
                dotnet_dir = str(d)
                break

    env = os.environ.copy()
    sep = os.pathsep
    parts = env.get("PATH", "").split(sep) if env.get("PATH") else []
    changed = False

    # The dotnet SDK dir -- so Stryker can spawn `dotnet`.
    if dotnet_dir and dotnet_dir not in parts:
        parts.insert(0, dotnet_dir)
        changed = True

    # The global-tools dir -- so a nested `dotnet tool`/`dotnet-stryker` resolves
    # too, matching what engine_command already found.
    home = os.environ.get("DOTNET_CLI_HOME") or os.path.expanduser("~")
    tools_dir = str(Path(home) / ".dotnet" / "tools")
    if os.path.isdir(tools_dir) and tools_dir not in parts:
        parts.append(tools_dir)
        changed = True

    # The dotnet-stryker apphost locates the runtime through DOTNET_ROOT or a registered default
    # location; a package-manager install (Homebrew, a distro package) is neither, and the tool
    # dies with "You must install .NET to run this application". Point it at the SDK that is on PATH.
    if not env.get("DOTNET_ROOT") and not env.get("DOTNET_ROOT_ARM64") and dotnet_dir:
        root_dir = _dotnet_root(dotnet_dir)
        if root_dir is not None:
            env["DOTNET_ROOT"] = str(root_dir)
            changed = True

    if not changed:
        return None
    env["PATH"] = sep.join(parts)
    return env


def _nearest_csproj(start_dir: Path):
    """Walk up from `start_dir` to the nearest directory holding a .csproj/.fsproj.

    Returns (project_dir, project_file_name), or (None, None) when none is found
    before the filesystem root. Used to place Stryker.NET's working directory on
    a real project instead of a multi-project repo root."""
    for d in [start_dir, *start_dir.parents]:
        try:
            projects = sorted(list(d.glob("*.csproj")) + list(d.glob("*.fsproj")))
        except OSError:
            projects = []
        if projects:
            return d, projects[0].name
    return None, None


def _line_ranges_to_spans(source_abs: Path, line_ranges):
    """[(first_line, last_line)] -> [(start_char, end_char)] for one source file, or None.

    Stryker.NET's `file{start..end}` filter takes CHARACTER offsets into the file (verified
    against Stryker.NET 5.0: `{1..100}` keeps only mutants inside the first 100 characters, and
    line numbers filter out every mutant). Offsets count UTF-16 code units, the way Roslyn
    positions do: a BOM is not counted, CRLF counts as two, a character outside the BMP as two.
    None when the file cannot be read, so the caller falls back to mutating the whole file."""
    try:
        text = Path(source_abs).read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return None
    # start offset of every line, counted in UTF-16 code units, newlines kept (no universal-newline folding)
    starts, pos = [], 0
    for raw in text.splitlines(keepends=True):
        starts.append(pos)
        pos += len(raw.encode("utf-16-le")) // 2
    total = pos
    starts.append(total)
    spans = []
    for a, b in line_ranges:
        if a < 1 or a > len(starts) - 1:
            continue
        b = min(b, len(starts) - 1)
        end = starts[b] - 1 if b < len(starts) - 1 else total  # up to the end of line b, newline excluded
        end = max(end, starts[a - 1])
        spans.append((starts[a - 1], end))
    return spans


def _dotnet_mutate_specs(source_abs: Path, project_dir: Path, productive):
    """Stryker.NET file-filter glob(s) for one source file.

    `**/` + the file's path relative to its OWN project keeps the intermediate
    folders (e.g. `V1/AppConfigurations`) that make the match unambiguous, so a
    V1 file never also mutates its identically-named V2 sibling. Stryker's
    `{start..end}` range syntax narrows to the productive lines the arid filter
    supplied (converted to the character span it expects, one bounding span per file);
    without them, or when the file cannot be read, the whole file is mutated."""
    try:
        rel = source_abs.relative_to(project_dir).as_posix()
    except ValueError:
        rel = source_abs.name
    pattern = f"**/{rel}"
    rngs = _ranges(productive) if productive else []
    if rngs and len(rngs) <= MAX_MUTATION_RANGES:
        spans = _line_ranges_to_spans(source_abs, rngs)
        if spans:
            # ONE span per file: Stryker.NET combines several ranges for the same file so that
            # every mutant is filtered out (verified against 5.0). The bounding span is a superset;
            # run.py's diff-scope and arid filters drop the mutants between the changed lines.
            return [f"{pattern}{{{min(a for a, _ in spans)}..{max(b for _, b in spans)}}}"]
    return [pattern]


def _run_stryker_dotnet(profile, project_root, module, targets, reports_dir,
                        timeout, quiet, log_path, productive_lines,
                        test_files=None, narrow_tests=True):
    engine = profile.engine_command(project_root)
    if engine is None:
        return 127, None, "dotnet-stryker not found. dotnet tool install -g dotnet-stryker"

    test_files = test_files or {}
    root = Path(project_root)

    # Stryker.NET shells out to the `dotnet` SDK; guarantee it is on PATH for the
    # child even when the launching shell lacked it (CI / agent sandbox).
    env = _dotnet_env(project_root)

    # Stryker.NET must run where a project/test-project lives, not at the repo
    # root of a multi-project solution -- run it there and it fails outright with
    # "No .csproj or .fsproj file found". For each target we resolve two things
    # from the filesystem: the TEST project whose directory becomes the working
    # dir (Stryker auto-detects the single test csproj there), and the SOURCE
    # project handed to --project so Stryker knows which of that test project's
    # several references to mutate. Targets sharing both are one invocation.
    groups = {}       # (cwd, project_name) -> {"specs": [...], "sources": [...]}
    unresolved = []
    for source_file, tid in targets:
        source_abs = (root / source_file).resolve()
        src_dir, src_proj = _nearest_csproj(source_abs.parent)
        test_ref = test_files.get(source_file)
        test_dir = None
        if test_ref:
            test_abs = Path(test_ref)
            if not test_abs.is_absolute():
                test_abs = root / test_abs
            test_dir, _ = _nearest_csproj(test_abs.resolve().parent)
        if src_dir is None or src_proj is None or test_dir is None:
            unresolved.append((source_file, tid))
            continue
        specs = _dotnet_mutate_specs(source_abs, src_dir,
                                     productive_lines.get(source_file))
        g = groups.setdefault((str(test_dir), src_proj), {"specs": [], "sources": []})
        g["specs"].extend(specs)
        g["sources"].append(source_file)

    mutations = []
    errors = []

    # Fallback for any target whose project context could not be resolved: run
    # from the repo root exactly as the original runner did, so a non-standard
    # layout still attempts a run rather than being silently dropped.
    if unresolved:
        specs = []
        for source_file, tid in unresolved:
            productive = productive_lines.get(source_file)
            rngs = _ranges(productive) if productive else []
            if rngs and len(rngs) <= MAX_MUTATION_RANGES:
                specs.extend(f"{tid}{{{a}..{b}}}" for a, b in rngs)
            else:
                specs.append(tid)
        cmd = engine + ["--reporter", "json", "--mutate", ",".join(specs)]
        if module:
            cmd += ["--project", module]
        start = time.time()
        rc, out = _run(cmd, project_root, timeout, quiet, log_path,
                       tolerate_nonzero=True, env=env)
        search_roots = [Path(reports_dir), root / "StrykerOutput"]
        report = _newest(search_roots, "mutation-report.json", start)
        if report is None:
            errors.append(f"Stryker.NET produced no mutation-report.json "
                          f"(exit {rc}). Last output:\n    {_tail(out)}")
        else:
            mutations.extend(_parse_stryker_json(report, project_root))

    for (cwd, project_name), g in groups.items():
        cmd = engine + ["--reporter", "json",
                        "--project", project_name,
                        "--mutate", ",".join(g["specs"])]
        start = time.time()
        rc, out = _run(cmd, cwd, timeout, quiet, log_path,
                       tolerate_nonzero=True, env=env)
        search_roots = [Path(cwd) / "StrykerOutput", Path(reports_dir), root / "StrykerOutput"]
        report = _newest(search_roots, "mutation-report.json", start)
        if report is None:
            errors.append(
                f"Stryker.NET produced no mutation-report.json for project "
                f"{project_name} (exit {rc}). Last output:\n    {_tail(out)}"
            )
            continue
        mutations.extend(_parse_stryker_json(report, project_root))

    if not mutations and errors:
        return 1, None, "\n".join(errors)
    return 0, mutations, None


# --- mutmut (Python) ---------------------------------------------------------

def _mutmut_major(engine, project_root) -> int:
    """mutmut's CLI changed incompatibly at 3.0: `--paths-to-mutate` and
    `junitxml` were dropped in favour of config-file driven runs and
    `mutmut results`. Detect once and branch, rather than failing on whichever
    version the developer happens to have."""
    try:
        result = subprocess.run(
            engine + ["--version"], cwd=project_root, check=False,
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        match = re.search(r"(\d+)\.\d+", (result.stdout or "") + (result.stderr or ""))
        return int(match.group(1)) if match else 2
    except (OSError, subprocess.SubprocessError):
        return 2


def _mutmut_v3_patterns(path: str) -> list:
    """mutmut 3.x `run` takes mutant-name globs (`pkg.mod.*`), not file paths --
    a path matches nothing and aborts with 'Filtered for specific mutants, but
    nothing matches'. A src/ or lib/ layout may or may not carry its prefix in
    mutant names, so both spellings are offered."""
    dotted = re.sub(r"\.py$", "", path.replace("\\", "/").strip("./")).replace("/", ".")
    dotted = re.sub(r"\.__init__$", "", dotted)
    pats = [dotted + ".*"]
    for prefix in ("src.", "lib."):
        if dotted.startswith(prefix):
            pats.append(dotted[len(prefix):] + ".*")
    return pats


def _run_mutmut(profile, project_root, module, targets, reports_dir,
                timeout, quiet, log_path, productive_lines,
                test_files=None, narrow_tests=True):
    engine = profile.engine_command(project_root)
    if engine is None:
        return 127, None, "mutmut not found. pip install mutmut"

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = ",".join(tid for _, tid in targets)
    major = _mutmut_major(engine, project_root)

    # mutmut's exit code is a bitmask (survivors set bits) -- a nonzero code is
    # the normal 'some mutants survived' outcome, not a tool failure.
    if major >= 3:
        # v3 has no file scoping: positional args are mutant-name globs (one set per target file).
        # _mutmut_results_v3 still filters the results back down to `targets`.
        patterns = [pat for _, tid in targets for pat in _mutmut_v3_patterns(tid)]
        rc, out = _run(engine + ["run", *patterns],
                       project_root, timeout, quiet, log_path, tolerate_nonzero=True)
        return _mutmut_results_v3(engine, project_root, targets, out)

    rc, out = _run(engine + ["run", "--paths-to-mutate", paths],
                   project_root, timeout, quiet, log_path, tolerate_nonzero=True)

    junit_path = reports_dir / "mutmut-junit.xml"
    try:
        result = subprocess.run(
            engine + ["junitxml"], cwd=project_root, timeout=180, check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        junit_path.write_text(result.stdout or "", encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, None, f"mutmut junitxml failed: {exc}\n    {_tail(out)}"

    mutations = _parse_mutmut_junit(junit_path, targets)
    if not mutations:
        return 1, None, (
            f"mutmut reported no mutants (exit {rc}). Last output:\n    {_tail(out)}"
        )
    return 0, mutations, None


_MUTMUT_STATUS_V3 = {
    "killed": "KILLED", "bad": "KILLED",
    "survived": "SURVIVED", "bad_survived": "SURVIVED", "ok": "SURVIVED",
    "no_tests": "NO_COVERAGE", "untested": "NO_COVERAGE", "skipped": "NO_COVERAGE",
    "timeout": "TIMED_OUT", "suspicious": "RUN_ERROR", "check_was_interrupted": "RUN_ERROR",
}


def _parse_mutmut_v3_line(raw: str):
    """(name, normalised status) for one `mutmut results` line, else None.
    mutmut prints some statuses with SPACES (`no tests`, `not checked`), like
    gremlins' `NOT COVERED` -- a `[a-z_]+` match silently dropped every
    uncovered mutant."""
    match = re.match(r"^\s*([\w./\\:-]+?)\s*:\s*([a-z_ ]+?)\s*$", raw)
    if not match:
        return None
    status = _MUTMUT_STATUS_V3.get(match.group(2).replace(" ", "_"))
    return (match.group(1), status) if status else None


def _mutmut_results_v3(engine, project_root, targets, run_output):
    """mutmut 3.x: parse `mutmut results` (one `<name>: <status>` line per mutant).

    v3 mutates everything in its configured paths, so results are filtered to `targets`' files;
    otherwise run.py's diff-scope filter would keep mutants from files it has no diff for.
    """
    try:
        result = subprocess.run(
            # Without --all, mutmut 3 lists only the mutants that still need attention and
            # omits every killed one, so kills would never reach the score.
            engine + ["results", "--all", "true"], cwd=project_root, timeout=180, check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, None, f"mutmut results failed: {exc}"

    default_file = os.path.basename(targets[0][0]) if targets else ""
    default_rel = targets[0][0] if targets else ""
    target_files = {Path(f).as_posix() for f, _ in targets}
    parsed = []
    for raw in (result.stdout or "").splitlines():
        record = _parse_mutmut_v3_line(raw)
        if record is None:
            continue
        name, status = record
        src, rel, line = _mutmut_locate(name, default_file, default_rel)
        parsed.append((name, status, src, rel, line))
    # v3 names carry no line number, and a line-0 mutant is dropped as "outside
    # the diff" -- so every Python mutant would vanish. Recover real lines.
    lines = _mutmut_v3_lines(engine, project_root, [(n, r) for n, _, _, r, _ in parsed])
    mutations = [
        _record(status, lines.get(name) or line, "mutmut", name, src,
                index=name, file_path=rel)
        for name, status, src, rel, line in parsed
    ]
    if not mutations:
        return 1, None, (
            "mutmut 3.x produced no parsable results. Last output:\n"
            f"    {_tail(run_output)}"
        )
    in_scope = [m for m in mutations if m["file"] in target_files]
    if not in_scope:
        return 1, None, (
            f"mutmut 3.x reported {len(mutations)} mutant(s), but none in "
            f"{sorted(target_files)}. Last output:\n    {_tail(run_output)}"
        )
    return 0, in_scope, None


def _mutmut_line_from_show(show_text: str, source_lines: list, name: str) -> int:
    """File line of a mutmut 3.x mutant: the function's `def` line plus the
    offset of the first changed line in `mutmut show`'s function-relative diff
    (its hunk header counts from the function, not the file). 0 if unknown."""
    fn = re.match(r"^x(?:\u01c1[^\u01c1]+\u01c1)?_?(.+?)__mutmut_\d+$", name.rsplit(".", 1)[-1])
    if not fn:
        return 0
    name_fn = fn.group(1)
    def_line = next((i + 1 for i, l in enumerate(source_lines)
                     if re.match(rf"^\s*(?:async\s+)?def\s+{re.escape(name_fn)}\s*\(", l)), 0)
    if not def_line:
        return 0
    in_hunk, offset = False, 0
    for l in show_text.splitlines():
        if l.startswith("@@"):
            in_hunk, offset = True, 0
        elif in_hunk and l.startswith("-") and not l.startswith("---"):
            return def_line + offset
        elif in_hunk and not l.startswith("+"):
            offset += 1
    return 0


def _mutmut_v3_lines(engine, project_root, items) -> dict:
    """{mutant name: file line} via `mutmut show`, in parallel; best effort."""
    from concurrent.futures import ThreadPoolExecutor
    sources = {}

    def one(item):
        name, rel = item
        try:
            if rel not in sources:
                sources[rel] = Path(project_root, rel).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            shown = subprocess.run(
                engine + ["show", name], cwd=project_root, timeout=60, check=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            return name, _mutmut_line_from_show(shown.stdout or "", sources[rel], name)
        except (OSError, subprocess.SubprocessError):
            return name, 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, items))


def _mutmut_locate(name, default_file, default_rel):
    """Best-effort (basename, relpath, line) from a mutmut mutant identifier.

    mutmut names mutants after the function, e.g.
    `pkg.mod.fn__mutmut_3` or `pkg/mod.py:42`. Only the second form carries a
    line number; when there is none the line stays 0 and the report says so
    rather than inventing a location."""
    rel, line = default_rel, 0
    path_match = re.match(r"^(.*\.py)(?:[:#](\d+))?", name)
    if path_match:
        rel = Path(path_match.group(1)).as_posix()
        if path_match.group(2):
            line = int(path_match.group(2))
        return os.path.basename(rel), rel, line
    dotted = re.match(r"^([\w.]+?)\.[\w]+__mutmut_\d+", name)
    if dotted:
        rel = dotted.group(1).replace(".", "/") + ".py"
        return os.path.basename(rel), rel, 0
    return default_file, default_rel, 0


def _parse_mutmut_junit(path, targets):
    try:
        tree = ET.parse(path)
    except (ET.ParseError, FileNotFoundError, OSError):
        return []
    default_file = os.path.basename(targets[0][0]) if targets else ""
    default_rel = targets[0][0] if targets else ""
    mutations = []
    for case in tree.iter("testcase"):
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if skipped is not None:
            status = "NO_COVERAGE"
        elif failure is not None or error is not None:
            status = "SURVIVED"
        else:
            status = "KILLED"

        # Try BOTH attributes: mutmut puts the line number in whichever of
        # name/classname carries the `path:line` form, and which one that is
        # varies by version. Reading only `classname` loses the line for every
        # passing (killed) mutant, leaving survivors reported at line 0.
        src, rel, line = _mutmut_locate(name, default_file, default_rel)
        if not line:
            src2, rel2, line2 = _mutmut_locate(classname, default_file, default_rel)
            if line2:
                src, rel, line = src2, rel2, line2
            elif rel == default_rel and rel2 != default_rel:
                src, rel = src2, rel2
        if not line:
            # mutmut puts the diff in the failure text; the `--- path:line` header
            # in that diff is the only place the real line number appears.
            blob = (failure.text if failure is not None else "") or ""
            hit = re.search(r"[-+]{3}\s*\S*?:(\d+)", blob) or re.search(r"\bline\s+(\d+)", blob)
            if hit:
                line = int(hit.group(1))
        mutations.append(_record(status, line, "mutmut", name, src,
                                 index=name, file_path=rel))
    return mutations


# --- gremlins (Go) -----------------------------------------------------------

_GREMLINS_STATUS = {
    "KILLED": "KILLED",
    "LIVED": "SURVIVED",
    "NOTCOVERED": "NO_COVERAGE",
    "NOCOVERAGE": "NO_COVERAGE",
    # TIMED_OUT stays an error, matching PIT's taxonomy (pit_parser.ERROR_STATUSES).
    # Calling it SURVIVED would put an unkillable mutant on the test-writer's
    # worklist; calling it KILLED would credit a kill nobody earned.
    "TIMEDOUT": "TIMED_OUT",
    "NOTVIABLE": "NON_VIABLE",
    "RUNERROR": "RUN_ERROR",
}


def _normalise_gremlins_status(raw: str) -> str:
    """gremlins prints statuses with SPACES -- `NOT COVERED`, `TIMED OUT`,
    `NOT VIABLE`. Keying the map on underscores made every single Go mutant
    fall through to RUN_ERROR, which reads as an infrastructure failure and
    zeroes the score."""
    key = re.sub(r"[^A-Z]", "", str(raw or "").upper())
    return _GREMLINS_STATUS.get(key, "RUN_ERROR")


def _run_gremlins(profile, project_root, module, targets, reports_dir,
                  timeout, quiet, log_path, productive_lines,
                  test_files=None, narrow_tests=True):
    engine = profile.engine_command(project_root)
    if engine is None:
        return 127, None, ("gremlins not found. "
                           "go install github.com/go-gremlins/gremlins/cmd/gremlins@latest")

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    pkgs = sorted({tid for _, tid in targets}) or ["./..."]
    # One run per package: the report names files relative to the package it ran on, so a
    # combined run could not tell two `util.go` apart.
    mutations = []
    for i, pkg in enumerate(pkgs):
        out_json = reports_dir / ("gremlins.json" if len(pkgs) == 1 else f"gremlins-{i}.json")
        cmd = engine + ["unleash", "--output", str(out_json), pkg]
        rc, out = _run(cmd, project_root, timeout, quiet, log_path, tolerate_nonzero=True)
        if not out_json.exists():
            return 1, None, (
                f"gremlins produced no JSON output for {pkg} (exit {rc}). Last output:\n"
                f"    {_tail(out)}"
            )
        mutations.extend(_parse_gremlins_json(out_json, project_root, package_dir=pkg))
    return 0, mutations, None


def _gremlins_rel_path(raw_path, root, package_dir=None):
    """A gremlins file entry as a path relative to the project root.

    Older gremlins write an absolute `filename`; current releases write `file_name`, relative to
    the package that was unleashed (`calc.go` for `./calc`). `package_dir` is that package
    ("./calc", "calc", ".", or None when unknown)."""
    p = Path(raw_path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return p.as_posix()
    pkg = (package_dir or "").replace("\\", "/")
    pkg = pkg[2:] if pkg.startswith("./") else pkg
    pkg = "" if pkg in (".", "...") or pkg.endswith("/...") else pkg
    if pkg and (root / pkg / p).is_file():
        return (Path(pkg) / p).as_posix()
    return p.as_posix()


def _parse_gremlins_json(path, project_root, package_dir=None):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Malformed gremlins report at {path}: {exc}") from exc

    root = Path(project_root).resolve()
    mutations = []
    for entry in data.get("files", []):
        raw_path = entry.get("file_name") or entry.get("filename") or ""
        rel = _gremlins_rel_path(raw_path, root, package_dir)
        base = os.path.basename(raw_path)
        for m in entry.get("mutations", []):
            operator = m.get("type", "") or m.get("mutator", "")
            mutations.append(_record(
                _normalise_gremlins_status(m.get("status", "")),
                m.get("line", 0), operator, operator, base, file_path=rel,
            ))
    return mutations
