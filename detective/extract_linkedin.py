"""LinkedIn full-profile extraction.

Two layers, kept strictly separate so the parser is testable offline:

  1. parse_experience_html(html)  -- PURE function. Takes the HTML of a
     LinkedIn experience section (or the /details/experience/ page) and
     returns a normalized list of experience entries. No network, no
     playwright, no cookies. This is the unit-tested surface.

  2. fetch_profile(url, live=False) -- the live orchestration that uses an
     in-repo Playwright session with a dedicated local Chrome profile to load
     the page, expand "Show all experience", and hand the raw HTML to the pure
     parser. Every heavy import here is LAZY (inside the function) so importing
     this module for the offline tests never needs playwright.

     Before opening the session, fetch_profile bridges a fresh Playwright
     storage_state session (LINKEDIN_STATE_PATH env var, a JSON file with a
     "cookies" array captured by a separate login flow) into the flat
     cookies.json shape and location HumanSession already knows how to read.
     See _bridge_fresh_session_cookies below.

Normalized experience entry shape:
    {
        "title":      str,
        "company":    str,
        "start_date": str,   # as displayed, e.g. "Jan 2020" ("" if unknown)
        "end_date":   str,   # e.g. "Present" ("" if unknown)
        "location":   str,   # "" if absent
    }

House rule: no em dashes anywhere in this file. Date ranges coming FROM
LinkedIn may contain hyphen, en dash, or em dash separators; we match those
via unicode escapes in a regex so we never have to type one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

# Separators LinkedIn may use inside a date range, referenced by escape so no
# literal long dash is ever typed in this source file.
_DASH_CLASS = "-\u2010\u2011\u2013\u2014"
_DATE_RANGE_RE = re.compile(
    r"^(?P<start>.*?)\s*[" + _DASH_CLASS + r"]\s*(?P<end>.*)$"
)
# A line looks like a date line if it starts with a month or a 4-digit year.
_DATE_LINE_RE = re.compile(
    r"(?:\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|\bPresent\b|\b\d{4}\b)",
    re.IGNORECASE,
)
_MIDDOT = "·"  # the "·" separator LinkedIn puts before durations / types

# A single date token: "Jan 2020" (month may carry a trailing dot), a bare
# 4-digit year, or "Present". Used to detect a date-RANGE line strictly, so a
# stray year inside a free-text description (e.g. "back in 2015 we shipped")
# is never mistaken for an entry's date spine.
_DATE_TOKEN = (
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{4}"
    r"|\d{4}"
    r"|Present)"
)
_DATE_RANGE_STRICT_RE = re.compile(
    r"^\s*" + _DATE_TOKEN + r"\s*[" + _DASH_CLASS + r"]\s*" + _DATE_TOKEN + r"\s*$",
    re.IGNORECASE,
)
# Workplace-type words LinkedIn appends to a location line ("Austin · Remote").
_WORKPLACE_RE = re.compile(r"\b(On[\-\s]?site|Remote|Hybrid)\b", re.IGNORECASE)

# Legacy-DOM markers. Their presence means the page is the older
# artdeco/pvs-list dual-render shape (visible copy in <span aria-hidden>),
# which the legacy parser below handles. Their absence means the current
# "Aero" React DOM (hashed atomic CSS classes, plain <p> text, no
# aria-hidden dual-render), handled by the Aero parser.
_LEGACY_DOM_MARKERS = ("artdeco-list__item", "pvs-list")


# ---------------------------------------------------------------------------
# Pure parser (offline-testable)
# ---------------------------------------------------------------------------


def _visible_texts(node) -> list[str]:
    """Return the ordered visible text lines inside a node.

    LinkedIn renders the visible copy inside <span aria-hidden="true"> and
    duplicates it in a visually-hidden sibling for screen readers. We keep only
    the aria-hidden spans to avoid doubled text, de-duplicating consecutive
    repeats defensively.
    """
    out: list[str] = []
    for span in node.find_all("span", attrs={"aria-hidden": "true"}):
        txt = span.get_text(" ", strip=True)
        if txt and (not out or out[-1] != txt):
            out.append(txt)
    return out


def _strip_duration(line: str) -> str:
    """Drop the trailing '· 4 yrs 2 mos' duration that LinkedIn appends."""
    if _MIDDOT in line:
        return line.split(_MIDDOT)[0].strip()
    return line.strip()


def _split_date_range(line: str) -> tuple[str, str]:
    """'Jan 2020 - Present · 4 yrs' -> ('Jan 2020', 'Present')."""
    core = _strip_duration(line)
    m = _DATE_RANGE_RE.match(core)
    if m:
        return m.group("start").strip(), m.group("end").strip()
    return core, ""


def _looks_like_date(line: str) -> bool:
    return bool(_DATE_LINE_RE.search(line))


def _is_date_range_line(line: str) -> bool:
    """True only if the whole line is a date RANGE ("Aug 2023 - Present").

    Stricter than `_looks_like_date`: it requires a real date token on BOTH
    sides of the separator (after any trailing "· N yrs" duration is dropped),
    so a description line that merely mentions a year is never treated as the
    date spine of an experience entry.
    """
    return bool(_DATE_RANGE_STRICT_RE.match(_strip_duration(line)))


def _is_location_line(line: str) -> bool:
    """Heuristic: does this line look like a location rather than a description?

    LinkedIn renders a location under the date as "Greater Seattle Area · On-site"
    or a bare "Seattle WA". A role may instead be followed by a free-text
    description paragraph. We treat a line as a location when it carries a
    workplace-type word (On-site/Remote/Hybrid), or when its geographic part is
    short and not a sentence. This keeps long description paragraphs out of the
    location field.
    """
    if _WORKPLACE_RE.search(line):
        return True
    geo = _strip_duration(line)
    if not geo or _is_date_range_line(line):
        return False
    return len(geo) <= 50 and len(geo.split()) <= 6 and not geo.endswith(".")


def _company_from_line(line: str) -> str:
    """'Google · Full-time' -> 'Google'. Leaves plain company names intact."""
    if _MIDDOT in line:
        return line.split(_MIDDOT)[0].strip()
    return line.strip()


def _parse_single_entry(bold: str, rest: list[str]) -> dict:
    """Parse a flat (single-role) experience block.

    bold : the primary bold line (the job title).
    rest : the remaining visible lines under it, in order.
    """
    company = ""
    start = end = ""
    location = ""
    for line in rest:
        if _looks_like_date(line) and not start and not end:
            start, end = _split_date_range(line)
        elif not company:
            company = _company_from_line(line)
        elif not location and not _looks_like_date(line):
            location = line.strip()
    return {
        "title": bold.strip(),
        "company": company,
        "start_date": start,
        "end_date": end,
        "location": location,
    }


def parse_experience_html(html: str) -> list[dict]:
    """Parse a LinkedIn experience section HTML string into entries.

    Dispatches on the DOM shape. LinkedIn currently ships two very different
    frontends:

      - the older artdeco/pvs-list dual-render (visible copy in
        <span aria-hidden="true">, one screen-reader duplicate), and
      - the current "Aero" React DOM: hashed atomic CSS class names
        (class="_9bf21961 _21283922"), plain <p> text, and NO aria-hidden
        dual-render.

    We detect the shape by the presence of the legacy list-item markers and
    route to the matching parser, rather than "try one, fall back if empty",
    so the Aero parser can never half-parse a legacy page (or vice versa) and
    return non-empty-but-wrong data.

    Both paths handle: single-role entries, "Present" or absent end dates, and
    date ranges with or without months plus a trailing "N yrs M mos" duration.
    Grouped multi-role (one company, nested promoted positions) is covered on
    the legacy path; see the module notes for the Aero limitation.

    This function is defensive by design: it must never raise on malformed,
    incomplete, or unexpected markup. Missing nodes, empty strings, and
    absent dates degrade to partial data (e.g. title present, dates empty)
    instead of blowing up, and one malformed entry never takes down the rest
    of the parse.

    Returns [] if BeautifulSoup is unavailable or nothing parses.
    """
    if not html or not html.strip():
        return []
    if _is_legacy_dom(html):
        return _parse_experience_legacy(html)
    try:
        return _parse_experience_aero(html)
    except Exception:
        return []


def _is_legacy_dom(html: str) -> bool:
    """True if the HTML uses the older artdeco/pvs-list list-item markup."""
    return any(marker in html for marker in _LEGACY_DOM_MARKERS)


# ---------------------------------------------------------------------------
# Aero parser (current LinkedIn React DOM: hashed classes, plain <p> text)
# ---------------------------------------------------------------------------


def _aero_lines(scope) -> list[str]:
    """Ordered, de-duplicated visible <p> text lines inside a scope node.

    The Aero DOM puts every field (title, company, date range, location,
    description) in its own <p>, in reading order, so the ordered list of <p>
    texts is the raw material both the experience and education parsers
    segment. Consecutive exact repeats are collapsed defensively.
    """
    out: list[str] = []
    for p in scope.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if txt and (not out or out[-1] != txt):
            out.append(txt)
    return out


# Each top-level Aero experience/education entry (flat or grouped) is wrapped
# in an element carrying componentkey="entity-collection-item--<id>" (seen
# identically across the live-shaped fixture and both synthetic grouped
# fixtures). This is the one stable structural anchor for "which top-level
# item does this <p> line belong to", used below to tell whether the role at
# a given date line sits inside a GROUPED item (one whose item also contains
# a standalone bare-duration line, i.e. a promoted-role block) versus a FLAT
# item, so the location-guard reserve (see _parse_experience_aero) can be
# based on the structure itself instead of guessing from line content alone.
_ITEM_COMPONENTKEY_RE = re.compile(r"^entity-collection-item")


def _company_url_from_node(node) -> str:
    """First LinkedIn company-page href anywhere inside `node`, or "".

    LinkedIn renders the employer's own /company/ page link on the experience
    row itself. That is the least ambiguous product/company URL we can get for
    a claim (no name search, no namesake risk), so it is harvested first and
    everything else is a fallback. Only /company/ counts: a row also links
    people, schools and posts, and none of those is the employer.

    Never raises: a junk href degrades to "" rather than costing the parse.
    """
    try:
        anchors = node.find_all("a", href=True)
    except Exception:
        return ""
    for a in anchors:
        try:
            href = (a.get("href") or "").strip()
            if not href:
                continue
            parsed = urlparse(href)
            if not _host_matches((parsed.netloc or "").lower(), "linkedin.com"):
                continue
            if (parsed.path or "").lower().lstrip("/").startswith("company/"):
                return href
        except Exception:
            continue
    return ""


def _aero_company_url_for(p) -> str:
    """The company URL owning the <p> line `p`, walking OUTWARD through the
    enclosing collection items. Outward, not just the nearest item, because a
    grouped promoted-role block puts the link on the group header while each
    sub-role renders in its own nested item; every sub-role belongs to that
    same employer and must inherit its URL.
    """
    node = getattr(p, "parent", None)
    while node is not None:
        try:
            ck = node.get("componentkey")
        except Exception:
            ck = None
        if ck and _ITEM_COMPONENTKEY_RE.match(ck):
            url = _company_url_from_node(node)
            if url:
                return url
        node = getattr(node, "parent", None)
    return ""


def _aero_lines_with_group_flags(scope) -> tuple[list[str], list[bool]]:
    """Back-compat wrapper: lines plus group flags only (see
    _aero_lines_with_item_meta for the third parallel list)."""
    lines, grouped, _urls = _aero_lines_with_item_meta(scope)
    return lines, grouped


def _aero_lines_with_item_meta(scope) -> tuple[list[str], list[bool], list[str]]:
    """Like _aero_lines, but also returns a parallel list of bools: for each
    line, whether the <p> it came from sits inside a GROUPED top-level item
    (an item that itself contains a bare-duration total-tenure line
    somewhere inside it, i.e. a promoted-role block), as opposed to a FLAT
    item (a plain single-role entry). A line with no identifiable enclosing
    item defaults to False (flat), the safe/conservative default.

    Third list: the enclosing item's LinkedIn company-page URL ("" when the row
    links nothing), so an entry can be stamped with the employer's own page
    without a second DOM walk that could drift out of alignment with the lines.
    """
    try:
        items = scope.find_all(attrs={"componentkey": _ITEM_COMPONENTKEY_RE})
    except Exception:
        items = []

    grouped_item_ids: set[int] = set()
    for item in items:
        try:
            for p in item.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if txt and _is_bare_duration_line(txt):
                    grouped_item_ids.add(id(item))
                    break
        except Exception:
            continue

    def _enclosing_item(p):
        node = p.parent
        while node is not None:
            try:
                ck = node.get("componentkey")
            except Exception:
                ck = None
            if ck and _ITEM_COMPONENTKEY_RE.match(ck):
                return node
            node = node.parent
        return None

    out_lines: list[str] = []
    out_grouped: list[bool] = []
    out_urls: list[str] = []
    for p in scope.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if txt and (not out_lines or out_lines[-1] != txt):
            item = _enclosing_item(p)
            out_lines.append(txt)
            out_grouped.append(item is not None and id(item) in grouped_item_ids)
            out_urls.append(_aero_company_url_for(p))
    return out_lines, out_grouped, out_urls


def _aero_scope(soup, section_testid_substr: str):
    """Locate the section container in the Aero DOM by stable anchors.

    Priority: a section-specific data-testid (LinkedIn suffixes it with the
    profile handle, e.g. profile_EducationDetailsSection_<handle>), then the
    LazyColumn wrapper the details list renders inside, then <main>, then the
    whole document. None of these are the volatile hashed class names.
    """
    scope = soup.find(attrs={"data-testid": re.compile(section_testid_substr)})
    if scope is None:
        scope = soup.find(attrs={"data-testid": "lazy-column"})
    if scope is None:
        scope = soup.find(attrs={"data-component-type": "LazyColumn"})
    if scope is None:
        scope = soup.find("main")
    return scope if scope is not None else soup


# A grouped company block (one employer, several promoted roles) renders a
# standalone TOTAL-tenure line right under the company name, e.g.
# "Full-time · 3 yrs 7 mos" or a bare "4 yrs 1 mo". Unlike a real role's
# date line, it carries no date tokens at all (no month name, no 4-digit
# year, no "Present"), just a duration, optionally prefixed by an employment
# type. That is the one reliable signal that the line above it is a COMPANY
# header opening a group, not a flat single-role's own company line, since a
# flat entry's date line always has its duration attached to the date range
# itself ("Jan 2020 - Present · 4 yrs 6 mos" as one line) rather than as a
# separate standalone line.
_EMPLOYMENT_TYPE_WORDS = (
    "full-time", "part-time", "internship", "self-employed", "contract",
    "freelance", "seasonal", "apprenticeship", "trainee",
)
_BARE_DURATION_CORE_RE = re.compile(
    r"^\d+\s*yrs?(?:\s*\d+\s*mos?)?$|^\d+\s*mos?$", re.IGNORECASE
)


def _is_bare_duration_line(line: str) -> bool:
    """True for a standalone total-tenure line ("Full-time · 3 yrs 7 mos",
    or a bare "4 yrs 1 mo"), never for a real date RANGE line (which always
    carries two date tokens on either side of a dash).
    """
    if _is_date_range_line(line):
        return False
    core = line.strip()
    if not core:
        return False
    if _MIDDOT in core:
        before, _, after = core.partition(_MIDDOT)
        before = before.strip()
        after = after.strip()
        if before and before.lower() not in _EMPLOYMENT_TYPE_WORDS:
            return False
        return bool(_BARE_DURATION_CORE_RE.match(after))
    return bool(_BARE_DURATION_CORE_RE.match(core))


def _is_employment_type_line(line: str) -> bool:
    """True for a role-level label such as Full-time, not a job title."""
    return (line or "").strip().lower() in _EMPLOYMENT_TYPE_WORDS


def _looks_like_description(line: str) -> bool:
    """True for a free-text paragraph (a role description, or a grouped
    company's own "about" boilerplate) rather than a short title/company
    label. Descriptions run long and read as full sentences; labels do not.

    Used only to decide whether a line buffered ahead of a date range is
    leftover trailing content from the PREVIOUS role (skip it) or the actual
    title of the NEXT one (use it): see _parse_experience_aero.
    """
    core = line.strip()
    if not core:
        return False
    return len(core) > 80 or core.count(".") >= 2


def _parse_experience_aero(html: str) -> list[dict]:
    """Parse the current Aero experience DOM into normalized entries.

    Walks the section's ordered <p> lines with a single forward-moving
    cursor instead of fixed index math per date line, because a GROUPED
    company block (one employer, several promoted roles, e.g. Analyst ->
    Senior Analyst -> Consultant -> Associate) never repeats the company
    name for each sub-role: every sub-role's title sits directly against its
    own date line, with no company line of its own. A flat single-role entry,
    in contrast, always has both a title line AND a company line before its
    date.

    A grouped block is recognized by its header: the company name followed
    by a standalone total-tenure line (see _is_bare_duration_line), never a
    real date range. Once that header is seen, every following date line is
    treated as a sub-role of that company (title = the single buffered line
    right before it, company inherited) UNTIL a date line's buffered lines
    instead look like a genuine new (title, company) pair rather than
    (leftover location/description text, title), at which point the group is
    closed and normal flat parsing resumes for the rest of the section.

    A location (if any) is the line directly after a role's own date line,
    but only when: it looks like a location rather than a description, it
    would not swallow the next role's own title/company, and the line right
    after IT is not itself a group's total-tenure line (otherwise a grouped
    block's company header, e.g. "Microsoft", would be misread as the
    PREVIOUS entry's location, a real bug seen live on grouped profiles).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    scope = _aero_scope(soup, "ExperienceDetailsSection")
    lines, item_grouped, item_company_urls = _aero_lines_with_item_meta(scope)
    if lines and lines[0].strip().lower() in ("experience", "education"):
        lines = lines[1:]
        item_grouped = item_grouped[1:]
        item_company_urls = item_company_urls[1:]

    date_idx = [i for i, ln in enumerate(lines) if _is_date_range_line(ln)]
    n = len(lines)
    entries: list[dict] = []
    pending: list[str] = []
    current_company: Optional[str] = None
    i = 0

    while i < n:
        line = lines[i]
        try:
            if _is_bare_duration_line(line):
                # The most recently buffered line is the company header this
                # duration belongs to; everything before that is irrelevant
                # (e.g. trailing description text from an earlier role that
                # never got consumed as a title).
                if pending:
                    current_company = _company_from_line(pending[-1])
                pending = []
                i += 1
                continue

            if _is_date_range_line(line):
                pre_date_location = ""
                if current_company is not None and item_grouped[i]:
                    # A grouped block can put a company-wide location before
                    # its first title and a role-level employment type after a
                    # later title. Structural group membership is authoritative:
                    # neither auxiliary label may shift the title/company pair.
                    if (
                        len(pending) >= 2
                        and _is_employment_type_line(pending[-1])
                    ):
                        title_start = len(pending) - 2
                    else:
                        title_start = len(pending) - 1 if pending else 0
                    title = pending[title_start].strip() if pending else ""
                    company = current_company
                    for candidate in reversed(pending[:title_start]):
                        if _is_location_line(candidate):
                            pre_date_location = _strip_duration(candidate)
                            break
                elif current_company is not None and len(pending) <= 1:
                    # Grouped sub-role: only the title precedes its date,
                    # company line omitted, company inherited from the group.
                    title = pending[-1].strip() if pending else ""
                    company = current_company
                    title_start = len(pending) - 1 if pending else 0
                elif current_company is not None and _looks_like_description(pending[-2]):
                    # Leftover description/boilerplate text, then this
                    # sub-role's title: still a continuation of the group.
                    title = pending[-1].strip()
                    company = current_company
                    title_start = len(pending) - 1
                elif current_company is not None:
                    # The line before the title reads as a real label (not
                    # leftover description text), i.e. a genuine (title,
                    # company) pair: the group has ended here.
                    title = pending[-2].strip()
                    company = _company_from_line(pending[-1])
                    current_company = None
                    title_start = len(pending) - 2
                elif len(pending) >= 2:
                    # Flat entry: title then company, same as before.
                    title = pending[-2].strip()
                    company = _company_from_line(pending[-1])
                    title_start = len(pending) - 2
                elif pending:
                    title = pending[-1].strip()
                    company = ""
                    title_start = len(pending) - 1
                else:
                    title = company = ""
                    title_start = 0

                # Any pending lines BEFORE this entry's title/company are the
                # PREVIOUS entry's free-text description (the paragraph that
                # sits between one role's header and the next). Attach the
                # description-looking ones to the previous entry so a founder's
                # own traction boast ("2,000+ users") survives into decompose.
                leftover = [
                    p.strip()
                    for p in pending[:title_start]
                    if _looks_like_description(p)
                ]
                if leftover and entries:
                    prev = entries[-1]
                    prev["description"] = " ".join(
                        x for x in [prev.get("description", ""), *leftover] if x
                    ).strip()

                start, end = _split_date_range(line)

                # A location is only ever the very next line, and only when
                # keeping it would not swallow the next role's own title (or,
                # for a grouped sub-role, the next sub-role's only line).
                pos = date_idx.index(i)
                next_date = date_idx[pos + 1] if pos + 1 < len(date_idx) else n + 2
                location = pre_date_location
                consumed_extra = 0
                cand = i + 1
                # A grouped sub-role has only its title line (company is
                # inherited) before the next date, so only 1 line needs to be
                # reserved for it; a flat entry needs 2 (title, company).
                # Whether the UPCOMING role (at next_date) is itself grouped
                # or flat is not decidable from current_company (a grouped
                # block's last sub-role and its middle sub-roles both have
                # current_company set, yet the item right after the last one
                # is typically a flat entry), so this reads the structural
                # item-grouping flag captured alongside the lines instead:
                # true group membership from the DOM, not a content guess.
                reserve = 1 if next_date < n and item_grouped[next_date] else 2
                if (
                    next_date < n
                    and item_grouped[next_date]
                    and next_date - 1 > i
                    and _is_employment_type_line(lines[next_date - 1])
                ):
                    reserve = 2
                if (
                    cand < n
                    and cand < next_date - reserve
                    and not _is_date_range_line(lines[cand])
                    and not _is_bare_duration_line(lines[cand])
                    and not (cand + 1 < n and _is_bare_duration_line(lines[cand + 1]))
                    and _is_location_line(lines[cand])
                ):
                    location = _strip_duration(lines[cand])
                    consumed_extra = 1

                entry = {
                    "title": title,
                    "company": company,
                    "start_date": start,
                    "end_date": end,
                    "location": location,
                }
                # LinkedIn's own link for THIS row's employer, when it rendered
                # one. Omitted entirely when absent (no empty-string key), so a
                # linkless row keeps the exact shape every caller already reads.
                company_url = item_company_urls[i] if i < len(item_company_urls) else ""
                if company_url:
                    entry["company_url"] = company_url
                entries.append(entry)
                pending = []
                i += 1 + consumed_extra
                continue

            pending.append(line)
            i += 1
        except Exception:
            # One malformed line/entry must not take down the whole parse.
            pending = []
            i += 1
            continue

    return entries


def _parse_experience_legacy(html: str) -> list[dict]:
    """Parse the older artdeco/pvs-list experience DOM (dual-render spans)."""
    try:
        from bs4 import BeautifulSoup  # lazy: not needed to import this module
    except ImportError:
        raise RuntimeError(
            "beautifulsoup4 is required to parse LinkedIn HTML "
            "(pip install beautifulsoup4)"
        )

    if not html or not html.strip():
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Prefer an explicit experience container if present, else the whole doc.
        container = soup.find(attrs={"data-section": "experience"})
        if container is None:
            container = soup.find("section", id="experience-section") or soup

        # Top-level experience items. LinkedIn marks each with these list classes.
        items = container.find_all(
            "li",
            class_=lambda c: bool(c)
            and ("artdeco-list__item" in c or "pvs-list__paged-list-item" in c),
        )
        # Keep only the outermost items (drop nested sub-role <li> handled below).
        top_items = [li for li in items if not _has_experience_ancestor(li, items)]
    except Exception:
        # Structure was too unexpected to even locate the items. Degrade to
        # "nothing parsed" rather than raise: callers can still proceed with
        # an empty experience list.
        return []

    entries: list[dict] = []
    for li in top_items:
        try:
            parsed_entries = _parse_item(li)
        except Exception:
            # One malformed entry must not take down the whole parse.
            continue
        # Same contract as the Aero parser: stamp LinkedIn's own company-page
        # link onto every entry this item produced (a grouped item's sub-roles
        # share one employer, so they share its URL), key omitted when absent.
        try:
            company_url = _company_url_from_node(li)
        except Exception:
            company_url = ""
        for entry in parsed_entries:
            if company_url:
                entry["company_url"] = company_url
            entries.append(entry)
    return entries


def _has_experience_ancestor(li, items) -> bool:
    """True if `li` is nested inside another experience <li> in `items`."""
    parent = li.parent
    item_set = set(id(x) for x in items)
    while parent is not None:
        if id(parent) in item_set:
            return True
        parent = parent.parent
    return False


def _parse_item(li) -> list[dict]:
    """Parse one top-level experience <li> into one or more entries."""
    # A grouped entry nests its roles in a sub-list of <li> items each with
    # their own bold title line. Detect that nested list.
    try:
        nested_role_lis = _nested_role_items(li)
    except Exception:
        nested_role_lis = []

    if nested_role_lis:
        # Grouped: header bold line is the COMPANY; each nested li is a role.
        try:
            header_lines = _header_lines(li, nested_role_lis)
        except Exception:
            header_lines = []
        company = header_lines[0].strip() if header_lines else ""
        # A location or employment-type may sit under the company header.
        header_location = ""
        for line in header_lines[1:]:
            if _MIDDOT in line or _looks_like_date(line):
                continue
            header_location = line.strip()
            break

        out: list[dict] = []
        for role_li in nested_role_lis:
            try:
                entry = _build_role_entry(role_li, company, header_location)
            except Exception:
                # A malformed sibling role must not drop the whole group.
                continue
            if entry is not None:
                out.append(entry)
        return out

    # Flat single-role entry. Exclude any text nested inside a further <ul>
    # under this <li> (skills pills, media captions, or other non-field
    # content LinkedIn sometimes embeds under a position); only the direct
    # title/company/date/location lines belong to a flat entry.
    lines = _flat_lines(li)
    if not lines:
        return []
    return [_parse_single_entry(lines[0], lines[1:])]


def _flat_lines(li) -> list[str]:
    """Visible lines for a flat (non-grouped) entry.

    Same as `_visible_texts` but skips any span nested inside a <ul> found
    under `li`, so stray nested lists (skills, media) never contaminate the
    title/company/date/location fields of a single-role entry.
    """
    try:
        nested_uls = li.find_all("ul", recursive=True)
    except Exception:
        nested_uls = []
    nested_ids = set(id(u) for u in nested_uls)

    def inside_nested_ul(span) -> bool:
        p = span.parent
        while p is not None:
            if id(p) in nested_ids:
                return True
            p = p.parent
        return False

    out: list[str] = []
    for span in li.find_all("span", attrs={"aria-hidden": "true"}):
        if inside_nested_ul(span):
            continue
        txt = span.get_text(" ", strip=True)
        if txt and (not out or out[-1] != txt):
            out.append(txt)
    return out


def _build_role_entry(role_li, company: str, header_location: str) -> Optional[dict]:
    """Build one nested-role entry, inheriting company and header location."""
    lines = _visible_texts(role_li)
    if not lines:
        return None
    title = lines[0].strip()
    start = end = ""
    location = header_location
    for line in lines[1:]:
        if _looks_like_date(line) and not start and not end:
            start, end = _split_date_range(line)
        elif not location and not _looks_like_date(line):
            location = line.strip()
    return {
        "title": title,
        "company": company,
        "start_date": start,
        "end_date": end,
        "location": location,
    }


def _has_bold_marker(node) -> bool:
    """True if `node` has its own bold (title) element.

    A real promoted/grouped role always renders its title in a "t-bold"
    element. Other nested <ul><li> content under an experience entry, such
    as a skills list or a "show more" description bullet, does not. Requiring
    this marker keeps those from being mistaken for sibling roles (a false
    "grouped" positive), which would otherwise fabricate fake positions.
    """
    try:
        return node.find(class_=lambda c: bool(c) and "t-bold" in c) is not None
    except Exception:
        return False


def _nested_role_items(li) -> list:
    """Return nested role <li> items for a grouped experience, else []."""
    sub_uls = li.find_all("ul", recursive=True)
    role_lis: list = []
    for ul in sub_uls:
        for sub in ul.find_all("li", recursive=False):
            # A role sub-item has its own visible text AND its own bold
            # title marker, not just any nested list content.
            if _visible_texts(sub) and _has_bold_marker(sub):
                role_lis.append(sub)
    return role_lis


def _header_lines(li, nested_role_lis) -> list[str]:
    """Visible lines that belong to the grouped-entry header (the company).

    We take visible spans that are NOT inside any nested role <li>.
    """
    nested_ids = set(id(x) for x in nested_role_lis)

    def inside_nested(span) -> bool:
        p = span.parent
        while p is not None:
            if id(p) in nested_ids:
                return True
            p = p.parent
        return False

    out: list[str] = []
    for span in li.find_all("span", attrs={"aria-hidden": "true"}):
        if inside_nested(span):
            continue
        txt = span.get_text(" ", strip=True)
        if txt and (not out or out[-1] != txt):
            out.append(txt)
    return out


# ---------------------------------------------------------------------------
# Live orchestration
# ---------------------------------------------------------------------------

# LINKEDIN_HUMAN_DIR remains an explicit compatibility override for operators
# who already have a compatible HumanSession module. The default public build
# is fully self-contained and uses _BuiltinHumanSession below.
_LINKEDIN_HUMAN_DIR = Path(os.environ.get("LINKEDIN_HUMAN_DIR", "").strip() or ".")
_LINKEDIN_PROFILE_DIR_RAW = os.environ.get("LARP_LINKEDIN_PROFILE_DIR", "").strip()
_LINKEDIN_PROFILE_DIR = (
    Path(_LINKEDIN_PROFILE_DIR_RAW).expanduser()
    if _LINKEDIN_PROFILE_DIR_RAW
    else None
)

# Fresh Playwright storage_state JSON (cookies + origins) captured by a
# separate live login flow. Configurable via env so no path is hardcoded
# here. This file is a live session artifact, never committed, and its
# contents (cookie values) are never logged. If unset, bridging is simply
# skipped and fetch_profile falls back to whatever cookies.json (if any)
# HumanSession already has.
_LINKEDIN_STATE_PATH = os.environ.get("LINKEDIN_STATE_PATH", "").strip()


def _default_linkedin_profile_dir() -> Path:
    if _LINKEDIN_PROFILE_DIR is not None:
        return _LINKEDIN_PROFILE_DIR
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "LARP Detector"
        / "linkedin-profile"
    )


@dataclass
class _BuiltinHumanConfig:
    """Small compatibility config for the in-repo single-profile session."""

    break_every_n_profiles: int = 1_000_000
    distraction_chance: float = 0.0
    page_dwell_mean_s: float = 1.0
    scroll_steps_mean: int = 3
    headless: bool = True


class _BuiltinHumanSession:
    """Authenticated, bounded Playwright session for one operator-triggered scan.

    The session uses only the dedicated profile created by
    scripts/login_linkedin_macos.py. It never reads the user's normal browser
    profile, exports cookies, or performs background retries.
    """

    def __init__(self, config: Optional[_BuiltinHumanConfig] = None):
        self.config = config or _BuiltinHumanConfig()
        self._playwright = None
        self._context = None
        self._page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(
                "Playwright is required for live LinkedIn scans. Run "
                "./scripts/setup_macos.sh first."
            ) from exc

        profile_dir = _default_linkedin_profile_dir()
        if not profile_dir.exists():
            raise RuntimeError(
                "LinkedIn is not connected. Open Settings in LARP Detector "
                "and complete the one-time LinkedIn login first."
            )

        self._playwright = sync_playwright().start()
        chrome_path = Path(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        launch_options = {
            "user_data_dir": str(profile_dir),
            "headless": bool(self.config.headless),
            "args": [
                "--password-store=basic",
                "--use-mock-keychain",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if chrome_path.exists():
            launch_options["executable_path"] = str(chrome_path)

        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
            if _LINKEDIN_STATE_PATH:
                self._add_storage_state_cookies(Path(_LINKEDIN_STATE_PATH))
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
        self._context = None
        self._playwright = None
        self._page = None

    def _add_storage_state_cookies(self, state_path: Path) -> None:
        if self._context is None or not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies", []) if isinstance(state, dict) else []
            linkedin_cookies = [
                cookie
                for cookie in cookies
                if "linkedin" in (cookie.get("domain") or "")
            ]
            if linkedin_cookies:
                self._context.add_cookies(linkedin_cookies)
        except Exception:
            pass

    def _read_profile_like_human(self) -> None:
        _fast_read_like_human(self._page)

    def scrape_profile(self, url: str) -> dict:
        self._page.goto(url, timeout=45000, wait_until="domcontentloaded")
        self._read_profile_like_human()
        html = self._page.content()
        identity = parse_identity_html(html, url)
        return {
            "url": self._page.url,
            "headline": identity.get("name", ""),
            "current_role": identity.get("headline", ""),
            "education": "",
            "html_excerpt": html,
        }


def _builtin_human_module():
    return SimpleNamespace(
        HumanSession=_BuiltinHumanSession,
        HumanConfig=_BuiltinHumanConfig,
        PROFILE_DIR=_default_linkedin_profile_dir(),
        COOKIES_PATH=_default_linkedin_profile_dir() / "cookies.json",
        _builtin=True,
    )


def _import_human_session():
    """Return the built-in session or an explicitly configured compatibility module.

    Returns the MODULE (not just the class) so callers can also reach its
    configuration constants. The compatibility module is never searched for
    implicitly, which prevents a clean install from depending on another local
    repository or importing an unexpected module from the working directory.
    """
    import sys

    configured_dir = os.environ.get("LINKEDIN_HUMAN_DIR", "").strip()
    if not configured_dir:
        return _builtin_human_module()

    configured_path = Path(configured_dir).expanduser()
    d = str(configured_path)
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import linkedin_human  # type: ignore
    except Exception as exc:  # ImportError or downstream (playwright) failure
        raise RuntimeError(
            "Could not import linkedin_human from "
            f"{configured_path}. Remove LINKEDIN_HUMAN_DIR to use the "
            f"built-in session. Underlying error: {exc}"
        ) from exc
    if _LINKEDIN_PROFILE_DIR is not None:
        linkedin_human.PROFILE_DIR = _LINKEDIN_PROFILE_DIR
        linkedin_human.COOKIES_PATH = _LINKEDIN_PROFILE_DIR / "cookies.json"
    return linkedin_human


def _bridge_fresh_session_cookies(linkedin_human_module) -> bool:
    """Adapt the fresh storage_state session into HumanSession's cookies.json.

    HumanSession only ever reads a flat list-of-dicts cookies.json (see its
    `cookies_path` handling: `self._ctx.add_cookies(raw)`). A Playwright
    storage_state JSON (what LINKEDIN_STATE_PATH points at) stores that exact
    same per-cookie shape (name, value, domain, path, expires, httpOnly,
    secure, sameSite) under its top-level "cookies" key, so bridging is a
    filter-and-write, not a format conversion.

    Writes only the linkedin.com cookies to linkedin_human_module.COOKIES_PATH
    (creating the parent directory if needed), treating the destination as a
    runtime cache: never committed (already gitignored), and never logged
    with values, only a count.

    Returns True if cookies were bridged, False on any miss (missing state
    file, unreadable JSON, no linkedin cookies found) so the caller can fall
    back to whatever cookies.json already existed, if any. Never raises: a
    bridging failure should degrade to "try the existing cookies.json", not
    crash the live fetch before it even starts.
    """
    if not _LINKEDIN_STATE_PATH:
        return False
    state_path = Path(_LINKEDIN_STATE_PATH)
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    cookies = state.get("cookies", []) if isinstance(state, dict) else []
    li_cookies = [c for c in cookies if "linkedin" in (c.get("domain") or "")]
    if not li_cookies:
        return False

    cookies_path = linkedin_human_module.COOKIES_PATH
    try:
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        cookies_path.write_text(json.dumps(li_cookies), encoding="utf-8")
    except Exception:
        return False
    return True


# Markers in the final page URL (not body text) that mean LinkedIn bounced
# the request to an authwall or login page instead of serving the profile,
# i.e. the session is invalid or expired. Checked separately from
# linkedin_human's own CAPTCHA_MARKERS (challenge/captcha text), since an
# authwall redirect renders a normal-looking login page with no such text and
# would otherwise be mistaken for a successful (but empty) fetch.
_AUTHWALL_URL_MARKERS = ("authwall", "/login", "/uas/login", "/checkpoint/")


def _looks_like_authwall(url: str) -> bool:
    low = (url or "").lower()
    return any(m in low for m in _AUTHWALL_URL_MARKERS)


def _dump_debug(tag: str, url: str, html: str) -> None:
    """Best-effort raw-HTML/URL dump for diagnosing a single live run.

    Opt-in only: a no-op unless LARP_LIVE_DEBUG_DIR is set. Never raises.
    Writes only the page URL and HTML body, never cookies or secrets, so a
    live run's output can be inspected afterward to tell an auth failure
    apart from a genuine parser gap.
    """
    debug_dir = os.environ.get("LARP_LIVE_DEBUG_DIR")
    if not debug_dir:
        return
    try:
        d = Path(debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tag}_url.txt").write_text(url or "", encoding="utf-8")
        (d / f"{tag}.html").write_text(html or "", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fast single-fetch mode (LARP Detector only, never Org Outreach)
# ---------------------------------------------------------------------------
#
# The shared linkedin_human.HumanSession is tuned for BULK outreach: its
# _read_profile_like_human does 6 hardcoded ~1.8s scroll sleeps plus an up-to
# 18s "finished reading" dwell, and its default config randomly detours to the
# LinkedIn feed/messages mid-fetch (distraction_chance=0.35). That pacing is
# REQUIRED for bulk stealth but ruinous for a single on-demand overlay check
# (110 to 150s+ per profile). We drive the SAME session in a fast mode WITHOUT
# editing the shared module: a fast HumanConfig instance (below) plus an
# instance-level monkeypatch of sess._read_profile_like_human (see fetch_profile).
# Nothing here changes Org Outreach behavior; it only affects the one session
# instance this module opens.

_FAST_READ_SCROLLS = 4
_FAST_READ_SCROLL_WAIT_MS = 350
# Short, graceful wait for the page's key content to render before we read the
# HTML. Fixes the empty-name race: a blind short dwell used to snapshot the
# page before the top-card h1 had rendered.
_FAST_READ_SELECTOR_TIMEOUT_MS = 6000


def _fast_read_like_human(page) -> None:
    """Lightweight, content-aware replacement for
    HumanSession._read_profile_like_human, used ONLY by this module's single
    on-demand fetch (monkeypatched onto the one session instance; never touches
    the shared class or Org Outreach's bulk pacing).

    Instead of 6 hardcoded ~1.8s scroll sleeps plus an up-to 18s dwell, it:
      1. waits (short, graceful) for THIS page's key content to actually
         render before the caller reads page HTML. On the base profile page
         that is the top-card h1 carrying real text (so the name is present,
         fixing the blind-dwell empty-name race); on a /details/experience or
         /details/education page that is the section container (or any line
         carrying a 4-digit year), so the list is loaded before we parse it.
      2. does a few quick scrolls with short waits to trigger any lazy-loaded
         rows, then
      3. a short fixed settle. We deliberately do NOT wait_for_load_state
         "networkidle": LinkedIn holds sockets open (messaging websocket,
         telemetry) so networkidle would almost always burn its full timeout
         for no benefit.
    Never raises: every step degrades to "move on" so a slow selector or a
    page without an h1 can never break a fetch.
    """
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""

    year_probe = (
        "Array.from(document.querySelectorAll('p, span'))"
        ".some(function(e){return /\\b(19|20)\\d{2}\\b/.test(e.innerText || '');})"
    )
    if "/details/experience" in url:
        wait_js = (
            "() => { const s = document.querySelector("
            "'[data-testid*=\"ExperienceDetailsSection\"]'); return !!s || " + year_probe + "; }"
        )
    elif "/details/education" in url:
        wait_js = (
            "() => { const s = document.querySelector("
            "'[data-testid*=\"EducationDetailsSection\"]'); return !!s || " + year_probe + "; }"
        )
    else:
        wait_js = (
            "() => { const h = document.querySelector('h1');"
            " return !!h && h.innerText.trim().length > 0; }"
        )

    try:
        page.wait_for_function(wait_js, timeout=_FAST_READ_SELECTOR_TIMEOUT_MS)
    except Exception:
        pass

    for _ in range(_FAST_READ_SCROLLS):
        try:
            page.mouse.wheel(0, 1400)
        except Exception:
            break
        try:
            page.wait_for_timeout(_FAST_READ_SCROLL_WAIT_MS)
        except Exception:
            break

    try:
        page.wait_for_timeout(500)
    except Exception:
        pass


def _build_fast_config(linkedin_human_module):
    """A fast, single-fetch HumanConfig built from the shared module's own
    dataclass (so we never edit its defaults). headless=True (a single
    on-demand fetch, and a popping window would break the overlay's stealth),
    no mid-fetch feed/messages detours, and a tiny post-scrape beat. The
    per-session / per-hour caps are kept AS-IS so single-fetch ban-safety
    semantics are unchanged; they do not slow one fetch. break_every_n is set
    huge so a single fetch never triggers a 5 to 12 minute idle break.
    """
    return linkedin_human_module.HumanConfig(
        break_every_n_profiles=1_000_000,
        distraction_chance=0.0,
        page_dwell_mean_s=1.0,
        scroll_steps_mean=3,
        headless=True,
    )


def fetch_profile(url: str, live: bool = False) -> dict:
    """Fetch a FULL structured profile for a LinkedIn URL.

    Returns:
        {
          "profile_url": str,
          "identity": {name, headline, current_company, location, image},
          "experience": [ {title, company, start_date, end_date, location}, ... ],
          "education":  [ {school, degree, start_date, end_date}, ... ],
          "posts":      [ {text, url}, ... ],   # the author's own recent posts
        }
        "posts" is the person's OWN recent activity text (bounded, best-effort);
        it is [] when the activity page could not be read or carried no authored
        posts, and its absence never affects the experience/education parse.
        identity["image"] is "" unless the Aero top-card's profile-picture
        <img> was actually found (see parse_identity_html); never fabricated.

    live=False (default) refuses to touch the network and raises, so nothing
    accidentally burns the owner's LinkedIn session during dev or tests. Pass
    live=True (wired to the CLI --live flag) to actually fetch.
    """
    if not live:
        raise RuntimeError(
            "fetch_profile called with live=False. Live LinkedIn fetches are "
            "gated behind the --live flag to protect the account. Use the "
            "pure parse_experience_html(html) for offline work."
        )

    # Chromium's omnibox ELIDES the scheme: a live read from Comet/Chrome/Edge
    # comes back as "linkedin.com/in/foo/", not "https://linkedin.com/in/foo/".
    # A schemeless URL fails the fetch and produces an empty, shallow scan, so
    # normalize to an absolute https URL here, the single choke point every
    # caller (auto-capture, paste, vision-resolved) flows through.
    url = (url or "").strip()
    if url and not re.match(r"^[a-z][a-z0-9+.\-]*://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")

    linkedin_human_module = _import_human_session()
    HumanSession = linkedin_human_module.HumanSession

    bridged = False
    if not getattr(linkedin_human_module, "_builtin", False):
        bridged = _bridge_fresh_session_cookies(linkedin_human_module)
    if bridged:
        import logging

        logging.getLogger(__name__).info(
            "Bridged fresh LinkedIn session cookies from LINKEDIN_STATE_PATH "
            "into %s", linkedin_human_module.COOKIES_PATH
        )

    fast_cfg = _build_fast_config(linkedin_human_module)
    with HumanSession(config=fast_cfg) as sess:
        # Fast single-fetch mode: swap the shared bulk-stealth reading rhythm
        # (6 hardcoded ~1.8s scrolls + up-to 18s dwell) for a lightweight,
        # content-aware reader on THIS instance only. scrape_profile and
        # _load_details_page both call sess._read_profile_like_human(), so this
        # one instance attribute makes all three page loads fast without
        # editing the shared linkedin_human module. Assigned as a plain
        # instance attribute (no self passed) to shadow the bound method.
        sess._read_profile_like_human = lambda: _fast_read_like_human(sess._page)

        # Reuse the existing single-page scrape for identity + top card.
        base = sess.scrape_profile(url)
        if base is None:
            raise RuntimeError(
                "LinkedIn session returned no profile (challenge or rate limit "
                "hit). Aborting without automatic retries."
            )

        base_url = base.get("url", "") or ""
        _dump_debug("base", base_url, base.get("html_excerpt", ""))
        if _looks_like_authwall(base_url):
            raise RuntimeError(
                "LinkedIn bounced this fetch to an authwall/login page "
                f"(landed on {base_url}) instead of the profile. The bridged "
                "session is invalid or expired, this is an auth failure, not "
                "a parser bug. Reconnect LinkedIn in Settings and try again; "
                "do not retry automatically."
            )

        # Expand full experience via the dedicated details page. This is the
        # key addition over the base scraper, which only grabs the current role.
        exp_html = _load_details_page(sess, url, "experience")
        if _looks_like_authwall(sess._page.url):
            raise RuntimeError(
                "LinkedIn bounced the experience details page to an "
                f"authwall/login page (landed on {sess._page.url}). The "
                "bridged session is invalid or expired, this is an auth "
                "failure, not a parser bug. Refresh the session and retry "
                "manually; do not retry automatically."
            )
        experience = parse_experience_html(exp_html) if exp_html else []

        edu_html = _load_details_page(sess, url, "education")
        education = parse_education_html(edu_html) if edu_html else []

        # Best-effort capture of the person's OWN recent posts (bounded, never
        # raises). This is where the inflatable content-claims live ("crossed
        # 50k users", "we raised $2M"); parse_posts_html degrades to [] on any
        # failure, so a posts miss can never break the experience/education
        # parse above. Skipped if the activity page bounced to an authwall.
        posts: list[dict] = []
        try:
            activity_html = _load_activity_page(sess, url)
            if activity_html and not _looks_like_authwall(sess._page.url):
                posts = parse_posts_html(activity_html)
        except Exception:
            posts = []

        # Best-effort capture of the profile's OWN declared external links from
        # the contact-info overlay (profile-declared GitHub handle / personal
        # site / Twitter). This unblocks the connector disambiguators with the
        # person's own data, no namesake guessing. Wholly best-effort: skipped
        # on an authwall bounce, and parse_contact_info_html degrades to {} on
        # any failure, so a contact-info miss can never break the parse above.
        contact: dict = {}
        contact_html = ""
        try:
            contact_html = _load_contact_info_page(sess, url)
            if contact_html and not _looks_like_authwall(sess._page.url):
                contact = parse_contact_info_html(contact_html)
        except Exception:
            contact = {}

        current_entry = next(
            (
                entry
                for entry in experience
                if (entry.get("end_date") or "").strip().lower() == "present"
                and (entry.get("company_url") or "").strip()
            ),
            None,
        )
        company_about_html = ""
        company_website = ""
        if not (contact.get("websites") or []) and current_entry is not None:
            try:
                company_about_html = _load_company_about_page(
                    sess, current_entry.get("company_url") or ""
                )
                if company_about_html and not _looks_like_authwall(sess._page.url):
                    company_website = parse_company_about_website_html(
                        company_about_html
                    )
            except Exception:
                company_website = ""
        # Map from the session scrape_profile output shape:
        #   {url, headline, current_role, education, html_excerpt}
        # where `headline` is the profile h1 (the person's NAME) and
        # `current_role` is the tagline under it (the true headline). On the
        # current Aero DOM that base scraper often returns empty, so we fall
        # back to the owner's top card parsed straight out of the details HTML.
        # The base scraper has no location selector, so location stays "".
        current_role = base.get("current_role", "") or ""
        base_name = (base.get("headline", "") or "").strip()
        parsed_id = parse_identity_html(exp_html or edu_html or "", url)
        name = base_name or parsed_id.get("name", "")
        headline = current_role.strip() or parsed_id.get("headline", "")
        inferred_company = _company_from_current_role(headline)
        if not inferred_company and current_entry is not None:
            inferred_company = (current_entry.get("company") or "").strip()
        hints = _build_hints(contact)
        if company_website:
            websites = list(hints.get("websites") or [])
            if company_website not in websites:
                websites.append(company_website)
            hints["websites"] = websites
            hints["company_website"] = company_website
            company_domain = _domain_of(company_website)
            if company_domain:
                hints["company_domain"] = company_domain
        identity = {
            "name": name,
            "headline": headline,
            "current_company": inferred_company,
            "location": "",
            # Best-effort profile photo URL, MINIMAL capture from the Aero
            # top-card (see parse_identity_html). "" when not reliably
            # present; never invented.
            "image": parsed_id.get("image", ""),
            # Connector hints derived from the profile's OWN contact-info
            # overlay (github_login / personal_site / website / domain /
            # twitter). Always present, possibly {}. JSON-serializable plain
            # strings (rides the queue file). Consumed by verify.gather_evidence.
            "hints": hints,
        }

    # Extraction manifest: a pure, code-computed record of WHAT this scrape
    # actually captured, stamped with zero changes to the parse above. The
    # honesty layer (dossier.scan_depth) reads method + experience_count to
    # decide whether this was a real, full check: a live scrape that parsed zero
    # experience (a login wall served instead of the profile, or a DOM shift)
    # is classified "shallow" off this manifest and can never accrue
    # absence-based suspicion. details_page_loaded records whether the
    # /details/experience expansion returned HTML at all, so a descriptions-poor
    # scrape is visible to the operator.
    with_description_count = sum(
        1 for e in experience if (e.get("description") or "").strip()
    )
    return {
        "profile_url": url,
        "identity": identity,
        "experience": experience,
        "education": education,
        "posts": posts,
        "_extraction": {
            "method": "live_scrape",
            "experience_count": len(experience),
            "with_description_count": with_description_count,
            "posts_count": len(posts),
            "details_page_loaded": bool(exp_html),
            # Observability only: whether the contact-info overlay returned HTML
            # and how many links it yielded. scan_depth must NOT read these (a
            # missing overlay never demotes a full scan to shallow).
            "contact_info_loaded": bool(contact_html),
            "contact_links_count": (
                len(contact.get("websites", []))
                + (1 if contact.get("github_url") else 0)
                + (1 if contact.get("twitter_url") else 0)
            ),
            "company_about_loaded": bool(company_about_html),
            "company_website_recovered": bool(company_website),
        },
    }


def _load_details_page(sess, url: str, section: str) -> str:
    """Navigate the existing session's page to /details/<section>/ and return HTML.

    Uses the session's own playwright page and human-paced reader so we stay
    within the established anti-detection behavior. Returns "" on failure.
    """
    try:
        page = sess._page  # reuse the authenticated, fingerprint-managed page
        base_url = url.split("?")[0].rstrip("/")
        details_url = f"{base_url}/details/{section}/"
        page.goto(details_url, timeout=45000, wait_until="domcontentloaded")
        # Reuse the human reading rhythm to load lazy content.
        try:
            sess._read_profile_like_human()
        except Exception:
            pass
        html = page.content()
        _dump_debug(section, page.url, html)
        return html
    except Exception:
        return ""


def _load_company_about_page(sess, company_url: str) -> str:
    """Load one current-employer company About page in the existing session."""
    if not company_url:
        return ""
    try:
        page = sess._page
        about_url = company_url.split("?")[0].rstrip("/") + "/about/"
        page.goto(about_url, timeout=45000, wait_until="domcontentloaded")
        try:
            sess._read_profile_like_human()
        except Exception:
            pass
        html = page.content()
        _dump_debug("company_about", page.url, html)
        return html
    except Exception:
        return ""


def _company_from_current_role(current_role: str) -> str:
    """Best-effort company from a 'Title at Company' or 'Title @ Company'
    current-role string. LinkedIn headlines phrase this either way (e.g.
    "CTO & Co-Founder @ Red Barn Robotics"), so both are matched: "at" is
    tried first since it is the more common LinkedIn phrasing, then "@".

    A headline can also contain several independent affiliations, for example
    "Returning SDE Intern @ AWS, Jane Street AMP | CS @ Texas A&M". That is not
    one company. Returning the whole suffix poisons every downstream query and
    can even turn an unrelated post number into a fake product claim. Reject
    multi-entity suffixes and let the structured experience rows provide the
    per-claim employer instead.
    """
    if not current_role:
        return ""

    def _single_company(value: str) -> str:
        candidate = _company_from_line(value.strip())
        if not candidate:
            return ""
        if any(separator in candidate for separator in ("|", ",", ";")):
            return ""
        if "@" in candidate or re.search(r"\s[/+]\s", candidate):
            return ""
        return candidate

    m = re.search(r"\bat\s+(.+)$", current_role)
    if m:
        return _single_company(m.group(1))
    m = re.search(r"@\s*(.+)$", current_role)
    if m:
        return _single_company(m.group(1))
    return ""


def parse_education_html(html: str) -> list[dict]:
    """Parse a LinkedIn education section into entries.

    Returns [{school, degree, start_date, end_date}]. Education entries are
    flat (no grouping), so this is a lighter cousin of the experience parser.
    Dispatches on DOM shape exactly like parse_experience_html.
    """
    if not html or not html.strip():
        return []
    if _is_legacy_dom(html):
        return _parse_education_legacy(html)
    try:
        return _parse_education_aero(html)
    except Exception:
        return []


_ACTIVITIES_RE = re.compile(r"^\s*Activities and societies\b", re.IGNORECASE)


def _looks_like_education_meta(line: str) -> bool:
    """True for a line that is neither a school nor a degree label: an
    "Activities and societies: ..." line, or a longer free-text description
    (grade, coursework, honors write-up). Reuses the same long-sentence
    heuristic _looks_like_description uses for the experience parser.

    Bug 3: the old education parser used a FIXED lines[i-2]/lines[i-1]
    lookback from each date line, which silently field-shifted whenever an
    entry did not have exactly "school, degree, date" in that order: an
    entry missing its degree line entirely (e.g. a bare "Y Combinator"
    program entry), or one with an "Activities and societies" / description
    line sitting BETWEEN the degree and the date, produced a garbled
    school/degree. Filtering meta lines like this one out of the candidate
    pool before indexing (see _parse_education_aero) fixes both cases.
    """
    if _ACTIVITIES_RE.match(line):
        return True
    return _looks_like_description(line)


def _parse_education_aero(html: str) -> list[dict]:
    """Parse the current Aero education DOM into normalized entries.

    Same forward-moving-cursor, date-line-as-spine approach
    _parse_experience_aero uses, simplified for education's flat (never
    grouped) shape: lines accumulate in `pending` since the last entry closed;
    when a date-RANGE line is hit, every meta line (see
    _looks_like_education_meta) is filtered out of `pending` first, and the
    LAST TWO remaining lines become (school, degree), so a stray "Activities
    and societies" or description line never becomes a school or degree,
    regardless of whether it trails the previous entry or sits ahead of this
    one's own date. An entry with only one real candidate line (a bare
    "Y Combinator" entry with no degree at all) still gets its school parsed
    correctly instead of stealing whatever text happened to sit two lines
    back in the flat list.

    Strict date-range detection keeps the ad / "people you may know" /
    footer noise that trails the section out of the results even when the
    scope falls back to <main>.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    scope = _aero_scope(soup, "EducationDetailsSection")
    lines = _aero_lines(scope)
    if lines and lines[0].strip().lower() in ("experience", "education"):
        lines = lines[1:]

    out: list[dict] = []
    pending: list[str] = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        try:
            if _is_date_range_line(line):
                candidates = [ln for ln in pending if not _looks_like_education_meta(ln)]
                if len(candidates) >= 2:
                    school = candidates[-2].strip()
                    degree = candidates[-1].strip()
                elif len(candidates) == 1:
                    school = candidates[-1].strip()
                    degree = ""
                else:
                    school = degree = ""
                start, end = _split_date_range(line)
                out.append(
                    {
                        "school": school,
                        "degree": degree,
                        "start_date": start,
                        "end_date": end,
                    }
                )
                pending = []
                i += 1
                continue

            pending.append(line)
            i += 1
        except Exception:
            # One malformed line/entry must not take down the whole parse.
            pending = []
            i += 1
            continue

    return out


def _parse_education_legacy(html: str) -> list[dict]:
    """Parse the older artdeco/pvs-list education DOM (dual-render spans)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("beautifulsoup4 is required to parse LinkedIn HTML")

    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(attrs={"data-section": "education"}) or soup
    items = container.find_all(
        "li",
        class_=lambda c: bool(c)
        and ("artdeco-list__item" in c or "pvs-list__paged-list-item" in c),
    )
    out: list[dict] = []
    for li in items:
        lines = _visible_texts(li)
        if not lines:
            continue
        school = lines[0].strip()
        degree = ""
        start = end = ""
        for line in lines[1:]:
            if _looks_like_date(line) and not start and not end:
                start, end = _split_date_range(line)
            elif not degree:
                degree = line.strip()
        out.append(
            {
                "school": school,
                "degree": degree,
                "start_date": start,
                "end_date": end,
            }
        )
    return out


def parse_identity_html(html: str, profile_url: str = "") -> dict:
    """Extract identity (name, headline, current_company, location, image) from HTML.

    The Aero details pages render the profile owner's top card as a self-link
    <a href=".../in/<handle>/"> whose two <p> lines are the name then the
    headline. We anchor on that self-link (matched against the profile handle
    when a URL is given, else the first self-link that carries a headline),
    and fall back to the document <title> ("Name | LinkedIn") for the name.

    image: MINIMAL, best-effort capture of the profile picture <img> src
    inside that same top-card anchor, matched by its media.licdn.com CDN
    domain (the one reliable signal that separates the real profile photo
    from the decorative background svg the Aero DOM also renders in that
    anchor). "" when no such img is found: this code never invents an image
    URL, it only reports what it actually saw in the markup.

    location is left "" here: the details-page top card does not carry it.
    Never raises; returns whatever it can, empty strings for the rest.
    """
    identity = {"name": "", "headline": "", "current_company": "", "location": "", "image": ""}
    if not html or not html.strip():
        return identity
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        handle = ""
        m = re.search(r"/in/([^/?#]+)", profile_url or "")
        if m:
            handle = m.group(1)

        anchor = None
        if handle:
            anchor = soup.find(
                "a", href=re.compile(r"/in/" + re.escape(handle) + r"/?(?:[?#]|$)")
            )
        if anchor is None:
            # First self-link to any profile that has at least two text lines
            # (name + headline), i.e. a real top card, not a bare mention.
            for a in soup.find_all("a", href=re.compile(r"/in/[^/?#]+")):
                if len([p for p in a.find_all("p") if p.get_text(strip=True)]) >= 2:
                    anchor = a
                    break

        if anchor is not None:
            plines = [
                p.get_text(" ", strip=True)
                for p in anchor.find_all("p")
                if p.get_text(strip=True)
            ]
            if plines:
                identity["name"] = plines[0].strip()
            if len(plines) >= 2:
                identity["headline"] = plines[1].strip()

            img = anchor.find("img", src=re.compile(r"media\.licdn\.com", re.IGNORECASE))
            if img is not None:
                src = (img.get("src") or "").strip()
                if src:
                    identity["image"] = src

        if not identity["name"] and soup.title and soup.title.string:
            identity["name"] = soup.title.string.split("|")[0].strip()

        identity["current_company"] = _company_from_current_role(identity["headline"])
    except Exception:
        return identity
    return identity


# ---------------------------------------------------------------------------
# Posts / activity extraction (offline-testable pure parser)
# ---------------------------------------------------------------------------
#
# The person's OWN recent posts are exactly where the inflatable content-claims
# live ("grew to 50k users", "we raised $2M", "hit #1 on the App Store"). This
# captures that text into raw_profile["posts"] = [{"text": ..., "url": ...}] so
# the same traction machinery that decomposes experience descriptions
# (llm._traction_claims_from_description) can turn a post's checkable numbers
# into real claims that flow through the aggregate -> director -> cross-check
# pipeline.
#
# This scraping is FRAGILE (the activity DOM is React-rendered and hashed), so
# the whole path is bounded and never-raises: a missing or broken posts section
# degrades to [] and can never break the experience/education parse.

# Class TOKENS that mark the root of ONE post/update on the activity page.
# These are matched as WHOLE class tokens (not substrings): LinkedIn hangs many
# sub-element classes off the same prefix (feed-shared-update-v2__description,
# feed-shared-update-v2__control-menu-container, update-components-update-v2__commentary),
# and a substring match on "feed-shared-update-v2" / "update-components-update"
# would grab all of those sub-parts and mistake a post's own children for
# nested reshares, dropping every real post. Whole-token matching pins the
# outermost post root only. feed-shared-update-v2 is the live-DOM root;
# update-components-update-v2 is kept as a resilient root variant.
_POST_ROOT_TOKENS = ("feed-shared-update-v2", "update-components-update-v2")
# urn:li:activity / share / ugcPost is the stable id every post root carries in
# its data-urn; used to build a permalink, never to fabricate one.
_POST_URN_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):[0-9]+")
# Class markers for the element that holds the AUTHOR's own commentary text
# (not the reshared original's text, which sits in a NESTED post root). Matched
# by substring so a hashed sibling class never hides the stable marker; the
# live DOM puts it in update-components-text / update-components-update-v2__commentary,
# the synthetic fixture in feed-shared-update-v2__description.
_POST_TEXT_MARKERS = (
    "update-components-text",
    "update-components-update-v2__commentary",
    "feed-shared-update-v2__description",
    "feed-shared-text",
    "feed-shared-inline-show-more-text",
)
# Bounds: cap how many posts and how much text per post we ever keep, so a
# runaway activity page can never blow up memory or the downstream decompose.
_MAX_POSTS = 10
_MAX_POST_CHARS = 3000


def _class_has_marker(class_value, markers) -> bool:
    """True if a BeautifulSoup class attribute (a list, a string, or None)
    carries any of `markers` as a substring. Never raises."""
    if not class_value:
        return False
    try:
        joined = " ".join(class_value) if isinstance(class_value, (list, tuple)) else str(class_value)
    except Exception:
        return False
    return any(m in joined for m in markers)


def _class_tokens(node) -> list:
    """The whole class tokens on a node as a list of strings. Never raises."""
    try:
        c = node.get("class")
    except Exception:
        return []
    if not c:
        return []
    if isinstance(c, (list, tuple)):
        return [str(x) for x in c]
    return str(c).split()


def _is_post_root(node) -> bool:
    """True if `node` is the root of ONE post: it carries a post-root class as a
    WHOLE token. Whole-token (not substring) so sub-elements that merely share
    the prefix (…__description, …__commentary, …__control-menu-container) are
    never mistaken for a post root. Never raises."""
    return any(t in _class_tokens(node) for t in _POST_ROOT_TOKENS)


def _is_descendant_of(node, ancestor) -> bool:
    """True if `ancestor` is somewhere above `node` in the tree."""
    p = getattr(node, "parent", None)
    while p is not None:
        if p is ancestor:
            return True
        p = getattr(p, "parent", None)
    return False


def _has_ancestor_in(node, id_set: set) -> bool:
    """True if any ancestor of `node` is in `id_set` (by python id)."""
    p = getattr(node, "parent", None)
    while p is not None:
        if id(p) in id_set:
            return True
        p = getattr(p, "parent", None)
    return False


def _post_permalink(node) -> str:
    """Best-effort permalink for a post. The live activity DOM carries no
    per-post anchor href, but every post root has a data-urn (urn:li:activity:...)
    from which the canonical /feed/update/ URL is built. Falls back to a
    feed/update anchor href if one is present. Never raises, and never
    fabricates a URL from a urn that does not match the expected shape."""
    try:
        urn = (node.get("data-urn") or "").strip()
    except Exception:
        urn = ""
    if urn and _POST_URN_RE.fullmatch(urn):
        return "https://www.linkedin.com/feed/update/" + urn + "/"
    try:
        a = node.find("a", href=re.compile(r"/feed/update/|/posts/|activity[:\-]"))
        if a is not None:
            href = (a.get("href") or "").strip()
            if href:
                return href
    except Exception:
        return ""
    return ""


def _own_commentary_text(node, all_containers) -> str:
    """The AUTHOR's own commentary text inside one post container.

    Only text nodes that are NOT inside a nested post container (the reshared
    original, i.e. someone else's words) count. A bare reshare, whose only text
    belongs to the nested original, yields "" and is skipped by parse_posts_html.
    """
    nested_ids = {
        id(c) for c in all_containers if c is not node and _is_descendant_of(c, node)
    }
    parts: list[str] = []
    try:
        text_nodes = node.find_all(
            attrs={"class": lambda c: _class_has_marker(c, _POST_TEXT_MARKERS)}
        )
    except Exception:
        text_nodes = []
    # Keep only the OUTERMOST matching text node per subtree: the live DOM nests
    # a matching feed-shared-inline-show-more-text INSIDE the matching
    # update-components-update-v2__commentary div, and get_text on the outer node
    # already covers the inner one. Appending both would double the post body
    # (and corrupt the downstream number extraction). This mirrors the
    # outermost-root filter and is safe for the synthetic fixture, whose text
    # node has no matching ancestor.
    matched_ids = {id(el) for el in text_nodes}
    for el in text_nodes:
        try:
            if nested_ids and _has_ancestor_in(el, nested_ids):
                continue
            if _has_ancestor_in(el, matched_ids):
                continue
            txt = el.get_text(" ", strip=True)
        except Exception:
            continue
        if txt and (not parts or parts[-1] != txt):
            parts.append(txt)
    joined = " ".join(parts).strip()
    # Drop the trailing "…more" expander label the DOM appends to a truncated
    # post; it is a UI control, not the author's words. The ellipsis is required
    # so a post that legitimately ends in the word "more" is left untouched.
    joined = re.sub(r"\s*[.…]{1,3}\s*more\s*$", "", joined, flags=re.IGNORECASE).strip()
    return joined


def parse_posts_html(html: str) -> list[dict]:
    """Parse a LinkedIn activity/posts HTML block into the author's own posts.

    Returns [{"text": str, "url": str}], capped at _MAX_POSTS and each text
    capped at _MAX_POST_CHARS. Captures only the AUTHOR's own posts: a bare
    reshare that carries no commentary of the author's own (only the nested
    original's text) is skipped, so another person's words never enter this
    subject's claims.

    Defensive by design, exactly like parse_experience_html: it must never
    raise on malformed, incomplete, or unexpected markup. A missing posts
    section, absent BeautifulSoup, or broken HTML all degrade to [] rather
    than blow up, so a posts failure can never break the experience/education
    parse or the downstream scan.
    """
    if not html or not html.strip():
        return []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        containers = [node for node in soup.find_all(True) if _is_post_root(node)]
        if not containers:
            return []
        # Keep only OUTERMOST post containers (a reshare nests the original
        # post in the same container class); the nested original is never a
        # post of this author.
        container_ids = {id(c) for c in containers}
        top = [c for c in containers if not _has_ancestor_in(c, container_ids)]

        out: list[dict] = []
        seen: set[str] = set()
        for node in top:
            try:
                text = _own_commentary_text(node, containers)
            except Exception:
                continue
            if not text:
                continue
            text = text[:_MAX_POST_CHARS].strip()
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"text": text, "url": _post_permalink(node)})
            if len(out) >= _MAX_POSTS:
                break
        return out
    except Exception:
        return []


# Activity-feed reader tunables. The /recent-activity/all/ feed is React-
# rendered and lazy-loads more posts only as you scroll, so it needs a
# different wait than a profile page: wait for a post CONTAINER to render (not
# the profile top-card, which does not exist here), then scroll several times
# to pull a handful of posts into the DOM before snapshotting the HTML.
_ACTIVITY_SELECTOR_TIMEOUT_MS = 9000
_ACTIVITY_SCROLL_STEPS = 6
_ACTIVITY_SCROLL_WAIT_MS = 1300
# Broad, resilient set of markers for "a post is in the DOM": the classic
# feed-shared-update-v2 class, the impression container, and the data-urn that
# every activity item carries even when the hashed class names churn.
_ACTIVITY_POST_SELECTOR = (
    "div.feed-shared-update-v2, div.fie-impression-container, "
    "[data-urn*='urn:li:activity'], [data-urn*='urn:li:share']"
)


def _load_activity_page(sess, url: str) -> str:
    """Navigate the existing session's page to /recent-activity/all/ and return
    its HTML. Best-effort and bounded: reuses the session's own authenticated
    page, waits for a POST CONTAINER (the activity feed is React-rendered and
    has no profile top-card, so the profile reader's top-card wait is wrong
    here), then scrolls to lazy-load a handful of posts. Returns "" on any
    failure so a posts miss never aborts a fetch.
    """
    try:
        page = sess._page
        base_url = url.split("?")[0].rstrip("/")
        activity_url = f"{base_url}/recent-activity/all/"
        page.goto(activity_url, timeout=45000, wait_until="domcontentloaded")
        # Wait for the first post to render (timeout is caught: a person with no
        # posts, or a churned DOM, still falls through to grab what is there).
        try:
            page.wait_for_selector(_ACTIVITY_POST_SELECTOR, timeout=_ACTIVITY_SELECTOR_TIMEOUT_MS)
        except Exception:
            pass
        # Scroll to trigger the lazy-load and pull a few posts into the DOM.
        for _ in range(_ACTIVITY_SCROLL_STEPS):
            try:
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(_ACTIVITY_SCROLL_WAIT_MS)
            except Exception:
                break
        try:
            page.wait_for_timeout(700)
        except Exception:
            pass
        html = page.content()
        _dump_debug("activity", page.url, html)
        return html
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Contact-info overlay: the profile's OWN declared external links.
#
# On LinkedIn the external links (website(s), GitHub, Twitter/X, email) live
# behind the "Contact info" modal at /overlay/contact-info/, NOT in the main
# profile DOM. Capturing them gives the connectors a profile-DECLARED GitHub
# handle / personal site (no namesake guessing). The parse is host-classification
# driven: it reads ANY anchor in the overlay and classifies by DOMAIN, never by
# hashed CSS class, so a LinkedIn DOM churn degrades to "fewer rows found", never
# a crash or a mislabeled link.
# ---------------------------------------------------------------------------

_CONTACT_SELECTOR_TIMEOUT_MS = 6000


def _host_matches(netloc: str, domain: str) -> bool:
    """True when netloc is exactly domain or a subdomain of it (case-folded)."""
    netloc = (netloc or "").lower()
    domain = domain.lower()
    return netloc == domain or netloc.endswith("." + domain)


def _resolve_linkedin_redirect(href: str) -> str:
    """Unwrap LinkedIn's outbound redirect wrappers (linkedin.com/redir/... or
    lnkd.in/...) by returning the unquoted `url` query param when present. Any
    other href, or a wrapper without a url param, is returned unchanged. Never
    raises."""
    try:
        parsed = urlparse(href)
    except Exception:
        return href
    netloc = (parsed.netloc or "").lower()
    if "linkedin.com" not in netloc and "lnkd.in" not in netloc:
        return href
    try:
        vals = parse_qs(parsed.query or "").get("url")
    except Exception:
        return href
    if vals:
        return unquote(vals[0])
    return href


def parse_contact_info_html(html: str) -> dict:
    """Pure parse of the contact-info overlay HTML into
    {"websites": list[str], "github_url": str, "twitter_url": str,
     "email": str}. Empty strings / [] when absent. Never raises; returns the
    empty shape on any malformed input.

    Classification is by HOST after resolving LinkedIn redirect wrappers:
    github.com -> github_url (first wins); twitter.com / x.com -> twitter_url;
    mailto: -> email; any linkedin.com link (the profile self-link row) is
    discarded; everything else is appended to websites in DOM order, deduped.
    """
    empty = {"github_url": "", "websites": [], "twitter_url": "", "email": ""}
    if not html:
        return dict(empty)
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return dict(empty)
    try:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.find_all("a", href=True)
    except Exception:
        return dict(empty)

    github_url = ""
    twitter_url = ""
    email = ""
    websites: list[str] = []
    seen: set[str] = set()

    for a in anchors:
        try:
            href = (a.get("href") or "").strip()
        except Exception:
            continue
        if not href:
            continue
        href = _resolve_linkedin_redirect(href)
        try:
            parsed = urlparse(href)
        except Exception:
            continue
        scheme = (parsed.scheme or "").lower()
        netloc = (parsed.netloc or "").lower()

        if scheme == "mailto":
            if not email:
                email = (parsed.path or "").strip()
            continue
        if not netloc:
            continue  # relative / junk anchor
        if _host_matches(netloc, "github.com"):
            if not github_url:
                github_url = href
            continue
        if _host_matches(netloc, "twitter.com") or _host_matches(netloc, "x.com"):
            if not twitter_url:
                twitter_url = href
            continue
        if _host_matches(netloc, "linkedin.com"):
            continue  # the profile self-link row: never a hint
        if href not in seen:
            seen.add(href)
            websites.append(href)

    return {
        "github_url": github_url,
        "websites": websites,
        "twitter_url": twitter_url,
        "email": email,
    }


def parse_company_about_website_html(html: str) -> str:
    """Return the first plausible official site from a LinkedIn company page."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        anchors = BeautifulSoup(html, "html.parser").find_all("a", href=True)
    except Exception:
        return ""
    excluded = (
        "linkedin.com",
        "lnkd.in",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "apps.apple.com",
        "play.google.com",
    )
    for anchor in anchors:
        href = _resolve_linkedin_redirect((anchor.get("href") or "").strip())
        try:
            parsed = urlparse(href)
        except Exception:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        host = parsed.netloc.lower()
        if any(_host_matches(host, domain) for domain in excluded):
            continue
        return href
    return ""


def _github_login_from_url(url: str) -> str:
    """First path segment of a github URL (the user/org login). '' when absent.
    github.com/JordanRivera-dev/repo -> 'JordanRivera-dev'; bare github.com -> ''."""
    if not url:
        return ""
    try:
        path = urlparse(url).path or ""
    except Exception:
        return ""
    segments = [p for p in path.split("/") if p]
    return segments[0] if segments else ""


def _domain_of(url: str) -> str:
    """netloc of a URL, www. stripped, lowercased. '' on failure."""
    try:
        netloc = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _build_hints(contact: dict) -> dict:
    """Fold a parse_contact_info_html result into the connector hint dict.
    Keys are EXACTLY what the connectors already look for
    (github._HINT_KEYS = ("domain", "personal_site", "website")) plus the new
    "github_login" (and "twitter", captured for the surface). Omit empty entries
    entirely (no empty-string keys), so a linkless profile yields {}.

    "websites" carries EVERY declared link, in DOM order. The singular keys keep
    their first-website-only semantics on purpose (github.py reads them as the
    person disambiguator and must not be re-pointed), but a founder's PRODUCT
    site is routinely the second or third row here, and the product-site
    resolver cannot weigh a candidate the extractor already discarded.
    """
    contact = contact or {}
    hints: dict = {}

    login = _github_login_from_url((contact.get("github_url") or "").strip())
    if login:
        hints["github_login"] = login

    websites = [w.strip() for w in (contact.get("websites") or []) if (w or "").strip()]
    first = websites[0] if websites else ""
    if first:
        hints["personal_site"] = first
        hints["website"] = first
        domain = _domain_of(first)
        if domain:
            hints["domain"] = domain
    if websites:
        hints["websites"] = websites

    twitter = (contact.get("twitter_url") or "").strip()
    if twitter:
        hints["twitter"] = twitter

    return hints


def _load_contact_info_page(sess, url: str) -> str:
    """Navigate the existing session's page to /overlay/contact-info/ and return
    its HTML. Best-effort and bounded (sibling of _load_details_page /
    _load_activity_page): reuses the session's own authenticated page, waits
    briefly for anchors to render (timeout caught), and returns "" on ANY
    failure so a contact-info miss never aborts a fetch. Never raises.
    """
    try:
        page = sess._page
        base_url = url.split("?")[0].rstrip("/")
        contact_url = f"{base_url}/overlay/contact-info/"
        page.goto(contact_url, timeout=45000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("a[href]", timeout=_CONTACT_SELECTOR_TIMEOUT_MS)
        except Exception:
            pass
        html = page.content()
        _dump_debug("contact", page.url, html)
        return html
    except Exception:
        return ""
