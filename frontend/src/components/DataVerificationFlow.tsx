import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { dataVerificationV1 } from "@/fixtures/dataVerificationV1";
import {
  dataVerificationApi,
  type DataVerificationD6,
  type DataVerificationPaymentState,
  type DataVerificationView,
} from "@/lib/api";


type Stage = "d6" | "quote" | "lifecycle";
type SingleField = "domain_class" | "record_granularity" | "temporal_scope" | "update_cadence";
type TagField = "intended_use_tags" | "known_limitation_tags";

const emptyD6: DataVerificationD6 = {
  domain_class: "",
  record_granularity: "",
  temporal_scope: "",
  update_cadence: "",
  intended_use_tags: [],
  known_limitation_tags: [],
};

const friendlyState: Record<DataVerificationPaymentState, string> = {
  CREATED: "Preparing local probe",
  QUOTED: "Quote ready",
  AUTHORIZING: "Authorizing maximum card hold",
  AUTHORIZED: "Card hold authorized",
  SCANNING_LOCAL: "Scanning inside AIM Data",
  NARRATING_CLOUD: "Creating one bounded allAI interpretation",
  CAPTURE_PENDING: "Final charge pending",
  CAPTURE_RECONCILING: "Confirming final charge",
  CAPTURED: "Captured and ready for review",
  PUBLISHED: "Findings published",
  DECLINED: "Publication declined",
  WITHDRAWN: "Published findings withdrawn",
  SUPERSEDED: "Superseded by a later publication",
  AUTH_FAILED: "Authorization failed; no scan or charge",
  CANCELLED_VOIDED: "Cancelled before cloud inference; hold voided",
  FAILED_VOIDED: "Run failed; hold voided",
  CAPTURE_FAILED: "Capture failed; no charge or findings",
};

function optionLabel(field: keyof typeof dataVerificationV1.vocabulary, value: string): string {
  return dataVerificationV1.vocabulary[field].options.find(([slug]) => slug === value)?.[1] || "";
}

function displayDate(value: unknown): string {
  if (typeof value !== "string") return "date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "date unavailable" : date.toISOString().slice(0, 10);
}

function displayTimestamp(value: unknown): string {
  if (typeof value !== "string") return "timestamp unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "timestamp unavailable" : date.toISOString();
}

function copyWithDate(copy: string, value: unknown): string {
  return copy.replace("[scan date and time]", displayTimestamp(value)).replace("[scan date]", displayDate(value));
}

type ArtifactReport = Record<string, unknown> & {
  started_at_utc?: string;
  completed_at_utc?: string;
  coverage?: {
    objects_discovered?: number;
    objects_scanned?: number;
    objects_skipped_by_reason?: Record<string, number>;
    skipped?: Array<{ object_id?: string; reason?: string }>;
  };
  objects?: Array<Record<string, unknown>>;
};

function factValue(value: unknown): string {
  if (value === "suppressed_low_occupancy") return "suppressed";
  if (value === null) return "not applicable";
  if (Array.isArray(value)) return value.map(factValue).join(" | ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value ?? "not returned");
}

function VerificationArtifact({
  report,
  reportIngest,
  d8Preview,
  capturedUsd,
  label,
}: {
  report: ArtifactReport;
  reportIngest: DataVerificationView["report_ingest"];
  d8Preview: Array<Record<string, unknown>> | null;
  capturedUsd: string | null;
  label: string;
}) {
  const coverage = report.coverage || {};
  const skipsByReason = Object.entries(coverage.objects_skipped_by_reason || {});
  const keyedSkips = coverage.skipped || [];
  const methodFields = [
    "depth_class",
    "row_count_algorithm_version",
    "distinct_algorithm_version",
    "histogram_version",
    "numeric_bucket_version",
    "canonicalization_version",
    "fingerprint_hash",
  ] as const;
  const narrative = reportIngest?.narrative_state === "withheld_grounding_failed"
    ? "allAI interpretation withheld because grounding validation failed"
    : reportIngest?.narrative_state === "grounded"
      ? "Server returned narrative_state: grounded. The report-ingest response returned no narrative text and no listing-claim comparison."
      : `Server returned narrative_state: ${reportIngest?.narrative_state ?? "null"}. The report-ingest response returned no narrative text and no listing-claim comparison.`;

  return (
    <section aria-label={label} className="space-y-4 rounded border p-4">
      <h3 className="text-lg font-semibold">{label}</h3>
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        <div><dt className="text-muted-foreground">Scan started (exact UTC)</dt><dd>{displayTimestamp(report.started_at_utc)}</dd></div>
        <div><dt className="text-muted-foreground">Scan completed (exact UTC)</dt><dd>{displayTimestamp(report.completed_at_utc)}</dd></div>
        <div><dt className="text-muted-foreground">Charged amount</dt><dd>{capturedUsd ? `$${capturedUsd}` : "Server returned no captured amount."}</dd></div>
        {methodFields.map((field) => (
          <div key={field}><dt className="text-muted-foreground">{field}</dt><dd>{factValue(report[field])}</dd></div>
        ))}
      </dl>

      <div className="space-y-2 text-sm">
        <h4 className="font-medium">Coverage and skips</h4>
        <p>Objects discovered: {factValue(coverage.objects_discovered)}</p>
        <p>Objects scanned: {factValue(coverage.objects_scanned)}</p>
        <div>
          <p>Objects skipped by fixed reason:</p>
          {skipsByReason.length ? <ul className="list-disc pl-5">{skipsByReason.map(([reason, count]) => <li key={reason}>{reason}: {count}</li>)}</ul> : <p>None returned.</p>}
        </div>
        <div>
          <p>Keyed skipped objects:</p>
          {keyedSkips.length ? <ul className="list-disc pl-5">{keyedSkips.map((skip, index) => <li key={`${skip.object_id}-${index}`}>{skip.object_id}: {skip.reason}</li>)}</ul> : <p>None returned.</p>}
        </div>
      </div>

      {(report.objects || []).map((object, index) => (
        <div key={String(object.object_id || index)} className="space-y-2 rounded border p-4 text-sm">
          <h4 className="font-medium">Object {index + 1}</h4>
          <p>object_id: {factValue(object.object_id)}</p>
          <p>row_count: {factValue(object.row_count)}</p>
          <p>row_count_method: {factValue(object.row_count_method)}</p>
          <p>column_names: {factValue(object.column_names)}</p>
          <p>column_types: {factValue(object.column_types)}</p>
          <p>null_rate: {factValue(object.null_rate)}</p>
          <p>approx_distinct_count: {factValue(object.approx_distinct_count)}</p>
          <p>length_histograms: {factValue(object.length_histograms)}</p>
          <p>numeric_range_buckets: {factValue(object.numeric_range_buckets)}</p>
        </div>
      ))}

      {d8Preview && (
        <div className="rounded border p-4 text-sm">
          <h4 className="font-medium">Schema preview and row counts selected before authorization</h4>
          <pre className="mt-2 overflow-auto whitespace-pre-wrap">{JSON.stringify(d8Preview, null, 2)}</pre>
        </div>
      )}
      <div className="rounded border p-4 text-sm"><h4 className="font-medium">allAI interpretation</h4><p>{narrative}</p></div>
      <div className="rounded border p-4 text-sm"><h4 className="font-medium">Listing-claim comparison</h4><p>The report-ingest response returned no listing-claim comparison.</p></div>
      {reportIngest?.terminal_error_code && <p className="text-sm">Server returned terminal_error_code: {reportIngest.terminal_error_code}</p>}
      <p className="text-sm">{copyWithDate(dataVerificationV1.copy.attestation, report.completed_at_utc)}</p>
      <p className="text-sm">{copyWithDate(dataVerificationV1.copy.disclaimer, report.completed_at_utc)}</p>
    </section>
  );
}

export function DataVerificationFlow({ datasetId, sourceName, listingId }: { datasetId: string; sourceName: string; listingId?: string | null }) {
  const [view, setView] = useState<DataVerificationView | null>(null);
  const [stage, setStage] = useState<Stage>("d6");
  const [d6, setD6] = useState<DataVerificationD6>(emptyD6);
  const [previewRequested, setPreviewRequested] = useState(false);
  const [publicationAck, setPublicationAck] = useState(false);
  const [corpusAck, setCorpusAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  const adopt = useCallback((next: DataVerificationView) => {
    setView(next);
    if (next.d6_description) setD6(next.d6_description);
    setPreviewRequested(Boolean(next.preview_requested));
    if (next.quote && next.state === "QUOTED") setStage("quote");
    else if (next.run_id && next.state && next.state !== "CREATED") setStage("lifecycle");
  }, []);

  const fail = useCallback((value: unknown) => {
    setError(value instanceof Error ? value.message : "Data verification could not complete that request.");
    requestAnimationFrame(() => errorRef.current?.focus());
  }, []);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      adopt(await dataVerificationApi.get(datasetId));
    } catch (value) {
      fail(value);
    } finally {
      setBusy(false);
    }
  }, [adopt, datasetId, fail]);

  useEffect(() => { void load(); }, [listingId, load]);

  const changeSingle = (field: SingleField, value: string) => setD6((current) => ({ ...current, [field]: value }));
  const changeTag = (field: TagField, value: string, checked: boolean) => {
    setD6((current) => {
      const existing = current[field];
      if (checked && existing.length >= 5) return current;
      const next = checked ? [...existing, value] : existing.filter((item) => item !== value);
      return { ...current, [field]: [...new Set(next)].sort() };
    });
  };
  const d6Complete = Boolean(d6.domain_class && d6.record_granularity && d6.temporal_scope && d6.update_cadence);

  const requestQuote = async (event: FormEvent) => {
    event.preventDefault();
    if (!d6Complete) return fail(new Error("Choose one option in each required group."));
    setBusy(true);
    setError(null);
    setPublicationAck(false);
    setCorpusAck(false);
    try {
      adopt(await dataVerificationApi.quote(datasetId, {
        d6_description: d6,
        preview_requested: previewRequested,
      }));
    } catch (value) {
      fail(value);
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    if (!publicationAck || !corpusAck) return fail(new Error("Check both separate acknowledgements to continue."));
    setBusy(true);
    setError(null);
    try {
      adopt(await dataVerificationApi.start(datasetId));
    } catch (value) {
      fail(value);
    } finally {
      setBusy(false);
    }
  };

  const command = async (action: "cancel" | "publish" | "decline" | "withdraw") => {
    const confirmation = action === "publish"
      ? dataVerificationV1.copy.publishConfirmation
      : action === "decline"
        ? dataVerificationV1.copy.declineConfirmation
        : action === "withdraw"
          ? dataVerificationV1.copy.withdrawalConfirmation
          : "Confirm cancellation with the consequence shown beside this action?";
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    setError(null);
    try {
      adopt(await dataVerificationApi.command(datasetId, action));
    } catch (value) {
      fail(value);
    } finally {
      setBusy(false);
    }
  };

  const beginRerun = () => {
    setD6(emptyD6);
    setPreviewRequested(false);
    setPublicationAck(false);
    setCorpusAck(false);
    setError(null);
    setStage("d6");
  };

  if (!view) {
    return <Card><CardContent className="py-6">{busy ? "Loading data verification…" : "Data verification unavailable."}</CardContent></Card>;
  }

  if (!view.supported) {
    return (
      <Card aria-label="Data verification" className="border-muted">
        <CardHeader><CardTitle>Data verification</CardTitle></CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{view.unavailable_reason}</p>
          <Button className="mt-4" disabled>Start data verification</Button>
        </CardContent>
      </Card>
    );
  }

  const status = view.payment_status;
  const state = status?.state || view.state;
  const canCancel = Boolean(state && ["AUTHORIZING", "AUTHORIZED", "SCANNING_LOCAL", "NARRATING_CLOUD", "CAPTURE_PENDING", "CAPTURE_RECONCILING"].includes(state));
  const lateCancel = Boolean(state && ["NARRATING_CLOUD", "CAPTURE_PENDING", "CAPTURE_RECONCILING"].includes(state));
  const report = view.findings as ArtifactReport | null;

  return (
    <Card aria-label="Data verification" className="border-primary/30">
      <CardHeader>
        <CardTitle>Point-in-time scan findings</CardTitle>
        <p className="text-sm text-muted-foreground">Scan the registered source inside AIM Data, then choose whether to publish the complete result.</p>
      </CardHeader>
      <CardContent className="space-y-6">
        {error && <div ref={errorRef} tabIndex={-1} role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</div>}

        {view.active_publication?.publication_state === "PUBLISHED" && (
          <section aria-label="Active scan publication" className="space-y-3">
            <h3 className="font-semibold">Scan findings — {displayDate(view.active_publication.scan_date)}</h3>
            <p className="text-sm text-muted-foreground">The active publication stays fully visible and unchanged while a rerun is prepared or completed.</p>
            <VerificationArtifact
              report={view.active_publication.report as ArtifactReport}
              reportIngest={view.active_publication.report_ingest}
              d8Preview={view.active_publication.d8_preview}
              capturedUsd={view.active_publication.captured_usd}
              label="Complete active published artifact"
            />
          </section>
        )}
        {view.active_publication?.publication_state === "WITHDRAWN" && (
          <p aria-label="Withdrawal marker" className="rounded border p-4 text-sm">
            Scan findings withdrawn by seller on {displayDate(view.active_publication.withdrawn_at_utc)}
          </p>
        )}

        {stage === "d6" && (
          <form onSubmit={requestQuote} className="space-y-6">
            <div className="grid gap-4 md:grid-cols-2">
              {(Object.keys(dataVerificationV1.vocabulary).slice(0, 4) as SingleField[]).map((field) => {
                const definition = dataVerificationV1.vocabulary[field];
                return (
                  <label key={field} className="space-y-1 text-sm">
                    <span className="font-medium">{definition.title}</span>
                    <span className="block text-xs text-muted-foreground">{definition.help}</span>
                    <select value={d6[field]} onChange={(event) => changeSingle(field, event.target.value)} required className="w-full rounded border bg-background p-2">
                      <option value="">Choose one</option>
                      {definition.options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </label>
                );
              })}
            </div>
            {(["intended_use_tags", "known_limitation_tags"] as TagField[]).map((field) => {
              const definition = dataVerificationV1.vocabulary[field];
              return (
                <fieldset key={field} className="rounded border p-4">
                  <legend className="px-1 font-medium">{definition.title} ({d6[field].length}/5)</legend>
                  <p className="mb-3 text-xs text-muted-foreground">{definition.help}</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {definition.options.map(([value, label]) => (
                      <label key={value} className="flex items-start gap-2 text-sm">
                        <input type="checkbox" checked={d6[field].includes(value)} disabled={!d6[field].includes(value) && d6[field].length >= 5} onChange={(event) => changeTag(field, value, event.target.checked)} />
                        <span>{label}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              );
            })}
            <p className="rounded bg-muted p-3 text-sm">{dataVerificationV1.copy.d6Note}</p>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" checked={previewRequested} onChange={(event) => setPreviewRequested(event.target.checked)} />
              <span>Include the schema-level preview and row counts from this same scan. This does not run a second scan.</span>
            </label>
            <Button type="submit" disabled={busy || !d6Complete}>Run free probe and request quote</Button>
          </form>
        )}

        {stage === "quote" && view.quote && view.quote_probe && (
          <section className="space-y-4">
            <div><h3 className="font-semibold">Source and quoted scan</h3><p className="text-sm">{sourceName} · eolymp connector · {previewRequested ? "schema preview included" : "schema preview not included"}</p></div>
            <div className="grid gap-2 text-sm sm:grid-cols-2">
              {(Object.keys(d6) as Array<keyof DataVerificationD6>).map((field) => (
                <div key={field}><span className="font-medium">{dataVerificationV1.vocabulary[field].title}:</span> {Array.isArray(d6[field]) ? (d6[field] as string[]).map((value) => optionLabel(field, value)).join(", ") || "None selected" : optionLabel(field, d6[field] as string)}</div>
              ))}
            </div>
            <div><h3 className="font-semibold">Complete structural scan</h3><p className="text-sm text-muted-foreground">Traverses all reachable supported objects. Row counts are exact or visibly declared estimates; low-occupancy aggregates are suppressed uniformly. Partial traversal is not allowed.</p></div>
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div><dt className="text-muted-foreground">Free probe status</dt><dd>{view.quote_probe.source_reachable ? "Succeeded — source reachable" : "Refused — source unreachable"}</dd></div>
              <div><dt className="text-muted-foreground">Server-returned depth class</dt><dd>Complete standard scan ({view.quote.depth_class})</dd></div>
              <div><dt className="text-muted-foreground">Objects discovered</dt><dd>{view.quote_probe.objects_discovered}</dd></div>
              <div><dt className="text-muted-foreground">Source size class</dt><dd>{view.quote_probe.size_class}</dd></div>
              <div><dt className="text-muted-foreground">Fixed-reason skips from probe</dt><dd>{Object.entries(view.quote_probe.fixed_reason_skips).length ? Object.entries(view.quote_probe.fixed_reason_skips).map(([reason, count]) => `${reason}: ${count}`).join(", ") : "None reported by the successful probe."}</dd></div>
              <div><dt className="text-muted-foreground">Maximum card hold</dt><dd>${view.quote.hard_maximum.authorization_usd}</dd></div>
            </dl>
            <p className="font-medium">{dataVerificationV1.copy.quoteFinalCharge}</p>
            <p className="text-sm text-muted-foreground">{dataVerificationV1.copy.quoteUsage}</p>
            <div className="space-y-2 rounded border p-4 text-sm">
              <p>{dataVerificationV1.copy.rawBoundary}</p>
              <details><summary className="cursor-pointer underline">Open the approved metadata manifest</summary><ul className="mt-2 list-disc space-y-1 pl-5">{dataVerificationV1.manifestFields.map((field) => <li key={field}>{field}</li>)}</ul></details>
              <p>{dataVerificationV1.copy.cancelBoundary}</p>
              <p>{dataVerificationV1.copy.hiddenUntilCapture}</p>
              <p>{dataVerificationV1.copy.noRefund}</p>
              <p>{dataVerificationV1.copy.ourFault}</p>
            </div>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={publicationAck} onChange={(event) => setPublicationAck(event.target.checked)} /><span>{dataVerificationV1.copy.publicationAcknowledgement}</span></label>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={corpusAck} onChange={(event) => setCorpusAck(event.target.checked)} /><span>{dataVerificationV1.copy.corpusAcknowledgement}</span></label>
            <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => { setPublicationAck(false); setCorpusAck(false); setStage("d6"); }}>Revise before authorization</Button><Button onClick={start} disabled={busy || !publicationAck || !corpusAck}>Accept maximum hold and start</Button></div>
          </section>
        )}

        {stage === "lifecycle" && (
          <section className="space-y-5">
            <div aria-live="polite"><h3 className="font-semibold">{state ? friendlyState[state] : "Waiting for server status"}</h3>{status?.authorization_usd && <p className="text-sm">Maximum card hold: ${status.authorization_usd}</p>}{status?.captured_usd && <p className="text-sm">Final charged amount: ${status.captured_usd}</p>}</div>
            {status?.reconciliation_required && <p className="rounded border p-3 text-sm">Capture truth is being reconciled. {dataVerificationV1.copy.hiddenUntilCapture}</p>}
            {canCancel && <div className="rounded border p-4 text-sm"><p>{lateCancel ? "Cancel now completes the charge and ends declined without revealing or publishing findings." : "Cancel now voids the maximum card hold with no charge and publishes nothing."}</p><Button variant="outline" className="mt-3" onClick={() => command("cancel")} disabled={busy}>Cancel this run</Button></div>}

            {state === "CAPTURED" && status?.result_available && report && (
              <section className="space-y-4">
                <VerificationArtifact
                  report={report}
                  reportIngest={view.report_ingest}
                  d8Preview={view.d8_preview}
                  capturedUsd={status.captured_usd}
                  label="Captured scan review"
                />
                {state === "CAPTURED" && <div className="grid gap-3 sm:grid-cols-2"><Button onClick={() => command("publish")} disabled={busy || !status.publication_allowed}>{dataVerificationV1.copy.publishAction}</Button><Button variant="outline" onClick={() => command("decline")} disabled={busy}>{dataVerificationV1.copy.declineAction}</Button></div>}
              </section>
            )}

            {state === "PUBLISHED" && <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => command("withdraw")} disabled={busy}>Withdraw active findings</Button><Button onClick={beginRerun} disabled={busy}>Start a paid rerun</Button></div>}
            {["DECLINED", "WITHDRAWN", "SUPERSEDED", "AUTH_FAILED", "CANCELLED_VOIDED", "FAILED_VOIDED", "CAPTURE_FAILED"].includes(state || "") && <Button onClick={beginRerun} disabled={busy}>Start a new paid scan</Button>}
            <Button variant="ghost" onClick={load} disabled={busy}>Refresh server status</Button>
          </section>
        )}
      </CardContent>
    </Card>
  );
}
