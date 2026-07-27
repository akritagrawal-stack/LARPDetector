"""Offline tests for detective.__main__. No network.

Bug 2 (from the live stress test on Gregor Zunic / Browser Use, YC W25):
_print_dossier crashed with UnicodeEncodeError when the dossier's identity
carried non-ASCII characters, on a narrow console codepage (Windows
cp1252). Fixed by routing every print through pipeline.safe_print, plus
main() reconfiguring stdout/stderr to UTF-8 at CLI entry (see
_reconfigure_utf8_streams).

No em dashes in this file (house rule).
"""

from __future__ import annotations

import io

from detective.__main__ import _print_dossier, _reconfigure_utf8_streams
from detective.models import Claim, Dossier, EvidenceTier

# "Gregor Zunic" (Z with caron, c with caron), written as-is; see
# tests/test_pipeline.py for why this specific name reproduces the crash
# (the "c with caron" is outside the cp1252 codepage).
_ACCENTED_NAME = "Gregor Žunič"


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def _accented_dossier() -> Dossier:
    d = Dossier(
        profile_url="https://www.linkedin.com/in/gregorzunic/",
        scan_type="person",
        identity={
            "name": _ACCENTED_NAME,
            "headline": f"Founder at Browser Use ({_ACCENTED_NAME}'s company)",
            "current_company": "Browser Use",
            "location": "",
        },
    )
    d.claims = [
        Claim(
            type="identity",
            assertion=f"{_ACCENTED_NAME} is the founder of Browser Use.",
            tier=EvidenceTier.CONFIRMED,
            evidence=[{"source_url": "https://example.test/a", "snippet": "ok"}],
        )
    ]
    d.founder_larp_score = 10
    d.larp_score = 10
    d.verdict = f"{_ACCENTED_NAME} appears to be a real founder."
    return d


def test_print_dossier_with_accented_identity_does_not_raise_on_default_stdout(capsys):
    _print_dossier(_accented_dossier())
    out = capsys.readouterr().out
    assert "Gregor" in out


def test_print_dossier_never_raises_on_a_narrow_console_stdout(monkeypatch):
    # The exact live-crash shape: stdout itself is a narrow-codepage stream.
    # Before the fix, this raised UnicodeEncodeError partway through printing
    # the dossier and killed the run.
    stream = _cp1252_stream()
    monkeypatch.setattr("sys.stdout", stream)

    _print_dossier(_accented_dossier())  # must not raise
    stream.flush()


def test_reconfigure_utf8_streams_never_raises_even_without_reconfigure(monkeypatch):
    class _NoReconfigureStream:
        """A stream with no reconfigure() at all, e.g. an older Python or a
        stream a test/host swapped in. Must degrade to a no-op, never raise.
        """

    monkeypatch.setattr("sys.stdout", _NoReconfigureStream())
    monkeypatch.setattr("sys.stderr", _NoReconfigureStream())

    _reconfigure_utf8_streams()  # must not raise
