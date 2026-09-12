"""Behavior contracts for plugin-guard dangerous-install diagnostics."""

from tools.plugin_guard import format_scan_report, should_allow_plugin_install
from tools.skills_guard import Finding, ScanResult


def test_dangerous_reason_counts_and_names_blocking_findings():
    result = ScanResult(
        skill_name="fixture-plugin",
        source="owner/repo",
        trust_level="community",
        verdict="dangerous",
        findings=[
            Finding(
                "hermes_config_mod_shell", "critical", "persistence",
                "runtime/setup.sh", 4, "echo x > config.yaml", "writes config",
            ),
            Finding(
                "unpinned_pip_install", "medium", "supply_chain",
                "README.md", 8, "pip install example", "unpinned dependency",
            ),
            Finding(
                "remote_fetch", "medium", "supply_chain",
                "README.md", 9, "curl https://example.test", "remote fetch",
            ),
        ],
    )

    allowed, reason = should_allow_plugin_install(result)

    assert allowed is False
    assert "1 critical of 3 findings" in reason
    assert "hermes_config_mod_shell" in reason
    assert "unpinned_pip_install" not in reason
    assert (
        "Decision: BLOCKED — Blocked (dangerous verdict, 1 critical of 3 findings"
        in format_scan_report(result)
    )


def test_plugin_report_uses_plugin_caution_policy():
    result = ScanResult(
        skill_name="fixture-plugin",
        source="owner/repo",
        trust_level="community",
        verdict="caution",
        findings=[
            Finding(
                "eval_string", "high", "obfuscation",
                "runtime/helper.py", 2, "eval('x')", "evaluates a string",
            ),
        ],
    )

    report = format_scan_report(result)

    assert (
        "Decision: NEEDS CONFIRMATION — Requires confirmation (caution verdict, 1 findings)"
        in report
    )