from .support_base import *
from .support_plans import install_fake_codex, write_plan


def fake_commit_harness_env(
    tmp_dir: Path, *, log_path: Path | None = None
) -> dict[str, str]:
    """Point the runner's `asl commit` subprocess at the fake codex harness.

    Auto-commit re-enters this checkout as `python -m agent_session_loop
    commit`, which resolves its message-generation harness from
    `harnesses.json` under `XDG_CONFIG_HOME`. A test that does not otherwise
    configure a harness home needs this or the commit would reach whatever
    real CLI the developer has installed. `log_path` opts into the fixture's
    per-commit JSONL record.
    """
    config_home = tmp_dir / "commit-config"
    config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fake_codex = install_fake_codex(tmp_dir)
    config_path.write_text(
        json.dumps({"codex": {"executable": str(fake_codex)}}),
        encoding="utf-8",
    )
    env = {"XDG_CONFIG_HOME": str(config_home)}
    if log_path is not None:
        env["FAKE_CODEX_COMMIT_LOG"] = str(log_path)
    return env


_GIT_TEMPLATE_LOCK = threading.Lock()
_git_template_dir: Path | None = None
#: Passed on the command line, not written to the repository's config, so the
#: copies stay identical to what ``git init`` produced. Without them ``git
#: commit`` may start detached auto-maintenance in the template, whose lock
#: files appear and vanish under a concurrent copy.
_TEMPLATE_GIT_OPTIONS = ("-c", "maintenance.auto=false", "-c", "gc.auto=0")


def _build_git_template() -> Path:
    """The one-commit repository every ``init_git_repo`` call copies.

    Five git subprocesses per repository dominated the suite's system time, so
    they run once per process and each caller gets a copy. The template lives
    in its own temporary directory, removed when the process exits.
    """
    global _git_template_dir
    with _GIT_TEMPLATE_LOCK:
        if _git_template_dir is not None:
            return _git_template_dir
        root = Path(tempfile.mkdtemp(prefix="asl-test-git-template-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        for args in (
            ["init"],
            ["config", "user.email", "agent-session-loop@example.com"],
            ["config", "user.name", "Agent Session Loop Tests"],
        ):
            subprocess.run(
                ["git", *_TEMPLATE_GIT_OPTIONS, *args],
                cwd=repo_dir,
                text=True,
                capture_output=True,
                check=True,
            )
        # `git init` seeds a dozen inert `*.sample` hooks and a `description`
        # nothing reads. They were over half the files every copy creates, and
        # creating a file is what each `init_git_repo` call pays for, so the
        # template drops them. The empty `hooks/` directory stays: tests
        # install real hooks into it.
        for sample in (repo_dir / ".git" / "hooks").glob("*.sample"):
            sample.unlink()
        (repo_dir / ".git" / "description").unlink(missing_ok=True)
        (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
        for args in (["add", "README.md"], ["commit", "-m", "Initial commit"]):
            subprocess.run(
                ["git", *_TEMPLATE_GIT_OPTIONS, *args],
                cwd=repo_dir,
                text=True,
                capture_output=True,
                check=True,
            )
        _git_template_dir = repo_dir
        return repo_dir


def init_git_repo(repo_dir: Path) -> None:
    """Make ``repo_dir`` a git repository with one commit of ``README.md``.

    Copies a per-process template, so every repository a process creates has
    the same initial commit SHA.
    """
    shutil.copytree(_build_git_template(), repo_dir, symlinks=True, dirs_exist_ok=True)


DEFAULT_MULTI_REPO_SPECS: tuple[tuple[str, str, str], ...] = (
    ("SVC", "tundra_svc", "git@github.com:tundra/SVC.git"),
    ("OAK", "tundra_OAK", "git@github.com:tundra/OAK.git"),
    ("OWL", "tundra_OWL_J", "git@github.com:tundra/OWL.J.git"),
    ("Kiln", "tundra_Kiln", "git@github.com:tundra/Kiln.git"),
)


#: Two clones of one remote, checked out side by side. Repository identity is
#: the resolved Git top-level, so these are two repositories even though
#: ``workspaces.origins_equal`` holds between them.
KILN_TWIN_REPO_SPECS: tuple[tuple[str, str, str], ...] = (
    ("Kiln", "tundra_Kiln", "git@github.com:tundra/Kiln.git"),
    (
        "KilnTesting",
        "tundra_Kiln_testing",
        "git@github.com:tundra/Kiln.git",
    ),
)


def init_multi_repo_workspace(
    workspace_dir: Path,
    specs: tuple[tuple[str, str, str], ...] = DEFAULT_MULTI_REPO_SPECS,
) -> dict[str, Path]:
    """Create a non-Git workspace with sibling Git repositories and origins."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    repositories: dict[str, Path] = {}
    for alias, relative_path, origin in specs:
        repository = workspace_dir / relative_path
        init_git_repo(repository)
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=repository,
            text=True,
            capture_output=True,
            check=True,
        )
        repositories[alias] = repository
    return repositories


def write_multi_repo_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
    specs: tuple[tuple[str, str, str], ...] = DEFAULT_MULTI_REPO_SPECS,
) -> None:
    """Write a minimal registered plan with valid per-session scopes."""
    write_plan(plan_dir, statuses, contracts_by_id=contracts_by_id)
    registry_rows = "\n".join(
        f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
    )
    (plan_dir / "manifest.md").write_text(
        "# Manifest\n\n## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        f"{registry_rows}\n",
        encoding="utf-8",
    )
    aliases = ", ".join(alias for alias, _path, _origin in specs)
    for number in range(1, len(statuses) + 1):
        path = plan_dir / f"session-{number}.md"
        text = path.read_text(encoding="utf-8")
        heading, remainder = text.split("\n", 1)
        path.write_text(
            f"{heading}\n\n**Reads repositories**: {aliases}\n"
            f"**Writes repositories**: {aliases}\n{remainder}",
            encoding="utf-8",
        )


def git_stdout(repo_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fail_open_for_path(target: Path):
    canonical_target = plans_files.canonicalize_path(target)
    original_open = Path.open

    def failing_open(path: Path, *args, **kwargs):
        if plans_files.canonicalize_path(path) == canonical_target:
            raise PermissionError(
                errno.EACCES, "permission denied", os.fspath(canonical_target)
            )
        return original_open(path, *args, **kwargs)

    return mock.patch.object(Path, "open", failing_open)


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


#: A grandchild of a verification command check: it publishes its PID, waits to
#: be orphaned, and only then records a late side effect. It inherits the
#: check's captured pipes, so surviving it also keeps the reader threads that
#: drain those pipes from ever seeing EOF.
