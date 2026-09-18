import hashlib
import hmac

import pytest

from src.desktop_shell_readiness import (
    DESKTOP_SHELL_NONCE_ENV,
    DESKTOP_SHELL_ORIGIN_ENV,
    DesktopShellBinding,
)


NONCE = "11" * 32
CHALLENGE = "22" * 32
ORIGIN = "http://127.0.0.1:49193"


def test_capture_is_absent_without_shell_environment():
    environment = {"unrelated": "kept"}
    assert DesktopShellBinding.capture(environment, pid=123) is None
    assert environment == {"unrelated": "kept"}


def test_capture_scrubs_secret_and_freezes_exact_loopback_origin():
    environment = {
        DESKTOP_SHELL_NONCE_ENV: NONCE,
        DESKTOP_SHELL_ORIGIN_ENV: f"{ORIGIN}/",
        "unrelated": "kept",
    }
    binding = DesktopShellBinding.capture(environment, pid=123)

    assert binding is not None
    assert binding.origin == ORIGIN
    assert binding.expected_host_header == "127.0.0.1:49193"
    assert environment == {"unrelated": "kept"}


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:49193",
        "http://localhost:49193",
        "http://0.0.0.0:49193",
        "http://127.0.0.1",
        "http://127.0.0.1:49193/path",
        "http://user@127.0.0.1:49193",
        "http://127.0.0.1:49193?query=1",
    ],
)
def test_capture_rejects_every_non_exact_loopback_origin(origin):
    environment = {
        DESKTOP_SHELL_NONCE_ENV: NONCE,
        DESKTOP_SHELL_ORIGIN_ENV: origin,
    }
    with pytest.raises(ValueError, match="exactly http://127.0.0.1"):
        DesktopShellBinding.capture(environment, pid=123)
    assert DESKTOP_SHELL_NONCE_ENV not in environment
    assert DESKTOP_SHELL_ORIGIN_ENV not in environment


def test_capture_rejects_incomplete_or_malformed_binding():
    with pytest.raises(ValueError, match="must be set together"):
        DesktopShellBinding.capture({DESKTOP_SHELL_NONCE_ENV: NONCE}, pid=123)
    with pytest.raises(ValueError, match="32-byte lowercase hex"):
        DesktopShellBinding.capture(
            {
                DESKTOP_SHELL_NONCE_ENV: "AA" * 32,
                DESKTOP_SHELL_ORIGIN_ENV: ORIGIN,
            },
            pid=123,
        )


def test_payload_is_bound_to_challenge_origin_pid_readiness_and_auth():
    binding = DesktopShellBinding(bytes.fromhex(NONCE), ORIGIN, 123)
    payload = binding.payload(CHALLENGE, ready=True, auth_enabled=True)
    message = (
        "open-clank-desktop-shell-v1\n"
        f"{CHALLENGE}\n{ORIGIN}\n123\n1\n1"
    ).encode("ascii")
    expected = hmac.new(bytes.fromhex(NONCE), message, hashlib.sha256).hexdigest()

    assert payload == {
        "schema_version": 1,
        "ready": True,
        "auth_enabled": True,
        "origin": ORIGIN,
        "pid": 123,
        "proof": expected,
    }
    assert binding.payload(CHALLENGE, ready=False, auth_enabled=True)["proof"] != expected
    assert binding.payload(CHALLENGE, ready=True, auth_enabled=False)["proof"] != expected


@pytest.mark.parametrize("challenge", ["", "22" * 31, "AA" * 32, "x" * 64])
def test_payload_rejects_malformed_challenge(challenge):
    binding = DesktopShellBinding(bytes.fromhex(NONCE), ORIGIN, 123)
    with pytest.raises(ValueError, match="challenge"):
        binding.payload(challenge, ready=True, auth_enabled=True)
