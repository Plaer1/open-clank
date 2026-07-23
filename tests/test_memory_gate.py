"""Memory gate proof tests — Sub 06-04.

Exercises the three-mode policy gate (off/automatic/manual) across:
  - capture_allowed() for per-turn capture decisions
  - write_allowed() for manual/tool write decisions
  - backward compatibility with legacy auto_memory pref
  - mode switching safety (non-destructive)
  - two-owner isolation (per-user pref independence)
  - degradation (incognito/compare override)

No private memory text is printed. Uses synthetic fixtures only.
"""

import pytest
from src.memory_gate import (
    capture_allowed,
    write_allowed,
    memory_mode,
    VALID_MODES,
    DEFAULT_MODE,
    PREF_KEY,
)


# ─── Mode resolution ─────────────────────────────────────────────────────

class TestMemoryMode:
    def test_default_when_absent(self):
        assert memory_mode({}) == DEFAULT_MODE
        assert memory_mode(None) == DEFAULT_MODE

    def test_valid_modes(self):
        for mode in VALID_MODES:
            assert memory_mode({PREF_KEY: mode}) == mode

    def test_case_insensitive(self):
        assert memory_mode({PREF_KEY: "OFF"}) == "off"
        assert memory_mode({PREF_KEY: "Manual"}) == "manual"

    def test_garbage_falls_back_to_default(self):
        assert memory_mode({PREF_KEY: "yolo"}) == DEFAULT_MODE
        assert memory_mode({PREF_KEY: 42}) == DEFAULT_MODE

    def test_strips_whitespace(self):
        assert memory_mode({PREF_KEY: "  off  "}) == "off"


# ─── Capture gate ────────────────────────────────────────────────────────

class TestCaptureAllowed:
    def test_automatic_allows_capture(self):
        assert capture_allowed({}) is True
        assert capture_allowed({PREF_KEY: "automatic"}) is True

    def test_off_blocks_capture(self):
        assert capture_allowed({PREF_KEY: "off"}) is False

    def test_manual_allows_capture(self):
        """Manual mode still captures — candidates go to pending queue."""
        assert capture_allowed({PREF_KEY: "manual"}) is True

    def test_incognito_blocks_all(self):
        """Incognito is a hard override — blocks in every mode."""
        for mode in VALID_MODES:
            assert capture_allowed({PREF_KEY: mode}, incognito=True) is False

    def test_compare_blocks_all(self):
        for mode in VALID_MODES:
            assert capture_allowed({PREF_KEY: mode}, compare_mode=True) is False

    def test_legacy_auto_memory_false_blocks(self):
        """When memory_mode is NOT set, auto_memory=False blocks capture."""
        assert capture_allowed({"auto_memory": False}) is False
        assert capture_allowed({"auto_memory": True}) is True

    def test_mode_pref_overrides_legacy(self):
        """When memory_mode IS set, it takes precedence over auto_memory."""
        # mode=manual + auto_memory=False → capture still allowed (mode wins)
        assert capture_allowed({PREF_KEY: "manual", "auto_memory": False}) is True
        # mode=automatic + auto_memory=False → capture allowed (mode wins)
        assert capture_allowed({PREF_KEY: "automatic", "auto_memory": False}) is True
        # mode=off + auto_memory=True → capture blocked (mode wins)
        assert capture_allowed({PREF_KEY: "off", "auto_memory": True}) is False


# ─── Write gate ──────────────────────────────────────────────────────────

class TestWriteAllowed:
    def test_automatic_allows_write(self):
        ok, _ = write_allowed({})
        assert ok is True
        ok, _ = write_allowed({PREF_KEY: "automatic"})
        assert ok is True

    def test_off_blocks_write(self):
        ok, reason = write_allowed({PREF_KEY: "off"})
        assert ok is False
        assert "off" in reason.lower()

    def test_manual_blocks_write(self):
        """In manual mode, direct writes are refused — must use candidate review."""
        ok, reason = write_allowed({PREF_KEY: "manual"})
        assert ok is False
        assert "manual" in reason.lower()

    def test_write_allowed_ignores_auto_memory(self):
        """auto_memory controls capture, not manual writes. Writes always allowed
        when memory_mode is not set."""
        ok, _ = write_allowed({"auto_memory": False})
        assert ok is True


# ─── Mode switching safety ──────────────────────────────────────────────

class TestModeSwitchSafety:
    """Mode switches are non-destructive — they change gate decisions,
    not stored data. These tests prove the gate produces correct decisions
    after a 'switch' (pref change), confirming no silent approval/discard."""

    def test_automatic_to_manual(self):
        """Switching to manual: capture still fires, writes now blocked."""
        old_prefs = {PREF_KEY: "automatic"}
        new_prefs = {PREF_KEY: "manual"}
        assert capture_allowed(old_prefs) is True
        assert capture_allowed(new_prefs) is True  # capture still works
        assert write_allowed(old_prefs)[0] is True
        assert write_allowed(new_prefs)[0] is False  # writes now blocked

    def test_automatic_to_off(self):
        """Switching to off: capture blocked, writes blocked."""
        old_prefs = {PREF_KEY: "automatic"}
        new_prefs = {PREF_KEY: "off"}
        assert capture_allowed(old_prefs) is True
        assert capture_allowed(new_prefs) is False
        assert write_allowed(old_prefs)[0] is True
        assert write_allowed(new_prefs)[0] is False

    def test_manual_to_automatic(self):
        """Switching from manual to automatic: writes now allowed.
        Pending candidates are NOT auto-approved (they stay in candidates table
        until explicitly reviewed). The gate only controls new writes."""
        old_prefs = {PREF_KEY: "manual"}
        new_prefs = {PREF_KEY: "automatic"}
        assert write_allowed(old_prefs)[0] is False
        assert write_allowed(new_prefs)[0] is True

    def test_off_to_automatic(self):
        """Switching from off to automatic: everything works again."""
        old_prefs = {PREF_KEY: "off"}
        new_prefs = {PREF_KEY: "automatic"}
        assert capture_allowed(old_prefs) is False
        assert capture_allowed(new_prefs) is True

    def test_switch_is_not_destructive(self):
        """Mode switch changes prefs, not data. No function in the gate module
        touches the database — it's a pure decision function."""
        import inspect
        src = inspect.getsource(__import__("src.memory_gate", fromlist=[""]))
        # Gate has no DB/file operations
        assert "open(" not in src
        assert "sqlite" not in src.lower()
        assert "delete" not in src.lower() or "delete" in src.lower().count("delete") == 0
        assert "save" not in src.lower() or src.lower().count("save") == 0


# ─── Two-owner isolation ────────────────────────────────────────────────

class TestTwoOwnerIsolation:
    """Each owner has their own prefs dict. Changing one owner's mode
    does not affect another owner's gate decisions."""

    def test_independent_modes(self):
        alice = {PREF_KEY: "off"}
        bob = {PREF_KEY: "automatic"}
        assert capture_allowed(alice) is False
        assert capture_allowed(bob) is True
        assert write_allowed(alice)[0] is False
        assert write_allowed(bob)[0] is True

    def test_changing_alice_does_not_affect_bob(self):
        alice = {PREF_KEY: "automatic"}
        bob = {PREF_KEY: "automatic"}
        # Alice switches to off
        alice[PREF_KEY] = "off"
        # Bob is unaffected
        assert capture_allowed(alice) is False
        assert capture_allowed(bob) is True
        assert write_allowed(bob)[0] is True

    def test_different_modes_per_owner(self):
        owners = {
            "alice": {PREF_KEY: "manual"},
            "bob": {PREF_KEY: "automatic"},
            "carol": {PREF_KEY: "off"},
        }
        assert capture_allowed(owners["alice"]) is True   # manual captures
        assert write_allowed(owners["alice"])[0] is False   # but can't write directly
        assert capture_allowed(owners["bob"]) is True
        assert write_allowed(owners["bob"])[0] is True
        assert capture_allowed(owners["carol"]) is False
        assert write_allowed(owners["carol"])[0] is False


# ─── Degradation ─────────────────────────────────────────────────────────

class TestDegradation:
    """Gate degrades safely: malformed prefs read as default (automatic),
    never as a mode that blocks everything or allows everything silently."""

    def test_none_prefs(self):
        assert capture_allowed(None) is True
        assert write_allowed(None)[0] is True

    def test_empty_prefs(self):
        assert capture_allowed({}) is True
        assert write_allowed({})[0] is True

    def test_corrupted_mode_value(self):
        """Garbage in prefs doesn't crash — falls back to default."""
        assert capture_allowed({PREF_KEY: {"nested": "garbage"}}) is True
        assert write_allowed({PREF_KEY: {"nested": "garbage"}})[0] is True

    def test_list_instead_of_string(self):
        assert capture_allowed({PREF_KEY: ["off"]}) is True  # not a valid mode str
        assert memory_mode({PREF_KEY: ["off"]}) == DEFAULT_MODE


# ─── Integration: manage_memory tool add respects gate ──────────────────

class TestManageMemoryGateIntegration:
    """Verify the gate is wired into do_manage_memory's add action."""

    @pytest.mark.asyncio
    async def test_add_refused_in_manual_mode(self):
        """In manual mode, manage_memory add returns an error."""
        from src.ai_interaction import do_manage_memory, set_memory_manager
        from unittest.mock import MagicMock

        # Wire up a stub provider so the function doesn't bail early
        provider = MagicMock()
        provider.provider_id = "frankenmemory"
        provider.list_memories = MagicMock(return_value=[])
        set_memory_manager(MagicMock(), provider=provider)

        # Patch prefs to return manual mode
        import routes.prefs_routes as prefs_mod
        original = prefs_mod._load_for_user
        prefs_mod._load_for_user = lambda user: {PREF_KEY: "manual"}
        try:
            result = await do_manage_memory("add\ntest memory", owner="alice")
            assert "error" in result
            assert "manual" in result["error"].lower()
        finally:
            prefs_mod._load_for_user = original

    @pytest.mark.asyncio
    async def test_add_refused_in_off_mode(self):
        from src.ai_interaction import do_manage_memory, set_memory_manager
        from unittest.mock import MagicMock

        provider = MagicMock()
        provider.provider_id = "frankenmemory"
        set_memory_manager(MagicMock(), provider=provider)

        import routes.prefs_routes as prefs_mod
        original = prefs_mod._load_for_user
        prefs_mod._load_for_user = lambda user: {PREF_KEY: "off"}
        try:
            result = await do_manage_memory("add\ntest memory", owner="alice")
            assert "error" in result
            assert "off" in result["error"].lower()
        finally:
            prefs_mod._load_for_user = original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
