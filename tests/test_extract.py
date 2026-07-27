"""Offline parser tests. MUST pass with no network and no LinkedIn login.

These exercise parse_experience_html against the saved fixture, which is the
spec for the parser. No em dashes (house rule).
"""

from __future__ import annotations

from pathlib import Path

import detective.extract_linkedin as extract_linkedin
from detective.extract_linkedin import (
    fetch_profile,
    parse_company_about_website_html,
    parse_experience_html,
    parse_education_html,
    parse_identity_html,
    _split_date_range,
    _company_from_current_role,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "experience_section.html"


def _entries():
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_experience_html(html)


def _entries_from(name: str):
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return parse_experience_html(html)


def test_fixture_exists():
    assert FIXTURE.exists(), "experience fixture is missing"


def test_entry_count():
    # 3 single roles + 2 grouped sub-roles = 5 entries.
    assert len(_entries()) == 5


def test_single_role_present():
    e = _entries()[0]
    assert e["title"] == "Senior Software Engineer"
    assert e["company"] == "Google"
    assert e["start_date"] == "Jan 2020"
    assert e["end_date"] == "Present"
    assert "Mountain View" in e["location"]


def test_single_role_closed_range():
    e = _entries()[1]
    assert e["title"] == "Software Engineer"
    assert e["company"] == "Stripe"
    assert e["start_date"] == "Jun 2017"
    assert e["end_date"] == "Dec 2019"
    assert "San Francisco" in e["location"]


def test_entry_with_no_dates():
    e = _entries()[2]
    assert e["title"] == "Founder"
    assert e["company"] == "Stealth Startup"
    assert e["start_date"] == ""
    assert e["end_date"] == ""


def test_grouped_multi_role_shares_company():
    entries = _entries()
    grouped = [e for e in entries if e["company"] == "Acme Corporation"]
    assert len(grouped) == 2
    titles = {e["title"] for e in grouped}
    assert titles == {"Engineering Manager", "Staff Engineer"}
    # Company must be inherited onto every nested role, not left blank.
    assert all(e["company"] == "Acme Corporation" for e in grouped)
    # And the header location should propagate.
    assert all("New York" in e["location"] for e in grouped)


def test_grouped_dates_parsed():
    entries = _entries()
    by_title = {e["title"]: e for e in entries}
    assert by_title["Engineering Manager"]["start_date"] == "Jan 2023"
    assert by_title["Engineering Manager"]["end_date"] == "Present"
    assert by_title["Staff Engineer"]["start_date"] == "Jun 2021"
    assert by_title["Staff Engineer"]["end_date"] == "Jan 2023"


def test_empty_html_is_safe():
    assert parse_experience_html("") == []
    assert parse_experience_html("<div>nothing here</div>") == []
    assert parse_experience_html("   ") == []
    assert parse_experience_html("<not even closed") == []


# ---------------------------------------------------------------------------
# Date-range variants: year-only "Present" and month-less closed ranges.
# ---------------------------------------------------------------------------


def test_year_only_present_range():
    entries = _entries_from("experience_date_variants.html")
    e = next(e for e in entries if e["title"] == "Product Manager")
    assert e["company"] == "Meta"
    assert e["start_date"] == "2020"
    assert e["end_date"] == "Present"


def test_month_less_closed_range():
    entries = _entries_from("experience_date_variants.html")
    e = next(e for e in entries if e["title"] == "Data Analyst")
    assert e["company"] == "Amazon"
    assert e["start_date"] == "2019"
    assert e["end_date"] == "2021"


def test_split_date_range_unit_variants():
    # Direct unit coverage of every date-range shape the task calls out,
    # including the trailing "· N yrs M mos" duration that must be dropped.
    assert _split_date_range("Jan 2020 - Dec 2021") == ("Jan 2020", "Dec 2021")
    assert _split_date_range("2020 - Present") == ("2020", "Present")
    assert _split_date_range("Jan 2020 - Present · 2 yrs 3 mos") == (
        "Jan 2020",
        "Present",
    )
    assert _split_date_range("2019 - 2021") == ("2019", "2021")


# ---------------------------------------------------------------------------
# Grouped/nested promotion path: the highest-risk shape. 3 nested roles plus
# mixed date-range styles across siblings.
# ---------------------------------------------------------------------------


def test_grouped_promotion_3roles_shares_company_and_location():
    entries = _entries_from("experience_grouped_promotion_3roles.html")
    assert len(entries) == 3
    assert all(e["company"] == "Initech Corp" for e in entries)
    assert all("Austin" in e["location"] for e in entries)


def test_grouped_promotion_3roles_titles_and_dates():
    entries = _entries_from("experience_grouped_promotion_3roles.html")
    by_title = {e["title"]: e for e in entries}
    assert set(by_title) == {
        "Director of Engineering",
        "Senior Engineering Manager",
        "Engineering Manager",
    }
    assert by_title["Director of Engineering"]["start_date"] == "Mar 2024"
    assert by_title["Director of Engineering"]["end_date"] == "Present"
    assert by_title["Senior Engineering Manager"]["start_date"] == "2021"
    assert by_title["Senior Engineering Manager"]["end_date"] == "2024"
    assert by_title["Engineering Manager"]["start_date"] == "Jan 2019"
    assert by_title["Engineering Manager"]["end_date"] == "Dec 2020"


def test_nested_skill_bullets_are_not_mistaken_for_grouped_roles():
    # A nested <ul> under a single position (e.g. a skills pill list) has no
    # bold title marker on its <li> items, so it must NOT be treated as a
    # grouped/multi-role company header, and its text must not bleed into
    # the entry's location field.
    entries = _entries_from("experience_single_with_skill_bullets.html")
    assert len(entries) == 1
    e = entries[0]
    assert e["title"] == "Backend Engineer"
    assert e["company"] == "Netflix"
    assert e["start_date"] == "Jan 2018"
    assert e["end_date"] == "Mar 2022"
    assert e["location"] == ""


# ---------------------------------------------------------------------------
# Degrade to partial data: title-only entries and malformed siblings.
# ---------------------------------------------------------------------------


def test_title_only_entry_degrades_to_partial_data():
    entries = _entries_from("experience_title_only_and_malformed.html")
    # The malformed sibling <li> (no bold/title, no aria-hidden content) is
    # silently skipped; it must not prevent the good entry from parsing.
    assert len(entries) == 1
    e = entries[0]
    assert e["title"] == "Independent Consultant"
    assert e["company"] == ""
    assert e["start_date"] == ""
    assert e["end_date"] == ""
    assert e["location"] == ""


def test_one_raising_entry_does_not_kill_the_whole_parse(monkeypatch):
    # Directly exercise the try/except around the per-item loop: force
    # _parse_item to raise for exactly one of the top-level <li> items and
    # confirm the entries from the other, well-formed <li> items still come
    # back instead of the whole parse blowing up.
    real_parse_item = extract_linkedin._parse_item
    calls = {"n": 0}

    def flaky_parse_item(li):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated malformed entry")
        return real_parse_item(li)

    monkeypatch.setattr(extract_linkedin, "_parse_item", flaky_parse_item)
    entries = _entries()
    # 3 single roles + 2 grouped sub-roles = 5 normally; the first top-level
    # <li> (the "Present" Google entry) raised and was dropped, so 5 - 1 = 4
    # remain, all from the still-good entries.
    assert len(entries) == 4
    assert all(e["title"] != "" for e in entries)


def test_malformed_html_structures_never_raise():
    # Assorted structurally odd inputs that must degrade to [] rather than
    # raise, exercising the defensive wrapping in parse_experience_html.
    assert parse_experience_html("<html><body></body></html>") == []
    assert parse_experience_html('<li class="artdeco-list__item"></li>') == []
    assert (
        parse_experience_html(
            '<section data-section="experience"><ul><li class="artdeco-list__item">'
            '<div class="t-bold"></div></li></ul></section>'
        )
        == []
    )


# ---------------------------------------------------------------------------
# Live-fetch gate stays untouched: confirm it refuses before any network or
# lazy import happens, so the offline test suite never risks the network.
# ---------------------------------------------------------------------------


def test_fetch_profile_refuses_without_live_flag():
    try:
        fetch_profile("https://www.linkedin.com/in/someone/", live=False)
        assert False, "fetch_profile should have raised with live=False"
    except RuntimeError as exc:
        assert "live=False" in str(exc) or "live" in str(exc).lower()


def test_live_session_defaults_to_in_repo_implementation(monkeypatch):
    import detective.extract_linkedin as extract_linkedin

    monkeypatch.delenv("LINKEDIN_HUMAN_DIR", raising=False)
    module = extract_linkedin._import_human_session()

    assert module._builtin is True
    assert module.HumanSession is extract_linkedin._BuiltinHumanSession


def test_company_about_parser_recovers_official_website_and_skips_socials():
    html = """
    <a href="https://www.linkedin.com/company/acme/">LinkedIn</a>
    <a href="https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Facme.example%2Fapp">
      Website
    </a>
    <a href="https://x.com/acme">X</a>
    """

    assert parse_company_about_website_html(html) == "https://acme.example/app"


# ---------------------------------------------------------------------------
# Live "Aero" DOM: the current LinkedIn React frontend (hashed atomic CSS
# classes, plain <p> text, no artdeco/pvs-list, no aria-hidden dual-render).
# Fixtures are hand-built to match that exact tag/attribute shape for a
# synthetic founder profile (Jordan Rivera, Northwind Robotics), not a live
# capture of any real person's page. These are the spec for the current
# parser; the synthetic fixtures above cover the legacy DOM the dispatcher
# still falls back to.
# ---------------------------------------------------------------------------

LIVE_EXPERIENCE = FIXTURES / "live_experience_sample_founder.html"
LIVE_EDUCATION = FIXTURES / "live_education_sample_founder.html"
LIVE_IDENTITY = FIXTURES / "live_identity_sample_founder.html"

# Ground truth for the synthetic Jordan Rivera profile (title, company, start, end).
_JORDAN_EXPERIENCE = [
    ("Cofounder & CEO", "Northwind Robotics", "Aug 2017", "Present"),
    ("Garage Builder", "My Garage", "Feb 2013", "Aug 2017"),
    ("Hardware Engineer", "Vantage Labs", "Jan 2010", "Feb 2013"),
    ("Systems Test Engineer", "Talon Aerospace", "May 2006", "Dec 2009"),
    ("Test Engineering Intern", "Talon Aerospace", "Jun 2005", "Aug 2005"),
    ("Intern", "Bramwell Engineering", "Jun 2002", "Aug 2002"),
]


def _live_experience():
    return parse_experience_html(LIVE_EXPERIENCE.read_text(encoding="utf-8"))


def _live_education():
    return parse_education_html(LIVE_EDUCATION.read_text(encoding="utf-8"))


def test_live_fixtures_exist():
    assert LIVE_EXPERIENCE.exists()
    assert LIVE_EDUCATION.exists()
    assert LIVE_IDENTITY.exists()


def test_live_experience_count():
    # All six roles must come back, no more (no PYMK/other-people bleed-in).
    assert len(_live_experience()) == 6


def test_live_experience_all_roles_titles_companies_dates():
    entries = _live_experience()
    got = [
        (e["title"], e["company"], e["start_date"], e["end_date"]) for e in entries
    ]
    assert got == _JORDAN_EXPERIENCE


def test_live_experience_present_role_and_location():
    e = _live_experience()[0]
    assert e["title"] == "Cofounder & CEO"
    assert e["company"] == "Northwind Robotics"
    assert e["start_date"] == "Aug 2017"
    assert e["end_date"] == "Present"
    assert "Greater Denver Area" in e["location"]


def test_live_experience_description_not_mistaken_for_location():
    # The Talon Aerospace intern role is followed immediately by a free-text
    # description (no location line). That paragraph must not land in the
    # location field.
    entries = _live_experience()
    intern = next(
        e for e in entries if e["title"] == "Test Engineering Intern"
    )
    assert intern["company"] == "Talon Aerospace"
    assert intern["location"] == ""


def test_live_education_both_degrees():
    entries = _live_education()
    assert len(entries) == 2
    by_school = {e["school"]: e for e in entries}
    cascade = by_school["Cascade State University"]
    assert "Electrical and Electronics Engineering" in cascade["degree"]
    assert cascade["start_date"] == "2006"
    assert cascade["end_date"] == "2008"
    col = by_school["Piermont University"]
    assert "Electrical Engineering" in col["degree"]
    assert col["start_date"] == "2001"
    assert col["end_date"] == "2006"


def test_live_education_ignores_trailing_page_noise():
    # The education section is trailed by ad / "people you may know" / footer
    # copy in the raw page; strict date-range detection must keep it out.
    entries = _live_education()
    schools = {e["school"] for e in entries}
    assert schools == {"Cascade State University", "Piermont University"}


def test_live_identity_name_headline_company():
    idn = parse_identity_html(
        LIVE_IDENTITY.read_text(encoding="utf-8"),
        "https://linkedin.com/in/jordan-rivera",
    )
    assert idn["name"] == "Jordan Rivera"
    assert idn["headline"] == "Co-founder & CEO at Northwind Robotics (Forge Labs '18)"
    assert "Northwind Robotics" in idn["current_company"]


def test_live_identity_falls_back_to_title_tag():
    # With no matching self-link handle, the name still comes from <title>.
    html = "<html><head><title>Jane Doe | LinkedIn</title></head><body></body></html>"
    idn = parse_identity_html(html, "https://linkedin.com/in/nomatch")
    assert idn["name"] == "Jane Doe"


def test_live_identity_captures_profile_image_from_top_card():
    # The Aero top-card anchor carries a real media.licdn.com profile photo
    # <img> alongside the name/headline <p> lines; MINIMAL capture only.
    idn = parse_identity_html(
        LIVE_IDENTITY.read_text(encoding="utf-8"),
        "https://linkedin.com/in/jordan-rivera",
    )
    assert idn["image"].startswith("https://media.licdn.com/")


def test_identity_image_empty_when_not_present():
    # No fabrication: when the top card has no matching img, image stays "".
    html = "<html><head><title>Jane Doe | LinkedIn</title></head><body></body></html>"
    idn = parse_identity_html(html, "https://linkedin.com/in/nomatch")
    assert idn["image"] == ""


def test_aero_experience_never_raises_on_noise():
    # Aero path (no legacy markers) must degrade to [] on junk, not raise.
    assert parse_experience_html("<div><p>random</p><p>text</p></div>") == []
    assert parse_education_html("<main><p>nothing</p></main>") == []


# ---------------------------------------------------------------------------
# Bug 3: current_company must parse "Title @ Company" headlines too, not
# just "Title at Company".
# ---------------------------------------------------------------------------


def test_company_from_current_role_handles_at():
    assert (
        _company_from_current_role("Co-founder and COO at Northwind Robotics")
        == "Northwind Robotics"
    )


def test_company_from_current_role_handles_at_sign():
    assert (
        _company_from_current_role("CTO & Co-Founder @ Northwind Robotics")
        == "Northwind Robotics"
    )


def test_company_from_current_role_empty_when_neither_present():
    assert _company_from_current_role("Building things") == ""
    assert _company_from_current_role("") == ""


def test_company_from_current_role_rejects_multi_affiliation_headline():
    headline = "Returning SDE Intern @ AWS, Jane Street AMP | CS @ Texas A&M"
    assert _company_from_current_role(headline) == ""


# ---------------------------------------------------------------------------
# Bug 1: grouped/promoted-role Aero blocks. SYNTHETIC fixtures built to match
# the exact tag/attribute shape of a live stress-test failure (see each
# fixture file's own header comment for the reconstruction narrative). Not a
# capture of, or built from, any real person's profile.
# ---------------------------------------------------------------------------

GROUPED_RETAIL_CONSULTING = FIXTURES / "experience_aero_grouped_retail_consulting.html"
GROUPED_BIGTECH = FIXTURES / "experience_aero_grouped_bigtech.html"
GROUPED_LOCATION_EMPLOYMENT_TYPE = (
    FIXTURES / "experience_aero_grouped_location_employment_type.html"
)


def _grouped_retail_consulting_entries():
    return parse_experience_html(GROUPED_RETAIL_CONSULTING.read_text(encoding="utf-8"))


def _grouped_bigtech_entries():
    return parse_experience_html(GROUPED_BIGTECH.read_text(encoding="utf-8"))


def test_grouped_aero_location_and_employment_type_do_not_shift_role_columns():
    entries = parse_experience_html(
        GROUPED_LOCATION_EMPLOYMENT_TYPE.read_text(encoding="utf-8")
    )
    assert entries[:2] == [
        {
            "title": "CEO",
            "company": "Replit",
            "start_date": "Apr 2016",
            "end_date": "Present",
            "location": "San Francisco Bay Area",
            "company_url": "https://www.linkedin.com/company/18542592/",
        },
        {
            "title": "Head of Engineering",
            "company": "Replit",
            "start_date": "Dec 2022",
            "end_date": "Apr 2024",
            "location": "",
            "company_url": "https://www.linkedin.com/company/18542592/",
        },
    ]
    assert entries[2]["title"] == "Software Engineer"
    assert entries[2]["company"] == "Facebook"


def test_grouped_aero_retail_block_three_roles_employer_intact():
    entries = _grouped_retail_consulting_entries()
    retail_roles = [e for e in entries if e["company"] == "Meridian Retail"]
    assert len(retail_roles) == 3
    titles = {e["title"] for e in retail_roles}
    assert titles == {
        "Sr. Product Manager, Search Customer Experience",
        "Sr. Product Manager, Meridian Glow",
        "Sr. Product Manager, Inventory and Supply Chain Excellence Team",
    }
    # No duration string or description text may leak into any title.
    for e in retail_roles:
        assert e["title"] != "Full-time · 3 yrs 7 mos"
        assert not e["title"].endswith("mos")
        assert len(e["title"]) < 100


def test_grouped_aero_retail_block_dates_and_location():
    entries = _grouped_retail_consulting_entries()
    by_title = {e["title"]: e for e in entries}
    scx = by_title["Sr. Product Manager, Search Customer Experience"]
    assert scx["start_date"] == "Jan 2017"
    assert scx["end_date"] == "Mar 2018"
    glow = by_title["Sr. Product Manager, Meridian Glow"]
    assert glow["start_date"] == "Apr 2016"
    assert glow["end_date"] == "Dec 2016"
    inv = by_title["Sr. Product Manager, Inventory and Supply Chain Excellence Team"]
    assert inv["start_date"] == "Sep 2014"
    assert inv["end_date"] == "Mar 2016"
    assert "Austin" in inv["location"]


def test_grouped_aero_consulting_block_four_roles_employer_intact():
    entries = _grouped_retail_consulting_entries()
    consulting_roles = [e for e in entries if e["company"] == "Highfield Strategy Group"]
    assert len(consulting_roles) == 4
    titles = {e["title"] for e in consulting_roles}
    assert titles == {
        "Analyst",
        "Senior Analyst",
        "Consultant (Engagement Manager)",
        "Associate (Senior Engagement Manager)",
    }
    # The group's own long "about the firm" boilerplate, and its bare
    # "4 yrs 1 mo" total-duration header, must never leak into a title.
    for e in consulting_roles:
        assert "leading strategy" not in e["title"]
        assert e["title"] != "4 yrs 1 mo"
        assert len(e["title"]) < 60


def test_grouped_aero_consulting_block_dates():
    entries = _grouped_retail_consulting_entries()
    by_title = {e["title"]: e for e in entries}
    assert by_title["Analyst"]["start_date"] == "Apr 2008"
    assert by_title["Analyst"]["end_date"] == "Mar 2009"
    assert by_title["Senior Analyst"]["start_date"] == "Apr 2009"
    assert by_title["Senior Analyst"]["end_date"] == "Apr 2010"
    assert by_title["Consultant (Engagement Manager)"]["start_date"] == "May 2010"
    assert by_title["Consultant (Engagement Manager)"]["end_date"] == "Oct 2011"
    assert by_title["Associate (Senior Engagement Manager)"]["start_date"] == "Nov 2011"
    assert by_title["Associate (Senior Engagement Manager)"]["end_date"] == "Apr 2012"


def test_grouped_aero_flat_entries_around_groups_unaffected():
    # The flat entries surrounding both groups must still parse correctly,
    # and in particular must NOT inherit a neighboring group's company or
    # pick up its header line as a bogus location (the location-bleed bug).
    entries = _grouped_retail_consulting_entries()
    by_title = {e["title"]: e for e in entries}

    anchor_point = by_title["Co-founder, COO"]
    assert anchor_point["company"] == "Anchor Point Robotics"

    intern = by_title["PM Intern"]
    assert intern["company"] == "Fernbridge"
    # Must not have picked up the following "Highfield Strategy Group" group
    # header as its own location.
    assert intern["location"] == ""

    rd_lead = by_title["R&D Engineering Lead"]
    assert rd_lead["company"] == "Solace Systems, Inc."
    assert "Redwood City" in rd_lead["location"]


def test_grouped_aero_bigtech_block_two_roles_employer_intact():
    entries = _grouped_bigtech_entries()
    bigtech_roles = [e for e in entries if e["company"] == "Halcyon Systems"]
    assert len(bigtech_roles) == 2
    titles = {e["title"] for e in bigtech_roles}
    assert titles == {"Software and Algorithms Engineer", "Incubation Engineer"}
    for e in bigtech_roles:
        # Neither the bare "3 yrs 6 mos" duration header nor the long role
        # description paragraph may leak into a title.
        assert e["title"] != "3 yrs 6 mos"
        assert "Design, develop" not in e["title"]
        assert len(e["title"]) < 60


def test_grouped_aero_bigtech_block_dates_and_location():
    entries = _grouped_bigtech_entries()
    by_title = {e["title"]: e for e in entries}
    swe = by_title["Software and Algorithms Engineer"]
    assert swe["start_date"] == "Feb 2006"
    assert swe["end_date"] == "Oct 2008"
    assert swe["location"] == "Ashgrove"
    incubation = by_title["Incubation Engineer"]
    assert incubation["start_date"] == "May 2005"
    assert incubation["end_date"] == "Feb 2006"


def test_grouped_aero_bigtech_neighbors_no_location_bleed_and_group_closes():
    entries = _grouped_bigtech_entries()
    by_title = {e["title"]: e for e in entries}

    vertex = by_title["Director of Firmware and Algorithms"]
    assert vertex["company"] == "Vertex Cycles"
    # Must not have picked up the following "Halcyon Systems" group header as
    # its own location (the location-bleed bug seen live).
    assert vertex["location"] == ""

    research = by_title["Research Assistant"]
    # The group must close correctly: this flat entry's own company, not
    # "Halcyon Systems" inherited from the group above it.
    assert research["company"] == "Bellweather Institute of Technology"


# ---------------------------------------------------------------------------
# Bug 3: education parser field-shift. _parse_education_aero used to use a
# FIXED lines[i-2]/lines[i-1] lookback from each date line (the experience
# parser had already moved to a forward-cursor approach); a "Y Combinator"
# entry missing its degree line, or an "Activities and societies" line
# sitting between a real degree and its date, shifted the fixed lookback and
# garbled school/degree for those (and only those) entries.
# ---------------------------------------------------------------------------

EDU_GROUPED_ODD = FIXTURES / "education_aero_grouped_odd.html"


def _grouped_odd_education_entries():
    return parse_education_html(EDU_GROUPED_ODD.read_text(encoding="utf-8"))


def test_education_grouped_odd_fixture_exists():
    assert EDU_GROUPED_ODD.exists()


def test_education_aero_normal_entry_still_correct_among_odd_ones():
    entries = _grouped_odd_education_entries()
    by_school = {e["school"]: e for e in entries}
    tamu = by_school["Texas A&M University"]
    assert tamu["degree"] == "BS, Computer Science"
    assert tamu["start_date"] == "2018"
    assert tamu["end_date"] == "2022"


def test_education_aero_missing_degree_entry_does_not_steal_prior_text():
    # The "Y Combinator" entry has no degree line at all (just school, then
    # date). The old fixed lines[i-2] lookback would have grabbed whatever
    # sat two lines back in the flat list (part of the TAMU entry above) as
    # this entry's "school" instead of "Y Combinator".
    entries = _grouped_odd_education_entries()
    by_school = {e["school"]: e for e in entries}
    assert "Y Combinator" in by_school
    yc = by_school["Y Combinator"]
    assert yc["degree"] == ""
    assert yc["start_date"] == "2021"
    assert yc["end_date"] == "2021"


def test_education_aero_interleaved_activities_line_does_not_shift_fields():
    # "Activities and societies: ..." sits between the real degree and the
    # date. The old fixed lines[i-1]/lines[i-2] lookback would have read the
    # activities line as the degree and the real degree as the school.
    entries = _grouped_odd_education_entries()
    by_school = {e["school"]: e for e in entries}
    rice = by_school["Rice University"]
    assert rice["degree"] == "PhD, Physics"
    assert rice["start_date"] == "2010"
    assert rice["end_date"] == "2016"


def test_education_aero_trailing_activities_never_becomes_phantom_entry():
    entries = _grouped_odd_education_entries()
    assert len(entries) == 3
    schools = {e["school"] for e in entries}
    assert schools == {"Texas A&M University", "Y Combinator", "Rice University"}
    degrees = {e["degree"] for e in entries}
    assert not any("Activities and societies" in d for d in degrees)


def test_live_experience_captures_role_description():
    """A role's free-text description must be CAPTURED into the entry (not just
    kept out of the location field). This is the plumbing that lets a founder's
    own traction boast ("2,000+ users") reach mechanical_decompose and, via a
    user_count claim, detect_inflation. RED before the capture is implemented:
    the parser recognizes description lines only to discard them."""
    entries = _live_experience()
    intern = next(e for e in entries if e["title"] == "Test Engineering Intern")
    assert "Systems Test Hardware group" in (intern.get("description") or "")
