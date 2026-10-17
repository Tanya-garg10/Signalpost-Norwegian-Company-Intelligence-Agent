import { useState } from 'react';
import {
  Activity,
  AlertTriangle,
  Building2,
  Clock3,
  ExternalLink,
  RefreshCw,
  Search,
  ShieldCheck,
} from 'lucide-react';

const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';

type Evidence = {
  source: string;
  url: string;
  retrieved_at: string;
  source_date: string | null;
};

type Fact = {
  value: unknown;
  status: string;
  source_date: string | null;
  evidence: Evidence[];
};

type FactKey =
  | 'name'
  | 'organization_form'
  | 'registered'
  | 'registration_status'
  | 'registration_date'
  | 'foundation_date'
  | 'business_register_date'
  | 'vat_register_date'
  | 'employee_count_date'
  | 'business_address'
  | 'postal_address'
  | 'industry_code'
  | 'industry_description'
  | 'website'
  | 'employees'
  | 'description'
  | 'organization_number';

type Profile = {
  org_number: string;
  status: string;
  summary: string;
  source_url: string;
  retrieved_at: string;
  changes: { field: string; old: unknown; new: unknown }[];
  errors: { message: string }[];
  refresh: {
    last_checked_at: string;
    cache_hit: boolean;
    snapshot_digest: string | null;
  };
} & Record<FactKey, Fact>;

type HistoryEntry = {
  retrieved_at: string;
  sha256: string;
  changes: { field: string; old: unknown; new: unknown }[];
};

type BatchResult = {
  input_count: number;
  result_count: number;
  complete: boolean;
  distinct_input_count: number;
  results: Profile[];
};

const facts: [FactKey, string][] = [
  ['organization_number', 'Organisation number'],
  ['organization_form', 'Legal form'],
  ['registration_status', 'Registration status'],
  ['registered', 'Registered'],
  ['registration_date', 'Enhetsregisteret registration date'],
  ['foundation_date', 'Foundation date'],
  ['business_register_date', 'Business Register date'],
  ['vat_register_date', 'VAT Register date'],
  ['employee_count_date', 'Employee-count source date'],
  ['business_address', 'Business address'],
  ['postal_address', 'Postal address'],
  ['industry_code', 'Industry code'],
  ['industry_description', 'Industry'],
  ['website', 'Website'],
  ['employees', 'Employees'],
  ['description', 'Purpose / activity'],
];

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'Not available';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function timestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init);
  if (!response.ok) {
    let detail = `API returned HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail) detail = String(body.detail);
    } catch {
      // Keep the HTTP error if the server did not return a JSON error body.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export default function App() {
  const [number, setNumber] = useState('974760673');
  const [profile, setProfile] = useState<Profile | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [forceRefresh, setForceRefresh] = useState(false);
  const [error, setError] = useState('');
  const [batchNumbers, setBatchNumbers] = useState('');
  const [batchResult, setBatchResult] = useState<BatchResult | null>(null);

  async function lookup() {
    setBusy(true);
    setError('');
    setProfile(null);
    setHistory([]);
    try {
      const result = await jsonRequest<Profile>('/api/company', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ org_number: number, refresh: forceRefresh }),
      });
      setProfile(result);
      if (result.status === 'available') {
        const prior = await jsonRequest<HistoryEntry[]>(
          `/api/company/${encodeURIComponent(result.org_number)}/history`,
        );
        setHistory(prior);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Company lookup failed.');
    } finally {
      setBusy(false);
    }
  }

  async function runBatch() {
    const numbers = batchNumbers.split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean);
    if (!numbers.length) {
      setError('Paste at least one organisation number.');
      return;
    }
    if (numbers.length > 1000) {
      setError('A batch can contain at most 1,000 organisation numbers.');
      return;
    }
    setBusy(true);
    setError('');
    setBatchResult(null);
    try {
      const result = await jsonRequest<BatchResult>('/api/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ org_numbers: numbers }),
      });
      setBatchResult(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Batch research failed.');
    } finally {
      setBusy(false);
    }
  }

  function downloadBatch() {
    if (!batchResult) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(batchResult, null, 2)], { type: 'application/json' }),
    );
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'signalpost-results.json';
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const batchInputCount = batchNumbers.split(/[\s,;]+/).filter(Boolean).length;

  return (
    <main className="shell">
      <header>
        <div className="brand">
          <div className="logo"><Activity /></div>
          <div><b>signalpost<span>.ai</span></b><small>COMPANY INTELLIGENCE</small></div>
        </div>
        <div className="live"><i /> Official registry research</div>
      </header>

      <section className="hero">
        <div className="eyebrow"><ShieldCheck size={15} /> EVIDENCE-FIRST COMPANY RESEARCH</div>
        <h1>Know the company.<br /><span>Verify every fact.</span></h1>
        <p>
          Research Norwegian companies using the official Brønnøysund Register Centre.
          Every available claim links to its source and records retrieval and source dates.
        </p>
        <div className="search">
          <Search />
          <input
            aria-label="Norwegian organisation number"
            value={number}
            onChange={(event) => setNumber(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && lookup()}
            placeholder="9-digit organisation number"
          />
          <button onClick={lookup} disabled={busy}>{busy ? 'Working…' : 'Research company'}</button>
        </div>
        <div className="lookup-options">
          <small className="hint">Example: <a onClick={() => setNumber('974760673')}>974 760 673</a></small>
          <label><input type="checkbox" checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)} /> Bypass cache</label>
        </div>
      </section>

      {error && <div className="error"><AlertTriangle />{error}</div>}

      {profile && (
        <section className="panel">
          <div className="paneltop">
            <div>
              <div className="eyebrow">RESEARCH RESULT</div>
              <h2>{display(profile.name.value)}</h2>
              <small>Organisation number {profile.org_number}</small>
            </div>
            <span className={`status ${profile.status}`}>{profile.status.replace(/_/g, ' ')}</span>
          </div>
          <p className="summary">{profile.summary}</p>
          {profile.errors.map((item, index) => <p className="error" key={index}>{item.message}</p>)}
          <div className="meta">
            <span><Clock3 size={14} /> Last checked {timestamp(profile.refresh.last_checked_at)}</span>
            <span>{profile.refresh.cache_hit ? 'Cached official response' : `Retrieved ${timestamp(profile.retrieved_at)}`}</span>
            <a href={profile.source_url} target="_blank" rel="noreferrer">Official record <ExternalLink size={13} /></a>
          </div>
          <div className="facts">
            {facts.map(([key, label]) => {
              const claim = profile[key];
              return (
                <article key={key}>
                  <small>{label}</small>
                  <b>{display(claim.value)}</b>
                  <div className="proof">
                    {claim.evidence.length ? claim.evidence.map((item, index) => (
                      <div className="evidence-item" key={`${item.url}-${index}`}>
                        <a href={item.url} target="_blank" rel="noreferrer">
                          <ShieldCheck size={13} /> {item.source} <ExternalLink size={12} />
                        </a>
                        <span>Retrieved {timestamp(item.retrieved_at)}</span>
                        <span>Source date: {item.source_date ?? 'Not supplied by source'}</span>
                      </div>
                    )) : <span>No verified value in the official record</span>}
                  </div>
                </article>
              );
            })}
          </div>

          <div className="history-section">
            <h3><RefreshCw size={16} /> Snapshot and change history</h3>
            {!history.length && <p>No previous distinct source snapshots have been recorded.</p>}
            {history.map((entry) => (
              <article className="history-item" key={entry.sha256}>
                <div><b>{timestamp(entry.retrieved_at)}</b><small>Snapshot SHA-256: {entry.sha256}</small></div>
                {entry.changes.length
                  ? entry.changes.map((change, index) => (
                    <p key={`${change.field}-${index}`}>
                      {change.field}: {display(change.old)} → {display(change.new)}
                    </p>
                  ))
                  : <span>No material changes from the prior snapshot.</span>}
              </article>
            ))}
          </div>
        </section>
      )}

      <section className="panel">
        <div className="sectionhead">
          <Building2 />
          <div><h2>Batch research</h2><small>Discover real registry entries from a JSON list of numbers or use the CLI to discover 1,000.</small></div>
        </div>
        <textarea
          rows={4}
          value={batchNumbers}
          onChange={(event) => setBatchNumbers(event.target.value)}
          placeholder={'Paste organisation numbers separated by new lines or commas\n974760673\n923609016'}
        />
        <div className="batchactions">
          <small>{batchInputCount} inputs · Maximum 1,000 per request</small>
          <button className="secondary" onClick={runBatch} disabled={busy}>
            <RefreshCw size={15} /> Run batch
          </button>
        </div>
        {batchResult && (
          <div className="batchresult">
            <b>{batchResult.result_count} result envelopes for {batchResult.input_count} inputs</b>
            <p>
              {batchResult.results.filter((result) => result.status === 'available').length} verified profiles ·
              {' '}{batchResult.distinct_input_count} distinct valid input numbers
            </p>
            <div className="batchresult-actions">
              <button onClick={downloadBatch}>Download auditable JSON</button>
              {!batchResult.complete && <span className="warning">Some input results are missing.</span>}
            </div>
          </div>
        )}
      </section>

      <footer>
        <b><Activity size={15} /> signalpost.ai</b>
        <span>Public facts · Source-linked · Missing values are never invented</span>
        <span>Signalpost challenge prototype</span>
      </footer>
    </main>
  );
}
