"""Bounded external enrichment for Signalpost profiles.

Adds non-registry claims: latest annual-account figures from the official
Regnskapsregisteret, plus facts observed on the company's own registry-linked
website (title, description, social profiles, dated news items, job postings).

Every website-derived claim is gated on exact-entity identity: the page that
backs the claim must display the registered company name or the nine-digit
organisation number. Pages that cannot prove identity never produce claims.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import research as registry

ACCOUNTS_API = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
ACCOUNTS_SOURCE_CLASS = "official_annual_accounts"
SITE_SOURCE_CLASS = "company_website"

PAGE_TIMEOUT_SECONDS = 10.0
MAX_PAGE_BYTES = 1_500_000
MAX_SECONDARY_PAGES = 4
MAX_NEWS_ITEMS = 3
MAX_JOB_ITEMS = 3

NEWS_PATH = re.compile(
    r"/(?:news|newsroom|press|aktuelt|nyheter|nyhed|blog|innsikter|media|presse|insiderr)(?:/|-|$)",
    re.IGNORECASE,
)
JOB_PATH = re.compile(
    r"/(?:karriere|careers?|karri(?:e|è)re|jobs?|jobber?|vacanc|stillinger|ledige|recruit|work-with|join-us|open-positions)(?:/|-|$)",
    re.IGNORECASE,
)
JOB_ITEM_PATH = re.compile(
    r"(job|still|position|vacanc|role|career|karriere|leder|apply|seek|ledige)",
    re.IGNORECASE,
)
JOB_ITEM_NEGATIVE = re.compile(
    r"(scam|advarsel|warning|contact|sales|personvern|privacy|thank|om-|about|team)",
    re.IGNORECASE,
)
ATS_HOSTS = {
    "workable.com",
    "teamtailor.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "reachmee.com",
    "jobbnorge.no",
    "careers-page.com",
    "recruitee.com",
    "bamboohr.com",
    "smartrecruiters.com",
    "varbi.com",
    "comeet.com",
    "myworkdayjobs.com",
    "jobvite.com",
    "icims.com",
    "taleo.net",
    "successfactors.com",
    "successfactors.eu",
    "phenompeople.com",
    "eightfold.ai",
}
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
}
MONTHS_NO = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "desember": 12,
}
DATE_TEXT_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b"),
    re.compile(
        r"\b(\d{1,2})\.\s*(januar|februar|mars|april|mai|juni|juli|august|"
        r"september|oktober|november|desember)\s+(\d{4})\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(\d{1,2})\s+(januar|februar|mars|april|mai|juni|juli|august|"
               r"september|oktober|november|desember)\s+(\d{4})\b", re.IGNORECASE),
)

NEW_CLAIM_KEYS = (
    "website_title",
    "website_description",
    "social_profiles",
    "company_news",
    "job_postings",
    "latest_reporting_period",
    "latest_annual_revenue",
    "latest_operating_result",
    "latest_annual_result",
    "latest_total_assets",
    "latest_equity",
    "latest_debt",
)


def _dig(body: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(body, dict):
            return None
        body = body.get(key)
    return body


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _fetch_html(
    client: httpx.AsyncClient,
    url: str,
    limiter: registry.RequestLimiter | None,
    request_counter: list[int],
) -> tuple[httpx.Response | None, str | None]:
    if limiter:
        await limiter.wait()
    request_counter[0] += 1
    try:
        response = await asyncio.wait_for(
            client.get(
                url,
                follow_redirects=True,
                headers={
                    "User-Agent": registry.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=PAGE_TIMEOUT_SECONDS,
            ),
            timeout=PAGE_TIMEOUT_SECONDS + 2,
        )
    except Exception:
        return None, "request_failed"
    if response.status_code != 200:
        return None, f"http_{response.status_code}"
    if "html" not in response.headers.get("content-type", "").lower():
        return None, "not_html"
    if len(response.content) > MAX_PAGE_BYTES:
        return None, "too_large"
    return response, None


async def _fetch_accounts(
    client: httpx.AsyncClient,
    org: str,
    limiter: registry.RequestLimiter | None,
    request_counter: list[int],
) -> dict[str, Any]:
    url = ACCOUNTS_API.format(org=org)
    retrieved_at = registry.now()
    try:
        response = await asyncio.wait_for(
            registry.request_with_retry(client, url, limiter, request_counter),
            timeout=PAGE_TIMEOUT_SECONDS * 2,
        )
    except Exception:
        response = None
    records: list[dict[str, Any]] = []
    status = "not_available"
    sha = ""
    if response is not None and response.status_code == 200:
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = None
        if isinstance(body, list):
            records = [item for item in body if isinstance(item, dict)]
            sha = _sha256(response.content)
            status = "available" if records else "not_available"
        else:
            status = "failed"
    elif response is not None and response.status_code in (404, 410):
        status = "not_available"
    elif response is not None:
        status = "failed"

    latest = records[0] if records else {}
    result_block = _dig(latest, "resultatregnskapResultat")
    period_raw = latest.get("regnskapsperiode")
    if isinstance(period_raw, dict):
        fra = period_raw.get("fraDato")
        til = period_raw.get("tilDato")
        period = f"{fra}/{til}" if fra and til else (til or fra)
    else:
        period = period_raw if isinstance(period_raw, str) else None
    span = (
        f"regnskap record id={latest.get('id') or 'unknown'} "
        f"type={latest.get('regnskapstype') or 'unknown'} period={period or 'unknown'}"
    )
    values = {
        "latest_reporting_period": period,
        "latest_annual_revenue": _dig(
            result_block, "driftsresultat", "driftsinntekter", "sumDriftsinntekter"
        ),
        "latest_operating_result": _dig(
            result_block, "driftsresultat", "driftsresultat"
        ),
        "latest_annual_result": _dig(result_block, "aarsresultat"),
        "latest_total_assets": _dig(latest, "eiendeler", "sumEiendeler"),
        "latest_equity": _dig(
            latest, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"
        ),
        "latest_debt": _dig(
            latest, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"
        ),
    }
    claims: dict[str, Any] = {}
    for key, value in values.items():
        available = status == "available" and value not in (None, "")
        claims[key] = registry.fact(
            value if available else None,
            url,
            retrieved_at,
            source_date=period if isinstance(period, str) else None,
            content_sha256=sha,
            claim_span=span,
            unavailable_status=status if status != "available" else "not_available",
            source=registry.SOURCE,
            source_class=ACCOUNTS_SOURCE_CLASS,
        )
    return claims


def _identity_needles(name: Any, org: str) -> list[str]:
    needles: list[str] = []
    if org:
        needles.append(" ".join(org[index : index + 3] for index in range(0, 9, 3)))
    text_name = " ".join(str(name or "").casefold().split())
    if len(text_name) >= 4:
        needles.append(text_name)
        reduced = re.sub(
            r"\b(as|asa|enk|ans|da|nuf|sam|sf|sti|flp|holding|gruppen|group|ab)\b",
            " ",
            text_name,
        )
        reduced = " ".join(reduced.split())
        if len(reduced) >= 4 and reduced != text_name:
            needles.append(reduced)
    return needles


def _identity_match(
    folded_text: str, squashed_text: str, needles: list[str], org: str
) -> str | None:
    for needle in needles:
        if needle.isdigit():
            continue
        if needle in folded_text:
            return needle
    spaced_org = " ".join(org[index : index + 3] for index in range(0, 9, 3))
    if org and org in squashed_text:
        return org
    if spaced_org and spaced_org in folded_text:
        return spaced_org
    return None


def _match_span(folded_text: str, needle: str, width: int = 100) -> str:
    index = folded_text.find(needle)
    if index < 0:
        return needle
    start = max(0, index - width // 2)
    return folded_text[start : index + len(needle) + width // 2].strip()


def _normalize_site_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed._replace(path=parsed.path or "/", params="", query="", fragment="").geturl()


def _same_site(url: str, base_host: str) -> bool:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    base = base_host.casefold().removeprefix("www.")
    return host == base or host.endswith("." + base)


def _social_url(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    platform = next(
        (
            label
            for domain, label in SOCIAL_HOSTS.items()
            if host == domain or host.endswith("." + domain)
        ),
        None,
    )
    if not platform:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    lowered = [part.casefold() for part in parts]
    if platform == "linkedin" and (not parts or lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered and lowered[0] not in {"channel", "c", "user"} and not parts[0].startswith("@"):
        return None
    if platform == "tiktok" and (not parts or not parts[0].startswith("@")):
        return None
    if platform == "x" and (not parts or lowered[0] in {"intent", "share", "home", "search", "i"}):
        return None
    return {"platform": platform, "url": parsed._replace(scheme="https", fragment="").geturl()}


def _candidate_links(soup: BeautifulSoup, base_url: str, base_host: str) -> tuple[list[str], list[str]]:
    news: list[str] = []
    jobs: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not _same_site(url, base_host):
            continue
        clean = parsed._replace(query="", fragment="").geturl().rstrip("/")
        if clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(clean)
        path = parsed.path or "/"
        depth = len([part for part in path.split("/") if part])
        if JOB_PATH.search(path):
            jobs.append((depth, clean))
        elif NEWS_PATH.search(path):
            news.append((depth, clean))
    jobs.sort(key=lambda item: (-item[0], item[1]))
    news.sort(key=lambda item: (-item[0], item[1]))
    return [url for _, url in jobs], [url for _, url in news]


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def _ats_host(url: str) -> bool:
    host = _host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in ATS_HOSTS)


def _extract_date(soup_page: BeautifulSoup, anchor: Any = None) -> str | None:
    """Find a publication-style date.

    With ``anchor`` only anchor-local evidence counts (ancestor <time> tags and
    the link text), so navigation links cannot inherit a page-level date.
    Without ``anchor`` the page's own structured dates, title and leading text
    are considered.
    """
    candidates: list[str] = []
    if anchor is not None:
        node = anchor
        for _ in range(3):
            if node is None:
                break
            time_tag = node.find("time") if hasattr(node, "find") else None
            if time_tag is not None and time_tag.get("datetime"):
                candidates.append(str(time_tag.get("datetime")))
            node = getattr(node, "parent", None)
        candidates.append(anchor.get_text(" ", strip=True))
        raw_candidates = candidates
    else:
        for time_tag in soup_page.select("time[datetime]")[:5]:
            candidates.append(str(time_tag.get("datetime") or ""))
        candidates.append(soup_page.title.get_text(" ", strip=True) if soup_page.title else "")
        raw_candidates = candidates

    def match_date(raw: str) -> str | None:
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if iso:
            return iso.group(0)
        for pattern in DATE_TEXT_PATTERNS:
            match = pattern.search(raw)
            if not match:
                continue
            groups = match.groups()
            if groups[0].isdigit() and len(groups[0]) == 4:
                year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            elif groups[1].casefold() in MONTHS_NO:
                day = int(groups[0])
                month = MONTHS_NO[groups[1].casefold()]
                year = int(groups[2])
            else:
                day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
        return None

    for raw in raw_candidates:
        found = match_date(raw)
        if found:
            return found

    if anchor is not None:
        return None

    for script in soup_page.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, json.JSONDecodeError):
            continue
        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key in ("datePublished", "dateModified"):
                    value = node.get(key)
                    if isinstance(value, str):
                        found.append(value)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(data)
        for value in found:
            iso = re.match(r"(\d{4}-\d{2}-\d{2})", value)
            if iso:
                return iso.group(1)
    body_text = soup_page.get_text(" ", strip=True)[:3000]
    return match_date(body_text)


ARTICLE_JSONLD_TYPES = {
    "article", "newsarticle", "blogposting", "report", "analysisnewsarticle",
    "scholarlyarticle", "techarticle", "socialmediaposting",
}


def _looks_like_article(soup: BeautifulSoup) -> bool:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, json.JSONDecodeError):
            continue
        kinds: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                kind = node.get("@type")
                kinds.extend([kind] if isinstance(kind, str) else [str(k) for k in kind] if isinstance(kind, list) else [])
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(data)
        if any(kind.casefold() in ARTICLE_JSONLD_TYPES for kind in kinds):
            return True
    article = soup.find("article")
    if article is not None and (article.find("time") or _extract_date(BeautifulSoup(str(article), "html.parser"))):
        return True
    return False


def _page_record(
    response: httpx.Response,
    soup: BeautifulSoup,
    retrieved_at: str,
) -> dict[str, Any]:
    return {
        "url": str(response.url),
        "title": soup.title.get_text(" ", strip=True)[:300] if soup.title else "",
        "sha256": _sha256(response.content),
        "retrieved_at": retrieved_at,
        "soup": soup,
        "folded_text": " ".join(soup.get_text(" ", strip=True).casefold().split()),
    }


def _site_fact(
    key: str,
    value: Any,
    pages: list[dict[str, Any]],
    *,
    unavailable_status: str = "not_available",
    source_dates: list[str | None] | None = None,
) -> dict[str, Any]:
    available = value not in (None, "", [])
    if not available:
        return registry.fact(
            None,
            pages[0]["url"] if pages else "",
            pages[0]["retrieved_at"] if pages else registry.now(),
            unavailable_status=unavailable_status,
            source_class=SITE_SOURCE_CLASS,
        )
    evidence_items = []
    for index, page in enumerate(pages):
        span = page.get("claim_span") or page["title"] or page["url"]
        if source_dates is not None:
            source_date = source_dates[index] if index < len(source_dates) else None
        else:
            source_date = page.get("date")
        evidence_items.append(
            registry.evidence(
                page["url"],
                page["retrieved_at"],
                source_date,
                page["sha256"],
                str(span)[:1000],
                source=(urlparse(page["url"]).hostname or "company website"),
                source_class=SITE_SOURCE_CLASS,
            )[0]
        )
    return {
        "value": value,
        "status": "available",
        "source_date": None,
        "evidence": evidence_items,
    }


async def _fetch_site_claims(
    client: httpx.AsyncClient,
    start_url: str,
    needles: list[str],
    org: str,
    limiter: registry.RequestLimiter | None,
    request_counter: list[int],
) -> dict[str, Any]:
    claims: dict[str, Any] = {}
    home, error = await _fetch_html(client, start_url, limiter, request_counter)
    if home is None:
        status = "blocked" if error in {"http_403", "http_401", "http_429"} else (
            "failed" if error in {"request_failed", "too_large"} else "not_available"
        )
        for key in ("website_title", "website_description", "social_profiles", "company_news", "job_postings"):
            claims[key] = registry.fact(
                None, start_url, registry.now(), unavailable_status=status,
                source_class=SITE_SOURCE_CLASS,
            )
        return claims

    retrieved_at = registry.now()
    soup = BeautifulSoup(home.text, "html.parser")
    home_page = _page_record(home, soup, retrieved_at)
    base_host = urlparse(home_page["url"]).hostname or ""
    squashed_home = re.sub(r"\s+", "", home_page["folded_text"])
    home_match = _identity_match(home_page["folded_text"], squashed_home, needles, org)
    if not home_match:
        for key in (
            "website_title",
            "website_description",
            "social_profiles",
            "company_news",
            "job_postings",
        ):
            claims[key] = registry.fact(
                None,
                home_page["url"],
                retrieved_at,
                unavailable_status="ambiguous",
                source_class=SITE_SOURCE_CLASS,
            )
        return claims

    social: dict[tuple[str, str], dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        normalized = _social_url(urljoin(home_page["url"], str(anchor.get("href") or "")))
        if normalized:
            social[(normalized["platform"], normalized["url"])] = normalized

    job_links, news_links = _candidate_links(soup, home_page["url"], base_host)
    secondary_urls: list[str] = []
    for bucket in (job_links[:2], news_links[:2]):
        for url in bucket:
            if url not in secondary_urls:
                secondary_urls.append(url)
    for url in job_links[2:] + news_links[2:]:
        if len(secondary_urls) >= MAX_SECONDARY_PAGES:
            break
        if url not in secondary_urls:
            secondary_urls.append(url)
    secondary_urls = secondary_urls[:MAX_SECONDARY_PAGES]

    secondary_pages: list[dict[str, Any]] = []
    for url in secondary_urls:
        response, _ = await _fetch_html(client, url, limiter, request_counter)
        if response is None:
            continue
        page_soup = BeautifulSoup(response.text, "html.parser")
        page = _page_record(response, page_soup, registry.now())
        page["squashed"] = re.sub(r"\s+", "", page["folded_text"])
        page["match"] = _identity_match(page["folded_text"], page["squashed"], needles, org)
        page["verified"] = bool(page["match"])
        secondary_pages.append(page)

    verified_pages = [page for page in secondary_pages if page["verified"]]
    news_items: list[dict[str, Any]] = []
    job_items: list[dict[str, Any]] = []
    seen_items: set[str] = set()

    for page in verified_pages:
        path = urlparse(page["url"]).path or "/"
        page_soup = page["soup"]
        is_jobs = bool(JOB_PATH.search(path))
        is_news = bool(NEWS_PATH.search(path))
        if is_jobs:
            anchor_items: list[tuple[str, str]] = []
            candidate_seen = {page["url"].rstrip("/")}
            for anchor in page_soup.select("a[href]"):
                href = str(anchor.get("href") or "").strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                url = urljoin(page["url"], href).split("#", 1)[0]
                clean = urlparse(url)._replace(query="", fragment="").geturl()
                key = clean.rstrip("/")
                if key in candidate_seen:
                    continue
                label = anchor.get_text(" ", strip=True)[:200]
                if len(label) < 8:
                    continue
                same_site = _same_site(url, base_host)
                if not same_site and not _ats_host(url):
                    continue
                url_path = urlparse(url).path or ""
                if same_site and (
                    not JOB_ITEM_PATH.search(url_path)
                    or JOB_ITEM_NEGATIVE.search(url_path)
                ):
                    continue
                if not same_site and JOB_ITEM_NEGATIVE.search(label):
                    continue
                candidate_seen.add(key)
                anchor_items.append((label, clean))
                if len(anchor_items) >= MAX_JOB_ITEMS:
                    break
            postings = anchor_items or [(page["title"] or "Job posting", page["url"])]
            for title, url in postings:
                key = url.rstrip("/")
                if key in seen_items:
                    continue
                seen_items.add(key)
                job_items.append({"title": title, "url": url, "_page": page["url"]})
                if len(job_items) >= MAX_JOB_ITEMS:
                    break
        if is_news:
            page_date = _extract_date(page_soup) if _looks_like_article(page_soup) else None
            item_date = page_date
            headline = page["title"] or urlparse(page["url"]).path.rsplit("/", 1)[-1]
            if page_date:
                news_items.append({"title": headline, "url": page["url"], "published_date": item_date, "_page": page["url"]})
                seen_items.add(page["url"].rstrip("/"))
            for anchor in page_soup.select("a[href]"):
                if len(news_items) >= MAX_NEWS_ITEMS:
                    break
                href = str(anchor.get("href") or "").strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                url = urljoin(page["url"], href).split("#", 1)[0]
                if not _same_site(url, base_host):
                    continue
                clean = urlparse(url)._replace(query="", fragment="").geturl()
                if clean.rstrip("/") in seen_items or clean.rstrip("/") == page["url"].rstrip("/"):
                    continue
                label = anchor.get_text(" ", strip=True)[:200]
                if len(label) < 10:
                    continue
                anchor_date = _extract_date(page_soup, anchor)
                if not anchor_date:
                    continue
                seen_items.add(clean.rstrip("/"))
                news_items.append({"title": label, "url": clean, "published_date": anchor_date, "_page": page["url"]})
            if len(news_items) >= MAX_NEWS_ITEMS:
                continue

    news_items = news_items[:MAX_NEWS_ITEMS]
    job_items = job_items[:MAX_JOB_ITEMS]

    meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(meta.get("content") or "").strip() if meta else ""

    identity_status = "available" if home_match else "ambiguous"
    home_record = dict(home_page)
    home_record["claim_span"] = _match_span(home_page["folded_text"], home_match) if home_match else ""

    claims["website_title"] = _site_fact(
        "website_title",
        home_page["title"] if home_match else None,
        [home_record] if home_match else [],
        unavailable_status=identity_status if not home_match else "not_available",
    )
    claims["website_description"] = _site_fact(
        "website_description",
        description[:2000] if (home_match and description) else None,
        [home_record] if home_match and description else [],
        unavailable_status=(
            "not_available" if home_match else identity_status
        ),
    )
    social_list = sorted(social.values(), key=lambda item: (item["platform"], item["url"]))
    claims["social_profiles"] = _site_fact(
        "social_profiles",
        social_list if home_match else None,
        [home_record] if home_match and social_list else [],
        unavailable_status=("not_available" if home_match else identity_status),
    )

    page_by_url = {page["url"].rstrip("/"): page for page in verified_pages}
    fallback_page = verified_pages[0] if verified_pages else home_record
    news_pages = []
    for item in news_items:
        source_page = page_by_url.get(str(item.get("_page") or "").rstrip("/"), fallback_page)
        record = dict(source_page)
        record["url"] = str(item.get("_page") or record["url"])
        record["claim_span"] = f"{item['title']} — published {item.get('published_date') or 'undated'}"
        record["date"] = item.get("published_date")
        news_pages.append(record)
    claims["company_news"] = _site_fact(
        "company_news",
        [{key: value for key, value in item.items() if not key.startswith("_")} for item in news_items] if news_items else None,
        news_pages if news_items else [],
        unavailable_status="not_available" if verified_pages or home_match else identity_status,
        source_dates=[item.get("published_date") for item in news_items],
    )

    job_pages = []
    for item in job_items:
        source_page = page_by_url.get(str(item.get("_page") or "").rstrip("/"), fallback_page)
        record = dict(source_page)
        record["url"] = str(item.get("_page") or record["url"])
        record["claim_span"] = f"Job posting: {item['title']} — {item['url']}"
        job_pages.append(record)
    claims["job_postings"] = _site_fact(
        "job_postings",
        [{key: value for key, value in item.items() if not key.startswith("_")} for item in job_items] if job_items else None,
        job_pages if job_items else [],
        unavailable_status="not_available" if verified_pages or home_match else identity_status,
    )
    return claims


def _unavailable_site_claims(start_url: str, status: str) -> dict[str, Any]:
    claims = {}
    for key in ("website_title", "website_description", "social_profiles", "company_news", "job_postings"):
        claims[key] = registry.fact(
            None, start_url, registry.now(), unavailable_status=status,
            source_class=SITE_SOURCE_CLASS,
        )
    return claims


async def enrich_profile(
    profile: dict[str, Any],
    client: httpx.AsyncClient,
    limiter: registry.RequestLimiter | None,
    request_counter: list[int],
) -> None:
    """Attach external claims to an available registry profile in place."""
    org = str(profile.get("org_number") or "")
    name = (profile.get("name") or {}).get("value")
    account_claims = await _fetch_accounts(client, org, limiter, request_counter)
    profile.update(account_claims)

    website = _normalize_site_url((profile.get("website") or {}).get("value"))
    needles = _identity_needles(name, org)
    if not website:
        profile.update(_unavailable_site_claims("", "not_available"))
    else:
        profile.update(
            await _fetch_site_claims(client, website, needles, org, limiter, request_counter)
        )

    _extend_summary(profile)


def _extend_summary(profile: dict[str, Any]) -> None:
    parts: list[str] = []
    period = (profile.get("latest_reporting_period") or {}).get("value")
    revenue = (profile.get("latest_annual_revenue") or {}).get("value")
    result = (profile.get("latest_annual_result") or {}).get("value")
    if period and (revenue is not None or result is not None):
        fragments = [f"latest annual accounts for {period}"]
        for label, value in (("revenue", revenue), ("annual result", result)):
            try:
                fragments.append(f"{label} of {float(value):,.0f} NOK".replace(",", " "))
            except (TypeError, ValueError):
                continue
        parts.append("The company's " + " and ".join(fragments) + ".")
    news = (profile.get("company_news") or {}).get("value") or []
    jobs = (profile.get("job_postings") or {}).get("value") or []
    site_bits = []
    if jobs:
        site_bits.append(f"{len(jobs)} job posting(s)")
    if news:
        site_bits.append(f"{len(news)} dated news item(s)")
    if site_bits:
        parts.append("Its own website shows " + " and ".join(site_bits) + ".")
    if parts:
        summary = str(profile.get("summary") or "").strip()
        placeholder = "Year-over-year financial comparison is not available without annual accounts data."
        if placeholder in summary and period:
            summary = summary.replace(placeholder, "").strip()
        profile["summary"] = (summary + " " + " ".join(parts)).strip()
