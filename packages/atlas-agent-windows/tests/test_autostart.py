"""Logon autostart, and the promise that it needs no administrator rights.

The round-trip tests below actually write to the registry, under a test-only
entry name, and remove it again. That is the point: the requirement is "works
without elevation", and only really doing it proves that.
"""

from __future__ import annotations

import inspect
import sys

import pytest

from atlas_agent import autostart

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")

#: Distinct from the production name so a test can never disturb a real install.
TEST_ENTRY_NAME = "ATLAS Agent (test)"
PROBE_COMMAND = '"C:\\Windows\\System32\\cmd.exe" /c exit'


@pytest.fixture
def isolated_entry(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(autostart, "ENTRY_NAME", TEST_ENTRY_NAME)
    yield
    try:
        autostart.uninstall()
    except autostart.AutostartError:
        pass


class TestCommandConstruction:
    def test_command_is_quoted_and_runs_the_agent(self) -> None:
        command = autostart.agent_command()
        assert command.startswith('"')
        assert command.endswith(" run")

    @staticmethod
    def _code_only() -> str:
        """Source with the module docstring removed.

        The docstring names the rejected approaches in order to record *why*
        they were rejected; scanning it would make these tests fail on their own
        rationale.
        """
        return inspect.getsource(autostart).split('"""', 2)[-1]

    def test_uses_the_per_user_hive_only(self) -> None:
        # HKEY_LOCAL_MACHINE would need elevation and would affect every account
        # on the machine. Neither is wanted.
        source = self._code_only()
        assert "HKEY_CURRENT_USER" in source
        assert "HKEY_LOCAL_MACHINE" not in source

    def test_never_requests_elevation(self) -> None:
        source = self._code_only()
        for forbidden in ("runas", "ShellExecute", "schtasks", "HIGHEST"):
            assert forbidden not in source


@on_windows
class TestRoundTripWithoutElevation:
    def test_install_query_uninstall(self, isolated_entry: None) -> None:
        assert autostart.status().installed is False

        installed = autostart.install(command=PROBE_COMMAND)
        assert installed.installed is True
        assert installed.detail == PROBE_COMMAND

        # This process is not elevated and the write succeeded — which is the
        # requirement, stated as a test.
        current = autostart.status()
        assert current.installed is True
        assert current.detail == PROBE_COMMAND

        removed = autostart.uninstall()
        assert removed.installed is False
        assert autostart.status().installed is False

    def test_install_is_idempotent(self, isolated_entry: None) -> None:
        autostart.install(command=PROBE_COMMAND)
        autostart.install(command=PROBE_COMMAND)
        assert autostart.status().installed is True

    def test_reinstalling_replaces_the_command(self, isolated_entry: None) -> None:
        autostart.install(command=PROBE_COMMAND)
        replacement = '"C:\\Windows\\System32\\cmd.exe" /c rem'
        autostart.install(command=replacement)
        assert autostart.status().detail == replacement

    def test_uninstalling_something_absent_is_not_an_error(self, isolated_entry: None) -> None:
        assert autostart.uninstall().installed is False

    def test_removal_is_complete(self, isolated_entry: None) -> None:
        # "Fully disable it" has to mean the entry is gone, not merely blanked.
        import winreg

        autostart.install(command=PROBE_COMMAND)
        autostart.uninstall()

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, autostart.RUN_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            with pytest.raises(FileNotFoundError):
                winreg.QueryValueEx(key, TEST_ENTRY_NAME)


@on_windows
def test_a_real_install_does_not_disturb_the_production_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = autostart.status()

    monkeypatch.setattr(autostart, "ENTRY_NAME", TEST_ENTRY_NAME)
    autostart.install(command=PROBE_COMMAND)
    autostart.uninstall()

    monkeypatch.undo()
    after = autostart.status()
    assert (before.installed, before.detail) == (after.installed, after.detail)
