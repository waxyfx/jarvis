"""What has to stay true for the backend to run in a container.

The backend is meant to move to a small always-on host, where there is no
microphone, no GPU, no Windows and none of the voice stack. The image is built
from `atlas-shared` and `atlas-backend` only — see `infra/backend.Dockerfile` —
so any import that reaches outside those two packages is a build that succeeds
here and fails there.

This file is the cheap version of that check: static, fast, and it runs on every
commit rather than on every deployment. It cannot replace actually building the
image, and says so.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SOURCE = BACKEND / "src" / "atlas_backend"

#: Packages the container image does not contain. Importing one at module level
#: makes the backend unimportable there, and the failure arrives as a crash loop
#: on a host with nobody watching.
ABSENT_FROM_THE_IMAGE = ("atlas_voice", "atlas_agent")


def python_files() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Every module name imported at the top level of a file.

    Deliberately not following `if TYPE_CHECKING:` — a type-only import costs
    nothing at runtime, and forbidding it would push people towards strings.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                continue
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])

    return found


class TestTheImageCanContainIt:
    def test_the_backend_never_imports_the_voice_or_agent_packages(self) -> None:
        """They are Windows-only and enormous — torch, sherpa, sounddevice. A
        container that had to carry them would be a different kind of artefact
        for no gain, and the backend has no business touching either."""
        offenders: list[str] = []

        for path in python_files():
            reached = imported_modules(path) & set(ABSENT_FROM_THE_IMAGE)
            if reached:
                offenders.append(f"{path.relative_to(SOURCE)} imports {sorted(reached)}")

        assert not offenders, "\n".join(offenders)

    def test_every_third_party_import_is_a_declared_dependency(self) -> None:
        """A dependency that is installed here because something else pulled it
        in is a dependency the image will not have. The lockfile is resolved
        from the declarations, not from what happens to be present."""
        declared = _declared_dependencies()
        strays: list[str] = []

        for path in python_files():
            for module in imported_modules(path):
                if module in _STANDARD_LIBRARY or module.startswith("atlas_"):
                    continue
                if _distribution_for(module) not in declared:
                    strays.append(f"{path.relative_to(SOURCE)} imports {module}")

        assert not strays, "\n".join(strays)


def _declared_dependencies() -> set[str]:
    with (BACKEND / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return {
        _normalise(requirement)
        for requirement in project.get("dependencies", [])
    }


def _normalise(requirement: str) -> str:
    for separator in (">=", "==", "<=", "~=", ">", "<", "[", ";"):
        requirement = requirement.split(separator)[0]
    return requirement.strip().replace("-", "_").lower()


#: Import names that differ from the distribution that provides them.
_IMPORT_TO_DISTRIBUTION = {
    "jwt": "pyjwt",
    "sqlalchemy": "sqlalchemy",
    "pydantic_settings": "pydantic_settings",
    "alembic": "alembic",
    "pydantic": "pydantic",
}


def _distribution_for(module: str) -> str:
    return _IMPORT_TO_DISTRIBUTION.get(module, module).replace("-", "_").lower()


#: Enough of the standard library to keep this test honest without listing all
#: of it. Anything genuinely missing here shows up as a false failure naming the
#: module, which is a two-second fix rather than a mystery.
_STANDARD_LIBRARY = {
    "__future__", "abc", "argparse", "ast", "asyncio", "base64", "collections",
    "contextlib", "contextvars", "copy", "csv", "dataclasses", "datetime",
    "decimal", "enum", "functools", "hashlib", "hmac", "html", "importlib",
    "inspect", "io", "ipaddress", "itertools", "json", "logging", "math",
    "os", "pathlib", "platform", "random", "re", "secrets", "signal", "socket",
    "string", "sys", "textwrap", "threading", "time", "tomllib", "traceback",
    "types", "typing", "unicodedata", "urllib", "uuid", "warnings", "zoneinfo",
}


class TestTheDeclarationItself:
    def test_tzdata_is_declared(self) -> None:
        """Not academic. Windows ships no IANA database and neither does a slim
        Debian image; the proactive scheduler resolves the owner's zone when it
        is constructed, so without this the backend does not start at all."""
        assert "tzdata" in _declared_dependencies()

    @pytest.mark.parametrize("required", ["fastapi", "uvicorn", "sqlalchemy", "asyncpg", "httpx"])
    def test_the_obvious_ones_are_declared(self, required: str) -> None:
        assert required in _declared_dependencies()
