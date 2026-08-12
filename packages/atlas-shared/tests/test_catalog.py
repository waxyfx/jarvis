"""Invariants the real tool catalogue must hold.

These are the checks that would have caught a dangerous manifest before it
shipped, so they assert properties rather than restating each declaration.
"""

import pytest

from atlas_shared.enums import RiskLevel
from atlas_shared.tools.catalog import CATALOG
from atlas_shared.tools.manifest import RiskContext, ToolManifest

ROOTS = RiskContext(
    allowed_roots=(r"C:\Users\serik\Desktop",),
    executable_roots=(r"C:\Program Files",),
)


def test_catalogue_is_not_empty() -> None:
    assert CATALOG.names()


@pytest.mark.parametrize("manifest", CATALOG.all(), ids=lambda m: m.name)
class TestEveryManifest:
    def test_name_is_dotted_lowercase(self, manifest: ToolManifest) -> None:
        assert manifest.name.islower()
        assert "." in manifest.name

    def test_has_a_summary(self, manifest: ToolManifest) -> None:
        assert manifest.summary.strip()

    def test_timeout_is_bounded(self, manifest: ToolManifest) -> None:
        assert 0 < manifest.timeout_s <= 300

    def test_declares_capabilities(self, manifest: ToolManifest) -> None:
        assert manifest.requires_capabilities

    def test_base_risk_is_never_deny(self, manifest: ToolManifest) -> None:
        # DENY is a verdict produced by a rule, not a resting state for a tool.
        assert manifest.base_risk is not RiskLevel.DENY

    def test_descriptor_round_trips(self, manifest: ToolManifest) -> None:
        descriptor = manifest.to_descriptor()
        assert descriptor.name == manifest.name
        assert descriptor.args_schema["type"] == "object"

    def test_irreversible_tools_are_at_least_medium(self, manifest: ToolManifest) -> None:
        if not manifest.reversible:
            assert manifest.base_risk.rank >= RiskLevel.MEDIUM.rank

    def test_rules_reference_declared_arguments(self, manifest: ToolManifest) -> None:
        declared = set(manifest.args_model.model_fields)
        for rule in manifest.escalations:
            for condition in rule.conditions:
                root_field = condition.field.split(".")[0]
                assert root_field in declared, (
                    f"{manifest.name}: rule {rule.reason!r} tests undeclared "
                    f"argument {condition.field!r}"
                )


def test_duplicate_registration_is_refused() -> None:
    from atlas_shared.tools.catalog import ToolCatalog

    catalogue = ToolCatalog()
    manifest = CATALOG.get("system.metrics")
    catalogue.register(manifest)
    with pytest.raises(RuntimeError, match="duplicate tool registration"):
        catalogue.register(manifest)


def test_unknown_tool_lookup_raises() -> None:
    assert not CATALOG.has("does.not.exist")
    with pytest.raises(KeyError, match="unknown tool"):
        CATALOG.get("does.not.exist")


class TestFilesystemDeleteRisk:
    """fs.delete is the highest-consequence tool declared in M1."""

    def setup_method(self) -> None:
        self.tool = CATALOG.get("fs.delete")

    def test_single_in_scope_file_is_medium(self) -> None:
        result = self.tool.assess(
            {"paths": (r"C:\Users\serik\Desktop\note.txt",), "recursive": False}, ROOTS
        )
        assert result.level is RiskLevel.MEDIUM

    def test_recursive_escalates_to_high(self) -> None:
        result = self.tool.assess(
            {"paths": (r"C:\Users\serik\Desktop\project",), "recursive": True}, ROOTS
        )
        assert result.level is RiskLevel.HIGH

    def test_bulk_escalates_to_high(self) -> None:
        paths = tuple(rf"C:\Users\serik\Desktop\f{index}.txt" for index in range(21))
        assert self.tool.assess({"paths": paths, "recursive": False}, ROOTS).level is RiskLevel.HIGH

    def test_out_of_scope_path_is_denied(self) -> None:
        result = self.tool.assess(
            {"paths": (r"C:\Windows\System32\drivers\etc\hosts",), "recursive": False}, ROOTS
        )
        assert result.level is RiskLevel.DENY

    def test_traversal_out_of_scope_is_denied(self) -> None:
        result = self.tool.assess(
            {"paths": (r"C:\Users\serik\Desktop\..\.ssh\id_ed25519",), "recursive": False}, ROOTS
        )
        assert result.level is RiskLevel.DENY

    def test_deny_wins_over_high(self) -> None:
        result = self.tool.assess({"paths": (r"C:\Windows\System32",), "recursive": True}, ROOTS)
        assert result.level is RiskLevel.DENY

    def test_delete_is_declared_reversible(self) -> None:
        # The executor moves items to the Recycle Bin; ATLAS has no hard delete.
        assert self.tool.reversible


class TestOtherToolRisks:
    def test_launching_a_known_application_is_low(self) -> None:
        result = CATALOG.get("app.launch").assess({"name": "chrome"}, ROOTS)
        assert result.level is RiskLevel.LOW

    def test_launching_an_unknown_binary_is_high(self) -> None:
        result = CATALOG.get("app.launch").assess(
            {"name": "thing", "executable_path": r"C:\Users\serik\Downloads\thing.exe"}, ROOTS
        )
        assert result.level is RiskLevel.HIGH

    def test_forced_close_escalates(self) -> None:
        tool = CATALOG.get("app.close")
        assert tool.assess({"name": "code", "force": False}, ROOTS).level is RiskLevel.MEDIUM
        assert tool.assess({"name": "code", "force": True}, ROOTS).level is RiskLevel.HIGH

    def test_opening_a_document_is_low_but_a_script_is_high(self) -> None:
        tool = CATALOG.get("fs.open")
        document = {"path": r"C:\Users\serik\Desktop\notes.txt"}
        script = {"path": r"C:\Users\serik\Desktop\payload.ps1"}
        assert tool.assess(document, ROOTS).level is RiskLevel.LOW
        assert tool.assess(script, ROOTS).level is RiskLevel.HIGH

    def test_search_outside_roots_is_denied(self) -> None:
        tool = CATALOG.get("fs.search")
        assert tool.assess({"query": "*", "root": r"C:\Windows"}, ROOTS).level is RiskLevel.DENY

    def test_reading_system_metrics_is_low(self) -> None:
        assert CATALOG.get("system.metrics").assess({}, ROOTS).level is RiskLevel.LOW
