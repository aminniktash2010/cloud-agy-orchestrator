"""AC-11: static guard — the probe can never send a prompt.

The probe's subprocess argv allowlist is exactly
{agy models, agy --print /usage, agy --print /credits}. This test fails the
build if any argv gains -p/--prompt or any other subcommand, and asserts
_run_cmd is the module's only subprocess call site (so nothing can smuggle a
prompt past the allowlist assert).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import cli_probe  # noqa: E402

EXPECTED_ALLOWLIST = {
    ("agy", "models"),
    ("agy", "--print", "/usage"),
    ("agy", "--print", "/credits"),
}


def test_probe_argv_allowlist_is_exact():
    assert set(cli_probe.PROBE_ARGV_ALLOWLIST) == EXPECTED_ALLOWLIST


def test_no_prompt_sending_invocation_in_allowlist():
    for argv in cli_probe.PROBE_ARGV_ALLOWLIST:
        assert "-p" not in argv, f"prompt flag in probe argv: {argv}"
        assert "--prompt" not in argv, f"prompt flag in probe argv: {argv}"
        # only the three known inspection forms
        assert argv[0] == "agy"
        assert argv[1] in ("models", "--print")


def test_single_subprocess_call_site():
    """_run_cmd is the only place cli_probe spawns a subprocess, and it
    asserts argv membership in the allowlist before running."""
    src = Path(cli_probe.__file__).read_text()
    spawns = re.findall(r"subprocess\.(run|Popen|call|check_output)\b", src)
    assert spawns == ["run"], f"unexpected subprocess call sites: {spawns}"
    assert "assert argv in PROBE_ARGV_ALLOWLIST" in src
