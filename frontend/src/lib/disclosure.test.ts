import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY,
  buildApprovedMetadataDraft,
  buildDisclosureSnapshotPayload,
  classifyDisclosureSnapshotFailure,
  prepareDisclosureSample,
} from "./disclosure";
import {
  DisclosureSnapshotRequestError,
  marketplaceApi,
  type ApiDataset,
  type DatasetListingMetadata,
} from "./api";
import type { ListingEditorValue } from "@/components/ListingEditorForm";

const form: ListingEditorValue = {
  title: " Customer Spend ",
  description: " Buyer-facing spend data. ",
  category: "retail",
  tags: ["customers", "spend"],
  priceUsd: "25",
};

const metadata: DatasetListingMetadata = {
  title: "Customer Spend",
  description: "Buyer-facing spend data.",
  tags: ["customers"],
  column_summary: [
    { name: "segment", type: "string", null_percentage: 0, uniqueness_ratio: 0.5, sample_values: [] },
    { name: "spend", type: "float", null_percentage: 0, uniqueness_ratio: 0.9, sample_values: [] },
  ],
  row_count: 500,
  column_count: 2,
  file_format: "csv",
  size_bytes: 1024,
  freshness_score: 0.8,
  privacy_score: 9,
  data_categories: ["retail"],
  generated_at: "2026-07-08T00:00:00Z",
};

const dataset = {
  id: "ds-1",
  original_filename: "customers.csv",
  file_type: "csv",
  status: "preview_ready",
  created_at: "2026-07-08T00:00:00Z",
  updated_at: "2026-07-08T00:00:00Z",
  metadata: { row_count: 500, column_count: 2, size_bytes: 1024, columns: [] },
} as ApiDataset;

const expectedApprovedFields = {
  title: "Customer Spend",
  description: "Buyer-facing spend data.",
  category: "retail",
  tags: ["customers", "spend"],
  schema: {
    columns: [
      { name: "segment", type: "string", null_percentage: 0, uniqueness_ratio: 0.5 },
      { name: "spend", type: "float", null_percentage: 0, uniqueness_ratio: 0.9 },
    ],
  },
  data_format: "csv",
  source_row_count: 500,
  source_column_count: 2,
  compliance_summary: {
    privacy_score: 9,
    freshness_score: 0.8,
    data_categories: ["retail"],
  },
  source_delivery_public_metadata: {
    file_format: "csv",
    size_bytes: 1024,
  },
};

beforeEach(() => {
  vi.stubGlobal("localStorage", {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("disclosure payload builder", () => {
  it("maps approved metadata to approved_fields", () => {
    const approved = buildApprovedMetadataDraft(form, metadata, dataset);

    expect(approved).toMatchObject({
      title: "Customer Spend",
      description: "Buyer-facing spend data.",
      category: "retail",
      tags: ["customers", "spend"],
      data_format: "csv",
      source_row_count: 500,
      source_column_count: 2,
    });
    expect(approved.schema).toEqual([
      { name: "segment", type: "string", null_percentage: 0, uniqueness_ratio: 0.5 },
      { name: "spend", type: "float", null_percentage: 0, uniqueness_ratio: 0.9 },
    ]);
  });

  it("serializes the exact no-sample request with schema.columns", async () => {
    const payload = buildDisclosureSnapshotPayload({
      approvedFields: buildApprovedMetadataDraft(form, metadata, dataset),
      sampleDecision: "none",
      approvedSample: null,
      confirmed: true,
      sourcePublishOperationId: "op-1",
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 201,
      json: vi.fn().mockResolvedValue({ status: "complete", listing_id: "listing-1" }),
    } as unknown as Response);

    await marketplaceApi.createDisclosureSnapshot("listing-1", { dataset_id: dataset.id, ...payload });

    const request = fetchMock.mock.calls[0]?.[1];
    expect(JSON.parse(String(request?.body))).toEqual({
      dataset_id: "ds-1",
      approved_fields: expectedApprovedFields,
      sample_decision: "none",
      approved_sample: null,
      ai_training_notification_ack: true,
      ai_training_notification_text: AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY,
      license: "standard_marketplace",
      approval_source: "aim_channel",
      source_publish_operation_id: "op-1",
    });
  });

  it("approved_rows includes only displayed columns and rows with deterministic refs", () => {
    const prepared = prepareDisclosureSample([
      { a: 1, b: "x", hidden: "not-hidden-until-column-truncation" },
      { a: 2, b: "y", hidden: "not-hidden-until-column-truncation" },
    ]);

    expect(prepared.sample).toEqual({
      columns: ["a", "b", "hidden"],
      row_refs: ["preview:0", "preview:1"],
      rows: [
        { a: 1, b: "x", hidden: "not-hidden-until-column-truncation" },
        { a: 2, b: "y", hidden: "not-hidden-until-column-truncation" },
      ],
    });
  });

  it("serializes the exact approved-row request without changing the sample", async () => {
    const approvedSample = prepareDisclosureSample([
      { segment: "enterprise", spend: 125 },
      { segment: "consumer", spend: 75 },
    ]).sample;
    const payload = buildDisclosureSnapshotPayload({
      approvedFields: buildApprovedMetadataDraft(form, metadata, dataset),
      sampleDecision: "approved_rows",
      approvedSample,
      confirmed: true,
      sourcePublishOperationId: "op-2",
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 201,
      json: vi.fn().mockResolvedValue({ status: "complete", listing_id: "listing-1" }),
    } as unknown as Response);

    await marketplaceApi.createDisclosureSnapshot("listing-1", { dataset_id: dataset.id, ...payload });

    const request = fetchMock.mock.calls[0]?.[1];
    expect(JSON.parse(String(request?.body))).toEqual({
      dataset_id: "ds-1",
      approved_fields: expectedApprovedFields,
      sample_decision: "approved_rows",
      approved_sample: {
        columns: ["segment", "spend"],
        row_refs: ["preview:0", "preview:1"],
        rows: [
          { segment: "enterprise", spend: 125 },
          { segment: "consumer", spend: 75 },
        ],
      },
      ai_training_notification_ack: true,
      ai_training_notification_text: AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY,
      license: "standard_marketplace",
      approval_source: "aim_channel",
      source_publish_operation_id: "op-2",
    });
  });

  it("surfaces a deterministic 422 as a plain-English rejection", async () => {
    const payload = buildDisclosureSnapshotPayload({
      approvedFields: buildApprovedMetadataDraft(form, metadata, dataset),
      sampleDecision: "none",
      approvedSample: null,
      confirmed: true,
      sourcePublishOperationId: "op-1",
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 422,
      json: vi.fn().mockResolvedValue({
        detail: [{ msg: "Input should be a valid dictionary" }],
      }),
    } as unknown as Response);

    const request = marketplaceApi.createDisclosureSnapshot("listing-1", { dataset_id: dataset.id, ...payload });

    await expect(request).rejects.toEqual(expect.objectContaining<Partial<DisclosureSnapshotRequestError>>({
      name: "DisclosureSnapshotRequestError",
      status: 422,
      message: "ai.market rejected the disclosure snapshot because its data is invalid: Input should be a valid dictionary",
    }));
  });

  it("classifies a 422 validation response as a non-retryable rejection", () => {
    const message = "ai.market rejected the disclosure snapshot because its data is invalid: Input should be a valid dictionary";

    expect(classifyDisclosureSnapshotFailure(new DisclosureSnapshotRequestError(message, 422))).toEqual({
      status: "snapshot_rejected",
      title: "Disclosure snapshot rejected",
      description: message,
    });
  });

  it.each([502, 504])("classifies HTTP %s as a pending snapshot", (status) => {
    expect(classifyDisclosureSnapshotFailure(
      new DisclosureSnapshotRequestError(`Disclosure snapshot failed: ${status}`, status)
    )).toEqual({
      status: "snapshot_pending",
      title: "Listing published, disclosure snapshot pending",
      description: `Disclosure snapshot failed: ${status}`,
    });
  });

  it("classifies an indeterminate audit result as disclosure unknown", () => {
    const message = "Disclosure snapshot status unknown after local audit persistence failed";

    expect(classifyDisclosureSnapshotFailure(new Error(message))).toEqual({
      status: "disclosure_unknown",
      title: "Disclosure status unknown",
      description: message,
    });
  });

  it("truncates over 100 rows and over 25 columns before submit", () => {
    const wideRow = Object.fromEntries(Array.from({ length: 30 }, (_, index) => [`c${index}`, index]));
    const rows = Array.from({ length: 110 }, () => wideRow);
    const prepared = prepareDisclosureSample(rows);

    expect(prepared.sample?.rows).toHaveLength(100);
    expect(prepared.sample?.columns).toHaveLength(25);
    expect(prepared.truncatedRows).toBe(true);
    expect(prepared.truncatedColumns).toBe(true);
  });

  it("requires final confirmation before building acked payload", () => {
    expect(() =>
      buildDisclosureSnapshotPayload({
        approvedFields: buildApprovedMetadataDraft(form, metadata, dataset),
        sampleDecision: "none",
        approvedSample: null,
        confirmed: false,
        sourcePublishOperationId: "op-1",
      })
    ).toThrow("Final disclosure confirmation is required.");
  });
});
