# ASL maintenance

- Whenever you change ASL behavior or architecture, review `README.md` in this directory and update it if needed.
- `runner.py` is a facade: a docstring, its imports, and the two argv entry points `cli.py` addresses by name. Put new behavior in the module that owns it, never here. `tests/test_asl_runner_shrink.py` asserts the runner's line count and public-name count by equality, so anything added here fails by name.
- Do not re-export a symbol from `runner.py`. The `X = <module>.Y` convention the extraction program used is retired: a `mock.patch.object(runner, "X")` target becomes a silent no-op once nothing reads `runner.X`, so patch and call at the module that owns the symbol. `RunError` is the one exception, and it is an import — an `except runner.RunError` needs the same class object `run_errors` raises.
