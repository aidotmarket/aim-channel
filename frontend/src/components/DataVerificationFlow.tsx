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


type Stage = "d6" | "consent" | "quote" | "lifecycle";
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
  return dataVerificationV1.vocabulary[field].options.find(([slug]) => slug === value)?.[1] || value;
}

function displayDate(value: unknown): string {
  if (typeof value !== "string") return "date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "date unavailable" : date.toISOString().slice(0, 10);
}

function copyWithDate(copy: string, value: unknown): string {
  return copy.replace("[scan date and time]", displayDate(value)).replace("[scan date]", displayDate(value));
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

  const reviewD6 = (event: FormEvent) => {
    event.preventDefault();
    if (!d6Complete) return fail(new Error("Choose one option in each required group."));
    setError(null);
    setPublicationAck(false);
    setCorpusAck(false);
    setStage("consent");
  };

  const requestQuote = async () => {
    if (!publicationAck || !corpusAck) return fail(new Error("Check both separate acknowledgements to continue."));
    setBusy(true);
    setError(null);
    try {
      adopt(await dataVerificationApi.quote(datasetId, {
        d6_description: d6,
        preview_requested: previewRequested,
        publication_terms_ack: true,
        corpus_ack: true,
      }));
    } catch (value) {
      fail(value);
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
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
  const report = view.findings as { completed_at_utc?: string; coverage?: Record<string, unknown>; objects?: Array<Record<string, unknown>> } | null;

  return (
    <Card aria-label="Data verification" className="border-primary/30">
      <CardHeader>
        <CardTitle>Point-in-time scan findings</CardTitle>
        <p className="text-sm text-muted-foreground">Scan the registered source inside AIM Data, then choose whether to publish the complete result.</p>
      </CardHeader>
      <CardContent className="space-y-6">
        {error && <div ref={errorRef} tabIndex={-1} role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</div>}

        {view.active_publication && (
          <section aria-label="Active scan publication" className="rounded border p-4">
            <h3 className="font-semibold">Scan findings — {displayDate(view.active_publication.scan_date)}</h3>
            <p className="mt-1 text-sm text-muted-foreground">The active publication stays visible while a rerun is prepared or completed.</p>
          </section>
        )}

        {stage === "d6" && (
          <form onSubmit={reviewD6} className="space-y-6">
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
            <Button type="submit" disabled={busy || !d6Complete}>Review scan terms</Button>
          </form>
        )}

        {stage === "consent" && (
          <section className="space-y-5">
            <div><h3 className="font-semibold">Source and scan</h3><p className="text-sm">{sourceName} · eolymp connector · complete supported depth · {previewRequested ? "schema preview included" : "schema preview not included"}</p></div>
            <div className="grid gap-2 text-sm sm:grid-cols-2">
              {(Object.keys(d6) as Array<keyof DataVerificationD6>).map((field) => (
                <div key={field}><span className="font-medium">{dataVerificationV1.vocabulary[field].title}:</span> {Array.isArray(d6[field]) ? (d6[field] as string[]).map((value) => optionLabel(field, value)).join(", ") || "None selected" : optionLabel(field, d6[field] as string)}</div>
              ))}
            </div>
            <div className="space-y-2 rounded border p-4 text-sm">
              <p>{dataVerificationV1.copy.rawBoundary} <a href="/docs/data-verification-manifest" className="underline">Open the approved metadata manifest</a>.</p>
              <p>{dataVerificationV1.copy.cancelBoundary}</p>
              <p>{dataVerificationV1.copy.hiddenUntilCapture}</p>
              <p>{dataVerificationV1.copy.noRefund}</p>
              <p>{dataVerificationV1.copy.ourFault}</p>
            </div>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={publicationAck} onChange={(event) => setPublicationAck(event.target.checked)} /><span>{dataVerificationV1.copy.publicationAcknowledgement}</span></label>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={corpusAck} onChange={(event) => setCorpusAck(event.target.checked)} /><span>{dataVerificationV1.copy.corpusAcknowledgement}</span></label>
            <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => setStage("d6")}>Back and revise</Button><Button onClick={requestQuote} disabled={busy || !publicationAck || !corpusAck}>Run free probe and request quote</Button></div>
          </section>
        )}

        {stage === "quote" && view.quote && (
          <section className="space-y-4">
            <div><h3 className="font-semibold">Complete structural scan</h3><p className="text-sm text-muted-foreground">Traverses all reachable supported objects. Row counts are exact or visibly declared estimates; low-occupancy aggregates are suppressed uniformly. Partial traversal is not allowed.</p></div>
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div><dt className="text-muted-foreground">Coverage</dt><dd>All reachable supported objects; known fixed-reason skips: none</dd></div>
              <div><dt className="text-muted-foreground">Maximum card hold</dt><dd>${view.quote.hard_maximum.authorization_usd}</dd></div>
            </dl>
            <p className="font-medium">{dataVerificationV1.copy.quoteFinalCharge}</p>
            <p className="text-sm text-muted-foreground">{dataVerificationV1.copy.quoteUsage}</p>
            <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => setStage("d6")}>Revise before authorization</Button><Button onClick={start} disabled={busy}>Accept maximum hold and start</Button></div>
          </section>
        )}

        {stage === "lifecycle" && (
          <section className="space-y-5">
            <div aria-live="polite"><h3 className="font-semibold">{state ? friendlyState[state] : "Waiting for server status"}</h3>{status?.authorization_usd && <p className="text-sm">Maximum card hold: ${status.authorization_usd}</p>}{status?.captured_usd && <p className="text-sm">Final charged amount: ${status.captured_usd}</p>}</div>
            {status?.reconciliation_required && <p className="rounded border p-3 text-sm">Capture truth is being reconciled. {dataVerificationV1.copy.hiddenUntilCapture}</p>}
            {canCancel && <div className="rounded border p-4 text-sm"><p>{lateCancel ? "Cancel now completes the charge and ends declined without revealing or publishing findings." : "Cancel now voids the maximum card hold with no charge and publishes nothing."}</p><Button variant="outline" className="mt-3" onClick={() => command("cancel")} disabled={busy}>Cancel this run</Button></div>}

            {status?.result_available && report && (
              <section aria-label="Captured scan review" className="space-y-4">
                <h3 className="text-lg font-semibold">Captured scan review</h3>
                <div className="rounded border p-4 text-sm"><h4 className="font-medium">Coverage and skips</h4><pre className="mt-2 overflow-auto whitespace-pre-wrap">{JSON.stringify(report.coverage, null, 2)}</pre></div>
                {(report.objects || []).map((object, index) => <div key={String(object.object_id || index)} className="rounded border p-4 text-sm"><p>Object {index + 1}: {String(object.row_count)} rows ({String(object.row_count_method)})</p><p>Columns: {(object.column_names as string[] || []).join(", ")}</p><p>Types: {(object.column_types as string[] || []).join(", ")}</p></div>)}
                {view.d8_preview && <div className="rounded border p-4 text-sm"><h4 className="font-medium">Schema preview and row counts</h4><pre className="mt-2 overflow-auto whitespace-pre-wrap">{JSON.stringify(view.d8_preview, null, 2)}</pre></div>}
                <div className="rounded border p-4 text-sm"><h4 className="font-medium">allAI interpretation</h4><p>{view.report_ingest?.narrative_state === "grounded" ? "Grounded interpretation completed." : "Model-authored fields were withheld; the result is fingerprint-only."}</p></div>
                <p className="text-sm">{copyWithDate(dataVerificationV1.copy.attestation, report.completed_at_utc)}</p>
                <p className="text-sm">{copyWithDate(dataVerificationV1.copy.disclaimer, report.completed_at_utc)}</p>
                {state === "CAPTURED" && <div className="grid gap-3 sm:grid-cols-2"><Button onClick={() => command("publish")} disabled={busy || !status.publication_allowed}>{dataVerificationV1.copy.publishAction}</Button><Button variant="outline" onClick={() => command("decline")} disabled={busy}>{dataVerificationV1.copy.declineAction}</Button></div>}
              </section>
            )}

            {state === "PUBLISHED" && report && <section className="space-y-3 rounded border p-4"><h3 className="font-semibold">Scan findings — {displayDate(report.completed_at_utc)}</h3><p className="text-sm">Coverage: {JSON.stringify(report.coverage)}</p><p className="text-sm">{copyWithDate(dataVerificationV1.copy.disclaimer, report.completed_at_utc)}</p><div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => command("withdraw")} disabled={busy}>Withdraw active findings</Button><Button onClick={beginRerun} disabled={busy}>Start a paid rerun</Button></div></section>}
            {["DECLINED", "WITHDRAWN", "SUPERSEDED", "AUTH_FAILED", "CANCELLED_VOIDED", "FAILED_VOIDED", "CAPTURE_FAILED"].includes(state || "") && <Button onClick={beginRerun} disabled={busy}>Start a new paid scan</Button>}
            <Button variant="ghost" onClick={load} disabled={busy}>Refresh server status</Button>
          </section>
        )}
      </CardContent>
    </Card>
  );
}
