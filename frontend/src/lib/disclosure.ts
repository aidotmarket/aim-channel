import type {
  ApiDataset,
  DatasetListingMetadata,
  DisclosureApprovedFields,
} from "@/lib/api";
import type { ListingEditorValue } from "@/components/ListingEditorForm";

export const AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY =
  "When I publish, my approved title, description, tags, category, schema, and sample-row choice become public on ai.market. They may be shared with search engines, AI assistants, HuggingFace, and other AI discovery systems. If I approve sample rows, exactly the rows shown here will be public. I understand this public listing may be used by AI-training crawlers.";

export const AIM_CHANNEL_DISCLOSURE_LICENSE = "standard_marketplace";
export const AIM_CHANNEL_DISCLOSURE_APPROVAL_SOURCE = "aim_channel";

export type DisclosureSampleDecision = "none" | "approved_rows";

export type ApprovedMetadataDraft = Omit<DisclosureApprovedFields, "schema"> & {
  schema: DisclosureApprovedFields["schema"]["columns"];
};

export interface ApprovedSample {
  columns: string[];
  row_refs: string[];
  rows: Record<string, unknown>[];
}

export interface DisclosureSnapshotPayload {
  approved_fields: DisclosureApprovedFields;
  sample_decision: DisclosureSampleDecision;
  approved_sample: ApprovedSample | null;
  ai_training_notification_ack: boolean;
  ai_training_notification_text: string;
  license: string;
  approval_source: typeof AIM_CHANNEL_DISCLOSURE_APPROVAL_SOURCE;
  source_publish_operation_id: string;
}

export interface PreparedDisclosureSample {
  sample: ApprovedSample | null;
  truncatedRows: boolean;
  truncatedColumns: boolean;
  truncatedForBytes: boolean;
  sizeBytes: number;
}

export type DisclosureSnapshotFailureStatus =
  | "snapshot_pending"
  | "snapshot_rejected"
  | "disclosure_unknown";

export interface DisclosureSnapshotFailure {
  status: DisclosureSnapshotFailureStatus;
  title: string;
  description: string;
}

export function classifyDisclosureSnapshotFailure(error: unknown): DisclosureSnapshotFailure {
  const message = error instanceof Error ? error.message : "";
  const status = typeof error === "object" && error !== null && "status" in error
    ? (error as { status?: unknown }).status
    : undefined;

  if (status === 422) {
    return {
      status: "snapshot_rejected",
      title: "Disclosure snapshot rejected",
      description: message || "ai.market rejected the submitted disclosure data. Review the disclosure decision before trying again.",
    };
  }
  if (message.toLowerCase().includes("status unknown")) {
    return {
      status: "disclosure_unknown",
      title: "Disclosure status unknown",
      description: message,
    };
  }
  return {
    status: "snapshot_pending",
    title: "Listing published, disclosure snapshot pending",
    description: message || "The listing exists on ai.market, but the public discovery package is not complete.",
  };
}

const MAX_SAMPLE_ROWS = 100;
const MAX_SAMPLE_COLUMNS = 25;
const MAX_SAMPLE_BYTES = 250 * 1024;

export function buildApprovedMetadataDraft(
  form: ListingEditorValue,
  metadata: DatasetListingMetadata | null,
  dataset: ApiDataset
): ApprovedMetadataDraft {
  const schema = metadata?.column_summary?.length
    ? metadata.column_summary.map((column) => ({
        name: column.name,
        type: column.type,
        null_percentage: column.null_percentage,
        uniqueness_ratio: column.uniqueness_ratio,
      }))
    : (dataset.metadata?.columns || []).map((column) => ({
        name: column.name,
        type: column.type,
      }));

  return {
    title: form.title.trim(),
    description: form.description.trim(),
    category: form.category,
    tags: [...form.tags],
    schema,
    data_format: metadata?.file_format || dataset.file_type || null,
    source_row_count: metadata?.row_count ?? dataset.metadata?.row_count ?? null,
    source_column_count: metadata?.column_count ?? dataset.metadata?.column_count ?? schema.length ?? null,
    compliance_summary: {
      privacy_score: metadata?.privacy_score ?? null,
      freshness_score: metadata?.freshness_score ?? null,
      data_categories: metadata?.data_categories ?? [],
    },
    source_delivery_public_metadata: {
      file_format: metadata?.file_format || dataset.file_type || null,
      size_bytes: metadata?.size_bytes || dataset.metadata?.size_bytes || null,
    },
  };
}

export function prepareDisclosureSample(rows: Record<string, unknown>[]): PreparedDisclosureSample {
  const sourceRows = rows.slice(0, MAX_SAMPLE_ROWS);
  const sourceColumns = Object.keys(sourceRows[0] || {}).slice(0, MAX_SAMPLE_COLUMNS);
  let columns = [...sourceColumns];
  let candidateRows = projectRows(sourceRows, columns);
  let sample: ApprovedSample = {
    columns,
    row_refs: candidateRows.map((_, index) => `preview:${index}`),
    rows: candidateRows,
  };

  let truncatedForBytes = false;
  while (byteLength(sample) > MAX_SAMPLE_BYTES && sample.rows.length > 0) {
    truncatedForBytes = true;
    candidateRows = candidateRows.slice(0, -1);
    sample = {
      columns,
      row_refs: candidateRows.map((_, index) => `preview:${index}`),
      rows: candidateRows,
    };
  }

  while (byteLength(sample) > MAX_SAMPLE_BYTES && columns.length > 0) {
    truncatedForBytes = true;
    columns = columns.slice(0, -1);
    candidateRows = projectRows(candidateRows, columns);
    sample = {
      columns,
      row_refs: candidateRows.map((_, index) => `preview:${index}`),
      rows: candidateRows,
    };
  }

  return {
    sample: sample.columns.length > 0 && sample.rows.length > 0 ? sample : null,
    truncatedRows: rows.length > MAX_SAMPLE_ROWS,
    truncatedColumns: Object.keys(rows[0] || {}).length > MAX_SAMPLE_COLUMNS,
    truncatedForBytes,
    sizeBytes: sample.columns.length > 0 && sample.rows.length > 0 ? byteLength(sample) : 0,
  };
}

export function buildDisclosureSnapshotPayload({
  approvedFields,
  sampleDecision,
  approvedSample,
  confirmed,
  sourcePublishOperationId,
}: {
  approvedFields: ApprovedMetadataDraft;
  sampleDecision: DisclosureSampleDecision;
  approvedSample: ApprovedSample | null;
  confirmed: boolean;
  sourcePublishOperationId: string;
}): DisclosureSnapshotPayload {
  if (!confirmed) {
    throw new Error("Final disclosure confirmation is required.");
  }
  if (sampleDecision === "none" && approvedSample !== null) {
    throw new Error("No sample rows must submit approved_sample=null.");
  }
  if (sampleDecision === "approved_rows") {
    validateApprovedSample(approvedSample);
  }

  return {
    approved_fields: {
      ...approvedFields,
      schema: { columns: [...approvedFields.schema] },
    },
    sample_decision: sampleDecision,
    approved_sample: sampleDecision === "approved_rows" ? approvedSample : null,
    ai_training_notification_ack: true,
    ai_training_notification_text: AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY,
    license: AIM_CHANNEL_DISCLOSURE_LICENSE,
    approval_source: AIM_CHANNEL_DISCLOSURE_APPROVAL_SOURCE,
    source_publish_operation_id: sourcePublishOperationId,
  };
}

export function validateApprovedSample(sample: ApprovedSample | null): asserts sample is ApprovedSample {
  if (!sample) throw new Error("Approved sample rows are required.");
  if (sample.rows.length > MAX_SAMPLE_ROWS) throw new Error("Approved sample exceeds 100 rows.");
  if (sample.columns.length > MAX_SAMPLE_COLUMNS) throw new Error("Approved sample exceeds 25 columns.");
  if (sample.rows.length !== sample.row_refs.length) throw new Error("Approved sample row_refs must match rows.");
  const expected = JSON.stringify([...sample.columns].sort());
  for (const row of sample.rows) {
    if (JSON.stringify(Object.keys(row).sort()) !== expected) {
      throw new Error("Every approved sample row must contain exactly the approved columns.");
    }
  }
  if (byteLength(sample) > MAX_SAMPLE_BYTES) throw new Error("Approved sample exceeds 250 KB.");
}

function projectRows(rows: Record<string, unknown>[], columns: string[]): Record<string, unknown>[] {
  return rows.map((row) => {
    const projected: Record<string, unknown> = {};
    for (const column of columns) {
      projected[column] = row[column] ?? null;
    }
    return projected;
  });
}

function byteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length;
}
