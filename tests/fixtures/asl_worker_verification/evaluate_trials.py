# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""Live worker trial setup, execution, and receipt capture."""

from evaluate_shared import *  # noqa: F403
from evaluate_policy import *  # noqa: F403


def _git(repo: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _now() -> str:
    return datetime.now(UTC).isoformat()


def seed_repository(repo: Path, scenario: Scenario, *, log_path: Path) -> None:
    """Copy the template, inject the scenario's defect, and commit the result.

    The defect substitution is asserted rather than attempted: a template edit
    that silently stopped matching would seed a repository with nothing wrong
    in it, and every criterion would then pass for the wrong reason.
    """
    shutil.copytree(
        REPO_TEMPLATE,
        repo,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    toolkit = repo / "toolkit.py"
    source = toolkit.read_text(encoding="utf-8")
    old, new = scenario.defect
    if source.count(old) != 1:
        raise RuntimeError(
            f"scenario {scenario.key}: defect anchor is not unique in toolkit.py"
        )
    toolkit.write_text(source.replace(old, new), encoding="utf-8")
    (repo / "checks" / "_log_path.txt").write_text(f"{log_path}\n", encoding="utf-8")
    (repo / "README.md").write_text(
        "Disposable repository for one ASL worker-verification trial.\n",
        encoding="utf-8",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "asl-worker-verification@example.com")
    _git(repo, "config", "user.name", "ASL Worker Verification")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "Seed the trial repository")


def write_plan(plan_dir: Path, scenario: Scenario) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text(
        "\n".join(
            [
                "# Trial context",
                "",
                "A disposable single-session plan. `toolkit.py` holds small",
                "helpers; `checks/` holds their checks; `legacy_report/` is",
                "owned by another repair group.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    (plan_dir / "session-1.md").write_text(session_text(scenario), encoding="utf-8")
    plans_readiness.write_readiness_metadata(
        plan_dir,
        plans_models.ReadinessMetadata(
            state="loop-ready",
            readiness_sha256=plans_content_hash.stable_readiness_sha256(plan_dir),
            checked_at=_now(),
        ),
    )


def trial_env(*, harness_config_home: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    for name in STRIPPED_ENV_VARS:
        env.pop(name, None)
    if harness_config_home is not None:
        env["XDG_CONFIG_HOME"] = str(harness_config_home)
    return env


def _write_harness_config(config_home: Path, *, harness: str, executable: Path) -> None:
    from agent_session_loop import config as asl_config

    path = config_home / asl_config.APP_NAME / asl_config.HARNESS_CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({harness: {"executable": str(executable)}}), encoding="utf-8"
    )


@dataclass
class TrialRequest:
    scenario: Scenario
    trial: int
    trial_dir: Path
    harness: str = "claude"
    model: str | None = "claude-opus-5"
    harness_executable: Path | None = None
    timeout_seconds: float = 2400.0
    #: Extra environment for the run. Only the deterministic tests set this,
    #: to script the fake harness they inject alongside it.
    extra_env: Mapping[str, str] = field(default_factory=dict)


def run_trial(request: TrialRequest) -> dict[str, Any]:
    """Execute one trial and write its receipt. Returns the receipt."""
    scenario = request.scenario
    workspace = request.trial_dir / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    repo = workspace / "repo"
    plan_dir = workspace / "plan"
    state_dir = workspace / "state"
    log_path = workspace / "invocations.jsonl"
    state_dir.mkdir()

    seed_repository(repo, scenario, log_path=log_path)
    write_plan(plan_dir, scenario)

    config_home: Path | None = None
    if request.harness_executable is not None:
        config_home = workspace / "harness-config"
        _write_harness_config(
            config_home,
            harness=request.harness,
            executable=request.harness_executable,
        )
    env = trial_env(harness_config_home=config_home)
    env.update(request.extra_env)

    argv = [
        sys.executable,
        str(CANDIDATE_WRAPPER),
        "run",
        str(plan_dir),
        "--cwd",
        str(repo),
        "--harness",
        request.harness,
        "--state-dir",
        str(state_dir),
        "--max-sessions",
        "1",
        "--no-supervisor-audit",
        "--no-mailbox",
        "--json",
    ]
    if request.model:
        argv.extend(["--harness-model", request.model])

    started = time.monotonic()
    started_at = _now()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(CANDIDATE_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=request.timeout_seconds,
            check=False,
        )
        returncode: int | None = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as expired:
        returncode = None
        stdout = expired.stdout or "" if isinstance(expired.stdout, str) else ""
        stderr = expired.stderr or "" if isinstance(expired.stderr, str) else ""
        timed_out = True
    duration = time.monotonic() - started

    (request.trial_dir / "run-stdout.log").write_text(stdout, encoding="utf-8")
    (request.trial_dir / "run-stderr.log").write_text(stderr, encoding="utf-8")

    final_state = _sweep_final_state(repo, scenario)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_VERSION,
        "scenario": scenario.key,
        "case": scenario.case,
        "trial": request.trial,
        "created_at": _now(),
        "live": request.harness_executable is None,
        "harness": request.harness,
        "model_requested": request.model,
        "candidate": _candidate_metadata(),
        "policy_surfaces": policy_surfaces(CANDIDATE_ROOT),
        "fixture_digest": fixture_digest(),
        "workspace": {
            "root": str(workspace),
            "repo": str(repo),
            "plan_dir": str(plan_dir),
            "state_dir": str(state_dir),
            "invocations": str(log_path),
        },
        "run": {
            "argv": argv,
            "returncode": returncode,
            "timed_out": timed_out,
            "started_at": started_at,
            "duration_seconds": round(duration, 3),
        },
        "final_state": final_state,
    }
    receipt["policy_digest"] = policy_digest(receipt["policy_surfaces"])
    receipt_path = request.trial_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


def _candidate_metadata() -> dict[str, Any]:
    def git(*argv: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *argv],
                cwd=CANDIDATE_ROOT,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        except OSError, subprocess.CalledProcessError:
            return None

    return {
        "root": str(CANDIDATE_ROOT),
        "wrapper": str(CANDIDATE_WRAPPER),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


def _sweep_final_state(repo: Path, scenario: Scenario) -> dict[str, Any]:
    """Record what the repository actually does now, under the evaluator's name.

    The exit codes here are the only ones nobody under evaluation produced, so
    they are what "the defect is fixed" means. ``ASL_EVAL_ROLE`` keeps these
    invocations out of the worker and runner tallies.
    """
    env = os.environ.copy()
    for name in STRIPPED_ENV_VARS:
        env.pop(name, None)
    env["ASL_EVAL_ROLE"] = "evaluator"

    def probe(*argv: str) -> int:
        return subprocess.run(
            [sys.executable, *argv],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        ).returncode

    def git(*argv: str) -> str:
        try:
            return _git(repo, *argv)
        except OSError, subprocess.CalledProcessError:
            return ""

    changed = sorted(
        {
            line[3:].strip()
            for line in git("status", "--porcelain").splitlines()
            if line.strip()
        }
        | {
            line.strip()
            for line in git("diff", "--name-only", "HEAD").splitlines()
            if line.strip()
        }
    )
    return {
        "focused_exit": probe("checks/focused.py", scenario.case),
        "contract_exit": probe("checks/contract.py"),
        "changed_paths": changed,
        "head": git("rev-parse", "HEAD").strip(),
    }


# --------------------------------------------------------------------------
# Evidence reading
# --------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


__all__ = [name for name in globals() if not name.startswith("__")]
