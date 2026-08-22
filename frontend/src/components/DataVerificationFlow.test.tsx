import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dataVerificationV1 } from "@/fixtures/dataVerificationV1";
import { dataVerificationApi, type DataVerificationView } from "@/lib/api";
import { DataVerificationFlow } from "./DataVerificationFlow";


const d6 = {
  domain_class: "education_learning",
  record_granularity: "entity",
  temporal_scope: "current_snapshot",
  update_cadence: "one_time",
  intended_use_tags: ["analysis_reporting"],
  known_limitation_tags: [],
};

const baseView: DataVerificationView = {
  dataset_id: "ds-1",
  supported: true,
  unavailable_reason: null,
  run_id: null,
  listing_id: "11111111-1111-4111-8111-111111111111",
  state: null,
  d6_description: null,
  preview_requested: false,
  quote: null,
  payment_status: null,
  report_ingest: null,
  findings: null,
  d8_preview: null,
  active_publication: null,
};

const quoteView: DataVerificationView = {
  ...baseView,
  run_id: "run-1",
  state: "QUOTED",
  d6_description: d6,
  preview_requested: true,
  quote: {
    quote_id: "quote-1",
    depth_class: "complete_standard_v1",
    traversal_scope: "all_reachable_supported_objects",
    row_count_policy: "exact_or_declared_estimate",
    low_occupancy_behavior: "suppressed_low_occupancy",
    minimum_aggregate_occupancy: 10,
    hard_maximum: { authorization_usd: "25.00", inference: { max_input_tokens: 8192, max_output_tokens: 1024, model_request_count: 1 } },
    partial_traversal_allowed: false,
  },
};

const capturedView: DataVerificationView = {
  ...quoteView,
  state: "CAPTURED",
  payment_status: {
    verification_id: "verification-1",
    state: "CAPTURED",
    authorization_usd: "25.00",
    captured_usd: "1.23",
    result_available: true,
    publication_allowed: true,
    reconciliation_required: false,
  },
  report_ingest: { verification_id: "22222222-2222-4222-8222-222222222222", accepted: true, terminal_error_code: null, narrative_state: "grounded" },
  findings: {
    completed_at_utc: "2026-08-22T12:00:00Z",
    coverage: { objects_discovered: 1, objects_scanned: 1, objects_skipped_by_reason: {}, skipped: [] },
    objects: [{ object_id: "opaque", row_count: 12, row_count_method: "exact", column_names: ["problem_id"], column_types: ["integer"] }],
  },
  d8_preview: [{ row_count: 12, row_count_method: "exact", column_names: ["problem_id"] }],
};

beforeEach(() => {
  vi.spyOn(dataVerificationApi, "get").mockResolvedValue(baseView);
  vi.spyOn(dataVerificationApi, "quote").mockResolvedValue(quoteView);
  vi.spyOn(dataVerificationApi, "start").mockResolvedValue({
    ...capturedView,
    state: "CAPTURE_RECONCILING",
    payment_status: { ...capturedView.payment_status!, state: "CAPTURE_RECONCILING", captured_usd: null, result_available: false, publication_allowed: false, reconciliation_required: true },
  });
  vi.spyOn(dataVerificationApi, "command").mockResolvedValue(capturedView);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DataVerificationFlow", () => {
  it("renders canonical D6 and separate consent copy, then distinguishes hold from final charge", async () => {
    render(<DataVerificationFlow datasetId="ds-1" sourceName="problems.csv" listingId={baseView.listing_id} />);

    expect(await screen.findByText("Data domain")).toBeInTheDocument();
    expect(screen.getByText(dataVerificationV1.copy.d6Note)).toBeInTheDocument();
    expect(screen.queryByText("education_learning")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    for (const definition of Object.values(dataVerificationV1.vocabulary)) {
      expect(screen.getByText(definition.help)).toBeInTheDocument();
      for (const [slug, label] of definition.options) {
        expect(screen.getByText(label)).toBeInTheDocument();
        expect(screen.queryByText(slug)).not.toBeInTheDocument();
      }
    }
    fireEvent.change(screen.getByRole("combobox", { name: /Data domain/ }), { target: { value: "education_learning" } });
    fireEvent.change(screen.getByRole("combobox", { name: /One record represents/ }), { target: { value: "entity" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Time coverage/ }), { target: { value: "current_snapshot" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Update frequency/ }), { target: { value: "one_time" } });
    fireEvent.click(screen.getByLabelText(/Include the schema-level preview/));
    fireEvent.click(screen.getByRole("button", { name: "Review scan terms" }));

    const publication = screen.getByLabelText(dataVerificationV1.copy.publicationAcknowledgement);
    const corpus = screen.getByLabelText(dataVerificationV1.copy.corpusAcknowledgement);
    for (const copy of ["rawBoundary", "cancelBoundary", "hiddenUntilCapture", "noRefund", "ourFault"] as const) {
      expect(screen.getByText(dataVerificationV1.copy[copy], { exact: false })).toBeInTheDocument();
    }
    expect(publication).not.toBeChecked();
    expect(corpus).not.toBeChecked();
    expect(dataVerificationV1.copy.publicationAcknowledgement).toContain("separate choice");
    expect(dataVerificationV1.copy.publicationAcknowledgement).not.toContain("I consent to publish");
    fireEvent.click(publication);
    fireEvent.click(corpus);
    fireEvent.click(screen.getByRole("button", { name: "Run free probe and request quote" }));

    expect(await screen.findByText(dataVerificationV1.copy.quoteFinalCharge)).toBeInTheDocument();
    expect(screen.getByText("Maximum card hold")).toBeInTheDocument();
    expect(dataVerificationApi.quote).toHaveBeenCalledWith("ds-1", expect.objectContaining({ publication_terms_ack: true, corpus_ack: true }));

    fireEvent.click(screen.getByRole("button", { name: "Accept maximum hold and start" }));
    expect(await screen.findByText("Confirming final charge")).toBeInTheDocument();
    expect(screen.getByText(/Capture truth is being reconciled/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Captured scan review")).not.toBeInTheDocument();
  });

  it("reveals only captured findings and presents publish and decline as equal explicit choices", async () => {
    vi.mocked(dataVerificationApi.get).mockResolvedValue(capturedView);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    render(<DataVerificationFlow datasetId="ds-1" sourceName="problems.csv" listingId={baseView.listing_id} />);

    expect(await screen.findByLabelText("Captured scan review")).toBeInTheDocument();
    expect(screen.getByText("Publish all findings")).toBeInTheDocument();
    expect(screen.getByText("Decline publication")).toBeInTheDocument();
    expect(screen.getByText("Final charged amount: $1.23")).toBeInTheDocument();
    expect(screen.queryByText(/quality score/i)).not.toBeInTheDocument();
    expect(screen.getByText(dataVerificationV1.copy.attestation.replace("[scan date and time]", "2026-08-22"))).toBeInTheDocument();
    expect(screen.getByText(dataVerificationV1.copy.disclaimer.replace("[scan date]", "2026-08-22"))).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Publish all findings" }));
    await waitFor(() => expect(dataVerificationApi.command).toHaveBeenCalledWith("ds-1", "publish"));
    expect(window.confirm).toHaveBeenCalledWith(dataVerificationV1.copy.publishConfirmation);
  });

  it("keeps the active badge during rerun and resets both acknowledgements", async () => {
    vi.mocked(dataVerificationApi.get).mockResolvedValue({
      ...capturedView,
      state: "PUBLISHED",
      payment_status: { ...capturedView.payment_status!, state: "PUBLISHED", publication_allowed: false },
      active_publication: { verification_id: "verification-1", scan_date: "2026-08-22T12:00:00Z", coverage: {}, narrative_state: "grounded" },
    });
    render(<DataVerificationFlow datasetId="ds-1" sourceName="problems.csv" listingId={baseView.listing_id} />);

    expect(await screen.findAllByText("Scan findings — 2026-08-22")).not.toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Start a paid rerun" }));
    expect(screen.getByText("Data domain")).toBeInTheDocument();
    expect(screen.getByText("Scan findings — 2026-08-22")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: /Data domain/ }), { target: { value: "education_learning" } });
    fireEvent.change(screen.getByRole("combobox", { name: /One record represents/ }), { target: { value: "entity" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Time coverage/ }), { target: { value: "current_snapshot" } });
    fireEvent.change(screen.getByRole("combobox", { name: /Update frequency/ }), { target: { value: "one_time" } });
    fireEvent.click(screen.getByRole("button", { name: "Review scan terms" }));
    expect(screen.getByLabelText(dataVerificationV1.copy.publicationAcknowledgement)).not.toBeChecked();
    expect(screen.getByLabelText(dataVerificationV1.copy.corpusAcknowledgement)).not.toBeChecked();
  });
});
