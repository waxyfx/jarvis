"""Path guard: the check that runs on the machine that owns the files."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from atlas_agent.safety.paths import PathGuard, PathRefusedError
from atlas_shared.enums import RefusalReason

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    allowed = tmp_path / "allowed"
    (allowed / "sub").mkdir(parents=True)
    (allowed / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "forbidden").mkdir()
    (tmp_path / "forbidden" / "secret.txt").write_text("nope", encoding="utf-8")
    return tmp_path


@pytest.fixture
def guard(workspace: Path) -> PathGuard:
    return PathGuard([workspace / "allowed"])


def make_junction(link: Path, target: Path) -> bool:
    """Create a directory junction. Returns False if the OS refused.

    Junctions, unlike symlinks, need no administrator rights on Windows — which
    also makes them the realistic escape route to defend against.
    """
    if sys.platform != "win32":
        return False
    result = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and link.exists()


class TestInsideTheRoots:
    def test_file_in_root_is_allowed(self, guard: PathGuard, workspace: Path) -> None:
        resolved = guard.check(str(workspace / "allowed" / "notes.txt"))
        assert resolved.resolved.name == "notes.txt"
        assert resolved.followed_reparse_point is False

    def test_nested_file_is_allowed(self, guard: PathGuard, workspace: Path) -> None:
        assert guard.is_allowed(str(workspace / "allowed" / "sub" / "x.txt"))

    def test_a_file_that_does_not_exist_yet_is_allowed(
        self, guard: PathGuard, workspace: Path
    ) -> None:
        # Creating a file is a legitimate operation; only its location matters.
        assert guard.is_allowed(str(workspace / "allowed" / "new-file.txt"))

    def test_the_root_itself_is_allowed(self, guard: PathGuard, workspace: Path) -> None:
        assert guard.is_allowed(str(workspace / "allowed"))

    @on_windows
    def test_case_and_separator_differences_do_not_matter(
        self, guard: PathGuard, workspace: Path
    ) -> None:
        mixed = str(workspace / "allowed" / "notes.txt").upper().replace("\\", "/")
        assert guard.is_allowed(mixed)


class TestOutsideTheRoots:
    def test_sibling_directory_is_refused(self, guard: PathGuard, workspace: Path) -> None:
        with pytest.raises(PathRefusedError) as exc:
            guard.check(str(workspace / "forbidden" / "secret.txt"))
        assert exc.value.reason is RefusalReason.PATH_OUTSIDE_ROOTS

    def test_traversal_is_refused(self, guard: PathGuard, workspace: Path) -> None:
        with pytest.raises(PathRefusedError) as exc:
            guard.check(str(workspace / "allowed" / ".." / "forbidden" / "secret.txt"))
        assert exc.value.reason is RefusalReason.PATH_OUTSIDE_ROOTS

    def test_deep_traversal_is_refused(self, guard: PathGuard, workspace: Path) -> None:
        assert not guard.is_allowed(str(workspace / "allowed" / "sub" / ".." / ".." / "forbidden"))

    def test_a_prefix_lookalike_is_refused(self, workspace: Path) -> None:
        # "allowed-evil" starts with the same characters as "allowed"; a naive
        # string prefix check would let it through.
        (workspace / "allowed-evil").mkdir()
        guard = PathGuard([workspace / "allowed"])
        assert not guard.is_allowed(str(workspace / "allowed-evil" / "x.txt"))

    def test_relative_paths_are_refused(self, guard: PathGuard) -> None:
        with pytest.raises(PathRefusedError, match="must be absolute"):
            guard.check("notes.txt")

    @pytest.mark.parametrize(
        "path",
        [r"\\server\share\file.txt", r"\\?\C:\Windows\System32", r"\\.\PhysicalDrive0"],
    )
    def test_unc_and_device_paths_are_refused(self, guard: PathGuard, path: str) -> None:
        with pytest.raises(PathRefusedError, match="UNC and device paths"):
            guard.check(path)


@on_windows
class TestReparsePoints:
    def test_junction_pointing_out_of_bounds_is_refused(self, workspace: Path) -> None:
        link = workspace / "allowed" / "escape"
        if not make_junction(link, workspace / "forbidden"):
            pytest.skip("this system does not permit creating junctions")

        guard = PathGuard([workspace / "allowed"])
        # The path *looks* like it is inside the allowed root. Only resolving it
        # first reveals that it is not.
        with pytest.raises(PathRefusedError) as exc:
            guard.check(str(link / "secret.txt"))
        assert exc.value.reason is RefusalReason.PATH_OUTSIDE_ROOTS
        assert "resolves outside" in exc.value.message

    def test_junction_staying_in_bounds_is_allowed_and_flagged(self, workspace: Path) -> None:
        link = workspace / "allowed" / "inner"
        if not make_junction(link, workspace / "allowed" / "sub"):
            pytest.skip("this system does not permit creating junctions")

        guard = PathGuard([workspace / "allowed"])
        resolved = guard.check(str(link / "x.txt"))
        assert resolved.followed_reparse_point is True

    def test_a_root_that_is_itself_a_junction_still_works(self, workspace: Path) -> None:
        real_root = workspace / "real-root"
        real_root.mkdir()
        (real_root / "file.txt").write_text("x", encoding="utf-8")
        link = workspace / "linked-root"
        if not make_junction(link, real_root):
            pytest.skip("this system does not permit creating junctions")

        guard = PathGuard([link])
        assert guard.is_allowed(str(link / "file.txt"))
        assert guard.is_allowed(str(real_root / "file.txt"))


@on_windows
class TestWindowsSpecials:
    def test_alternate_data_streams_are_refused(self, guard: PathGuard, workspace: Path) -> None:
        with pytest.raises(PathRefusedError) as exc:
            guard.check(str(workspace / "allowed" / "notes.txt") + ":hidden")
        assert exc.value.reason is RefusalReason.PATH_DENYLISTED

    @pytest.mark.parametrize("name", ["CON", "nul", "COM1", "LPT9", "con.txt"])
    def test_reserved_device_names_are_refused(
        self, guard: PathGuard, workspace: Path, name: str
    ) -> None:
        with pytest.raises(PathRefusedError, match="reserved device name"):
            guard.check(str(workspace / "allowed" / name))


class TestDenylist:
    @pytest.mark.parametrize(
        "relative",
        [
            "agent_identity.json",
            "atlas_device_key.bin",
            ".env",
            "sub/.env.production",
            "keys/id_ed25519",
            "vault.kdbx",
            "certs/server.pem",
            ".ssh/config",
        ],
    )
    def test_protected_patterns_are_refused_even_inside_a_root(
        self, guard: PathGuard, workspace: Path, relative: str
    ) -> None:
        target = workspace / "allowed" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(PathRefusedError) as exc:
            guard.check(str(target))
        assert exc.value.reason is RefusalReason.PATH_DENYLISTED

    def test_the_agents_own_key_is_unreadable(self, workspace: Path) -> None:
        # ATLAS must not be able to read the credential that authorises ATLAS.
        guard = PathGuard([workspace / "allowed"])
        assert not guard.is_allowed(str(workspace / "allowed" / "agent_identity.json"))

    def test_extra_patterns_can_be_added(self, workspace: Path) -> None:
        guard = PathGuard([workspace / "allowed"], extra_denied=("*/taxes/*",))
        (workspace / "allowed" / "taxes").mkdir()
        assert not guard.is_allowed(str(workspace / "allowed" / "taxes" / "2026.pdf"))
        assert guard.is_allowed(str(workspace / "allowed" / "notes.txt"))

    def test_ordinary_documents_are_unaffected(self, guard: PathGuard, workspace: Path) -> None:
        for name in ("report.docx", "photo.jpg", "notes.txt", "data.csv"):
            assert guard.is_allowed(str(workspace / "allowed" / name))


class TestConstruction:
    def test_at_least_one_root_is_required(self) -> None:
        with pytest.raises(ValueError, match="at least one allowed root"):
            PathGuard([])

    def test_relative_root_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            PathGuard(["relative/path"])

    def test_multiple_roots_are_all_honoured(self, tmp_path: Path) -> None:
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        guard = PathGuard([first, second])

        assert guard.is_allowed(str(first / "a.txt"))
        assert guard.is_allowed(str(second / "b.txt"))
        assert not guard.is_allowed(str(tmp_path / "three" / "c.txt"))
