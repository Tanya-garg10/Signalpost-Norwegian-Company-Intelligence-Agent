from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

API = "https://data.brreg.no/enhetsregisteret/api/enheter"
SOURCE = "Brønnøysund Register Centre"
DB = Path(__file__).resolve().parents[1] / "signalpost.sqlite3"
CACHE_TTL = timedelta(minutes=15)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 0.5
USER_AGENT = "SignalpostStudentAgent/1.0"
LOGGER = logging.getLogger("signalpost")
FACT_FIELDS = (
    "name",
    "organization_form",
    "registered",
    "registration_status",
    "registration_date",
    "foundation_date",
    "business_register_date",
    "vat_register_date",
    "employee_count_date",
    "business_address",
    "postal_address",
    "industry_code",
    "industry_description",
    "website",
    "employees",
    "description",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def valid_org_number(value: str) -> bool:
    if not re.fullmatch(r"\d{9}", value):
        return False
    weighted_sum = sum(
        int(digit) * weight
        for digit, weight in zip(value[:8], (3, 2, 7, 6, 5, 4, 3, 2))
    )
    check_digit = 11 - weighted_sum % 11
    if check_digit == 11:
        check_digit = 0
    return check_digit != 10 and check_digit == int(value[8])


def normalize_org_number(value: Any) -> str | None:
    raw = str(value or "").strip()
    compact = re.sub(r"\s+", "", raw)
    return compact if valid_org_number(compact) else None


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS profiles (
                org TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                checked_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS source_snapshots (
                id INTEGER PRIMARY KEY,
                org TEXT NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                changes TEXT NOT NULL,
                UNIQUE(org, digest)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY,
                org TEXT NOT NULL,
                changed TEXT NOT NULL,
                changes TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS history_org_changed ON history(org, changed)"
        )


class RequestLimiter:
    def __init__(self, interval_seconds: float = 0.2) -> None:
        self.interval_seconds = interval_seconds
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next_request - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request = loop.time() + self.interval_seconds


async def request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    limiter: RequestLimiter | None = None,
    request_counter: list[int] | None = None,
) -> httpx.Response:
    for attempt in range(MAX_ATTEMPTS):
        if limiter:
            await limiter.wait()
        if request_counter is not None:
            request_counter[0] += 1
        try:
            response = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(RETRY_BASE_SECONDS * (2**attempt))
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if attempt == MAX_ATTEMPTS - 1:
            return response

        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(
                    0.0,
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                delay = RETRY_BASE_SECONDS * (2**attempt)
        await asyncio.sleep(delay)
    raise RuntimeError("Registry retry loop ended unexpectedly.")


def evidence(
    url: str,
    retrieved_at: str,
    source_date: str | None,
    content_sha256: str,
    claim_span: str,
) -> list[dict[str, Any]]:
    return [
        {
            "id": "",
            "source": SOURCE,
            "url": url,
            "source_class": "official_registry",
            "retrieved_at": retrieved_at,
            "source_date": source_date,
            "content_sha256": content_sha256,
            "claim_span": claim_span,
        }
    ]


def fact(
    value: Any,
    url: str,
    retrieved_at: str,
    source_date: str | None = None,
    content_sha256: str = "",
    claim_span: str = "",
    unavailable_status: str = "not_available",
) -> dict[str, Any]:
    available = value is not None and value != "" and value != []
    return {
        "value": value if available else None,
        "status": "available" if available else unavailable_status,
        "source_date": source_date,
        "evidence": (
            evidence(url, retrieved_at, source_date, content_sha256, claim_span)
            if available
            else []
        ),
    }


def nested(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def address(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    parts: list[str] = []
    for key in ("adresse", "postnummer", "poststed", "land"):
        value = data.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if item)
        elif value:
            parts.append(str(value))
    return ", ".join(parts) or None


def empty_profile(
    requested_number: str, status: str, retrieved_at: str, message: str
) -> dict[str, Any]:
    url = f"{API}/{requested_number}"
    claims = {
        key: fact(
            None,
            url,
            retrieved_at,
            unavailable_status=status,
        )
        for key in FACT_FIELDS
    }
    claims["organization_number"] = fact(
        None, url, retrieved_at, unavailable_status=status
    )
    return {
        "org_number": requested_number,
        "status": status,
        **claims,
        "summary": "No verified company summary is available.",
        "source_url": url,
        "retrieved_at": retrieved_at,
        "source_snapshots": [],
        "refresh": {
            "retrieved_at": retrieved_at,
            "last_checked_at": retrieved_at,
            "cache_hit": False,
            "snapshot_digest": None,
            "changes": [],
        },
        "changes": [],
        "errors": [{"message": message}],
        "operations": {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0},
    }


def _source_dates(data: dict[str, Any]) -> dict[str, str | None]:
    return {
        "registration_date": data.get("registreringsdatoEnhetsregisteret"),
        "foundation_date": data.get("stiftelsesdato"),
        "business_register_date": data.get("registreringsdatoIForetaksregisteret"),
        "vat_register_date": data.get("registreringsdatoIMvaregisteret"),
        "employee_count_date": (
            data.get("registreringsdatoAntallAnsatteEnhetsregisteret")
            or data.get("registreringsdatoAntallAnsatteNAVAaregisteret")
        ),
    }


def _registration_status(data: dict[str, Any]) -> str | None:
    if data.get("konkurs") is True:
        return "Bankruptcy recorded"
    if data.get("underTvangsavviklingEllerTvangsopplosning") is True:
        return "Compulsory liquidation or dissolution recorded"
    if data.get("underAvvikling") is True:
        return "Liquidation recorded"
    if data.get("registreringsdatoEnhetsregisteret"):
        return "Registered in the Central Coordinating Register"
    return None


def from_data(
    org: str,
    data: dict[str, Any],
    retrieved_at: str,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    url = f"{API}/{org}"
    if str(data.get("organisasjonsnummer") or "") != org:
        return empty_profile(
            org,
            "ambiguous",
            retrieved_at,
            "Registry organisation number did not exactly match the requested number.",
        )

    if content_sha256 is None:
        canonical_data = json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        content_sha256 = hashlib.sha256(canonical_data).hexdigest()
    source_dates = _source_dates(data)
    values = {
        "name": data.get("navn"),
        "organization_form": nested(data, "organisasjonsform", "beskrivelse"),
        "registered": (
            True if data.get("registreringsdatoEnhetsregisteret") else None
        ),
        "registration_status": _registration_status(data),
        **source_dates,
        "business_address": address(data.get("forretningsadresse")),
        "postal_address": address(data.get("postadresse")),
        "industry_code": nested(data, "naeringskode1", "kode"),
        "industry_description": nested(data, "naeringskode1", "beskrivelse"),
        "website": data.get("hjemmeside"),
        "employees": data.get("antallAnsatte"),
        "description": data.get("vedtektsfestetFormaal") or data.get("aktivitet"),
        "organization_number": org,
    }
    source_values = {
        "name": ("navn", data.get("navn")),
        "organization_form": ("organisasjonsform", data.get("organisasjonsform")),
        "registered": (
            "registreringsdatoEnhetsregisteret",
            data.get("registreringsdatoEnhetsregisteret"),
        ),
        "registration_status": (
            "registration_status_flags",
            {
                key: data.get(key)
                for key in (
                    "konkurs",
                    "underAvvikling",
                    "underTvangsavviklingEllerTvangsopplosning",
                    "registreringsdatoEnhetsregisteret",
                )
                if key in data
            },
        ),
        "registration_date": ("registreringsdatoEnhetsregisteret", source_dates["registration_date"]),
        "foundation_date": ("stiftelsesdato", source_dates["foundation_date"]),
        "business_register_date": (
            "registreringsdatoIForetaksregisteret",
            source_dates["business_register_date"],
        ),
        "vat_register_date": (
            "registreringsdatoIMvaregisteret",
            source_dates["vat_register_date"],
        ),
        "employee_count_date": (
            (
                "registreringsdatoAntallAnsatteEnhetsregisteret"
                if data.get("registreringsdatoAntallAnsatteEnhetsregisteret")
                else "registreringsdatoAntallAnsatteNAVAaregisteret"
            ),
            source_dates["employee_count_date"],
        ),
        "business_address": ("forretningsadresse", data.get("forretningsadresse")),
        "postal_address": ("postadresse", data.get("postadresse")),
        "industry_code": ("naeringskode1", nested(data, "naeringskode1", "kode")),
        "industry_description": (
            "naeringskode1",
            nested(data, "naeringskode1", "beskrivelse"),
        ),
        "website": ("hjemmeside", data.get("hjemmeside")),
        "employees": ("antallAnsatte", data.get("antallAnsatte")),
        "description": (
            "vedtektsfestetFormaal" if data.get("vedtektsfestetFormaal") else "aktivitet",
            data.get("vedtektsfestetFormaal") or data.get("aktivitet"),
        ),
        "organization_number": ("organisasjonsnummer", org),
    }
    claims = {
        key: fact(
            value,
            url,
            retrieved_at,
            source_dates.get(key),
            content_sha256,
            (
                f'{source_values[key][0]}: '
                f'{json.dumps(source_values[key][1], ensure_ascii=False, separators=(",", ":"))}'
            ),
        )
        for key, value in values.items()
    }
    # Keep identity evidence explicit, even when no other company attributes exist.
    summary_bits = [str(data.get("navn") or "The company")]
    organization_form = values["organization_form"]
    industry = values["industry_description"]
    if organization_form:
        summary_bits.append(f"is registered as {organization_form}")
    if industry:
        summary_bits.append(f"and is classified in {industry}")
    return {
        "org_number": org,
        "status": "available",
        **claims,
        "summary": " ".join(summary_bits)
        + ". Other details are shown only when present in the official registry record.",
        "source_url": url,
        "retrieved_at": retrieved_at,
        "source_snapshots": [],
        "refresh": {
            "retrieved_at": retrieved_at,
            "last_checked_at": retrieved_at,
            "cache_hit": False,
            "snapshot_digest": None,
            "changes": [],
        },
        "changes": [],
        "errors": [],
        "operations": {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0},
    }


def _changed_fields(
    previous: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]]:
    changes = []
    for key in FACT_FIELDS:
        old_value = (previous.get(key) or {}).get("value")
        new_value = (current.get(key) or {}).get("value")
        if old_value != new_value:
            changes.append({"field": key, "old": old_value, "new": new_value})
    return changes


def store(profile: dict[str, Any], source_digest: str) -> list[dict[str, Any]]:
    init_db()
    org = profile["org_number"]
    retrieved_at = profile["retrieved_at"]
    changes: list[dict[str, Any]] = []
    with db_connection() as connection:
        previous_row = connection.execute(
            "SELECT digest, payload FROM profiles WHERE org=?", (org,)
        ).fetchone()
        previous = json.loads(previous_row[1]) if previous_row else None
        prior_digest = previous_row[0] if previous_row else None

        if previous and prior_digest != source_digest:
            changes = _changed_fields(previous, profile)
            if changes:
                connection.execute(
                    "INSERT INTO history(org, changed, changes) VALUES(?,?,?)",
                    (org, retrieved_at, json.dumps(changes, ensure_ascii=False)),
                )

        profile["changes"] = changes
        profile["refresh"].update(
            {"snapshot_digest": source_digest, "changes": changes}
        )
        profile["source_snapshots"] = [
            {
                "source": SOURCE,
                "url": profile["source_url"],
                "retrieved_at": retrieved_at,
                "source_date": profile["registration_date"]["value"],
                "sha256": source_digest,
            }
        ]
        payload = json.dumps(profile, ensure_ascii=False)
        if prior_digest != source_digest:
            connection.execute(
                """INSERT OR IGNORE INTO source_snapshots
                   (org, digest, payload, retrieved_at, changes) VALUES(?,?,?,?,?)""",
                (
                    org,
                    source_digest,
                    payload,
                    retrieved_at,
                    json.dumps(changes, ensure_ascii=False),
                ),
            )
        connection.execute(
            """INSERT INTO profiles(org, digest, payload, checked_at)
               VALUES(?,?,?,?)
               ON CONFLICT(org) DO UPDATE SET
                 digest=excluded.digest, payload=excluded.payload,
                 checked_at=excluded.checked_at""",
            (org, source_digest, payload, retrieved_at),
        )
    return changes


def invalidate_current_profile(org: str) -> None:
    init_db()
    with db_connection() as connection:
        connection.execute("DELETE FROM profiles WHERE org=?", (org,))


def cached_profile(org: str) -> dict[str, Any] | None:
    init_db()
    with db_connection() as connection:
        row = connection.execute(
            "SELECT payload, checked_at FROM profiles WHERE org=?", (org,)
        ).fetchone()
    if not row:
        return None
    try:
        checked_at = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
    except ValueError:
        return None
    if datetime.now(timezone.utc) - checked_at > CACHE_TTL:
        return None
    profile = json.loads(row[0])
    profile["refresh"]["cache_hit"] = True
    profile["refresh"]["last_checked_at"] = row[1]
    return profile


async def research(
    number: Any,
    client: httpx.AsyncClient | None = None,
    *,
    refresh: bool = False,
    limiter: RequestLimiter | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    org = normalize_org_number(number)
    if org is None:
        result = empty_profile(
            str(number or ""),
            "ambiguous",
            now(),
            "Norwegian organisation numbers must be 9 digits and pass the modulus-11 check.",
        )
        result["operations"]["runtime_ms"] = round((perf_counter() - started) * 1000)
        return result

    if not refresh:
        cached = cached_profile(org)
        if cached:
            cached["operations"] = {
                "requests": 0,
                "runtime_ms": round((perf_counter() - started) * 1000),
                "third_party_cost_usd": 0,
            }
            return cached

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
    retrieved_at = now()
    request_counter = [0]
    try:
        response = await request_with_retry(
            client, f"{API}/{org}", limiter, request_counter
        )
        retrieved_at = now()
        if response.status_code == 404:
            invalidate_current_profile(org)
            result = empty_profile(org, "not_available", retrieved_at, "No company record found for this number.")
        elif response.status_code == 410:
            invalidate_current_profile(org)
            result = empty_profile(
                org,
                "not_available",
                retrieved_at,
                "Registry returned HTTP 410; the current cached profile was removed.",
            )
        elif response.status_code in (401, 403, 429):
            result = empty_profile(
                org, "blocked", retrieved_at, f"Registry returned HTTP {response.status_code}."
            )
        elif response.status_code >= 500:
            result = empty_profile(
                org, "failed", retrieved_at, f"Registry returned HTTP {response.status_code} after retries."
            )
        else:
            response.raise_for_status()
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError):
                result = empty_profile(
                    org, "failed", retrieved_at, "Could not parse the registry response as JSON."
                )
            else:
                if not isinstance(data, dict):
                    result = empty_profile(
                        org, "failed", retrieved_at, "Unexpected registry response format."
                    )
                else:
                    content_sha256 = hashlib.sha256(response.content).hexdigest()
                    result = from_data(org, data, retrieved_at, content_sha256)
                    if result["status"] == "available":
                        store(result, content_sha256)
    except httpx.TimeoutException:
        result = empty_profile(org, "failed", now(), "Registry request timed out after retries.")
    except httpx.TransportError as error:
        result = empty_profile(
            org,
            "failed",
            now(),
            f"Registry request failed after retries: {type(error).__name__}.",
        )
    except httpx.HTTPStatusError as error:
        result = empty_profile(
            org,
            "failed",
            now(),
            f"Registry returned an unexpected HTTP {error.response.status_code}.",
        )
    finally:
        if owns_client:
            await client.aclose()
    result["operations"] = {
        "requests": request_counter[0],
        "runtime_ms": round((perf_counter() - started) * 1000),
        "third_party_cost_usd": 0,
    }
    return result


async def discover_org_numbers(
    client: httpx.AsyncClient,
    limit: int,
    limiter: RequestLimiter | None = None,
) -> tuple[list[str], list[str], int]:
    if limit < 1 or limit > 10_000:
        raise ValueError("Discovery limit must be between 1 and 10,000.")
    page = 0
    numbers: list[str] = []
    seen: set[str] = set()
    pages: list[str] = []
    request_counter = [0]
    while len(numbers) < limit:
        url = f"{API}?size=100&page={page}"
        response = await request_with_retry(client, url, limiter, request_counter)
        response.raise_for_status()
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Registry company-list response was not valid JSON.") from error
        entities = nested(payload, "_embedded", "enheter")
        if not isinstance(entities, list):
            raise RuntimeError("Registry company-list response did not contain an entity list.")
        pages.append(url)
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            org = normalize_org_number(entity.get("organisasjonsnummer"))
            if org and org not in seen:
                seen.add(org)
                numbers.append(org)
                if len(numbers) == limit:
                    break
        if not entities or len(entities) < 100:
            break
        page += 1
    if len(numbers) < limit:
        raise RuntimeError(
            f"Registry discovery returned only {len(numbers)} distinct valid organisation numbers; "
            f"{limit} were requested."
        )
    return numbers, pages, request_counter[0]


async def research_many(
    numbers: list[str],
    client: httpx.AsyncClient,
    *,
    refresh: bool = False,
    concurrency: int = 5,
    limiter: RequestLimiter | None = None,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def run(number: str) -> dict[str, Any]:
        async with semaphore:
            try:
                return await research(
                    number, client, refresh=refresh, limiter=limiter
                )
            except Exception as error:
                LOGGER.exception("Unexpected failure for registry input %r", number)
                return empty_profile(
                    str(number),
                    "failed",
                    now(),
                    f"Unexpected per-company failure: {type(error).__name__}.",
                )

    return list(await asyncio.gather(*(run(number) for number in numbers)))
