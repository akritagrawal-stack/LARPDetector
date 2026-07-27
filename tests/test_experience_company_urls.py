"""Offline tests: an experience row carries LinkedIn's OWN link for the company.

Why this exists: on a person scan, a founder claim arrives as a bare employer
STRING, and the three connectors that can actually assess a live product
(wayback, domain_age, techstack) are URL-keyed. LinkedIn already renders a link
on the exact row that produced the claim; until now both parsers read text only
and threw every href away. This is the cheapest, least ambiguous candidate the
product-site resolver can be handed, which is exactly why it is harvested first.

Both DOMs are covered on purpose. The Aero parser is the live one, but the
legacy artdeco/pvs-list parser is still reachable, and a company_url that only
appears on one of them is a feature that silently half-exists.

Handwritten HTML, no network, no Playwright. Synthetic company names only.
No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from detective.extract_linkedin import parse_experience_html


def _aero(items_html: str) -> str:
    return f"""
    <div data-component-type="LazyColumn" data-testid="lazy-column">
      <p>Experience</p>
      {items_html}
    </div>
    """


def _legacy(items_html: str) -> str:
    return f"""
    <section id="experience-section" data-section="experience">
      <ul>{items_html}</ul>
    </section>
    """


# ---------------------------------------------------------------------------
# Aero DOM
# ---------------------------------------------------------------------------


def test_aero_entry_carries_the_company_url():
    html = _aero(
        """
        <div componentkey="entity-collection-item--1">
          <a href="https://www.linkedin.com/company/acme-widgets/">Acme Widgets</a>
          <p>Founder</p>
          <p>Acme Widgets &middot; Full-time</p>
          <p>Jan 2021 - Present &middot; 3 yrs</p>
        </div>
        """
    )
    entries = parse_experience_html(html)
    assert len(entries) == 1
    assert entries[0]["company"] == "Acme Widgets"
    assert entries[0]["company_url"] == "https://www.linkedin.com/company/acme-widgets/"


def test_aero_entry_without_a_link_has_no_company_url_key():
    # Shape discipline: no empty-string keys, so a linkless row is byte-identical
    # to what every existing caller already expects.
    html = _aero(
        """
        <div componentkey="entity-collection-item--1">
          <p>Founder</p>
          <p>Acme Widgets &middot; Full-time</p>
          <p>Jan 2021 - Present &middot; 3 yrs</p>
        </div>
        """
    )
    entries = parse_experience_html(html)
    assert len(entries) == 1
    assert "company_url" not in entries[0]


def test_aero_ignores_a_people_link_and_takes_the_company_one():
    # A row links plenty of things (the person who referred you, a school, a
    # post). Only /company/ is the employer's own page.
    html = _aero(
        """
        <div componentkey="entity-collection-item--1">
          <a href="https://www.linkedin.com/in/someone-else/">Someone Else</a>
          <a href="https://www.linkedin.com/company/acme-widgets/">Acme Widgets</a>
          <p>Founder</p>
          <p>Acme Widgets</p>
          <p>Jan 2021 - Present &middot; 3 yrs</p>
        </div>
        """
    )
    assert parse_experience_html(html)[0]["company_url"] == (
        "https://www.linkedin.com/company/acme-widgets/"
    )


def test_aero_grouped_subroles_all_inherit_the_group_company_url():
    # A promoted-role block: one employer, several roles. The link sits on the
    # group header, so every sub-role must carry it or the founder's later role
    # resolves to nothing.
    html = _aero(
        """
        <div componentkey="entity-collection-item--1">
          <a href="https://www.linkedin.com/company/northwind-labs/">Northwind Labs</a>
          <p>Northwind Labs</p>
          <p>Full-time &middot; 4 yrs 2 mos</p>
          <div componentkey="entity-collection-item--1a">
            <p>Staff Engineer</p>
            <p>Mar 2023 - Present &middot; 1 yr</p>
            <p>Senior Engineer</p>
            <p>Jan 2021 - Mar 2023 &middot; 2 yrs 2 mos</p>
          </div>
        </div>
        """
    )
    entries = parse_experience_html(html)
    assert len(entries) == 2
    assert {e["company"] for e in entries} == {"Northwind Labs"}
    assert all(
        e["company_url"] == "https://www.linkedin.com/company/northwind-labs/"
        for e in entries
    )


def test_aero_each_entry_gets_its_own_company_url():
    html = _aero(
        """
        <div componentkey="entity-collection-item--1">
          <a href="https://www.linkedin.com/company/acme-widgets/">Acme Widgets</a>
          <p>Founder</p>
          <p>Acme Widgets</p>
          <p>Jan 2021 - Present &middot; 3 yrs</p>
        </div>
        <div componentkey="entity-collection-item--2">
          <a href="https://www.linkedin.com/company/northwind-labs/">Northwind Labs</a>
          <p>Engineer</p>
          <p>Northwind Labs</p>
          <p>Jun 2018 - Dec 2020 &middot; 2 yrs 7 mos</p>
        </div>
        """
    )
    entries = parse_experience_html(html)
    assert [e.get("company_url") for e in entries] == [
        "https://www.linkedin.com/company/acme-widgets/",
        "https://www.linkedin.com/company/northwind-labs/",
    ]


# ---------------------------------------------------------------------------
# Legacy DOM (artdeco / pvs-list). Same contract, or the feature half-exists.
# ---------------------------------------------------------------------------


def test_legacy_entry_carries_the_company_url():
    html = _legacy(
        """
        <li class="artdeco-list__item">
          <a href="https://www.linkedin.com/company/acme-widgets/">
            <span aria-hidden="true">Founder</span>
          </a>
          <span aria-hidden="true">Acme Widgets &middot; Full-time</span>
          <span aria-hidden="true">Jan 2021 - Present &middot; 3 yrs</span>
        </li>
        """
    )
    entries = parse_experience_html(html)
    assert len(entries) == 1
    assert entries[0]["company_url"] == "https://www.linkedin.com/company/acme-widgets/"


def test_legacy_entry_without_a_link_has_no_company_url_key():
    html = _legacy(
        """
        <li class="artdeco-list__item">
          <span aria-hidden="true">Founder</span>
          <span aria-hidden="true">Acme Widgets &middot; Full-time</span>
          <span aria-hidden="true">Jan 2021 - Present &middot; 3 yrs</span>
        </li>
        """
    )
    assert "company_url" not in parse_experience_html(html)[0]


def test_malformed_href_never_breaks_the_parse():
    # Never-raise discipline: a junk anchor degrades to "no url", never an
    # exception that costs the whole experience section.
    html = _aero(
        """
        <div componentkey="entity-collection-item--1">
          <a href="::not a url::">junk</a>
          <p>Founder</p>
          <p>Acme Widgets</p>
          <p>Jan 2021 - Present &middot; 3 yrs</p>
        </div>
        """
    )
    entries = parse_experience_html(html)
    assert len(entries) == 1
    assert entries[0]["company"] == "Acme Widgets"
