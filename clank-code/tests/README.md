# Running Open Clank tests

From the repository root, enter `clank-code/` before running the commands below.

Run a focused module with the project Python environment and `python -m pytest tests/test_name.py -q`. The root conftest provides a disposable data directory and an in-memory database when no explicit locations are set. Use a disposable environment for any override.

Select by area with `python tests/run_focus.py --help` or pytest's registered markers. Use `--durations=10` to identify expensive cases. To reproduce an order-dependent failure, run `python tests/run_order_report.py --seed 123 -- tests/cli/ -q`.

Browser and Node checks live alongside the Python suite. Check the script's setup before running it. Some need a built frontend, a disposable server or a browser.

## Shared helpers

- `tests/helpers/sqlite_db.py` supplies temporary SQLite databases for tests that need real persistence.
- `tests/helpers/import_state.py` restores module bindings for tests that inject stubs.
- `tests/helpers/db_stubs.py` supplies lightweight database stand-ins.
- `tests/helpers/cli_loader.py` loads command-line scripts with controlled imports.

See the [testing standard](TESTING_STANDARD.md) for what belongs in the suite.
