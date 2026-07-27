"""Offline tests for detective.sources.github. No network: the internal
_search_users / _get_user / _get_repos / _get_contributor_count functions are
monkeypatched with realistic sample GitHub API response shapes.

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

from types import SimpleNamespace

import detective.sources.github as github


# A realistic /search/users item (trimmed to the fields this module reads).
def _search_item(login: str) -> dict:
    return {"login": login, "html_url": f"https://github.com/{login}"}


# A realistic /users/{login} response (trimmed to the fields this module reads).
def _user_response(
    login: str,
    created_at: str = "2012-03-01T10:00:00Z",
    public_repos: int = 42,
    bio: str = "",
    company: str = "",
    blog: str = "",
) -> dict:
    return {
        "login": login,
        "html_url": f"https://github.com/{login}",
        "created_at": created_at,
        "public_repos": public_repos,
        "bio": bio,
        "company": company,
        "blog": blog,
    }


def _repo(
    name: str,
    fork: bool = False,
    stars: int = 0,
    description: str = "",
    language: str = "",
    topics: list = None,
    created_at: str = "",
    pushed_at: str = "",
) -> dict:
    return {
        "name": name,
        "fork": fork,
        "stargazers_count": stars,
        "description": description,
        "language": language,
        "topics": topics or [],
        "created_at": created_at,
        "pushed_at": pushed_at,
    }


def _maintained_repo(
    name: str,
    stars: int = 2,
    language: str = "Python",
    description: str = "a real project",
    topics: list = None,
) -> dict:
    """An original repo that was created once and then pushed to over a long
    span (multi-commit, maintained, self-describing): the shape of real work."""
    return _repo(
        name,
        fork=False,
        stars=stars,
        description=description,
        language=language,
        topics=topics if topics is not None else ["cli"],
        created_at="2018-01-01T00:00:00Z",
        pushed_at="2021-06-01T00:00:00Z",
    )


def _single_shot_repo(name: str, language: str = "") -> dict:
    """An original repo created and never pushed to again, no description or
    topics: the shape of a single-commit dump or a tutorial clone."""
    return _repo(
        name,
        fork=False,
        stars=0,
        description="",
        language=language,
        topics=[],
        created_at="2020-05-01T00:00:00Z",
        pushed_at="2020-05-01T00:05:00Z",
    )


# ---------------------------------------------------------------------------
# verify_github: basic gating
# ---------------------------------------------------------------------------


def test_verify_github_empty_name_returns_empty():
    assert github.verify_github("") == []
    assert github.verify_github(None) == []


def test_verify_github_no_candidates_returns_empty(monkeypatch):
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [])
    assert github.verify_github("Nobody Findable") == []


def test_verify_github_search_failure_returns_empty(monkeypatch):
    def boom(name, count=3):
        raise RuntimeError("network down")

    monkeypatch.setattr(github, "_search_users", boom)
    assert github.verify_github("Someone") == []


def test_rate_limit_response_sets_process_cooldown(monkeypatch):
    monkeypatch.setattr(github, "_github_dead_until", 0.0)
    response = SimpleNamespace(
        status_code=403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "4102444800",
        },
    )

    github._observe_rate_limit(response)

    assert github._github_dead_until == 4102444800.0


def test_github_cli_keyring_token_is_used_without_project_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(github, "_gh_cli_path", lambda: "/tmp/fake-gh")
    github._token_from_gh_cli.cache_clear()

    def fake_run(args, **kwargs):
        assert args == [
            "/tmp/fake-gh",
            "auth",
            "token",
            "--hostname",
            "github.com",
        ]
        assert kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout="secret-token\n", stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    headers = github._headers()

    assert headers["Authorization"] == "Bearer secret-token"
    assert github.github_auth_status() == {
        "authenticated": True,
        "source": "github_cli_keyring",
    }
    github._token_from_gh_cli.cache_clear()


def test_explicit_environment_token_precedes_cli_keyring(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "environment-token")
    monkeypatch.setattr(
        github,
        "_token_from_gh_cli",
        lambda: (_ for _ in ()).throw(AssertionError("CLI must not be read")),
    )

    assert github._github_token() == "environment-token"
    assert github._headers()["Authorization"] == "Bearer environment-token"


def test_authenticated_connector_uses_two_repo_deep_limit(monkeypatch):
    observed = {}
    monkeypatch.setattr(github, "_github_token", lambda: "keyring-token")
    monkeypatch.setattr(
        github,
        "_select_deep_repositories",
        lambda repos, company, limit: observed.update(limit=limit) or [],
    )
    monkeypatch.delenv("GITHUB_DEEP_MAX_REPOS", raising=False)

    assert github._deep_inspect_repositories("builder", [], "") == []
    assert observed["limit"] == 2


def test_rate_limited_api_uses_free_public_html_fallback(monkeypatch):
    def limited(*args, **kwargs):
        raise RuntimeError("GitHub API is in rate-limit cooldown")

    monkeypatch.setattr(github, "_search_users", limited)
    monkeypatch.setattr(
        github,
        "_html_search_users",
        lambda name, count=3: [_search_item("janedoe")],
    )
    monkeypatch.setattr(github, "_get_user", limited)
    monkeypatch.setattr(
        github,
        "_html_get_user",
        lambda login: _user_response(login, public_repos=3),
    )
    monkeypatch.setattr(github, "_get_repos", limited)
    monkeypatch.setattr(
        github,
        "_html_get_repos",
        lambda login, count=30: [
            {
                **_maintained_repo("real-project"),
                "full_name": f"{login}/real-project",
                "html_url": f"https://github.com/{login}/real-project",
                "default_branch": "main",
            }
        ],
    )

    evidence = github.verify_github("Jane Doe")

    assert evidence
    assert evidence[0]["source_url"] == "https://github.com/janedoe"


def test_archive_deep_inspection_labels_missing_commit_attribution(monkeypatch):
    repo = {
        **_maintained_repo("engine"),
        "full_name": "builder/engine",
        "html_url": "https://github.com/builder/engine",
        "default_branch": "main",
    }
    monkeypatch.setattr(github, "_get_repo_tree", lambda *args: (_ for _ in ()).throw(RuntimeError("rate limit")))
    monkeypatch.setattr(
        github,
        "_archive_repository_tree",
        lambda *args: {
            "tree": [
                {"type": "blob", "path": "src/main.py", "size": 100},
                {"type": "blob", "path": "tests/test_main.py", "size": 50},
                {"type": "blob", "path": ".github/workflows/test.yml", "size": 30},
                {"type": "blob", "path": "pyproject.toml", "size": 20},
            ],
            "truncated": False,
        },
    )
    monkeypatch.setattr(github, "_sample_repository_configs", lambda *args: {})

    analyses = github._deep_inspect_repositories("builder", [repo], "")
    snippet = github._build_snippet(
        "builder",
        _user_response("builder"),
        "high",
        None,
        deep_analyses=analyses,
    )

    assert analyses[0]["inspection_method"] == "public_archive_fallback"
    assert "commit authorship and account attribution were not checked" in snippet


def test_deep_artifacts_override_conflicting_shallow_thin_heuristic():
    thin = {
        "name": "product",
        "is_fork": False,
        "stars": 0,
        "contributor_count": 1,
        "looks_thin": True,
    }
    analyses = [
        {
            "repo": "product",
            "artifact_read": "substantial",
            "account_linked_commits": 8,
        }
    ]

    reconciled = github._reconcile_thin_with_deep(thin, analyses)

    assert reconciled["looks_thin"] is False
    assert reconciled["deep_override"] is True


# ---------------------------------------------------------------------------
# Evidence record shape + match_confidence policy
# ---------------------------------------------------------------------------


def test_evidence_record_shape_and_low_confidence_by_default(monkeypatch):
    # Login "octodev" carries neither name token, so no disambiguator and no
    # name-pattern match: the load-bearing "low" default.
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("octodev")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    evidence = github.verify_github("Jane Doe", company="Acme Corp")
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record.keys()) == {
        "source_url",
        "snippet",
        "source_name",
        "weight",
        "match_confidence",
    }
    assert record["source_name"] == "github"
    assert record["weight"] == 0.48
    # No disambiguator in the bio/company/blog matched "Acme Corp": low confidence.
    assert record["match_confidence"] == "low"
    assert "2012-03-01" in record["snippet"]


def test_match_confidence_high_when_company_in_bio(monkeypatch):
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("janedoe")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, bio="Building things at Acme Corp"),
    )
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    evidence = github.verify_github("Jane Doe", company="Acme Corp")
    assert evidence[0]["match_confidence"] == "high"


def test_match_confidence_high_when_domain_hint_matches_blog(monkeypatch):
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("janedoe")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, blog="https://janedoe.dev"),
    )
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    evidence = github.verify_github(
        "Jane Doe", company="Acme Corp", hints={"domain": "janedoe.dev"}
    )
    assert evidence[0]["match_confidence"] == "high"


def test_never_asserts_identity_without_disambiguator(monkeypatch):
    # A candidate with a totally unrelated bio/company/blog AND a handle that
    # carries no name token: this module must never silently upgrade confidence
    # just because a candidate exists.
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("octodev")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, bio="I like hiking", company="Other Inc"),
    )
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    evidence = github.verify_github("Jane Doe", company="Acme Corp")
    assert evidence[0]["match_confidence"] == "low"
    assert "may not be the same person" in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Thin-wrapper repo check
# ---------------------------------------------------------------------------


def test_thin_wrapper_repo_detected_via_fork_and_low_stars(monkeypatch):
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("janedoe")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(
        github, "_get_repos", lambda login, count=30: [_repo("acmecorp", fork=True, stars=0)]
    )
    monkeypatch.setattr(github, "_get_contributor_count", lambda owner, repo: 1)

    evidence = github.verify_github("Jane Doe", company="AcmeCorp")
    assert "looks thin" in evidence[0]["snippet"]
    assert "fork" in evidence[0]["snippet"]


def test_substantial_repo_not_flagged_as_thin(monkeypatch):
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("janedoe")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [_repo("acmecorp", fork=False, stars=500)],
    )
    monkeypatch.setattr(github, "_get_contributor_count", lambda owner, repo: 12)

    evidence = github.verify_github("Jane Doe", company="AcmeCorp")
    assert "looks substantial" in evidence[0]["snippet"]


def test_no_matching_repo_means_no_thin_wrapper_note(monkeypatch):
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("janedoe")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(
        github, "_get_repos", lambda login, count=30: [_repo("unrelated-repo")]
    )

    evidence = github.verify_github("Jane Doe", company="AcmeCorp")
    assert "Product repo" not in evidence[0]["snippet"]


# ---------------------------------------------------------------------------
# Feature 2: technical authenticity read
# ---------------------------------------------------------------------------


def test_technical_authenticity_read_substantial(monkeypatch):
    """A real engineer: aged account, several original repos across multiple
    languages with real stars, reads 'substantial'. Judges code substance."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("realdev")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login, created_at="2013-01-01T00:00:00Z"))
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [
            _repo("engine", fork=False, stars=120, language="Rust"),
            _repo("api", fork=False, stars=40, language="Go"),
            _repo("cli", fork=False, stars=15, language="Python"),
        ],
    )

    snippet = github.verify_github("Real Dev")[0]["snippet"]
    assert "Technical authenticity read: substantial" in snippet


def test_technical_authenticity_read_thin_or_absent(monkeypatch):
    """A brand-new account whose only repo is a fork reads 'thin-or-absent':
    no real original engineering footprint."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("newbie")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login, created_at="2025-11-01T00:00:00Z", public_repos=1))
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [_repo("someones-template", fork=True, stars=0)],
    )

    snippet = github.verify_github("New Bie")[0]["snippet"]
    assert "Technical authenticity read: thin-or-absent" in snippet


def test_technical_authenticity_read_present_even_without_company(monkeypatch):
    """The authenticity read is account-wide: it appears even when no company
    was passed (no product-repo match to grade), so a person-identity check
    still gets the 'can they build' signal."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("dev")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(
        github, "_get_repos", lambda login, count=30: [_repo("proj", fork=False, stars=5, language="C++")]
    )

    snippet = github.verify_github("Some Dev")[0]["snippet"]
    assert "Technical authenticity read:" in snippet
    assert "Product repo" not in snippet


# ---------------------------------------------------------------------------
# Feature 3: NUANCED technical authenticity, JUSTIFIED with concrete signals
# ---------------------------------------------------------------------------


def test_every_tech_read_cites_a_signals_clause(monkeypatch):
    """Any technical authenticity read must justify itself with a 'Signals:'
    clause that cites the concrete artifacts behind the call, so a downstream
    reasoning brain and a human can see the basis and not just a bare label."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("dev")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(
        github, "_get_repos", lambda login, count=30: [_maintained_repo("proj")]
    )

    snippet = github.verify_github("Some Dev")[0]["snippet"]
    assert "Technical authenticity read:" in snippet
    assert "Signals:" in snippet


def test_substantial_read_justified_by_craftsmanship_not_just_stars(monkeypatch):
    """Substance, not stardom: an aged account with several ORIGINAL,
    self-describing, multi-commit (maintained) repos reads 'substantial' and
    cites those craftsmanship signals, even when stars are modest and the work
    is all in one language. A downstream brain should see WHY."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("builder")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, created_at="2013-01-01T00:00:00Z"),
    )
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [
            _maintained_repo("parser", stars=2),
            _maintained_repo("scheduler", stars=1),
            _maintained_repo("indexer", stars=3),
            _maintained_repo("toolkit", stars=2),
        ],
    )

    snippet = github.verify_github("Real Builder")[0]["snippet"]
    assert "Technical authenticity read: substantial" in snippet
    # Justified by descriptions and maintenance, not by a big star count.
    assert "maintained" in snippet
    assert "describe themselves" in snippet


def test_thin_read_for_fork_pile_with_one_bare_dump(monkeypatch):
    """An account that is mostly forks plus a single bare, single-commit
    original (no description, no topics, never pushed again, no stars) reads
    'thin-or-absent' and cites the forks and the single-commit dump. This is
    the wrapper/tutorial-grade tell: no real original engineering footprint."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("larper")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, created_at="2016-01-01T00:00:00Z", public_repos=5),
    )
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [
            _repo("awesome-list", fork=True, stars=0),
            _repo("react-tutorial", fork=True, stars=0),
            _repo("dotfiles-clone", fork=True, stars=0),
            _repo("todo-app", fork=True, stars=0),
            _single_shot_repo("my-startup-engine"),
        ],
    )

    snippet = github.verify_github("The Larper")[0]["snippet"]
    assert "Technical authenticity read: thin-or-absent" in snippet
    assert "fork(s)" in snippet
    assert "single-commit" in snippet


def test_early_career_dev_with_one_real_original_is_mixed_not_thin(monkeypatch):
    """Substance, not presence: a fork pile PLUS one genuinely maintained,
    self-describing original is still-learning ('mixed'), NOT 'thin-or-absent'.
    A described multi-commit original is demonstrated engineering, however small,
    so it must not be dumped into the wrapper bucket."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("junior")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, created_at="2022-01-01T00:00:00Z", public_repos=5),
    )
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [
            _repo("awesome-list", fork=True, stars=0),
            _repo("react-tutorial", fork=True, stars=0),
            _repo("dotfiles-clone", fork=True, stars=0),
            _repo("some-fork", fork=True, stars=0),
            _maintained_repo("weather-cli", stars=1),
        ],
    )

    snippet = github.verify_github("Junior Dev")[0]["snippet"]
    assert "Technical authenticity read: mixed" in snippet
    assert "Signals:" in snippet


def test_low_confidence_namesake_substantial_repos_do_not_clear_claim(monkeypatch):
    """Discipline: a low-match-confidence namesake NEVER clears the person's
    technical claim, no matter how substantial ITS repos are. The record stays
    match_confidence 'low' and the read carries an explicit note that this
    footprint neither clears nor deepens the claimed person's technical claim."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("namesake")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, created_at="2013-01-01T00:00:00Z", bio="I like hiking"),
    )
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [
            _repo("engine", fork=False, stars=120, language="Rust"),
            _repo("api", fork=False, stars=40, language="Go"),
            _repo("cli", fork=False, stars=15, language="Python"),
        ],
    )

    record = github.verify_github("Jane Doe", company="Acme Corp")[0]
    assert record["match_confidence"] == "low"
    assert "may not be the same person" in record["snippet"]
    # The substantial footprint must NOT be allowed to clear the claim.
    assert "neither clears nor deepens" in record["snippet"]


def test_low_confidence_namesake_thin_repos_do_not_deepen_the_tell(monkeypatch):
    """The mirror discipline: a low-confidence namesake's THIN repos must not
    be used to deepen the tell against the claimed person either. Same caveat,
    same non-identifying framing."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("namesake")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, created_at="2025-11-01T00:00:00Z", public_repos=1, bio="unrelated"),
    )
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [_repo("random-fork", fork=True, stars=0)],
    )

    record = github.verify_github("Jane Doe", company="Acme Corp")[0]
    assert record["match_confidence"] == "low"
    assert "neither clears nor deepens" in record["snippet"]


def test_high_confidence_read_has_no_non_clearing_caveat(monkeypatch):
    """Positive control: when a strong disambiguator DID match (high
    confidence), the non-clearing caveat must be ABSENT. The caveat is tied
    strictly to unconfirmed identity, so it must not silently weaken every read."""
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("janedoe")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(
            login, created_at="2013-01-01T00:00:00Z", bio="Building things at Acme Corp"
        ),
    )
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [
            _repo("engine", fork=False, stars=120, language="Rust"),
            _repo("api", fork=False, stars=40, language="Go"),
        ],
    )

    record = github.verify_github("Jane Doe", company="Acme Corp")[0]
    assert record["match_confidence"] == "high"
    assert "neither clears nor deepens" not in record["snippet"]


# ---------------------------------------------------------------------------
# Workstream C: profile-declared handle direct lookup (C.1), name-handle
# medium tier (C.3), and the pre-existing blog-hint disambiguator (C.2 enables).
# ---------------------------------------------------------------------------


def test_profile_linked_handle_direct_lookup_is_high(monkeypatch):
    # The person's OWN contact-info overlay declared this handle (hints
    # github_login). We look it up DIRECTLY, mark it high (no namesake risk),
    # and never run the name search at all.
    def _search_must_not_run(name, count=3):
        raise AssertionError("name search must not run when a handle was declared")

    monkeypatch.setattr(github, "_search_users", _search_must_not_run)
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(
        github,
        "_get_repos",
        lambda login, count=30: [_maintained_repo("cognition"), _maintained_repo("toolkit")],
    )

    evidence = github.verify_github(
        "Jordan Rivera", company="Pillar", hints={"github_login": "JordanRivera-dev"}
    )
    assert len(evidence) == 1
    assert evidence[0]["match_confidence"] == "high"
    assert "profile-declared" in evidence[0]["snippet"]


def test_linked_handle_404_falls_back_to_search(monkeypatch):
    # A typo'd / deleted linked handle (direct lookup returns None) degrades to
    # the normal name search, never to a crash or a false high.
    search_ran = {"count": 0}

    def _fake_search(name, count=3):
        search_ran["count"] += 1
        return [_search_item("somecandidate")]

    def _get_user(login):
        if login == "TypoHandle":
            return None  # the linked handle does not resolve
        return _user_response(login)

    monkeypatch.setattr(github, "_search_users", _fake_search)
    monkeypatch.setattr(github, "_get_user", _get_user)
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    evidence = github.verify_github(
        "Jordan Rivera", company="Pillar", hints={"github_login": "TypoHandle"}
    )
    assert search_ran["count"] == 1
    assert len(evidence) == 1
    # A bare namesake with no disambiguator and no name-token match stays low.
    assert evidence[0]["match_confidence"] == "low"


def test_name_handle_two_token_match_is_medium(monkeypatch):
    # The surname is present in full and the given name uses a three-letter
    # prefix: corroborating "medium", never identifying.
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("JorRivera-dev")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    record = github.verify_github("Jordan Rivera")[0]
    assert record["match_confidence"] == "medium"
    assert "NOT identifying" in record["snippet"]


def test_name_handle_one_token_match_stays_low(monkeypatch):
    # A bare first-name handle never qualifies: no surname token, stays low.
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("vedant")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    record = github.verify_github("Jordan Rivera")[0]
    assert record["match_confidence"] == "low"


def test_stranger_namesake_stays_low(monkeypatch):
    # A handle carrying no name tokens is a stranger: low, with the may-not-be
    # caveat preserved. Pins the load-bearing default.
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("codewizard42")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    record = github.verify_github("Jordan Rivera")[0]
    assert record["match_confidence"] == "low"
    assert "may not be the same person" in record["snippet"]


def test_name_handle_predicate_table():
    # Direct unit test of the pure predicate over the worked-examples table.
    assert github._name_handle_match("Jordan Rivera", "JorRivera-dev") is True
    assert github._name_handle_match("Jordan Rivera", "jordanrivera") is True
    assert github._name_handle_match("Jordan Rivera", "Jordan-Rivera-09") is True
    assert github._name_handle_match("Jordan Rivera", "vedant") is False
    assert github._name_handle_match("Jordan Rivera", "soni88") is False
    assert github._name_handle_match("Jordan Rivera", "codewizard42") is False
    # A single-token name never matches.
    assert github._name_handle_match("Madonna", "madonna") is False
    # A short given name (< 3 chars) must appear in FULL, not by prefix.
    assert github._name_handle_match("Al Roker", "alroker") is True
    assert github._name_handle_match("Al Roker", "roker") is False


def test_hints_blog_disambiguator_fires(monkeypatch):
    # The pre-existing blog-URL disambiguator, now reachable because C.2 passes
    # hints through: a personal_site hint matching the account blog -> high.
    monkeypatch.setattr(github, "_search_users", lambda name, count=3: [_search_item("someone")])
    monkeypatch.setattr(
        github, "_get_user", lambda login: _user_response(login, blog="https://jordan-rivera.example")
    )
    monkeypatch.setattr(github, "_get_repos", lambda login, count=30: [])

    record = github.verify_github("Jordan Rivera", hints={"personal_site": "jordan-rivera.example"})[0]
    assert record["match_confidence"] == "high"


# ---------------------------------------------------------------------------
# Feature 4: bounded deep repository artifact inspection
# ---------------------------------------------------------------------------


def _deep_repo(name: str = "acme") -> dict:
    repo = _maintained_repo(name, stars=1, language="Python")
    repo.update(
        {
            "full_name": f"janedoe/{name}",
            "default_branch": "main",
            "html_url": f"https://github.com/janedoe/{name}",
        }
    )
    return repo


def test_repository_artifact_analysis_recognizes_tests_ci_and_authorship():
    repo = _deep_repo()
    tree = {
        "truncated": False,
        "tree": [
            {"path": "src/acme/api.py", "type": "blob", "size": 4200},
            {"path": "src/acme/models.py", "type": "blob", "size": 3100},
            {"path": "src/acme/service.py", "type": "blob", "size": 5100},
            {"path": "tests/test_api.py", "type": "blob", "size": 2400},
            {"path": ".github/workflows/test.yml", "type": "blob", "size": 700},
            {"path": "pyproject.toml", "type": "blob", "size": 1200},
            {"path": "uv.lock", "type": "blob", "size": 9000},
            {"path": "README.md", "type": "blob", "size": 3500},
            {"path": "docs/architecture.md", "type": "blob", "size": 2200},
        ],
    }
    commits = [
        {"author": {"login": "janedoe"}, "commit": {"author": {"date": "2026-06-01T00:00:00Z"}}},
        {"author": {"login": "janedoe"}, "commit": {"author": {"date": "2026-05-01T00:00:00Z"}}},
        {"author": {"login": "contributor"}, "commit": {"author": {"date": "2026-04-01T00:00:00Z"}}},
    ]

    facts = github._analyze_repository_artifacts(repo, tree, commits, "janedoe")

    assert facts["artifact_read"] == "substantial"
    assert facts["source_files"] == 3
    assert facts["test_files"] == 1
    assert facts["ci"] is True
    assert facts["account_linked_commits"] == 2
    assert "pyproject.toml" in facts["manifests"]


def test_repository_nuance_recognizes_layered_build_and_verified_commands():
    repo = _deep_repo("product")
    repo["archived"] = False
    tree = {
        "truncated": False,
        "tree": [
            {"path": "frontend/app/page.tsx", "type": "blob", "size": 4200},
            {"path": "frontend/components/card.tsx", "type": "blob", "size": 2200},
            {"path": "api/routes/users.py", "type": "blob", "size": 3200},
            {"path": "api/services/accounts.py", "type": "blob", "size": 2800},
            {"path": "migrations/001_create_users.sql", "type": "blob", "size": 900},
            {"path": "workers/email.py", "type": "blob", "size": 1600},
            {"path": "tests/test_accounts.py", "type": "blob", "size": 2400},
            {"path": ".github/workflows/ci.yml", "type": "blob", "size": 700},
            {"path": "package.json", "type": "blob", "size": 1000},
            {"path": "package-lock.json", "type": "blob", "size": 9000},
            {"path": "Dockerfile", "type": "blob", "size": 500},
            {"path": "SECURITY.md", "type": "blob", "size": 400},
            {"path": "LICENSE", "type": "blob", "size": 1100},
        ],
    }
    configs = {
        "package.json": """
        {
          "dependencies": {"next": "15", "react": "19"},
          "devDependencies": {"eslint": "9"},
          "scripts": {
            "test": "vitest",
            "build": "next build",
            "lint": "eslint .",
            "typecheck": "tsc --noEmit"
          }
        }
        """,
        ".github/workflows/ci.yml": "run: npm test\nrun: npm run build",
        "Dockerfile": "RUN npm run build",
    }
    commits = [
        {"author": {"login": "janedoe"}},
        {"author": {"login": "janedoe"}},
        {"author": {"login": "contributor"}},
    ]

    facts = github._analyze_repository_artifacts(
        repo,
        tree,
        commits,
        "janedoe",
        config_samples=configs,
    )

    assert facts["artifact_read"] == "substantial"
    assert facts["architecture_layers"] == [
        "API boundary",
        "frontend",
        "persistence",
        "workers",
    ]
    assert facts["frameworks"] == ["Next.js", "React"]
    assert facts["engineering_commands"] == ["build", "lint", "test", "typecheck"]
    assert facts["dependency_count"] == 3
    assert facts["account_linked_commit_ratio"] == 0.67
    assert facts["distinct_commit_authors"] == 2
    assert "manifest with lockfile" in facts["quality_signals"]
    assert "multiple architecture layers" in facts["quality_signals"]


def test_generated_and_vendor_code_do_not_inflate_source_quality():
    facts = github._analyze_repository_artifacts(
        _deep_repo("wrapper"),
        {
            "truncated": False,
            "tree": [
                {"path": "public/build/app.bundle.js", "type": "blob", "size": 500000},
                {"path": "vendor/sdk/client.py", "type": "blob", "size": 100000},
                {"path": "src/generated/models.generated.ts", "type": "blob", "size": 80000},
                {"path": "README.md", "type": "blob", "size": 700},
            ],
        },
        [{"author": {"login": "janedoe"}}],
        "janedoe",
    )

    assert facts["artifact_read"] == "thin"
    assert facts["source_files"] == 0
    assert facts["generated_files_excluded"] == 3
    assert "only generated or vendor code detected" in facts["risk_signals"]


def test_repository_artifact_analysis_calls_docs_only_repo_thin():
    facts = github._analyze_repository_artifacts(
        _deep_repo("pitch"),
        {
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "size": 400},
                {"path": "LICENSE", "type": "blob", "size": 1100},
                {"path": "landing/index.html", "type": "blob", "size": 900},
            ],
        },
        [{"author": None, "commit": {"author": {"date": "2026-06-01T00:00:00Z"}}}],
        "janedoe",
    )

    assert facts["artifact_read"] == "thin"
    assert facts["source_files"] == 0
    assert facts["test_files"] == 0


def test_high_confidence_account_gets_deep_repository_evidence(monkeypatch):
    repo = _deep_repo("acmecorp")
    monkeypatch.setattr(github, "_search_users", lambda *a, **k: [_search_item("janedoe")])
    monkeypatch.setattr(
        github,
        "_get_user",
        lambda login: _user_response(login, company="Acme Corp"),
    )
    monkeypatch.setattr(github, "_get_repos", lambda *a, **k: [repo])
    monkeypatch.setattr(
        github,
        "_get_repo_tree",
        lambda *a, **k: {
            "truncated": False,
            "tree": [
                {"path": "src/app.py", "type": "blob", "size": 4000},
                {"path": "src/db.py", "type": "blob", "size": 3000},
                {"path": "src/jobs.py", "type": "blob", "size": 3000},
                {"path": "tests/test_app.py", "type": "blob", "size": 2000},
                {"path": ".github/workflows/test.yml", "type": "blob", "size": 600},
                {"path": "pyproject.toml", "type": "blob", "size": 900},
            ],
        },
    )
    monkeypatch.setattr(
        github,
        "_get_recent_commits",
        lambda *a, **k: [
            {"author": {"login": "janedoe"}, "commit": {"author": {"date": "2026-06-01T00:00:00Z"}}},
            {"author": {"login": "janedoe"}, "commit": {"author": {"date": "2026-05-01T00:00:00Z"}}},
        ],
    )
    monkeypatch.setattr(
        github,
        "_sample_repository_configs",
        lambda *args: {
            "pyproject.toml": "[project]\ndependencies = ['fastapi']",
            ".github/workflows/test.yml": "run: pytest",
        },
    )
    monkeypatch.setattr(github, "_get_contributor_count", lambda *a, **k: 1)

    record = github.verify_github("Jane Doe", company="Acme Corp")[0]

    assert record["match_confidence"] == "high"
    assert "Deep repository inspection" in record["snippet"]
    assert "tests: 1" in record["snippet"]
    assert "CI: yes" in record["snippet"]
    assert "account-linked commits: 2 of 2 sampled" in record["snippet"]


def test_unconfirmed_namesake_skips_deep_repository_calls(monkeypatch):
    repo = _deep_repo("acmecorp")
    monkeypatch.setattr(github, "_search_users", lambda *a, **k: [_search_item("stranger")])
    monkeypatch.setattr(github, "_get_user", lambda login: _user_response(login))
    monkeypatch.setattr(github, "_get_repos", lambda *a, **k: [repo])
    monkeypatch.setattr(
        github,
        "_get_repo_tree",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not inspect a namesake")),
    )

    record = github.verify_github("Jane Doe", company="Acme Corp")[0]

    assert record["match_confidence"] == "low"
    assert "Deep repository inspection" not in record["snippet"]


def test_maintained_solo_repo_is_not_thin_just_for_low_stars(monkeypatch):
    repo = _maintained_repo("acmecorp", stars=1)
    monkeypatch.setattr(github, "_get_contributor_count", lambda *a, **k: 1)

    result = github._find_thin_wrapper_repo([repo], "janedoe", "Acme Corp")

    assert result is not None
    assert result["looks_thin"] is False


# ---------------------------------------------------------------------------
# Live smoke test (skipped by default; no network in CI/offline runs)
# ---------------------------------------------------------------------------


def test_live_github_torvalds():
    import os

    import pytest

    if os.environ.get("LARP_LIVE_SMOKE") != "1":
        pytest.skip("set LARP_LIVE_SMOKE=1 to run the real GitHub API call")

    evidence = github.verify_github("Linus Torvalds", company="Linux Foundation")
    assert evidence, "expected at least one candidate for a well-known GitHub user"
