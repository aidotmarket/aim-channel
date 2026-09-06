import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { datasetsApi, marketplaceApi, piiApi, type ApiDataset, type DatasetListingMetadata, type PIIScanResponse } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY } from "@/lib/disclosure";
import { DisclosureSnapshotFailurePanel, ListingPreparation } from "./DatasetDetail";

vi.mock("@/hooks/use-toast", () => ({ toast: vi.fn() }));

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

function renderPreparation(apiDataset: ApiDataset, onDatasetRefresh = vi.fn()) {
  return render(
    <MemoryRouter>
      <ListingPreparation
        dataset={apiDataset}
        onDatasetRefresh={onDatasetRefresh}
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
  vi.clearAllMocks();
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

describe("disclosure snapshot failure panel", () => {
  it("renders the detailed rejection beside review without offering Retry", () => {
    const description = "ai.market rejected the disclosure snapshot because its data is invalid: Input should be a valid dictionary";

    render(
      <DisclosureSnapshotFailurePanel
        failure={{ status: "snapshot_rejected", title: "Disclosure snapshot rejected", description }}
        publishing={false}
        canRetry={true}
        onRetry={vi.fn()}
        onReview={vi.fn()}
      />
    );

    expect(screen.getByText(description)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review disclosure decision" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry disclosure snapshot" })).not.toBeInTheDocument();
  });

  it.each(["snapshot_pending", "disclosure_unknown"] as const)("offers Retry for %s", (status) => {
    render(
      <DisclosureSnapshotFailurePanel
        failure={{ status, title: "Disclosure incomplete", description: "Try again safely." }}
        publishing={false}
        canRetry={true}
        onRetry={vi.fn()}
        onReview={vi.fn()}
      />
    );

    expect(screen.getByRole("button", { name: "Retry disclosure snapshot" })).toBeEnabled();
  });
});


describe("publish completion", () => {
  async function preparePublish(onDatasetRefresh = vi.fn()) {
    vi.spyOn(piiApi, "getConfig").mockResolvedValue({
      dataset_id: "ds-1", column_actions: {}, privacy_attested: false, updated_at: null,
    });
    vi.spyOn(piiApi, "getScan").mockResolvedValue(cleanScan);
    vi.spyOn(datasetsApi, "getDisclosureSample").mockResolvedValue({
      dataset_id: "ds-1", sample: [], count: 0,
    });
    const publish = vi.spyOn(marketplaceApi, "publish").mockResolvedValue({
      status: "published", listing_id: "listing-1", marketplace_url: "https://ai.market/listing/listing-1",
    });
    renderPreparation(dataset(listingMetadata), onDatasetRefresh);
    fireEvent.click(await screen.findByRole("button", { name: "Accept all & continue" }));
    fireEvent.click(screen.getByRole("checkbox", { name: AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY }));
    fireEvent.click(screen.getByRole("button", { name: "Publish to ai.market" }));
    return publish;
  }

  function expectSuccessToast() {
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Dataset published to ai.market" }));
    const confirmation = vi.mocked(toast).mock.calls.find(([message]) => message.title === "Dataset published to ai.market")![0];
    render(<>{confirmation.description}</>);
    expect(screen.getByRole("link", { name: "View listing on ai.market" })).toHaveAttribute("href", "https://ai.market/listing/listing-1");
  }

  it("confirms completion, disables Publish and refetches the dataset for the published view", async () => {
    vi.spyOn(marketplaceApi, "createDisclosureSnapshot").mockResolvedValue({
      status: "complete", listing_id: "listing-1", disclosure_version: "v1",
    });
    const refreshed = { ...dataset(listingMetadata), listing_id: "listing-1" };
    const refetch = vi.spyOn(datasetsApi, "get").mockResolvedValue(refreshed);
    const onDatasetRefresh = vi.fn();
    const publish = await preparePublish(onDatasetRefresh);

    await waitFor(() => expect(onDatasetRefresh).toHaveBeenCalledWith(refreshed));
    expectSuccessToast();
    expect(refetch).toHaveBeenCalledWith("ds-1");
    expect(screen.getByText("Complete")).toBeInTheDocument();
    const published = screen.getByRole("button", { name: "Published" });
    expect(published).toBeDisabled();
    fireEvent.click(published);
    expect(publish).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Retry disclosure snapshot" })).not.toBeInTheDocument();
  });

  it("keeps Publish disabled after snapshot failure and retries only the snapshot", async () => {
    const snapshot = vi.spyOn(marketplaceApi, "createDisclosureSnapshot")
      .mockRejectedValueOnce(new Error("Service unavailable"))
      .mockResolvedValueOnce({ status: "complete", listing_id: "listing-1", disclosure_version: "v1" });
    const refetch = vi.spyOn(datasetsApi, "get").mockResolvedValue({ ...dataset(), listing_id: "listing-1" });
    const publish = await preparePublish();

    const retry = await screen.findByRole("button", { name: "Retry disclosure snapshot" });
    expect(retry).toBeEnabled();
    expect(screen.getAllByText("Listing published, disclosure snapshot pending").length).toBeGreaterThan(0);
    const published = screen.getByRole("button", { name: "Published" });
    expect(published).toBeDisabled();
    fireEvent.click(published);
    expect(publish).toHaveBeenCalledTimes(1);
    expect(refetch).not.toHaveBeenCalled();
    expect(screen.queryByText("Complete")).not.toBeInTheDocument();

    fireEvent.click(retry);
    await waitFor(() => expect(refetch).toHaveBeenCalledWith("ds-1"));
    expectSuccessToast();
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(snapshot.mock.calls[1]).toEqual(snapshot.mock.calls[0]);
    expect(publish).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Published" })).toBeDisabled();
  });

  it("preserves successful publication if the dataset refresh fails", async () => {
    vi.spyOn(marketplaceApi, "createDisclosureSnapshot").mockResolvedValue({
      status: "complete", listing_id: "listing-1", disclosure_version: "v1",
    });
    vi.spyOn(datasetsApi, "get").mockRejectedValue(new Error("Network unavailable"));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    await preparePublish();

    await waitFor(() => expect(console.warn).toHaveBeenCalled());
    expectSuccessToast();
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Published" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Retry disclosure snapshot" })).not.toBeInTheDocument();
  });
});
