import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_direct_driver_contract_checker_is_green():
    result = subprocess.run(
        ["./venv/bin/python", "scripts/check_agent_driver_contract.py", "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "agent-driver contract valid" in result.stdout
