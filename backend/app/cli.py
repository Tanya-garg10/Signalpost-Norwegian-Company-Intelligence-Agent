from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import random
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, TextIO

import httpx

from .research import (
    API,
    USER_AGENT,
    RequestLimiter,
    discover_org_numbers,
    normalize_org_number,
    research_many,
)

ROOT = Path(__file__).resolve().parents[2]
DEMO_NUMBER = "974760673"
UNIVERSE_URL = "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"
UNIVERSE_SHA256 = "1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384"
CLAIM_FIELDS = (
    ("official_name", "name"),
    ("organisation_number", "organization_number"),
    ("legal_form", "organization_form"),
    ("registration_status", "registration_status"),
    ("registered", "registered"),
    ("registration_date", "registration_date"),
    ("foundation_date", "foundation_date"),
    ("business_register_date", "business_register_date"),
    ("vat_register_date", "vat_register_date"),
    ("employee_count_date", "employee_count_date"),
    ("business_address", "business_address"),
    ("postal_address", "postal_address"),
    ("industry_code", "industry_code"),
    ("industry_description", "industry_description"),
    ("official_website", "website"),
    ("employees", "employees"),
    ("purpose_or_activity", "description"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number_from_row(row: Any) -> str:
    if isinstance(row, dict):
        for key in (
            "organisation_number",
            "org_number",
            "organization_number",
            "organisasjonsnummer",
            "company_number",
        ):
            if row.get(key):
                return str(row[key])
        return ""
    if isinstance(row, (str, int)):
        return str(row)
    return ""


def _read_json_lines(handle: TextIO) -> list[str]:
    numbers = []
    for line_number, line in enumerate(handle, 1):
        if not line.strip():
            continue
        try:
            numbers.append(_number_from_row(json.loads(line)))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSONL on line {line_number}: {error.msg}") from error
    return numbers


def parse(path: Path) -> list[str]:
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return _read_json_lines(handle)
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            return _read_json_lines(handle)

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        values = (
            raw.get("company_numbers")
            or raw.get("org_numbers")
            or raw.get("companies")
            or raw.get("input")
        )
    else:
        values = raw
    if not isinstance(values, list):
        raise ValueError(
            "Input must be a JSON array/object or a JSONL file with organisation numbers."
        )
    return [_number_from_row(item) for item in values]


def select_universe(path: Path, count: int, seed: int) -> tuple[list[str], str]:
    if count < 1 or count > 10_000:
        raise ValueError("Discovery count must be between 1 and 10,000.")
    if path.suffix.lower() != ".gz":
        raise ValueError("The official public universe must be the supplied .jsonl.gz archive.")
    archive_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if archive_hash != UNIVERSE_SHA256:
        raise ValueError(
            "The public-universe archive SHA-256 does not match the official contract. "
            "Download the current archive from the documented URL and verify its hash."
        )
    unique: list[str] = []
    seen: set[str] = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid official-universe JSONL at line {line_number}.") from error
            number = normalize_org_number(_number_from_row(row))
            if number and number not in seen:
                unique.append(number)
                seen.add(number)
    if len(unique) < count:
        raise ValueError(
            f"The verified universe contains {len(unique)} distinct valid numbers; {count} requested."
        )
    chosen = random.Random(seed).sample(unique, count)
    return chosen, archive_hash


async def run(
    numbers: list[str],
    *,
    refresh: bool = False,
    progress: bool = True,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    limiter = RequestLimiter(interval_seconds=0.2)
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for offset in range(0, len(numbers), 25):
            chunk = numbers[offset : offset + 25]
            results.extend(
                await research_many(
                    chunk,
                    client,
                    refresh=refresh,
                    concurrency=5,
                    limiter=limiter,
                )
            )
            if progress:
                print(
                    f"Processed {len(results)}/{len(numbers)} company numbers",
                    file=sys.stderr,
                )
    return results


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    claims = []
    for field, key in CLAIM_FIELDS:
        fact = profile.get(key) or {}
        availability = fact.get("status", "not_available")
        evidence_ids = []
        for source in fact.get("evidence", []):
            evidence_id = f"ev-{len(evidence) + 1}"
            evidence_ids.append(evidence_id)
            evidence.append(
                {
                    "id": evidence_id,
                    "source_url": source["url"],
                    "source_class": source.get("source_class", "official_registry"),
                    "retrieved_at": source["retrieved_at"],
                    "source_date": source.get("source_date"),
                    "content_sha256": source.get("content_sha256"),
                    "claim_span": source.get("claim_span", ""),
                }
            )
        claims.append(
            {
                "field": field,
                "value": fact.get("value"),
                "availability": availability,
                "confidence": 1.0 if availability == "available" else 0.0,
                "evidence_ids": evidence_ids,
            }
        )
    terminal_status = profile.get("status", "failed")
    if terminal_status in ("available", "not_available", "not_applicable"):
        run_status = "completed"
    else:
        run_status = terminal_status
    return {
        "organisation_number": profile["org_number"],
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": run_status,
        },
        "claims": claims,
        "evidence": evidence,
        "changes": profile.get("changes", []),
        "errors": profile.get("errors", []),
        "operations": profile.get(
            "operations",
            {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0},
        ),
    }


def validate_envelopes(
    envelopes: list[dict[str, Any]], input_numbers: list[str]
) -> dict[str, Any]:
    errors: list[str] = []
    allowed_availability = {
        "available",
        "not_available",
        "blocked",
        "not_applicable",
        "ambiguous",
        "failed",
    }
    allowed_run_status = {"completed", "blocked", "ambiguous", "failed"}
    if len(envelopes) != len(input_numbers):
        errors.append(
            f"Expected {len(input_numbers)} envelopes, received {len(envelopes)}."
        )
    for index, envelope in enumerate(envelopes):
        if index < len(input_numbers):
            expected = normalize_org_number(input_numbers[index]) or input_numbers[index]
            if envelope.get("organisation_number") != expected:
                errors.append(f"Envelope {index + 1} does not match its input number.")
        run = envelope.get("run", {})
        if run.get("terminal_status") not in allowed_run_status:
            errors.append(f"Envelope {index + 1} has an invalid terminal status.")
        evidence_ids = {
            item.get("id") for item in envelope.get("evidence", []) if item.get("id")
        }
        for claim in envelope.get("claims", []):
            availability = claim.get("availability")
            references = claim.get("evidence_ids", [])
            if availability not in allowed_availability:
                errors.append(f"Envelope {index + 1} has an invalid claim state.")
            if any(reference not in evidence_ids for reference in references):
                errors.append(f"Envelope {index + 1} references missing evidence.")
            if availability == "available" and not references:
                errors.append(f"Envelope {index + 1} has an unsupported available claim.")
        for item in envelope.get("evidence", []):
            for key in (
                "source_url",
                "source_class",
                "retrieved_at",
                "source_date",
                "content_sha256",
                "claim_span",
            ):
                if key not in item:
                    errors.append(f"Envelope {index + 1} evidence is missing {key}.")
    return {
        "passed": not errors,
        "expected_envelopes": len(input_numbers),
        "actual_envelopes": len(envelopes),
        "errors": errors,
    }


def run_tests() -> bool:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "backend"), pattern="tests.py", top_level_dir=str(ROOT)
    )
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stderr).run(suite)
    return result.wasSuccessful()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def main_async(args: argparse.Namespace) -> int:
    wall_started = perf_counter()
    started_at = now()
    discovery_pages: list[str] = []
    discovery_requests = 0
    universe_sha256: str | None = None
    demo = args.demo or args.test_and_demo

    if args.discover is not None:
        if args.input is not None or demo:
            raise ValueError("--discover cannot be combined with an input file or demo.")
        if args.universe:
            numbers, universe_sha256 = select_universe(
                args.universe, args.discover, args.seed
            )
        else:
            limiter = RequestLimiter(interval_seconds=0.2)
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                numbers, discovery_pages, discovery_requests = await discover_org_numbers(
                    client, args.discover, limiter
                )
        results = await run(numbers, refresh=args.refresh)
    elif args.universe:
        raise ValueError("--universe requires --discover N.")
    elif demo:
        if args.input is not None:
            raise ValueError("--demo cannot be combined with an input file.")
        numbers = [DEMO_NUMBER]
        results = await run(numbers, refresh=True)
    else:
        if args.input is None:
            raise ValueError("Provide an input JSON/JSONL file, --discover N, or --demo.")
        numbers = parse(args.input)
        if not numbers:
            raise ValueError("The input batch must contain at least one company number.")
        results = await run(numbers, refresh=args.refresh)

    completed_at = now()
    run_id = args.run_id or f"signalpost-{started_at.replace(':', '').replace('-', '')}"
    envelopes = [
        terminal_envelope(
            profile,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
        )
        for profile in results
    ]
    validation = validate_envelopes(envelopes, numbers)
    write_jsonl(args.out, envelopes)

    normalized = [normalize_org_number(number) for number in numbers]
    available_count = sum(
        envelope["run"]["terminal_status"] == "completed"
        and all(
            claim["availability"] in ("available", "not_available")
            for claim in envelope["claims"]
        )
        and any(claim["availability"] == "available" for claim in envelope["claims"])
        for envelope in envelopes
    )
    requests_total = sum(
        int(envelope["operations"].get("requests", 0)) for envelope in envelopes
    ) + discovery_requests
    latencies = sorted(
        int(envelope["operations"].get("runtime_ms", 0)) for envelope in envelopes
    )
    p50_ms = latencies[len(latencies) // 2] if latencies else None
    p95_ms = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": len(numbers),
        "emitted_envelopes": len(envelopes),
        "distinct_valid_organisation_numbers": len(
            {number for number in normalized if number}
        ),
        "verified_available_profiles": available_count,
        "terminal_status_counts": {
            state: sum(
                envelope["run"]["terminal_status"] == state for envelope in envelopes
            )
            for state in ("completed", "blocked", "ambiguous", "failed")
        },
        "complete": len(envelopes) == len(numbers),
        "validation": validation,
        "output_jsonl": str(args.out),
        "output_sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
        "operations": {
            "requests": requests_total,
            "runtime_ms": round((perf_counter() - wall_started) * 1000),
            "company_runtime_ms_sum": sum(
                int(envelope["operations"].get("runtime_ms", 0)) for envelope in envelopes
            ),
            "p50_ms": p50_ms,
            "p95_ms": p95_ms,
            "third_party_cost_usd": 0,
            "discovery_requests": discovery_requests,
        },
        "claim_availability_counts": {
            state: sum(
                claim["availability"] == state
                for envelope in envelopes
                for claim in envelope["claims"]
            )
            for state in (
                "available",
                "not_available",
                "blocked",
                "not_applicable",
                "ambiguous",
                "failed",
            )
        },
        "sources": [
            "https://data.brreg.no/enhetsregisteret/api/enheter/{organisation_number}"
        ],
    }
    if args.discover is not None:
        report["discovery"] = {
            "requested_count": args.discover,
            "universe_archive_sha256": universe_sha256,
            "universe_source": UNIVERSE_URL if universe_sha256 else None,
            "registry_list_source": f"{API}?size=100&page={{page}}"
            if discovery_pages
            else None,
            "registry_list_pages": discovery_pages,
            "organisation_number_sha256": hashlib.sha256(
                "\n".join(number for number in normalized if number).encode("ascii")
            ).hexdigest(),
            "seed": args.seed if universe_sha256 else None,
        }
    report_path = args.report or args.out.with_suffix(".report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {len(envelopes)}/{len(numbers)} JSONL envelopes; "
        f"{available_count} verified profiles; {requests_total} requests. "
        f"Report: {report_path}"
    )

    if not validation["passed"]:
        return 2
    if args.discover is not None and available_count < args.discover:
        print(
            "Fewer available profiles than requested; the report records the actual result.",
            file=sys.stderr,
        )
        return 2
    if demo and not envelopes[0]["claims"][0]["availability"] == "available":
        print("The live sample lookup did not return an available company.", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Research Norwegian companies and emit Signalpost terminal JSONL envelopes."
    )
    parser.add_argument("input", type=Path, nargs="?", help="JSON, JSONL, or gzip JSONL input")
    parser.add_argument("--out", type=Path, default=Path("out/signalpost-envelopes.jsonl"))
    parser.add_argument("--report", type=Path, help="machine-readable run report JSON")
    parser.add_argument("--run-id", help="reproducible run identifier")
    parser.add_argument(
        "--discover",
        type=int,
        metavar="N",
        help="select N companies from --universe or discover them from the official registry",
    )
    parser.add_argument(
        "--universe",
        type=Path,
        help="official, SHA-256-verified Signalpost public .jsonl.gz universe",
    )
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--refresh", action="store_true", help="bypass the 15-minute profile cache")
    parser.add_argument("--demo", action="store_true", help="look up the official registry's own company")
    parser.add_argument(
        "--test-and-demo",
        action="store_true",
        help="run backend tests, then perform and export one live sample lookup",
    )
    args = parser.parse_args()

    if args.test_and_demo and not run_tests():
        raise SystemExit(1)
    try:
        raise SystemExit(asyncio.run(main_async(args)))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
