import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { datasetsApi, piiApi, type ApiDataset, type DatasetListingMetadata, type PIIScanResponse } from "@/lib/api";
import { ListingPreparation } from "./DatasetDetail";

vi.mock("@/contexts/CoPilotContext", () => ({
  useCoPilot: () => ({
    allieAvailable: false,
    listingDraftUpdates: {},
    sendMessage: vi.fn(),
    setEmbeddedSurfaceActive: vi.fn(),
  }),
}));

vi.mock("@/components/DataVerificationFlow", () => ({
  DataVerificationFlow: ({ datasetId }: { datasetId: string }) => <div data-testid="data-verification-flow">Verification for {datasetId}</div>,
}));

const listingMetadata: DatasetListingMetadata = {
  title: "Customer Spend",
  description: "Buyer-facing customer spend data.",
  tags: ["customers", "spend"],
  column_summary: [
    { name: "email", type: "string", null_percentage: 0, uniqueness_ratio: 1, sample_values: [] },
    { name: "spend", type: "float", null_percentage: 0, uniqueness_ratio: 0.8, sample_values: [] },
  ],
  row_count: 500,
  column_count: 2,
  file_format: "csv",
  size_bytes: 1024,
  freshness_score: 0.9,
  privacy_score: 7,
  data_categories: ["commerce"],
  generated_at: "2026-07-14T00:00:00Z",
};

const cleanScan: PIIScanResponse = {
  dataset_id: "ds-1",
  scan_status: "completed",
  overall_risk: "none",
  columns_scanned: 2,
  columns_with_pii: 0,
  column_results: [],
};

const flaggedScan: PIIScanResponse = {
  ...cleanScan,
  overall_risk: "high",
  columns_with_pii: 1,
  column_results: [
    { column: "email", pii_types: ["EMAIL_ADDRESS"], risk_level: "high" },
  ],
};

function dataset(metadata: DatasetListingMetadata | null = null): ApiDataset {
  return {
    id: "ds-1",
    original_filename: "customer-spend.csv",
    file_type: "csv",
    status: "preview_ready",
    listing_id: null,
    created_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:00Z",
    metadata: {
      row_count: 500,
      column_count: 2,
      size_bytes: 1024,
      columns: [
        { name: "email", type: "string" },
        { name: "spend", type: "float" },
      ],
      ...(metadata ? { listing_metadata: metadata } : {}),
    },
  };
}

function renderPreparation(apiDataset: ApiDataset) {
  return render(
    <MemoryRouter>
      <ListingPreparation
        dataset={apiDataset}
        draftListingId={apiDataset.listing_id ?? null}
        backPath="/datasets"
        onDelete={vi.fn()}
        isDeleting={false}
      />
    </MemoryRouter>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("seller listing preparation", () => {
  it("enables metadata acceptance without a draft listing id", async () => {
    vi.spyOn(piiApi, "getConfig").mockResolvedValue({
      dataset_id: "ds-1",
      column_actions: {},
      privacy_attested: false,
      updated_at: null,
    });
    vi.spyOn(piiApi, "getScan").mockResolvedValue(cleanScan);
    vi.spyOn(datasetsApi, "getListingMetadata").mockResolvedValue(listingMetadata);

    renderPreparation(dataset());

    expect(screen.getByTestId("data-verification-flow")).toHaveTextContent("ds-1");

    const continueButton = await screen.findByRole("button", { name: "Continue to metadata" });
    await waitFor(() => expect(continueButton).toBeEnabled());
    fireEvent.click(continueButton);

    const acceptButton = await screen.findByRole("button", { name: "Accept all & continue" });
    expect(screen.getByLabelText("Title")).toHaveValue("Customer Spend");
    expect(screen.getByLabelText("Description")).toHaveValue("Buyer-facing customer spend data.");
    expect(acceptButton).toBeEnabled();
    expect(screen.getByText("Conversational field review with allAI is not available yet. Edit the fields directly and continue.")).toBeInTheDocument();
  });

  it("rehydrates persisted metadata and privacy decisions at step 2 without regenerating metadata", async () => {
    vi.spyOn(piiApi, "getConfig").mockResolvedValue({
      dataset_id: "ds-1",
      column_actions: { email: "redact" },
      privacy_attested: false,
      updated_at: "2026-07-14T01:00:00Z",
    });
    vi.spyOn(piiApi, "getScan").mockResolvedValue(flaggedScan);
    const generateMetadata = vi.spyOn(datasetsApi, "getListingMetadata");

    renderPreparation(dataset(listingMetadata));

    expect(await screen.findByText("Step 2: Metadata Review")).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Customer Spend");
    expect(screen.getByLabelText("Description")).toHaveValue("Buyer-facing customer spend data.");
    expect(screen.getByText("customers")).toBeInTheDocument();
    expect(screen.queryByText("Step 1: Privacy Review")).not.toBeInTheDocument();
    expect(generateMetadata).not.toHaveBeenCalled();
  });
});
