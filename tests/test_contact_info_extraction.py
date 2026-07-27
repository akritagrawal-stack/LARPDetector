"""Offline tests for the LinkedIn contact-info overlay parse.

Two PURE functions, same discipline as parse_posts_html / parse_experience_html:
small handwritten HTML strings, no network, no Playwright.

  parse_contact_info_html(html) -> {"websites", "github_url", "twitter_url",
      "email"}: pulls the profile's OWN declared external links out of the
      contact-info overlay, resolving LinkedIn's outbound redirect wrappers and
      classifying purely by host (never by hashed CSS class). Never raises.

  _build_hints(contact) -> the connector hint dict the source connectors already
      look for (domain / personal_site / website) plus github_login. Empty
      entries are omitted entirely, so a linkless profile yields {}.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from detective.extract_linkedin import parse_contact_info_html, _build_hints


# ---------------------------------------------------------------------------
# parse_contact_info_html
# ---------------------------------------------------------------------------


def test_parse_contact_info_extracts_github_and_site():
    html = """
    <section class="pv-contact-info">
      <a href="https://www.linkedin.com/in/jordan-rivera-synthetic">linkedin.com/in/jordan-rivera-synthetic</a>
      <a href="https://github.com/JordanRivera-dev">github.com/JordanRivera-dev</a>
      <a href="https://jordan-rivera.example">jordan-rivera.example</a>
    </section>
    """
    result = parse_contact_info_html(html)
    assert result == {
        "github_url": "https://github.com/JordanRivera-dev",
        "websites": ["https://jordan-rivera.example"],
        "twitter_url": "",
        "email": "",
    }


def test_parse_contact_info_resolves_redirect_wrapper():
    html = (
        '<section class="pv-contact-info">'
        '<a href="https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fjordan-rivera.example&amp;urlhash=abc">'
        "my site</a>"
        "</section>"
    )
    result = parse_contact_info_html(html)
    assert result["websites"] == ["https://jordan-rivera.example"]


def test_parse_contact_info_never_raises():
    for bad in ("", "<html>", "<a href='::bad url::'>x</a>"):
        result = parse_contact_info_html(bad)
        assert result == {
            "github_url": "",
            "websites": [],
            "twitter_url": "",
            "email": "",
        }


# ---------------------------------------------------------------------------
# _build_hints
# ---------------------------------------------------------------------------


def test_build_hints_maps_connector_keys():
    contact = {
        "github_url": "https://github.com/JordanRivera-dev",
        "websites": ["https://jordan-rivera.example"],
        "twitter_url": "",
        "email": "",
    }
    hints = _build_hints(contact)
    assert hints == {
        "github_login": "JordanRivera-dev",
        "personal_site": "https://jordan-rivera.example",
        "website": "https://jordan-rivera.example",
        "domain": "jordan-rivera.example",
        "websites": ["https://jordan-rivera.example"],
    }
    # No empty-string values may leak in (the connectors treat "" as a real hint).
    assert all(v for v in hints.values())


# ---------------------------------------------------------------------------
# _build_hints: EVERY declared website survives.
#
# The singular keys (personal_site / website / domain) keep their exact
# first-website semantics because github.py:_HINT_KEYS reads them and a change
# there would silently re-point the github disambiguator. The full list rides
# along under a NEW key, because a founder's PRODUCT site is very often the
# second or third link on the row, and the product-site resolver cannot resolve
# a candidate that the extractor already threw away.
# ---------------------------------------------------------------------------


def test_build_hints_keeps_every_declared_website():
    contact = {
        "websites": [
            "https://janedoe.example",
            "https://acmewidgets.example",
            "https://blog.janedoe.example",
        ]
    }
    hints = _build_hints(contact)
    assert hints["websites"] == [
        "https://janedoe.example",
        "https://acmewidgets.example",
        "https://blog.janedoe.example",
    ]


def test_build_hints_singular_keys_still_mean_the_first_website():
    # Regression guard for the github disambiguator: widening the harvest must
    # not re-point personal_site / website / domain at some other link.
    contact = {"websites": ["https://janedoe.example", "https://acmewidgets.example"]}
    hints = _build_hints(contact)
    assert hints["personal_site"] == "https://janedoe.example"
    assert hints["website"] == "https://janedoe.example"
    assert hints["domain"] == "janedoe.example"


def test_build_hints_omits_websites_when_there_are_none():
    # Same discipline as every other key: no empty containers, so a linkless
    # profile still yields {} exactly.
    assert "websites" not in _build_hints({"github_url": "https://github.com/someone"})


def test_build_hints_github_repo_url_takes_login():
    contact = {"github_url": "https://github.com/JordanRivera-dev/cognition"}
    assert _build_hints(contact)["github_login"] == "JordanRivera-dev"


def test_build_hints_empty_contact_is_empty_dict():
    assert _build_hints({}) == {}
