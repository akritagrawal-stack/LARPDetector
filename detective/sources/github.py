"""GitHub connector: identity, activity, and bounded repository artifact check.

Free API (api.github.com). Reuses GitHub CLI Keychain auth when available, or
uses GITHUB_TOKEN / GH_TOKEN when explicitly supplied (5000 requests/hour
authenticated). It falls back to unauthenticated access (60 requests/hour).
No token is ever logged or persisted by this connector.

IDENTITY RESOLUTION IS AMBIGUOUS (read before using this evidence): GitHub's
user search is a text index over logins/bios/names, not a real person
lookup, so a bare-name search for "Jane Doe" can return any number of
unrelated Jane Does. Every evidence record from this module therefore
defaults match_confidence to "low" and only raises it to "high" when a
strong disambiguator actually matched: the claimed company appears in the
account's own bio/company field, or a personal-site/domain hint matches the
account's blog URL. Absent that, this module NEVER asserts the account is
the claimed person; the reasoning provider must treat a "low" record as
weak, non-identifying evidence.

Public surface:
    verify_github(person_name, company=None, hints=None) -> list[dict]

For a confirmed identity only, the connector inspects at most one repository
without a token or two with a token. It reads the recursive public tree and a
bounded recent-commit sample to surface tests, CI, dependency manifests,
infrastructure, source shape, and account-linked authorship. It does not grade
code style or claim that a repository proves employment.

Evidence record shape:
    {"source_url", "snippet", "source_name", "weight", "match_confidence"}

No em dashes anywhere in this file (house rule).
"""

from __future__ import annotations

import logging
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote, quote_plus

from .registry import weight_for

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_TIMEOUT = 10
_USER_AGENT = "LARPDetector-research/1.0 (GitHub identity/activity check)"
_SOURCE_NAME = "github"
_MAX_CANDIDATES = 3
_HINT_KEYS = ("domain", "personal_site", "website")
_MAX_DEEP_REPOS_WITHOUT_TOKEN = 1
_MAX_DEEP_REPOS_WITH_TOKEN = 2
_RECENT_COMMIT_SAMPLE = 30
_GITHUB_FAILURE_COOLDOWN_S = 300
_MAX_ARCHIVE_BYTES = 15 * 1024 * 1024
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_CONFIG_SAMPLES = 5
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_github_dead_until = 0.0

_SOURCE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".kt", ".kts", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".ex", ".exs", ".scala", ".dart", ".vue", ".svelte",
    ".sql", ".sol",
)
_TEST_PATH_MARKERS = (
    "/test/", "/tests/", "/spec/", "/specs/", "__tests__/",
    ".test.", ".spec.", "test_", "_test.",
)
_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "poetry.lock",
    "pipfile", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "gemfile", "composer.json", "mix.exs", "pubspec.yaml",
}
_LOCKFILE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
    "bun.lockb", "uv.lock", "poetry.lock", "cargo.lock", "go.sum",
}
_IGNORED_TREE_PREFIXES = (
    "node_modules/", "vendor/", "dist/", "build/", "coverage/", ".next/",
    ".nuxt/", "target/", "__pycache__/",
)
_GENERATED_PATH_MARKERS = (
    "/generated/", "/vendor/", "/vendors/", "/third_party/", "/third-party/",
    "/public/build/", "/static/build/",
)
_GENERATED_FILE_SUFFIXES = (
    ".min.js", ".min.css", ".bundle.js", ".map", ".generated.ts",
    ".generated.js", ".g.dart", ".pb.go",
)
_HYGIENE_NAMES = {
    "license", "license.md", "license.txt", "changelog", "changelog.md",
    "contributing.md", "security.md", "codeowners", "dependabot.yml",
    "dependabot.yaml",
}
_ARCHITECTURE_MARKERS = {
    "frontend": ("frontend", "client", "web", "ui"),
    "API boundary": ("api", "routes", "controllers"),
    "service/backend": ("server", "backend"),
    "persistence": ("migrations", "schema", "schemas", "models", "database", "db"),
    "workers": ("workers", "jobs", "tasks", "queues"),
    "cli": ("cli", "cmd", "commands"),
    "infrastructure": ("infra", "infrastructure", "deploy", "k8s", "terraform"),
}
_FRAMEWORK_PATTERNS = {
    "Next.js": r"\bnext\b",
    "React": r"\breact\b",
    "Vue": r"\bvue\b",
    "Svelte": r"\bsvelte\b",
    "FastAPI": r"\bfastapi\b",
    "Django": r"\bdjango\b",
    "Flask": r"\bflask\b",
    "Rails": r"\brails\b",
    "Express": r"\bexpress\b",
    "NestJS": r"\b@nestjs/",
    "Spring": r"\bspring-boot\b",
}

# Technical-authenticity read emitted alongside identity/activity. This is the
# deep "can they actually build" signal: a roughly-three-way call over the
# account's real engineering footprint, computed from data already on the repos
# list (no extra API calls, so it stays fast and bounded). It is NOT "used AI =
# bad": a well-architected build with multiple original repos, several
# languages, and real stars/contributors reads substantial even if AI-assisted;
# a single thin fork/wrapper reads thin. The reasoning provider judges code
# SUBSTANCE, this connector only surfaces the artifacts it can see.
_TECH_AUTH_SUBSTANTIAL = "substantial"
_TECH_AUTH_MIXED = "mixed"
_TECH_AUTH_THIN = "thin-or-absent"

# Thresholds for the substantial read (all deliberately modest: a real engineer
# who is findable at all usually clears them; the point is to separate a real
# public builder from an empty/namesake/wrapper account, not to demand a
# superstar profile).
_SUBSTANTIAL_MIN_AGE_YEARS = 2.0
_SUBSTANTIAL_MIN_ORIGINAL_REPOS = 3
_SUBSTANTIAL_MIN_TOTAL_STARS = 10
_SUBSTANTIAL_MIN_LANGUAGES = 2
# Below this many enumerable public repos on an account we could not enumerate
# (empty repos list), fall back to "thin-or-absent"; otherwise "mixed" (we
# cannot judge substance without the list, and must not guess "substantial").
_MIN_PUBLIC_REPOS_FOR_SIGNAL = 2

# A repo whose last push trails its creation by at least this many days almost
# certainly saw more than one commit: a maintained project, not a single dump.
_MAINTAINED_MIN_SPAN_DAYS = 7
# When one repo holds at least this share of all original-repo stars AND there
# is more than one original, the star count is a single outlier (often a viral
# or lucky repo), not evidence of broad sustained activity.
_STAR_CONCENTRATION_SHARE = 0.9
# Craftsmanship path to "substantial" when stars are modest: at least this
# fraction of originals describe themselves AND at least this many are
# maintained over time. Real self-describing multi-commit work is engineering
# even without a big star count.
_SUBSTANTIAL_MIN_DESCRIBED_RATIO = 0.5
_SUBSTANTIAL_MIN_MAINTAINED = 2


def _gh_cli_path() -> Optional[str]:
    """Resolve the project-local GitHub CLI first, then a system install."""
    configured = os.environ.get("LARP_GH_CLI", "").strip()
    candidates = [
        configured,
        str(_PROJECT_ROOT / ".runtime" / "github-cli" / "gh"),
        shutil.which("gh") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def _token_from_gh_cli() -> str:
    """Read the active GitHub CLI token into memory without logging it.

    GitHub CLI stores the credential in the platform keyring when available.
    The token is captured only inside this process and is never persisted in
    project configuration.
    """
    cli = _gh_cli_path()
    if not cli:
        return ""
    try:
        result = subprocess.run(
            [cli, "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _github_token() -> str:
    """Resolve explicit environment auth before the local keyring-backed CLI."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(key, "").strip()
        if token:
            return token
    return _token_from_gh_cli()


def github_auth_status() -> dict:
    """Return non-secret GitHub connector status for setup diagnostics."""
    token = _github_token()
    return {
        "authenticated": bool(token),
        "source": (
            "environment"
            if any(os.environ.get(key, "").strip() for key in ("GITHUB_TOKEN", "GH_TOKEN"))
            else "github_cli_keyring" if token else "none"
        ),
    }


def _headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(path: str, params: Optional[dict] = None):
    import requests  # lazy: keeps offline paths import-free

    if time.time() < _github_dead_until:
        raise RuntimeError("GitHub API is in rate-limit cooldown")
    response = requests.get(
        f"{_API_BASE}{path}", headers=_headers(), params=params, timeout=_TIMEOUT
    )
    _observe_rate_limit(response)
    return response


def _observe_rate_limit(response) -> None:
    """Enter a process cooldown when GitHub says the request budget is gone."""
    global _github_dead_until
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    remaining = str(headers.get("X-RateLimit-Remaining", "")).strip()
    if status not in (403, 429) or remaining not in ("0", ""):
        return
    try:
        reset = float(headers.get("X-RateLimit-Reset") or 0)
    except (TypeError, ValueError):
        reset = 0.0
    _github_dead_until = max(
        time.time() + _GITHUB_FAILURE_COOLDOWN_S,
        reset,
    )


def _search_users(name: str, count: int = _MAX_CANDIDATES) -> list[dict]:
    resp = _get("/search/users", params={"q": name, "per_page": count})
    if resp.status_code in (403, 429):
        raise RuntimeError(f"GitHub API rate limited with HTTP {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("github: search/users HTTP %d for %r", resp.status_code, name)
        return []
    return resp.json().get("items", []) or []


def _get_user(login: str) -> Optional[dict]:
    resp = _get(f"/users/{login}")
    if resp.status_code in (403, 429):
        raise RuntimeError(f"GitHub API rate limited with HTTP {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("github: users/%s HTTP %d", login, resp.status_code)
        return None
    return resp.json()


def _get_repos(login: str, count: int = 30) -> list[dict]:
    resp = _get(f"/users/{login}/repos", params={"per_page": count, "sort": "updated"})
    if resp.status_code in (403, 429):
        raise RuntimeError(f"GitHub API rate limited with HTTP {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("github: users/%s/repos HTTP %d", login, resp.status_code)
        return []
    return resp.json() or []


def _is_rate_limit_error(exc: Exception) -> bool:
    low = str(exc or "").lower()
    return "rate limit" in low or "cooldown" in low or "http 429" in low


def _html_get(url: str):
    import requests

    return requests.get(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
        timeout=_TIMEOUT,
    )


def _html_search_users(name: str, count: int = _MAX_CANDIDATES) -> list[dict]:
    """Search public GitHub HTML when the free API budget is exhausted."""
    response = _html_get(
        f"https://github.com/search?q={quote_plus(name)}&type=users"
    )
    if response.status_code != 200:
        return []
    logins: list[str] = []
    for match in re.finditer(r'href="/([A-Za-z0-9-]{1,39})"', response.text or ""):
        login = match.group(1)
        if login.lower() in {
            "search",
            "login",
            "signup",
            "features",
            "marketplace",
            "pricing",
        }:
            continue
        if login not in logins:
            logins.append(login)
        if len(logins) >= count:
            break
    return [
        {"login": login, "html_url": f"https://github.com/{login}"}
        for login in logins
    ]


def _html_get_user(login: str) -> Optional[dict]:
    response = _html_get(f"https://github.com/{login}")
    if response.status_code != 200:
        return None
    html = response.text or ""

    def meta(name: str) -> str:
        match = re.search(
            rf'<meta[^>]+name="{re.escape(name)}"[^>]+content="([^"]*)"',
            html,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    resolved = meta("octolytics-dimension-user_login") or login
    public_match = re.search(
        r'href="/[^"]+\?tab=repositories"[^>]*>.*?<span[^>]*class="Counter"[^>]*>([\d,]+)',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    return {
        "login": resolved,
        "html_url": f"https://github.com/{resolved}",
        "created_at": "",
        "public_repos": int(public_match.group(1).replace(",", ""))
        if public_match
        else 0,
        "bio": "",
        "company": "",
        "blog": "",
        "_retrieval": "public_html_fallback",
    }


def _html_get_repos(login: str, count: int = 30) -> list[dict]:
    response = _html_get(f"https://github.com/{login}?tab=repositories")
    if response.status_code != 200:
        return []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(response.text or "", "html.parser")
    except Exception:
        return []
    repos: list[dict] = []
    for anchor in soup.select('a[itemprop="name codeRepository"]'):
        name = anchor.get_text(" ", strip=True)
        if not name:
            continue
        row = anchor.find_parent("li")
        row_text = row.get_text(" ", strip=True) if row else ""
        star_match = re.search(r"\b([\d,.]+)\s+stars?\b", row_text, re.IGNORECASE)
        repos.append(
            {
                "name": name,
                "full_name": f"{login}/{name}",
                "html_url": f"https://github.com/{login}/{name}",
                "default_branch": "main",
                "fork": bool(row and row.select_one('svg[aria-label="fork"]')),
                "stargazers_count": int(
                    float((star_match.group(1) if star_match else "0").replace(",", ""))
                ),
                "description": "",
                "language": "",
                "topics": [],
                "created_at": "",
                "pushed_at": "",
                "_retrieval": "public_html_fallback",
            }
        )
        if len(repos) >= count:
            break
    return repos


def _archive_repository_tree(owner: str, repo: str) -> Optional[dict]:
    """Read a bounded public source archive without consuming GitHub API quota."""
    import requests

    for branch in ("main", "master"):
        response = requests.get(
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}",
            headers={"User-Agent": _USER_AGENT},
            timeout=20,
        )
        if response.status_code != 200 or len(response.content) > _MAX_ARCHIVE_BYTES:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                tree = []
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    parts = info.filename.split("/", 1)
                    path = parts[1] if len(parts) == 2 else parts[0]
                    tree.append(
                        {"type": "blob", "path": path, "size": info.file_size}
                    )
                return {"tree": tree, "truncated": False}
        except (zipfile.BadZipFile, OSError):
            continue
    return None


def _get_contributor_count(owner: str, repo: str) -> Optional[int]:
    """Best-effort contributor count for one repo (first page, up to 100).

    None means "could not be determined" (an API hiccup, or a very large
    repo where 100 is not the true total), never a stand-in for zero.
    """
    resp = _get(
        f"/repos/{owner}/{repo}/contributors", params={"per_page": 100, "anon": "true"}
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not isinstance(data, list):
        return None
    return len(data)


def _get_repo_tree(owner: str, repo: str, branch: str) -> Optional[dict]:
    """Fetch one public repository tree recursively.

    GitHub accepts a ref name in the tree endpoint. A truncated tree remains
    useful as a lower-bound sample and is labeled as such in the evidence.
    """
    if not owner or not repo or not branch:
        return None
    resp = _get(
        f"/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    if resp.status_code != 200:
        logger.warning("github: tree HTTP %d for %s/%s", resp.status_code, owner, repo)
        return None
    data = resp.json()
    return data if isinstance(data, dict) else None


def _get_recent_commits(
    owner: str, repo: str, count: int = _RECENT_COMMIT_SAMPLE
) -> Optional[list[dict]]:
    """Fetch a bounded recent-commit sample for authorship linkage."""
    resp = _get(
        f"/repos/{owner}/{repo}/commits",
        params={"per_page": max(1, min(int(count), 100))},
    )
    if resp.status_code != 200:
        logger.warning("github: commits HTTP %d for %s/%s", resp.status_code, owner, repo)
        return None
    data = resp.json()
    return data if isinstance(data, list) else None


def _is_test_path(path: str) -> bool:
    normalized = "/" + (path or "").lower().lstrip("/")
    return any(marker in normalized for marker in _TEST_PATH_MARKERS)


def _is_generated_or_vendor_path(path: str) -> bool:
    normalized = "/" + (path or "").lower().lstrip("/")
    basename = normalized.rsplit("/", 1)[-1]
    return (
        any(marker in normalized for marker in _GENERATED_PATH_MARKERS)
        or any(basename.endswith(suffix) for suffix in _GENERATED_FILE_SUFFIXES)
        or "/node_modules/" in normalized
    )


def _is_ignored_tree_path(path: str) -> bool:
    normalized = (path or "").lower().lstrip("/")
    if any(normalized.startswith(prefix) for prefix in _IGNORED_TREE_PREFIXES):
        return True
    return any(f"/{prefix}" in f"/{normalized}" for prefix in _IGNORED_TREE_PREFIXES)


def _config_sample_paths(paths: list[str]) -> list[str]:
    """Select a bounded, deterministic set of build and deployment files."""
    candidates: list[tuple[int, str]] = []
    for path in paths:
        lower = path.lower()
        basename = lower.rsplit("/", 1)[-1]
        priority = None
        if basename in _MANIFEST_NAMES:
            priority = 0
        elif lower.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml")):
            priority = 1
        elif basename in {
            "dockerfile", "compose.yml", "compose.yaml", "makefile",
            "vercel.json", "netlify.toml", "fly.toml", "render.yaml",
            "railway.json", "procfile",
        }:
            priority = 2
        if priority is not None:
            candidates.append((priority, path))
    return [
        path
        for _, path in sorted(candidates, key=lambda item: (item[0], item[1].lower()))
        [:_MAX_CONFIG_SAMPLES]
    ]


def _get_repo_file_text(owner: str, repo: str, branch: str, path: str) -> Optional[str]:
    """Fetch one small public config file without spending GitHub API quota."""
    import requests

    if not all((owner, repo, branch, path)):
        return None
    url = (
        "https://raw.githubusercontent.com/"
        f"{quote(owner, safe='')}/{quote(repo, safe='')}/"
        f"{quote(branch, safe='')}/{quote(path, safe='/')}"
    )
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/plain"},
            timeout=min(_TIMEOUT, 6),
        )
    except Exception:  # noqa: BLE001 - optional public artifact sample
        return None
    if response.status_code != 200:
        return None
    content = bytes(getattr(response, "content", b"") or b"")
    if not content:
        text = str(getattr(response, "text", "") or "")
        content = text.encode("utf-8", errors="replace")
    if len(content) > _MAX_CONFIG_BYTES:
        return None
    return content.decode("utf-8", errors="replace")


def _sample_repository_configs(
    owner: str,
    repo: str,
    branch: str,
    paths: list[str],
) -> dict[str, str]:
    samples: dict[str, str] = {}
    for path in _config_sample_paths(paths):
        text = _get_repo_file_text(owner, repo, branch, path)
        if text:
            samples[path] = text
    return samples


def _analyze_config_samples(samples: Optional[dict[str, str]]) -> dict:
    """Extract factual build signals from bounded config-file contents."""
    samples = samples or {}
    frameworks: set[str] = set()
    commands: set[str] = set()
    deployments: set[str] = set()
    dependency_count = 0

    for path, text in samples.items():
        lower = (text or "").lower()
        for framework, pattern in _FRAMEWORK_PATTERNS.items():
            if re.search(pattern, lower):
                frameworks.add(framework)

        basename = path.lower().rsplit("/", 1)[-1]
        if basename == "package.json":
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                for key in ("dependencies", "devDependencies", "peerDependencies"):
                    values = payload.get(key)
                    if isinstance(values, dict):
                        dependency_count += len(values)
                scripts = payload.get("scripts")
                if isinstance(scripts, dict):
                    for name in scripts:
                        name_l = str(name).lower()
                        for command in ("test", "build", "lint", "typecheck"):
                            if command in name_l:
                                commands.add(command)

        command_patterns = {
            "test": r"\b(pytest|vitest|jest|go test|cargo test|npm test|pnpm test|mvn test)\b",
            "build": r"\b(next build|go build|cargo build|docker build|mvn package|npm run build|pnpm build)\b",
            "lint": r"\b(eslint|ruff check|flake8|golangci-lint|clippy)\b",
            "typecheck": r"\b(tsc --noemit|mypy|pyright)\b",
        }
        for command, pattern in command_patterns.items():
            if re.search(pattern, lower):
                commands.add(command)

        deployment_patterns = {
            "Vercel": r"\bvercel\b",
            "Netlify": r"\bnetlify\b",
            "Fly.io": r"\bfly\.io\b|\bflyctl\b",
            "Render": r"\brender\.com\b|\brender\.yaml\b",
            "Railway": r"\brailway\b",
            "Kubernetes": r"\bkubernetes\b|\bkubectl\b|\bapiVersion:\s*(apps/|v1)",
            "Terraform": r"\bterraform\b",
            "AWS": r"\baws\b|\bamazon web services\b",
            "GCP": r"\bgcp\b|\bgoogle cloud\b",
            "Azure": r"\bazure\b",
        }
        for deployment, pattern in deployment_patterns.items():
            if re.search(pattern, lower, flags=re.IGNORECASE):
                deployments.add(deployment)

    return {
        "config_files_sampled": sorted(samples),
        "frameworks": sorted(frameworks),
        "engineering_commands": sorted(commands),
        "deployment_targets": sorted(deployments),
        "dependency_count": dependency_count,
    }


def _analyze_repository_artifacts(
    repo: dict,
    tree_payload: dict,
    commits: Optional[list[dict]],
    login: str,
    config_samples: Optional[dict[str, str]] = None,
) -> dict:
    """Summarize repository structure and recent account-linked authorship.

    This reads artifact shape, not code correctness. It deliberately avoids
    source-code sentiment or a simplistic stars-equal-quality rule.
    """
    tree = (tree_payload or {}).get("tree") or []
    paths: list[str] = []
    generated_paths: list[str] = []
    generated_bytes = 0
    sizes: dict[str, int] = {}
    for item in tree:
        if (item or {}).get("type") != "blob":
            continue
        path = str((item or {}).get("path") or "").strip()
        if not path:
            continue
        lower = path.lower()
        try:
            size = max(0, int((item or {}).get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if _is_generated_or_vendor_path(path):
            generated_paths.append(path)
            generated_bytes += size
            continue
        if _is_ignored_tree_path(path):
            continue
        paths.append(path)
        sizes[path] = size

    test_paths = [path for path in paths if _is_test_path(path)]
    source_paths = [
        path
        for path in paths
        if not _is_test_path(path) and path.lower().endswith(_SOURCE_SUFFIXES)
    ]
    basenames = {path.rsplit("/", 1)[-1].lower() for path in paths}
    manifests = sorted(basenames & _MANIFEST_NAMES)
    lockfiles = sorted(basenames & _LOCKFILE_NAMES)
    ci = any(path.lower().startswith(".github/workflows/") for path in paths)
    docs = sum(
        1
        for path in paths
        if path.lower().startswith("docs/")
        or path.rsplit("/", 1)[-1].lower().startswith("readme")
    )
    infra = sum(
        1
        for path in paths
        if path.rsplit("/", 1)[-1].lower() in {"dockerfile", "compose.yml", "compose.yaml"}
        or path.lower().endswith((".tf", ".tfvars"))
    )
    hygiene_files = sorted(
        path
        for path in paths
        if path.rsplit("/", 1)[-1].lower() in _HYGIENE_NAMES
        or path.lower().startswith(".github/issue_template/")
        or path.lower().startswith(".github/pull_request_template")
    )
    architecture_layers = sorted(
        layer
        for layer, markers in _ARCHITECTURE_MARKERS.items()
        if any(
            any(
                segment in markers
                for segment in path.lower().lstrip("/").split("/")[:-1]
            )
            for path in source_paths
        )
    )
    config_facts = _analyze_config_samples(config_samples)

    sample = list(commits or [])
    login_l = (login or "").lower()
    linked = 0
    commit_authors: set[str] = set()
    for commit in sample:
        author_login = (((commit or {}).get("author") or {}).get("login") or "").lower()
        if author_login:
            commit_authors.add(author_login)
        if author_login and author_login == login_l:
            linked += 1
    linked_ratio = round(linked / len(sample), 2) if sample else None

    quality_signals: list[str] = []
    risk_signals: list[str] = []
    if test_paths:
        quality_signals.append("tests")
    else:
        risk_signals.append("no tests detected")
    if ci:
        quality_signals.append("CI workflow")
    else:
        risk_signals.append("no GitHub Actions workflow detected")
    if manifests and lockfiles:
        quality_signals.append("manifest with lockfile")
    elif set(manifests) & {
        "package.json", "pyproject.toml", "pipfile", "cargo.toml", "go.mod",
        "gemfile", "composer.json", "pubspec.yaml",
    }:
        risk_signals.append("manifest without detected lockfile")
    if hygiene_files:
        quality_signals.append("project hygiene files")
    if len(architecture_layers) >= 2:
        quality_signals.append("multiple architecture layers")
    if config_facts["engineering_commands"]:
        quality_signals.append("declared engineering commands")
    if config_facts["deployment_targets"] or infra:
        quality_signals.append("deployment or infrastructure artifacts")
    if generated_paths and not source_paths:
        risk_signals.append("only generated or vendor code detected")
    if sample and linked == 0:
        risk_signals.append("no account-linked commits in recent sample")
    if repo.get("archived"):
        risk_signals.append("repository archived")

    if (
        len(source_paths) >= 3
        and len(test_paths) >= 1
        and ci
        and linked >= 2
    ) or (
        len(source_paths) >= 8
        and bool(manifests)
        and linked >= 3
    ):
        artifact_read = "substantial"
    elif len(source_paths) == 0 and len(test_paths) == 0:
        artifact_read = "thin"
    else:
        artifact_read = "developing-or-mixed"

    return {
        "repo": repo.get("name") or "",
        "url": repo.get("html_url") or "",
        "artifact_read": artifact_read,
        "tree_truncated": bool((tree_payload or {}).get("truncated")),
        "files_sampled": len(paths),
        "source_files": len(source_paths),
        "source_bytes": sum(sizes.get(path, 0) for path in source_paths),
        "generated_files_excluded": len(generated_paths),
        "generated_bytes_excluded": generated_bytes,
        "test_files": len(test_paths),
        "test_to_source_ratio": (
            round(len(test_paths) / len(source_paths), 2) if source_paths else None
        ),
        "ci": ci,
        "docs": docs,
        "infra_files": infra,
        "hygiene_files": hygiene_files,
        "architecture_layers": architecture_layers,
        "quality_signals": quality_signals,
        "risk_signals": risk_signals,
        "manifests": manifests,
        "lockfiles": lockfiles,
        "commits_sampled": len(sample),
        "account_linked_commits": linked,
        "account_linked_commit_ratio": linked_ratio,
        "distinct_commit_authors": len(commit_authors),
        **config_facts,
    }


def _select_deep_repositories(repos: list[dict], company: str, limit: int) -> list[dict]:
    """Pick a tiny, deterministic set of original repositories to inspect."""
    company_norm = "".join(ch for ch in (company or "").lower() if ch.isalnum())

    def rank(repo: dict) -> tuple:
        name_norm = "".join(ch for ch in (repo.get("name") or "").lower() if ch.isalnum())
        product_match = bool(
            company_norm
            and name_norm
            and (name_norm in company_norm or company_norm in name_norm)
        )
        return (
            1 if product_match else 0,
            int(repo.get("stargazers_count") or 0),
            str(repo.get("pushed_at") or ""),
            str(repo.get("name") or ""),
        )

    eligible = [
        repo
        for repo in repos or []
        if not repo.get("fork")
        and repo.get("name")
        and repo.get("default_branch")
        and repo.get("full_name")
    ]
    return sorted(eligible, key=rank, reverse=True)[: max(0, limit)]


def _deep_inspect_repositories(login: str, repos: list[dict], company: str) -> list[dict]:
    """Inspect at most one unauthenticated or two authenticated public repos."""
    enabled = os.environ.get("GITHUB_DEEP_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return []
    authenticated = bool(_github_token())
    hard_limit = (
        _MAX_DEEP_REPOS_WITH_TOKEN
        if authenticated
        else _MAX_DEEP_REPOS_WITHOUT_TOKEN
    )
    default_limit = hard_limit
    try:
        configured_limit = int(os.environ.get("GITHUB_DEEP_MAX_REPOS", default_limit))
    except (TypeError, ValueError):
        configured_limit = default_limit
    limit = max(0, min(configured_limit, hard_limit))

    analyses: list[dict] = []
    for repo in _select_deep_repositories(repos, company, limit):
        name = repo.get("name") or ""
        branch = repo.get("default_branch") or ""
        try:
            tree = _get_repo_tree(login, name, branch)
            commits = _get_recent_commits(login, name)
        except Exception as exc:  # noqa: BLE001 - source failure never breaks a scan
            logger.warning("github: deep inspection failed for %s/%s: %s", login, name, exc)
            tree = _archive_repository_tree(login, name)
            commits = None
        if tree is None:
            tree = _archive_repository_tree(login, name)
            commits = None
        if tree is None:
            continue
        tree_paths = [
            str((item or {}).get("path") or "")
            for item in (tree.get("tree") or [])
            if (item or {}).get("type") == "blob"
        ]
        config_samples = _sample_repository_configs(
            login,
            name,
            branch,
            tree_paths,
        )
        analysis = _analyze_repository_artifacts(
            repo,
            tree,
            commits,
            login,
            config_samples=config_samples,
        )
        if commits is None:
            analysis["inspection_method"] = "public_archive_fallback"
        else:
            analysis["inspection_method"] = "github_api"
        analyses.append(analysis)
    return analyses


def _reconcile_thin_with_deep(
    thin: Optional[dict], analyses: list[dict]
) -> Optional[dict]:
    """Let stronger artifact evidence override the shallow reach heuristic."""
    if not thin or not thin.get("looks_thin"):
        return thin
    for analysis in analyses or []:
        if (analysis.get("repo") or "").lower() != (
            thin.get("name") or ""
        ).lower():
            continue
        if (
            analysis.get("artifact_read") == "substantial"
            and int(analysis.get("account_linked_commits") or 0) >= 2
        ):
            reconciled = dict(thin)
            reconciled["looks_thin"] = False
            reconciled["deep_override"] = True
            return reconciled
    return thin


def _strong_disambiguator_matched(user: dict, company: str, hints: dict) -> bool:
    """True only when a concrete disambiguator lines up: the claimed company
    shows up in the account's own bio/company field, or a personal-site /
    domain hint shows up in the account's blog URL.

    This is the ONLY thing that raises match_confidence out of "low": a bare
    name search is ambiguous by nature, so absent this, the account is never
    treated as confirmed to be the claimed person.
    """
    bio = (user.get("bio") or "").lower()
    company_field = (user.get("company") or "").lower()
    blog = (user.get("blog") or "").lower()

    company_l = (company or "").strip().lower()
    if company_l and (company_l in bio or company_l in company_field):
        return True

    for key in _HINT_KEYS:
        val = (hints.get(key) or "").strip().lower()
        if val and blog and val in blog:
            return True
    return False


def _name_handle_match(person_name: str, login: str) -> bool:
    """True only when BOTH primary name tokens are represented in the login.
    Corroborating-only: this can raise match_confidence to "medium", NEVER
    "high", and never identifies the person on its own. Rules:
      - Normalize login and tokens to lowercase alphanumerics.
      - Tokens are the FIRST and LAST whitespace-split parts of person_name;
        a single-token name never matches (return False).
      - The SURNAME token must appear in the login IN FULL.
      - The GIVEN-NAME token must appear in full OR as a prefix of itself of
        length >= 3 (so "ved" from "vedant" counts, "v" does not).
      - Tokens shorter than 3 characters must appear in full.
    """
    def _norm(value: str) -> str:
        return "".join(ch for ch in (value or "").lower() if ch.isalnum())

    login_norm = _norm(login)
    if not login_norm:
        return False
    parts = (person_name or "").split()
    if len(parts) < 2:
        return False
    given = _norm(parts[0])
    surname = _norm(parts[-1])
    if not given or not surname:
        return False
    if surname not in login_norm:
        return False
    if len(given) < 3:
        return given in login_norm
    # given >= 3: any prefix of length >= 3 (including the full token) suffices.
    for prefix_len in range(3, len(given) + 1):
        if given[:prefix_len] in login_norm:
            return True
    return False


def _verify_linked_login(login: str, company: str) -> Optional[dict]:
    """Direct lookup of a PROFILE-DECLARED GitHub handle (from the person's own
    contact-info overlay). Returns a single "high" evidence record, or None if
    the handle does not resolve (404, network error, empty) so the caller can
    fall through to the name search. Never raises out.
    """
    try:
        user = _get_user(login)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("github: linked-login lookup failed for %r: %s", login, exc)
        user = _html_get_user(login) if _is_rate_limit_error(exc) else None
    if not user:
        return None
    resolved_login = user.get("login") or login
    try:
        repos = _get_repos(resolved_login)
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning("github: repo list failed for linked %r: %s", resolved_login, exc)
        repos = (
            _html_get_repos(resolved_login)
            if _is_rate_limit_error(exc)
            else []
        )
    thin = _find_thin_wrapper_repo(repos, resolved_login, company or "")
    deep_analyses = _deep_inspect_repositories(resolved_login, repos, company or "")
    thin = _reconcile_thin_with_deep(thin, deep_analyses)
    tech_auth = _assess_technical_authenticity(user, repos, thin)
    return {
        "source_url": user.get("html_url", f"https://github.com/{resolved_login}"),
        "snippet": _build_snippet(
            resolved_login,
            user,
            "high",
            thin,
            tech_auth,
            profile_declared=True,
            deep_analyses=deep_analyses,
        ),
        "source_name": _SOURCE_NAME,
        "weight": weight_for(_SOURCE_NAME),
        "match_confidence": "high",
    }


def _parse_gh_time(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 GitHub timestamp to an aware datetime, or None if it
    is missing or unparseable. Never raises."""
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _account_age_years(created_at: str) -> Optional[float]:
    """Years between the account creation date and now, or None if the date
    cannot be parsed. Account age is un-backdatable, so a brand-new account
    behind a loud "I have built for a decade" claim is a real tell.
    """
    created = _parse_gh_time(created_at)
    if created is None:
        return None
    return (datetime.now(timezone.utc) - created).days / 365.25


def _repo_maintained(repo: dict) -> Optional[bool]:
    """Best-effort read of whether a repo looks maintained over time (a real
    multi-commit project) vs a single-commit dump or tutorial clone, using only
    fields already on the repos list. True when the account pushed to it a
    meaningful span after creating it; False when it was created and then never
    pushed to again; None when the dates are missing and we cannot tell.

    This is a proxy, not a commit count: GitHub does not put a commit total on
    the repos list, and fetching per-repo commit history would multiply API
    calls past the unauthenticated ceiling. A repo whose last push trails its
    creation by at least a week almost always saw more than one commit.
    """
    created = _parse_gh_time(repo.get("created_at", ""))
    pushed = _parse_gh_time(repo.get("pushed_at", ""))
    if created is None or pushed is None:
        return None
    return (pushed - created).days >= _MAINTAINED_MIN_SPAN_DAYS


def _find_thin_wrapper_repo(repos: list[dict], login: str, company: str) -> Optional[dict]:
    """Best-effort: find a repo whose name resembles the claimed
    product/company, and report whether it looks thin. A fork is thin; a solo,
    low-star project is thin only when its push dates also look like a one-shot
    dump. Legitimate maintained solo work is not penalized for low reach.

    Takes an already-fetched `repos` list (verify_github fetches it once and
    reuses it for both this product-repo read and the account-wide technical
    authenticity read below). Returns None if no plausibly-matching repo is
    found: absence of a matching repo is not itself a positive or negative
    signal, just nothing to grade.
    """
    if not company:
        return None

    company_norm = "".join(ch for ch in company.lower() if ch.isalnum())
    if not company_norm:
        return None

    match = None
    for repo in repos:
        repo_name_norm = "".join(ch for ch in (repo.get("name") or "").lower() if ch.isalnum())
        if repo_name_norm and (
            repo_name_norm in company_norm or company_norm in repo_name_norm
        ):
            match = repo
            break
    if match is None:
        return None

    is_fork = bool(match.get("fork"))
    stars = match.get("stargazers_count") or 0
    contributor_count: Optional[int] = None
    try:
        contributor_count = _get_contributor_count(login, match.get("name", ""))
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning("github: contributor count failed for %r: %s", match.get("name"), exc)

    # Low stars and one contributor are normal for a legitimate solo project.
    # Thin requires a stronger artifact-shaped tell: a fork, or a low-reach
    # repo that also looks like a one-shot dump.
    maintained = _repo_maintained(match)
    looks_thin = is_fork or (
        stars < 3
        and contributor_count is not None
        and contributor_count <= 1
        and maintained is False
    )

    return {
        "name": match.get("name", ""),
        "is_fork": is_fork,
        "stars": stars,
        "contributor_count": contributor_count,
        "looks_thin": looks_thin,
    }


def _assess_technical_authenticity(
    user: dict, repos: list[dict], thin: Optional[dict]
) -> tuple[str, dict]:
    """A roughly-three-way "can they actually build" read over an account's
    real engineering footprint, computed from data already on the repos list
    (no extra API calls). Returns (read, facts) where read is one of
    substantial / mixed / thin-or-absent and facts carries the numbers the
    snippet reports.

    Discipline (owner was explicit): this judges code SUBSTANCE, not the mere
    presence of AI. Multiple ORIGINAL (non-fork) repos, several distinct
    languages, and real stars read substantial even if AI-assisted; an empty
    account, an all-forks account, or a single thin wrapper reads thin. When
    the repos list could not be enumerated we return "mixed" rather than
    guessing, so an unfetched list never becomes a false "thin" accusation.
    """
    age_years = _account_age_years(user.get("created_at", ""))
    public_repos = int(user.get("public_repos") or 0)

    originals = [r for r in repos if not r.get("fork")]
    forks = [r for r in repos if r.get("fork")]
    star_counts = [int(r.get("stargazers_count") or 0) for r in originals]
    total_stars = sum(star_counts)
    max_stars = max(star_counts) if star_counts else 0
    languages = sorted(
        {(r.get("language") or "").strip() for r in originals if (r.get("language") or "").strip()}
    )
    n_orig = len(originals)
    n_repos = len(repos)

    # Derived substance signals, all from the repos list already fetched (no
    # extra API calls): fork dominance, star concentration, self-description,
    # topic curation, and maintenance-over-time vs single-commit dumps.
    fork_ratio = (len(forks) / n_repos) if n_repos else 0.0
    max_star_share = (max_stars / total_stars) if total_stars > 0 else 0.0
    star_concentrated = n_orig > 1 and max_star_share >= _STAR_CONCENTRATION_SHARE
    described = [r for r in originals if (r.get("description") or "").strip()]
    described_ratio = (len(described) / n_orig) if n_orig else 0.0
    topics_total = sum(len(r.get("topics") or []) for r in originals)
    maintained = sum(1 for r in originals if _repo_maintained(r) is True)
    single_shot = sum(1 for r in originals if _repo_maintained(r) is False)

    facts = {
        "age_years": round(age_years, 1) if age_years is not None else None,
        "public_repos": public_repos,
        "original_repos": n_orig,
        "forks": len(forks),
        "total_stars": total_stars,
        "languages": languages,
        "fork_ratio": round(fork_ratio, 2),
        "star_concentrated": star_concentrated,
        "max_stars": max_stars,
        "described": len(described),
        "topics_total": topics_total,
        "maintained": maintained,
        "single_shot": single_shot,
    }
    facts["reasons"] = _tech_reasons(facts)

    flagship_thin = bool(thin and thin.get("looks_thin"))
    flagship_substantial = bool(thin and not thin.get("looks_thin"))

    # Could not enumerate the repo list: judge nothing off substance, only the
    # crude public-repos count. Never "substantial" (unproven) and never a hard
    # "thin" unless the account is near-empty.
    if not repos:
        if public_repos < _MIN_PUBLIC_REPOS_FOR_SIGNAL:
            return _TECH_AUTH_THIN, facts
        return _TECH_AUTH_MIXED, facts

    # A confidently-graded flagship product repo is the strongest single signal.
    if flagship_substantial:
        return _TECH_AUTH_SUBSTANTIAL, facts

    # Thin / wrapper / tutorial-grade: no real original footprint. The last
    # clause is the fork-pile tell: an account that is mostly forks whose one
    # original is bare (no description, never pushed to again) and unstarred is
    # tutorial-grade, NOT demonstrated engineering. It is deliberately gated on
    # that lone original being bare so a described, maintained early-career
    # original lands in "mixed" (still-learning), not here (substance, not
    # presence: a real small project is not a wrapper tell).
    thin_or_absent = (
        n_orig == 0
        or (age_years is not None and age_years < 1.0 and total_stars < 3)
        or (flagship_thin and total_stars < 3)
        or (
            fork_ratio >= 0.6
            and n_orig <= 1
            and total_stars < 3
            and described_ratio == 0.0
            and topics_total == 0
            and maintained == 0
        )
    )
    if thin_or_absent:
        return _TECH_AUTH_THIN, facts

    aged_enough = age_years is None or age_years >= _SUBSTANTIAL_MIN_AGE_YEARS
    enough_originals = n_orig >= _SUBSTANTIAL_MIN_ORIGINAL_REPOS
    # Substance can show up as reach (stars / language breadth) OR as
    # craftsmanship (self-describing, maintained-over-time repos) even when
    # stars are modest. Either route clears "substantial"; a single viral repo
    # alone (concentrated stars, no breadth, no craftsmanship) does not.
    reach = total_stars >= _SUBSTANTIAL_MIN_TOTAL_STARS or len(languages) >= _SUBSTANTIAL_MIN_LANGUAGES
    craftsmanship = (
        described_ratio >= _SUBSTANTIAL_MIN_DESCRIBED_RATIO
        and maintained >= _SUBSTANTIAL_MIN_MAINTAINED
    )
    if aged_enough and enough_originals and (reach or craftsmanship):
        return _TECH_AUTH_SUBSTANTIAL, facts
    return _TECH_AUTH_MIXED, facts


def _tech_reasons(facts: dict) -> list[str]:
    """Turn the computed substance signals into short, human-readable reason
    strings so the snippet can JUSTIFY the read (cite the concrete artifacts),
    not just emit a bare label. Ordering goes footprint, languages, stars,
    self-description, maintenance."""
    n_orig = facts["original_repos"]
    forks = facts["forks"]
    languages = facts["languages"]
    total_stars = facts["total_stars"]

    reasons = [f"{n_orig} original repo(s) vs {forks} fork(s)"]

    if len(languages) >= 2:
        reasons.append(f"{len(languages)} languages ({', '.join(languages[:5])})")
    elif len(languages) == 1:
        reasons.append(f"single language ({languages[0]})")
    else:
        reasons.append("no languages detected on originals")

    if n_orig == 0:
        pass
    elif total_stars == 0:
        reasons.append("no stars on original repos")
    elif facts["star_concentrated"]:
        reasons.append(
            f"stars concentrated in one repo ({facts['max_stars']} of {total_stars})"
        )
    else:
        reasons.append(f"{total_stars} star(s) spread across originals")

    if n_orig > 0:
        if facts["described"] == 0 and facts["topics_total"] == 0:
            reasons.append("no repo descriptions or topics")
        else:
            note = f"{facts['described']} of {n_orig} originals describe themselves"
            if facts["topics_total"]:
                note += f", {facts['topics_total']} topic(s)"
            reasons.append(note)

    if facts["maintained"] > 0:
        reasons.append(
            f"{facts['maintained']} repo(s) maintained (pushed well after creation)"
        )
    elif facts["single_shot"] > 0:
        reasons.append(
            f"originals look like single-commit dumps "
            f"({facts['single_shot']} created then never pushed again)"
        )

    return reasons


def _build_snippet(
    login: str,
    user: dict,
    confidence: str,
    thin: Optional[dict],
    tech_auth: Optional[tuple[str, dict]] = None,
    profile_declared: bool = False,
    deep_analyses: Optional[list[dict]] = None,
) -> str:
    created_at = user.get("created_at", "")
    public_repos = user.get("public_repos", 0)
    parts = [
        f"GitHub account {login!r} created {created_at or 'an unknown date'}, "
        f"{public_repos} public repo(s)."
    ]
    if user.get("_retrieval") == "public_html_fallback":
        parts.append(
            "The free GitHub API budget was exhausted, so account metadata "
            "and repositories were recovered from GitHub's public HTML."
        )
    if confidence == "high":
        # A profile-declared handle leads with why identity is settled. A high
        # from a strong disambiguator carries no extra caveat (current behavior).
        if profile_declared:
            parts.insert(
                0,
                "The LinkedIn profile's own contact info links this GitHub "
                "account, so identity is profile-declared, not guessed.",
            )
    elif confidence == "medium":
        parts.append(
            "Name-pattern match only (both name tokens appear in the handle): "
            "corroborating, NOT identifying. This account is not confirmed to "
            "be the claimed person."
        )
    else:
        parts.append(
            "No strong disambiguator (claimed company or personal-site hint) "
            "matched this account; it may not be the same person."
        )
    if thin:
        verdict = "looks thin" if thin["looks_thin"] else "looks substantial"
        contributor_note = (
            f"{thin['contributor_count']} contributor(s)"
            if thin["contributor_count"] is not None
            else "contributor count unavailable"
        )
        fork_note = "fork, " if thin["is_fork"] else ""
        parts.append(
            f"Product repo {thin['name']!r} {verdict}: "
            f"{fork_note}{thin['stars']} star(s), {contributor_note}."
        )
        if thin.get("deep_override"):
            parts.append(
                "The deeper tree and commit inspection overrode the shallow "
                "low-reach heuristic; low stars and one contributor do not make "
                "a source-rich, repeatedly committed project a thin wrapper."
            )
    if tech_auth is not None:
        read, facts = tech_auth
        lang_note = ", ".join(facts["languages"][:5]) if facts["languages"] else "none detected"
        age_note = (
            f"{facts['age_years']}y old" if facts["age_years"] is not None else "age unknown"
        )
        parts.append(
            f"Technical authenticity read: {read} "
            f"({facts['original_repos']} original repo(s), {facts['forks']} fork(s), "
            f"{facts['total_stars']} star(s) across originals, languages: {lang_note}, "
            f"account {age_note}). This judges code SUBSTANCE, not whether AI was used: "
            "a well-structured real build is skill even if AI-assisted; a thin single-call "
            "wrapper sold as proprietary tech is not."
        )
        reasons = facts.get("reasons") or []
        if reasons:
            parts.append("Signals: " + "; ".join(reasons) + ".")
        # Discipline: an unconfirmed account's footprint (low namesake OR medium
        # name-pattern match), substantial OR thin, neither clears nor deepens
        # the CLAIMED person's technical claim. Say so explicitly so a downstream
        # brain never treats this read as identifying evidence when the identity
        # itself is unproven. Only a "high" (confirmed) identity omits it.
        if confidence != "high":
            parts.append(
                "Because this account is not confirmed to be the claimed person, "
                "this footprint neither clears nor deepens their technical claim."
            )
    for analysis in deep_analyses or []:
        manifests = ", ".join(analysis.get("manifests") or []) or "none detected"
        layers = ", ".join(analysis.get("architecture_layers") or []) or "single or unclear"
        frameworks = ", ".join(analysis.get("frameworks") or []) or "none identified"
        commands = ", ".join(analysis.get("engineering_commands") or []) or "none verified"
        deployments = ", ".join(analysis.get("deployment_targets") or []) or "none identified"
        quality = ", ".join(analysis.get("quality_signals") or []) or "none detected"
        risks = ", ".join(analysis.get("risk_signals") or []) or "none detected"
        truncation = " (tree truncated, counts are lower bounds)" if analysis.get("tree_truncated") else ""
        if analysis.get("inspection_method") == "public_archive_fallback":
            authorship_note = (
                "The free API budget was unavailable, so this used the public "
                "repository archive. File structure is verified, but commit "
                "authorship and account attribution were not checked."
            )
        else:
            linked_ratio = analysis.get("account_linked_commit_ratio")
            ratio_note = (
                f", {int(round(linked_ratio * 100))}% linked"
                if isinstance(linked_ratio, (int, float))
                else ""
            )
            authorship_note = (
                f"account-linked commits: {analysis.get('account_linked_commits', 0)} "
                f"of {analysis.get('commits_sampled', 0)} sampled{ratio_note}; "
                f"distinct sampled authors: {analysis.get('distinct_commit_authors', 0)}. "
                "This verifies public tree structure and GitHub-linked authorship only."
            )
        parts.append(
            f"Deep repository inspection for {analysis.get('repo')!r}: "
            f"{analysis.get('artifact_read')} artifact structure{truncation}; "
            f"source files: {analysis.get('source_files', 0)}, "
            f"tests: {analysis.get('test_files', 0)}, "
            f"CI: {'yes' if analysis.get('ci') else 'no'}, "
            f"dependency manifests: {manifests}, "
            f"generated/vendor files excluded: {analysis.get('generated_files_excluded', 0)}. "
            f"Technical nuance: repository layers: {layers}; "
            f"frameworks from sampled config: {frameworks}; "
            f"verified commands: {commands}; deployment targets: {deployments}; "
            f"quality signals: {quality}; caution signals: {risks}. "
            f"{authorship_note} "
            "It does not prove runtime correctness, security, originality, private "
            "work, or the person's claimed job title."
        )
    return " ".join(parts)


def verify_github(
    person_name: str, company: Optional[str] = None, hints: Optional[dict] = None
) -> list[dict]:
    """Best-effort GitHub identity/activity check for one claimed founder or
    engineer.

    Returns up to _MAX_CANDIDATES evidence records (one per plausible
    account), each carrying account creation date (un-backdatable), public
    repo count, a thin-wrapper-vs-substantial read on any plausibly-matching
    product repo, and an account-wide TECHNICAL AUTHENTICITY read (substantial
    / mixed / thin-or-absent) computed from the repos list. Returns [] on any
    network failure, or if no candidates are found. Never raises.

    match_confidence tiers:
      - "high": either the person's own contact-info overlay DECLARED this
        handle (hints["github_login"], looked up directly, zero namesake risk),
        or a strong disambiguator matched (see _strong_disambiguator_matched).
      - "medium": a two-token name-pattern match on the handle (see
        _name_handle_match). Corroborating ONLY: it can withdraw a GAP on the
        claim it rides, but it never identifies the person and must never be
        treated as confirming the claim.
      - "low": a bare namesake. Never identifying, never clears, never deepens.
    Never treat a "low" or "medium" record as identifying the claimed person.
    """
    person_name = (person_name or "").strip()
    if not person_name:
        return []
    hints = hints or {}

    # C.1 PRIMARY, zero namesake risk: a profile-DECLARED GitHub handle (from the
    # person's own contact-info overlay) is looked up directly and returned as a
    # single "high" record. The person told us the handle, so namesake candidates
    # are pure noise and extra unauthenticated API spend: skip the name search
    # entirely. A handle that does not resolve falls through to the search below.
    linked_login = (hints.get("github_login") or "").strip()
    if linked_login:
        record = _verify_linked_login(linked_login, company or "")
        if record is not None:
            return [record]

    try:
        candidates = _search_users(person_name)
    except Exception as exc:  # noqa: BLE001 - network must never crash the pipeline
        logger.warning("github: search failed for %r: %s", person_name, exc)
        candidates = (
            _html_search_users(person_name)
            if _is_rate_limit_error(exc)
            else []
        )
    if not candidates:
        return []

    evidence: list[dict] = []
    for candidate in candidates[:_MAX_CANDIDATES]:
        login = candidate.get("login")
        if not login:
            continue
        try:
            user = _get_user(login)
        except Exception as exc:  # noqa: BLE001
            logger.warning("github: user lookup failed for %r: %s", login, exc)
            user = _html_get_user(login) if _is_rate_limit_error(exc) else None
        if not user:
            continue

        # Three-tier read. A strong disambiguator (claimed company or a
        # personal-site/domain hint in the account) is identifying -> "high". A
        # two-token name-pattern match is corroborating only -> "medium" (never
        # identifying, never suppresses on its own beyond withdrawing a GAP).
        # Everything else, a bare namesake, stays "low", the load-bearing default
        # that never falsely clears OR falsely deepens on a stranger's account.
        if _strong_disambiguator_matched(user, company or "", hints):
            confidence = "high"
        elif _name_handle_match(person_name, login):
            confidence = "medium"
        else:
            confidence = "low"

        # Fetch the repos list ONCE and reuse it for both the product-repo read
        # and the account-wide technical authenticity read (no extra API call).
        try:
            repos = _get_repos(login)
        except Exception as exc:  # noqa: BLE001 - best effort only
            logger.warning("github: repo list failed for %r: %s", login, exc)
            repos = _html_get_repos(login) if _is_rate_limit_error(exc) else []

        thin = _find_thin_wrapper_repo(repos, login, company or "")
        deep_analyses = (
            _deep_inspect_repositories(login, repos, company or "")
            if confidence == "high"
            else []
        )
        thin = _reconcile_thin_with_deep(thin, deep_analyses)
        tech_auth = _assess_technical_authenticity(user, repos, thin)

        evidence.append(
            {
                "source_url": user.get("html_url", f"https://github.com/{login}"),
                "snippet": _build_snippet(
                    login,
                    user,
                    confidence,
                    thin,
                    tech_auth,
                    deep_analyses=deep_analyses,
                ),
                "source_name": _SOURCE_NAME,
                "weight": weight_for(_SOURCE_NAME),
                "match_confidence": confidence,
            }
        )

    return evidence
