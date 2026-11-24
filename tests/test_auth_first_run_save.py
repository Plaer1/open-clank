"""First save on an empty data dir under a registered policy coordinator (guarded profile); no app."""
import os
import secrets
import shutil
import stat
import tempfile

import src.openclank.memory_policy_projection as mpp
from core.auth import AuthManager


class _Coordinator:
    def __init__(self):
        self.validated = []

    def validate_authority(self, name):
        self.validated.append(name)


def _with_coordinator(run):
    root = tempfile.mkdtemp(prefix="auth-first-run-")
    real, fake = mpp.policy_coordinator, _Coordinator()
    mpp.policy_coordinator = lambda: fake
    try:
        run(os.path.join(root, "auth.json"), fake)
    finally:
        mpp.policy_coordinator = real
        os.chmod(root, 0o700)
        shutil.rmtree(root, ignore_errors=True)


def test_first_user_on_empty_dir_persists_and_reloads():
    def run(path, fake):
        pw = secrets.token_urlsafe(16)
        mgr = AuthManager(path)
        assert not os.path.exists(path)
        assert mgr.setup("admin", pw) is True
        assert os.path.exists(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # mkstemp default of the atomic writer
        again = AuthManager(path)
        assert again.is_configured and "admin" in again.users
        assert again.verify_password("admin", pw) is True
        # second save reads the now-existing file and still works
        assert mgr.create_user("second", secrets.token_urlsafe(16)) is True
        assert "second" in AuthManager(path).users
    _with_coordinator(run)


def test_malformed_existing_file_refuses_and_is_untouched():
    def run(path, fake):
        mgr = AuthManager(path)
        assert mgr.setup("admin", secrets.token_urlsafe(16))
        bad = b'{"users": '
        with open(path, "wb") as f:
            f.write(bad)
        try:
            mgr.create_user("other", secrets.token_urlsafe(16))
            assert False, "save over a malformed file must refuse"
        except ValueError as exc:
            assert "authentication policy unavailable" in str(exc)
        assert open(path, "rb").read() == bad
    _with_coordinator(run)


def test_unreadable_existing_file_refuses_and_is_untouched():
    if os.geteuid() == 0:
        return
    def run(path, fake):
        mgr = AuthManager(path)
        assert mgr.setup("admin", secrets.token_urlsafe(16))
        before = open(path, "rb").read()
        os.chmod(path, 0)
        try:
            try:
                mgr.create_user("other", secrets.token_urlsafe(16))
                assert False, "save over an unreadable file must refuse"
            except ValueError as exc:
                assert "authentication policy unavailable" in str(exc)
        finally:
            os.chmod(path, 0o600)
        assert open(path, "rb").read() == before
    _with_coordinator(run)
