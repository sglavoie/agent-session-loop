from .support_base import *
from .support_plans import write_plan


def _write_plan_dir(plan_dir: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    return plan_dir


def _seed_archivable_plan(
    tmp_path: Path,
    *,
    with_pin: bool = True,
    with_state: bool = True,
    create_plan: bool = True,
) -> tuple[Path, Path, Path]:
    """Seed a plan (optionally pinned, with ASL state) ready to archive.

    Returns ``(state_dir, registry_path, canonical_plan_dir)``.
    """
    state_dir = tmp_path / "state"
    registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
    plan_dir = tmp_path / "plans" / "alpha"
    if create_plan:
        _write_plan_dir(plan_dir)
    canonical = plans_files.canonicalize_path(plan_dir)
    if with_pin:
        seeded = workbench_registry.upsert_pin(
            workbench_registry.load_registry(registry_path),
            canonical,
            label="Alpha",
            profile=workbench_registry_models.LaunchProfile(harness="codex"),
        )
        workbench_registry.save_registry(seeded, registry_path)
    if with_state:
        plan_state = state.plan_state_dir(canonical, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / "state.json").write_text(
            json.dumps({"plan_dir": str(canonical)}), encoding="utf-8"
        )
    return state_dir, registry_path, canonical


_WORKBENCH_DEAD_PID = 4_242_424


def _make_workbench_plan(tmp_path: Path, name: str = "plan-alpha") -> Path:
    plan_dir = tmp_path / name
    write_plan(plan_dir, ("pending", "pending"))
    return plans_files.canonicalize_path(plan_dir)


def _write_state_payload(
    plan_dir: Path,
    state_root: Path,
    payload: dict[str, Any],
) -> Path:
    plan_state = state.plan_state_dir(plan_dir, state_root)
    plan_state.mkdir(parents=True, exist_ok=True)
    state_path = plan_state / events.STATE_FILE_NAME
    state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return state_path


def _write_raw_state_payload(
    plan_dir: Path,
    state_root: Path,
    payload: str,
) -> Path:
    plan_state = state.plan_state_dir(plan_dir, state_root)
    plan_state.mkdir(parents=True, exist_ok=True)
    state_path = plan_state / events.STATE_FILE_NAME
    state_path.write_text(payload, encoding="utf-8")
    return state_path


def _write_lock_file(
    plan_dir: Path,
    state_root: Path,
    *,
    pid: int,
    hostname: str | None = None,
    process_start: str | None = None,
    wakeup_endpoint: Path | str | None = None,
    redirect_endpoint: Path | str | None = None,
) -> Path:
    """Write a lock naming ``pid``, legacy-shaped unless a field is given.

    The default omits every optional field, which is what makes the lock
    unwakeable: with no ``wakeup_endpoint`` a producer has nothing to write to
    and reports ``no-endpoint``. Tests that need a lock a producer really wakes
    pass an endpoint served by :func:`live_wakeup_endpoint`.

    ``redirect_endpoint`` is the same for the urgent redirect channel, and its
    absence is what makes a lock refuse redirects rather than merely delay
    them; :func:`live_redirect_endpoint` serves one for tests that need
    delivery to land.
    """
    plan_state = state.plan_state_dir(plan_dir, state_root)
    plan_state.mkdir(parents=True, exist_ok=True)
    lock_path = plan_state / state.LOCK_FILE_NAME
    payload: dict[str, Any] = {
        "pid": pid,
        "plan_dir": str(plan_dir),
        "created_at": "2026-06-19T00:00:00+00:00",
    }
    if hostname is not None:
        payload["hostname"] = hostname
    if process_start is not None:
        payload["process_start"] = process_start
    if wakeup_endpoint is not None:
        payload["wakeup_endpoint"] = str(wakeup_endpoint)
    if redirect_endpoint is not None:
        payload["redirect_endpoint"] = str(redirect_endpoint)
    lock_path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    return lock_path


def _write_live_lock_file(
    plan_dir: Path,
    state_root: Path,
    *,
    wakeup_endpoint: Path | str | None = None,
) -> Path:
    """Write a lock describing this process as the live owner.

    Records the current host and this pid's real birth time. Those fields are
    what the staleness check reads; delivery itself needs ``wakeup_endpoint``,
    so a caller that wants a producer's wakeup to land passes one from
    :func:`live_wakeup_endpoint`.
    """
    return _write_lock_file(
        plan_dir,
        state_root,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        process_start=process_tree.process_birth(os.getpid()),
        wakeup_endpoint=wakeup_endpoint,
    )


@contextmanager
def live_wakeup_endpoint(plan_dir: Path, state_root: Path):
    """Serve a plan's wakeup FIFO from this process for the block's duration.

    Yields the endpoint path to record in a lock and the event a delivered
    wakeup sets. This is the production reader, so a test that writes to the
    yielded path exercises exactly what a runner would receive.
    """
    event = threading.Event()
    with control_signal.open_wakeup_endpoint(
        state.plan_state_dir(plan_dir, state_root), event
    ) as path:
        if path is None:
            raise unittest.SkipTest("this filesystem cannot host a wakeup FIFO")
        yield path, event


@contextmanager
def live_redirect_endpoint(plan_dir: Path, state_root: Path):
    """Serve a plan's urgent redirect FIFO from this process for the block.

    The redirect counterpart of :func:`live_wakeup_endpoint`, and the reason a
    test can distinguish "the run is live" from "the run serves redirects": a
    lock may name one endpoint, both, or neither.
    """
    event = threading.Event()
    with control_signal.open_redirect_endpoint(
        state.plan_state_dir(plan_dir, state_root), event
    ) as path:
        if path is None:
            raise unittest.SkipTest("this filesystem cannot host a redirect FIFO")
        yield path, event


def _discovered(
    plan_dir: Path, **overrides
) -> workbench_registry_models.DiscoveredPlan:
    kwargs = {
        "plan_dir": plan_dir,
        "sources": ("state",),
        "label": None,
        "profile": None,
        "exists": True,
    }
    kwargs.update(overrides)
    return workbench_registry_models.DiscoveredPlan(**kwargs)


class _FakePopen:
    """Minimal Popen stand-in that records argv/cwd/env and captures stdin."""

    def __init__(
        self,
        argv,
        *,
        stdin=None,
        stdout=None,
        stderr=None,
        env=None,
        cwd=None,
        text=False,
        start_new_session=False,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.text = text
        # Recorded, not ignored: whether the child gets its own session is the
        # difference between a run that survives its launcher's shutdown and
        # one the launcher's signal takes down with it.
        self.start_new_session = start_new_session
        self.pid = 4242
        self.returncode: int | None = None
        self._stdin_buffer = io.StringIO() if text else io.BytesIO()

        class _StdinProxy:
            def __init__(self, buf, owner):
                self._buf = buf
                self._owner = owner
                self.closed = False

            def write(self, data):
                self._buf.write(data)

            def close(self):
                self.closed = True
                self._owner.stdin_text = self._buf.getvalue()

        if stdin == subprocess.PIPE:
            self.stdin = _StdinProxy(self._stdin_buffer, self)
        else:
            self.stdin = None

        self.stdin_text: str | bytes = "" if text else b""

    def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - only used on error paths
        if self.returncode is None:
            self.returncode = -9


class _BrokenStdinFakePopen(_FakePopen):
    """Variant whose stdin raises BrokenPipeError on the first write."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        class _BrokenStdin:
            closed = False

            def write(self, _data):
                raise BrokenPipeError("simulated broken pipe")

            def close(self):
                self.closed = True

        if self.stdin is not None:
            self.stdin = _BrokenStdin()


def _http_request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict, dict]:
    """Make an HTTP request against the Workbench server.

    Returns ``(status, parsed_json, response_headers)``. JSON-decode errors
    bubble up as test failures via raising.
    """
    import urllib.error
    import urllib.request

    data = None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        data = payload
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(payload))
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
        response_headers = dict(error.headers.items())
    text = raw.decode("utf-8") if raw else ""
    parsed = json.loads(text) if text else {}
    return status, parsed, response_headers


def _http_request_raw(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes, dict]:
    """Make an HTTP request and return ``(status, body_bytes, headers)``.

    Mirrors :func:`_http_request` but does not attempt to JSON-decode the
    response body. Use for static-asset tests where the response is HTML,
    CSS, or JS rather than JSON.
    """
    import urllib.error
    import urllib.request

    request_headers = {"Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
        response_headers = dict(error.headers.items())
    return status, raw, response_headers
