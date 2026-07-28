import os

from src.builtin_mcp import _BUILTIN_SERVERS, builtin_python_env


def test_builtin_python_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(["/app/venv/lib/python3.13/site-packages", "/app", "/extra"]),
    )

    env = builtin_python_env("/app")

    assert env == {
        "PYTHONPATH": os.pathsep.join(["/app", "/app/venv/lib/python3.13/site-packages", "/extra"])
    }


def test_builtin_python_env_uses_app_root_without_existing_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    assert builtin_python_env("/srv/odysseus") == {"PYTHONPATH": "/srv/odysseus"}


def test_memory_is_not_started_as_a_parallel_builtin_provider():
    assert "memory" not in _BUILTIN_SERVERS
