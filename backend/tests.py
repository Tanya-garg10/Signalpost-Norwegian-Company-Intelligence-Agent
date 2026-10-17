from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app.cli import parse, terminal_envelope, validate_envelopes
from backend.app import research

ORG = "974760673"
SAMPLE = {
    "organisasjonsnummer": ORG,
    "navn": "Example AS",
    "registreringsdatoEnhetsregisteret": "2001-02-03",
    "organisasjonsform": {"beskrivelse": "Aksjeselskap"},
    "forretningsadresse": {
        "adresse": ["Main Street 1"],
        "postnummer": "0123",
        "poststed": "OSLO",
        "land": "Norge",
    },
    "naeringskode1": {"kode": "62.010", "beskrivelse": "Computer programming"},
    "antallAnsatte": 4,
    "registreringsdatoAntallAnsatteEnhetsregisteret": "2025-11-01",
}


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "test.sqlite3"
        self.db_patch = patch.object(research, "DB", self.db)
        self.db_patch.start()
        research.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_valid_and_invalid_norwegian_organisation_numbers(self) -> None:
        self.assertTrue(research.valid_org_number(ORG))
        self.assertEqual(research.normalize_org_number(" 974 760 673 "), ORG)
        self.assertFalse(research.valid_org_number("974760674"))
        self.assertIsNone(research.normalize_org_number("974760674"))
        self.assertIsNone(research.normalize_org_number("abc974760673"))
        self.assertIsNone(research.normalize_org_number("97476067"))

    def test_registry_identity_must_match_exactly(self) -> None:
        profile = research.from_data(
            ORG, {**SAMPLE, "organisasjonsnummer": "923609016"}, research.now()
        )
        self.assertEqual(profile["status"], "ambiguous")
        self.assertIsNone(profile["name"]["value"])
        self.assertEqual(profile["name"]["evidence"], [])

    def test_available_claims_have_official_evidence_and_source_dates(self) -> None:
        retrieved_at = research.now()
        profile = research.from_data(ORG, SAMPLE, retrieved_at)
        claim = profile["name"]
        self.assertEqual(claim["value"], "Example AS")
        self.assertEqual(claim["status"], "available")
        self.assertEqual(claim["evidence"][0]["source"], research.SOURCE)
        self.assertEqual(claim["evidence"][0]["url"], f"{research.API}/{ORG}")
        self.assertEqual(claim["evidence"][0]["retrieved_at"], retrieved_at)
        self.assertIsNone(claim["evidence"][0]["source_date"])
        self.assertEqual(
            profile["registration_date"]["evidence"][0]["source_date"],
            "2001-02-03",
        )

    def test_cli_emits_valid_official_jsonl_terminal_envelope(self) -> None:
        profile = research.from_data(ORG, SAMPLE, research.now())
        envelope = terminal_envelope(
            profile,
            run_id="test-run",
            started_at=research.now(),
            completed_at=research.now(),
        )
        validation = validate_envelopes([envelope], [ORG])
        self.assertTrue(validation["passed"], validation["errors"])
        self.assertEqual(envelope["run"]["terminal_status"], "completed")
        self.assertTrue(envelope["claims"])
        self.assertTrue(envelope["evidence"])
        self.assertTrue(
            all(isinstance(claim["confidence"], float) for claim in envelope["claims"])
        )
        self.assertEqual(
            envelope["evidence"][0]["source_url"],
            f"{research.API}/{ORG}",
        )
        self.assertIn("source_date", envelope["evidence"][0])
        self.assertIn("content_sha256", envelope["evidence"][0])

    def test_cli_reads_registry_batch_jsonl(self) -> None:
        input_path = Path(self.temp_dir.name) / "companies.jsonl"
        input_path.write_text(
            json.dumps({"organisation_number": ORG}) + "\n"
            + json.dumps({"organisation_number": "923609016"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(parse(input_path), [ORG, "923609016"])

    def test_invalid_number_does_not_make_api_request(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=SAMPLE)

        async def run() -> dict:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await research.research("974760674", client)

        result = asyncio.run(run())
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(requests, [])

    def test_temporary_api_failure_returns_failed_envelope_after_retries(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503, json={"error": "temporarily unavailable"})

        async def run() -> dict:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await research.research(ORG, client, refresh=True)

        with patch.object(research, "RETRY_BASE_SECONDS", 0):
            result = asyncio.run(run())
        self.assertEqual(result["status"], "failed")
        self.assertIn("after retries", result["errors"][0]["message"])
        self.assertEqual(len(requests), research.MAX_ATTEMPTS)

    def test_historical_updates_are_retained_and_identical_snapshots_deduplicate(self) -> None:
        records = [
            {**SAMPLE, "navn": "Example AS"},
            {**SAMPLE, "navn": "Example Group AS"},
            {**SAMPLE, "navn": "Example Group AS"},
        ]

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=records.pop(0))

        async def run() -> list[dict]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return [
                    await research.research(ORG, client, refresh=True)
                    for _ in range(3)
                ]

        first, second, third = asyncio.run(run())
        self.assertEqual(first["changes"], [])
        self.assertEqual(second["changes"][0]["field"], "name")
        self.assertEqual(second["changes"][0]["old"], "Example AS")
        self.assertEqual(second["changes"][0]["new"], "Example Group AS")
        self.assertEqual(third["changes"], [])
        connection = sqlite3.connect(self.db)
        try:
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM source_snapshots WHERE org=?", (ORG,)
            ).fetchone()[0]
            history = connection.execute(
                "SELECT COUNT(*) FROM history WHERE org=?", (ORG,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(snapshots, 2)
        self.assertEqual(history, 1)

    def test_gone_registry_record_invalidates_current_cache_but_keeps_history(self) -> None:
        responses = [
            httpx.Response(200, json=SAMPLE),
            httpx.Response(410),
        ]

        async def handler(_: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        async def run() -> dict:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await research.research(ORG, client, refresh=True)
                return await research.research(ORG, client, refresh=True)

        result = asyncio.run(run())
        self.assertEqual(result["status"], "not_available")
        self.assertIsNone(research.cached_profile(ORG))
        connection = sqlite3.connect(self.db)
        try:
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM source_snapshots WHERE org=?", (ORG,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(snapshots, 1)


if __name__ == "__main__":
    unittest.main()
