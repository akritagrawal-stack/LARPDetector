"""Offline tests for detective.pipeline. No network.

Bug 2 (from the live stress test on Gregor Zunic / Browser Use, YC W25):
printing a name with non-ASCII characters crashed with a UnicodeEncodeError
on a narrow console codepage (Windows cp1252), killing the whole run.
Fixed with pipeline.safe_print (used by _default_progress here, and by
__main__._print_dossier), plus __main__.main() reconfiguring stdout/stderr
to UTF-8 at CLI entry.

No em dashes in this file (house rule).
"""

from __future__ import annotations

import io

from detective.models import Claim, Dossier
from detective.pipeline import _default_progress, safe_print

# The real name from the live stress test: "Gregor Zunic" (Z with caron,
# c with caron). Written via escapes so this source file never carries a
# literal non-ASCII byte sequence that could itself trip an editor/encoding
# issue.
_ACCENTED_NAME = "Gregor Žunič"


def _cp1252_stream() -> io.TextIOWrapper:
    """A text stream that raises UnicodeEncodeError on an unencodable
    character, the same way a real Windows console does under the cp1252
    codepage, so these tests can reproduce the crash without needing an
    actual Windows console.
    """
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_cp1252_stream_assumption_holds():
    # Confirms the accented name really is unencodable in cp1252 (the
    # "c with caron" is outside that codepage), i.e. this test setup
    # actually reproduces the live crash rather than testing nothing.
    try:
        _ACCENTED_NAME.encode("cp1252")
        assert False, "test setup assumption failed: name should not encode in cp1252"
    except UnicodeEncodeError:
        pass


def test_plain_print_would_have_raised_on_the_narrow_stream():
    # Documents the bug directly: a bare print() to a cp1252 stream raises.
    stream = _cp1252_stream()
    try:
        print(_ACCENTED_NAME, file=stream)
        assert False, "expected UnicodeEncodeError from a plain print()"
    except UnicodeEncodeError:
        pass


def test_safe_print_does_not_raise_on_accented_name():
    stream = _cp1252_stream()
    safe_print(_ACCENTED_NAME, file=stream)  # must not raise
    stream.flush()


def test_safe_print_writes_replaced_text_instead_of_crashing():
    stream = _cp1252_stream()
    safe_print(_ACCENTED_NAME, file=stream)
    stream.flush()
    written = stream.buffer.getvalue().decode("cp1252")
    assert "Gregor" in written


def test_default_progress_status_event_with_accented_text_does_not_raise():
    _default_progress("status", f"scanning {_ACCENTED_NAME}'s profile")


def test_default_progress_claim_event_with_accented_assertion_does_not_raise():
    claim = Claim(
        type="identity",
        assertion=f"{_ACCENTED_NAME} is the founder of Browser Use.",
    )
    _default_progress("claim", claim)


def test_default_progress_verdict_event_with_accented_identity_does_not_raise():
    d = Dossier(profile_url="https://example.test/", identity={"name": _ACCENTED_NAME})
    _default_progress("verdict", d)


def test_default_progress_never_raises_on_a_narrow_console_stdout(monkeypatch):
    # The exact live-crash shape: stdout itself is a narrow-codepage stream
    # (simulating the real Windows cp1252 console), and _default_progress is
    # called the way pipeline.run actually calls it, with no explicit `file`
    # kwarg. Before the fix, this raised UnicodeEncodeError and killed the
    # whole run partway through.
    stream = _cp1252_stream()
    monkeypatch.setattr("sys.stdout", stream)

    _default_progress("status", f"scanning {_ACCENTED_NAME}'s profile")
    claim = Claim(type="identity", assertion=f"{_ACCENTED_NAME} is the founder of Browser Use.")
    _default_progress("claim", claim)
    d = Dossier(profile_url="https://example.test/", identity={"name": _ACCENTED_NAME})
    _default_progress("verdict", d)
