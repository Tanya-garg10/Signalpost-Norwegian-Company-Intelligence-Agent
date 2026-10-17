# Signalpost Norwegian Company Intelligence

An evidence-first company lookup tool for Norwegian organisation numbers. The React dashboard and Python CLI use the official Brønnøysund Register Centre (Brønnøysundregistrene) Enhetsregisteret API. Facts are deterministic registry extractions; there is no LLM in the factual path.

## Hackathon submission contract

I verified the current [Signalpost challenge page](https://builderr.ai/challenges/signalpost), its linked [evaluation contract](https://builderr.ai/docs/signalpost-evaluation-harness.md), and the published [starter kit](https://builderr.ai/signalpost-starter-kit.tar.gz).

The official starter kit's `OUTPUT_CONTRACT.md` requires **one JSON object per input organisation number, written as JSONL**. Each line contains `organisation_number`, `run`, `claims`, `evidence`, `changes`, `errors`, and `operations`. The CLI emits that shape, validates claim availability states and evidence references, and writes a separate machine-readable run report. Available facts cite the official record URL, retrieval time, source date when supplied, source class, response SHA-256, and a claim span. Missing source dates stay `null`; missing values are never turned into zero.

The public universe has 411,160 eligible company numbers. The official contract recommends a 100-company local smoke test and encourages larger local tests, but says precomputed local profiles are **not** submitted or scored. Official evaluator batches are supplied by Builderr and can grow; do not represent a local dataset as the official batch or a complete census. The public archive hash pinned by the current contract is `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`; the CLI verifies it before selecting a deterministic local sample.

The published contract describes the envelope fields and allowed states but does not publish a separate evaluator-specific JSON Schema or a private batch adapter. This project implements the published JSONL contract; if Builderr supplies a newer harness or input adapter with the official batch, use that supplied interface rather than assuming this local CLI replaces it.

The published starter instructs entrants to email the repository URL, run command, APIs/models, and expected cost per 100-company run to `submit@builderr.ai`. It says version 1 can be followed by up to four revised commit hashes, with revisions closing 18 October 2026. Your stated 17 October deadline is the safer target; re-check the live page before sending.

## Requirements

- Windows PowerShell
- Python 3.10 or newer
- Node.js 20 or newer and npm
- Git for submission

## Install and run

From the project root, in the first PowerShell terminal:

```powershell
py -m venv backend\.venv
backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.lock
python -m uvicorn backend.app.main:app --reload
```

The API is at <http://127.0.0.1:8000>; interactive API documentation is at <http://127.0.0.1:8000/docs>.

In a second PowerShell terminal:

```powershell
npm ci --prefix frontend
npm --prefix frontend run dev
```

Open the Vite URL printed by npm (normally <http://localhost:5173>). The UI supports a single lookup, explicit cache bypass, a batch of up to 1,000 numbers, source links, source/retrieval dates, and snapshot history.

## Tests, build, and live sample

Run the automated backend tests:

```powershell
backend\.venv\Scripts\python.exe -m unittest backend.tests
```

Build the frontend and run TypeScript checks:

```powershell
npm --prefix frontend run build
```

One command runs the backend tests, performs a live lookup of Brønnøysundregistrene's own company (`974760673`), and exports the terminal JSONL envelope plus a run report:

```powershell
backend\.venv\Scripts\python.exe -m backend.app.cli --test-and-demo --out out\sample.jsonl --run-id sample-001
```

It requires an internet connection for the final lookup; the automated tests use mocked HTTP responses and do not depend on the registry being online.

## Run a JSON/JSONL batch

JSON array input:

```json
["974760673", "923609016"]
```

Or JSONL input with one object per line:

```json
{"organisation_number":"974760673"}
{"organisation_number":"923609016"}
```

Run from the project root:

```powershell
backend\.venv\Scripts\python.exe -m backend.app.cli companies.jsonl --refresh --out out\envelopes.jsonl --report out\run-report.json --run-id local-001
```

The command preserves input order and emits one terminal envelope for every input, including invalid numbers, missing records, blocked requests, and per-company failures. It verifies the 9-digit Norwegian modulus-11 check digit before requesting the registry, and refuses to publish a returned record if its organisation number does not exactly match the request.

## Collect a real 1,000-company local dataset

The official starter publishes a reproducible public universe for local scale tests. Download it and verify the archive hash in PowerShell:

```powershell
New-Item -ItemType Directory -Force data,out,reports | Out-Null
Invoke-WebRequest -Uri "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz" -OutFile data\signalpost-company-universe-2025.jsonl.gz
if ((Get-FileHash data\signalpost-company-universe-2025.jsonl.gz -Algorithm SHA256).Hash -ne "1C89710E5B01F8617E86D09FBDFF4A52F2F8DBBBA297E74F7164B5984F5A0384") { throw "Public universe checksum mismatch; stop and check the current challenge contract." }
```

First run the recommended 100-company smoke test:

```powershell
backend\.venv\Scripts\python.exe -m backend.app.cli --universe data\signalpost-company-universe-2025.jsonl.gz --discover 100 --seed 20260823 --refresh --out out\smoke-100.jsonl --report reports\smoke-100.report.json --run-id smoke-100
```

Then sample and research 1,000 distinct eligible numbers:

```powershell
backend\.venv\Scripts\python.exe -m backend.app.cli --universe data\signalpost-company-universe-2025.jsonl.gz --discover 1000 --seed 20260823 --refresh --out out\companies-1000.jsonl --report out\companies-1000.report.json --run-id local-1000
```

The selector validates the archive SHA-256, deduplicates and checksum-validates numbers, and samples with a fixed seed. Every profile is then fetched from the individual official Enhetsregisteret endpoint; the JSONL includes actual response hashes and evidence. The report records selected-number hash, available profile count, output hash, request count, runtime, cost, and envelope validation. If fewer than 1,000 profiles are available from live lookups, the command exits nonzero and records the actual count; it does not synthesize replacements. The official API calls are paced at up to five requests per second, retry temporary failures up to three attempts, and honor `Retry-After`. A successful profile is cached in SQLite for 15 minutes; `--refresh` bypasses that cache.

Keep `out\companies-1000.jsonl` and the downloaded universe local. The official challenge says these precomputed profiles do not count toward the score. The 100-company report is a submission artifact; after generating it successfully, include `reports\smoke-100.report.json` with your submission. Do not commit a report that claims a run you did not complete.

## Architecture

- **Frontend:** React 18 + Vite + TypeScript. Search, profile, evidence links, freshness/cache indicator, batch UI, export, and history.
- **Backend:** FastAPI. `POST /api/company`, `POST /api/batch`, `GET /api/company/{org_number}/history`, and `GET /api/health`.
- **Research layer:** strict organisation-number checksum, exact identity match, deterministic field extraction, retry/rate limiter, per-fact evidence and response SHA-256.
- **Storage:** SQLite at `backend\signalpost.sqlite3`. `profiles` holds the current successful profile; `source_snapshots` retains distinct official source versions; `history` stores material field changes. Identical response hashes do not create duplicate snapshots or false changes.
- **CLI:** accepts JSON arrays/objects and JSONL, samples the checksum-pinned official challenge universe or discovers real numbers from the official registry collection endpoint, and emits validated terminal JSONL plus a run report.

## Official sources, model, and cost

- Primary source: [Brønnøysund Register Centre Enhetsregisteret API](https://data.brreg.no/enhetsregisteret/api/enheter/{organisation_number}).
- Local eligible-company sampling archive and checksum: [Signalpost public universe](https://builderr.ai/signalpost-company-universe-2025.jsonl.gz), published with the [challenge contract](https://builderr.ai/docs/signalpost-evaluation-harness.md).
- Data extraction is deterministic. **Model: none.** No API key or third-party model provider is used.
- Third-party inference/search cost: **$0**. Public registry requests have no per-request fee stated in this project; obey the current source terms and any evaluator outbound-request budget. Hosting, if added, may incur a separate cost.
- The starter's Norway source guide identifies the registry bulk data as Norwegian Licence for Open Government Data (NLOD) 2.0; check current Brønnøysund source terms before redistributing collected data.

## Limitations and integrity

- This is a registry profile, not a complete company-intelligence crawler. It does not fetch annual accounts, roles, group structure, jobs, reviews, news, or company websites.
- An absent source field remains `not_available`; source dates are only populated when the registry actually supplies a relevant date. Registry registration is not a guarantee of current trading activity.
- A 404, 429, network error, or source outage remains a distinct terminal outcome. The API does not silently turn failures into empty successful profiles.
- The challenge's exact frozen evaluator batch, resource budget, and any later private adapter are Builderr-controlled. Local API discoveries and cached profiles are not claimed as coverage of that batch.
- No secret handling is needed for the public registry connector. Do not add private credentials to source control.

## What to commit and how to get the final hash

Commit the source, tests, pinned manifests/lockfile, project configuration, README, and the generated 100-company smoke report once it exists. Do **not** commit `.venv`, `node_modules`, SQLite databases, downloaded universe files, local JSONL datasets, `.env` files, or fabricated/unfinished reports. Add new reports only after confirming their counts, hash, and validation status.

This workspace is not currently a Git repository. From the project root, initialize it and create the submission commit after the 100-company smoke run succeeds:

```powershell
git init
git add .gitignore README.md backend frontend
git add reports\smoke-100.report.json
git status --short
git commit -m "Build Signalpost Norwegian company intelligence" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git rev-parse HEAD
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

Copy the full output of `git rev-parse HEAD` and send it with the repository URL, run command, model/API declaration, and expected cost to the submission address on the official starter instructions. If you create any additional code revision, commit it separately and submit that exact new hash; do not amend the frozen submission commit.
