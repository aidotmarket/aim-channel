import { useState, useEffect, useRef, useCallback } from "react";
import { useLocation, useParams, useNavigate, Link } from "react-router-dom";
import {
  ArrowLeft,
  Code,
  Trash2,
  FileSpreadsheet,
  FileJson,
  FileText,
  Database,
  Loader2,
  Rows3,
  Columns3,
  HardDrive,
  Calendar,
  Clock,
  FileType,
  ChevronLeft,
  ChevronRight,
  Upload,
  ExternalLink,
  Eye,
  ShoppingCart,
  DollarSign,
  TrendingUp,
  ChevronRight as ChevronRightIcon,
  CheckCircle2,
  ShieldAlert,
  Store,
  XCircle,
  MessageSquareText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ScrollArea, ScrollBar } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  datasetsApi,
  marketplaceApi,
  piiApi,
  type ApiDataset,
  type DisclosureSnapshotProxyRequest,
  type DatasetSampleResponse,
  type DatasetStatisticsResponse,
  type DatasetReadinessResponse,
  type DatasetListingMetadata,
  type PIIColumnAction,
  type PIIScanResponse,
} from "@/lib/api";
import { type ColumnSchema, type Dataset } from "@/types/mockDatasets";
import { toast } from "@/hooks/use-toast";
import PublishModal from "@/components/PublishModal";
import ChatPanel from "@/components/copilot/ChatPanel";
import { DataVerificationFlow } from "@/components/DataVerificationFlow";
import { useMarketplace } from "@/contexts/MarketplaceContext";
import { useAuth } from "@/contexts/AuthContext";
import { useMode } from "@/contexts/ModeContext";
import { useCoPilot } from "@/contexts/CoPilotContext";
import { useChannel } from "@/hooks/useChannel";
import { cn } from "@/lib/utils";
import {
  openSellerSetup,
  sellerSetupRequiredDescription,
  sellerSetupToastAction,
} from "@/lib/sellerOnboarding";
import {
  filenameToTitle,
  ListingEditorForm,
  type ListingEditorValue,
} from "@/components/ListingEditorForm";
import {
  AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY,
  buildApprovedMetadataDraft,
  buildDisclosureSnapshotPayload,
  classifyDisclosureSnapshotFailure,
  prepareDisclosureSample,
  type ApprovedMetadataDraft,
  type ApprovedSample,
  type DisclosureSnapshotFailure,
  type DisclosureSampleDecision,
  type DisclosureSnapshotPayload,
  type PreparedDisclosureSample,
} from "@/lib/disclosure";

interface DisclosureSnapshotFailurePanelProps {
  failure: DisclosureSnapshotFailure;
  publishing: boolean;
  canRetry: boolean;
  onRetry: () => void;
  onReview: () => void;
}

export function DisclosureSnapshotFailurePanel({
  failure,
  publishing,
  canRetry,
  onRetry,
  onReview,
}: DisclosureSnapshotFailurePanelProps) {
  return (
    <div className="rounded-md border border-yellow-500/30 bg-yellow-500/5 p-3 text-sm">
      <h3 className="font-medium">{failure.title}</h3>
      <p className="mt-1 text-muted-foreground">{failure.description}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {failure.status !== "snapshot_rejected" && (
          <Button type="button" size="sm" onClick={onRetry} disabled={publishing || !canRetry}>
            Retry disclosure snapshot
          </Button>
        )}
        <Button type="button" size="sm" variant="outline" onClick={onReview} disabled={publishing}>
          Review disclosure decision
        </Button>
      </div>
    </div>
  );
}

const getFileIcon = (type: Dataset["type"]) => {
  switch (type) {
    case "csv":
    case "xlsx":
      return FileSpreadsheet;
    case "json":
      return FileJson;
    case "pdf":
      return FileText;
    case "parquet":
      return Database;
    default:
      return FileText;
  }
};

const formatNumber = (num: number): string => {
  return num.toLocaleString();
};

const formatDate = (date: Date): string => {
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
};

const DataTypeColors: Record<ColumnSchema["dataType"], string> = {
  string: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  integer: "bg-green-500/20 text-green-400 border-green-500/30",
  float: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  date: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  boolean: "bg-pink-500/20 text-pink-400 border-pink-500/30",
};

// Map API column type to schema data type
const mapApiTypeToSchemaType = (apiType: string | undefined | null): ColumnSchema["dataType"] => {
  if (!apiType) return "string";
  
  const type = apiType.toLowerCase();
  if (type.includes("int") || type.includes("bigint")) return "integer";
  if (type.includes("float") || type.includes("double") || type.includes("decimal") || type.includes("number")) return "float";
  if (type.includes("date") || type.includes("time")) return "date";
  if (type.includes("bool")) return "boolean";
  return "string";
};

// Helper to convert API dataset to frontend format
const mapApiDatasetToFrontend = (apiDataset: ApiDataset): Dataset => ({
  id: apiDataset.id,
  name: apiDataset.original_filename,
  type: apiDataset.file_type as "csv" | "xlsx" | "json" | "pdf" | "parquet",
  status: apiDataset.status === "error" ? "error" as const
    : apiDataset.status === "preview_ready" ? "preview_ready" as const
    : apiDataset.status === "cancelled" ? "error" as const
    : "processing" as const,
  rows: apiDataset.metadata?.row_count || 0,
  columns: apiDataset.metadata?.column_count || 0,
  size: apiDataset.metadata?.size_bytes
    ? `${(apiDataset.metadata.size_bytes / 1024 / 1024).toFixed(2)} MB`
    : "Unknown",
  sizeBytes: apiDataset.metadata?.size_bytes || 0,
  createdAt: new Date(apiDataset.created_at),
  modifiedAt: new Date(apiDataset.updated_at),
  processingTime: 0,
  marketplace: undefined,
});

type ListingStepState = "pending" | "running" | "passed" | "flagged" | "not_run" | "failed";

function getPrimaryCategory(metadata: DatasetListingMetadata | null): string {
  const category = metadata?.data_categories?.[0];
  if (!category) return "tabular";
  if (category === "geographic") return "geospatial";
  if (category === "commerce") return "retail";
  if (category === "people") return "other";
  return ["tabular", "financial", "healthcare", "retail", "geospatial", "documents", "other"].includes(category)
    ? category
    : "other";
}

function getPiiSignal(scan: PIIScanResponse | null, failed: boolean): ListingStepState {
  if (failed) return "not_run";
  if (!scan) return "pending";
  const risk = String(scan.overall_risk || "none").toLowerCase();
  return scan.columns_with_pii > 0 || !["none", "low"].includes(risk) ? "flagged" : "passed";
}

function StepIcon({ state }: { state: ListingStepState }) {
  if (state === "running") return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
  if (state === "passed") return <CheckCircle2 className="h-4 w-4 text-green-500" />;
  if (state === "flagged") return <ShieldAlert className="h-4 w-4 text-yellow-500" />;
  if (state === "failed" || state === "not_run") return <XCircle className="h-4 w-4 text-muted-foreground" />;
  return <Clock className="h-4 w-4 text-muted-foreground" />;
}

const PII_ACTIONS: Array<{ value: PIIColumnAction; label: string }> = [
  { value: "exclude", label: "Exclude" },
  { value: "redact", label: "Redact" },
  { value: "keep", label: "Keep" },
];

const categoryOptions = [
  { value: "tabular", label: "Tabular" },
  { value: "financial", label: "Financial" },
  { value: "healthcare", label: "Healthcare" },
  { value: "retail", label: "Retail" },
  { value: "geospatial", label: "Geospatial" },
  { value: "documents", label: "Documents" },
  { value: "other", label: "Other" },
];

const autoPiiScanAttemptedDatasetIds = new Set<string>();

const METADATA_REVIEW_INSIGHTS_ENABLED = false;
// TODO: Wire this shell to the future SEO/discoverability scoring engine.

type GuidedMetadataField = "title" | "description" | "category" | "tags";

const guidedMetadataFields: Array<{ value: GuidedMetadataField; label: string }> = [
  { value: "title", label: "Title" },
  { value: "description", label: "Description" },
  { value: "category", label: "Category" },
  { value: "tags", label: "Tags" },
];

function FieldReviewCard({
  activeField,
  draftListingId,
  onLooksGood,
  onChangeIt,
  controlsDisabled,
}: {
  activeField: GuidedMetadataField;
  draftListingId: string | null;
  onLooksGood: () => void;
  onChangeIt: () => void;
  controlsDisabled?: boolean;
}) {
  const label = guidedMetadataFields.find((field) => field.value === activeField)?.label || activeField;
  const needsDraft = !draftListingId;

  return (
    <div className="rounded-lg border border-yellow-500/30 bg-yellow-500/5 px-4 py-3 text-sm">
      <div className="flex items-center gap-2 mb-2">
        <MessageSquareText className="h-4 w-4 text-yellow-600" />
        <span className="font-medium text-yellow-700">Review {label}</span>
      </div>
      <p className="text-foreground/70 text-xs mb-3">
        {needsDraft
          ? "Conversational field review with allAI is not available yet. Edit the fields directly and continue."
          : "Confirm this field or ask allAI to change it conversationally."}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" onClick={onLooksGood} disabled={controlsDisabled || needsDraft}>
          Looks good
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={onChangeIt} disabled={controlsDisabled || needsDraft}>
          Change it
        </Button>
      </div>
    </div>
  );
}

export function ListingPreparation({
  dataset,
  backPath,
  onDelete,
  isDeleting,
  draftListingId: initialDraftListingId,
  onDatasetRefresh,
}: {
  dataset: ApiDataset;
  backPath: string;
  onDelete: () => void;
  isDeleting: boolean;
  draftListingId: string | null;
  onDatasetRefresh?: (dataset: ApiDataset) => void;
}) {
  const navigate = useNavigate();
  const {
    allieAvailable,
    listingDraftUpdates,
    sendMessage,
    setEmbeddedSurfaceActive,
  } = useCoPilot();
  const persistedListingMetadata = dataset.metadata?.listing_metadata ?? null;
  const [piiScan, setPiiScan] = useState<PIIScanResponse | null>(null);
  const [piiFailed, setPiiFailed] = useState(false);
  const [piiScanState, setPiiScanState] = useState<ListingStepState>("pending");
  const [piiActions, setPiiActions] = useState<Record<string, PIIColumnAction>>({});
  const [privacyAttested, setPrivacyAttested] = useState(false);
  const [privacyStateHydrated, setPrivacyStateHydrated] = useState(false);
  const [savingPrivacy, setSavingPrivacy] = useState(false);
  const [activeStep, setActiveStep] = useState<1 | 2 | 3>(1);
  const [metadata, setMetadata] = useState<DatasetListingMetadata | null>(persistedListingMetadata);
  const [metadataState, setMetadataState] = useState<ListingStepState>(persistedListingMetadata ? "passed" : "pending");
  const [metadataApproved, setMetadataApproved] = useState(false);
  const [activeMetadataField, setActiveMetadataField] = useState<GuidedMetadataField>("title");
  const [publishing, setPublishing] = useState(false);
  const [tagInput, setTagInput] = useState("");
  const autoPiiScanDatasetRef = useRef<string | null>(null);
  const autoMetadataDatasetRef = useRef<string | null>(persistedListingMetadata ? dataset.id : null);
  const hydrationStepResolvedRef = useRef(false);
  const [form, setForm] = useState<ListingEditorValue>({
    title: persistedListingMetadata?.title || filenameToTitle(dataset.original_filename) || dataset.original_filename,
    description: persistedListingMetadata?.description || "",
    priceUsd: "25",
    category: getPrimaryCategory(persistedListingMetadata),
    tags: persistedListingMetadata?.tags || [],
  });
  const [approvedMetadataDraft, setApprovedMetadataDraft] = useState<ApprovedMetadataDraft | null>(null);
  const [sampleDecision, setSampleDecision] = useState<DisclosureSampleDecision>("none");
  const [preparedSample, setPreparedSample] = useState<PreparedDisclosureSample>(() => prepareDisclosureSample([]));
  const [sampleLoading, setSampleLoading] = useState(false);
  const [sampleError, setSampleError] = useState<string | null>(null);
  const [finalDisclosureConfirmed, setFinalDisclosureConfirmed] = useState(false);
  const [disclosureFailure, setDisclosureFailure] = useState<DisclosureSnapshotFailure | null>(null);
  const [publishedListingId, setPublishedListingId] = useState<string | null>(dataset.listing_id ?? null);
  const [publishComplete, setPublishComplete] = useState(false);
  const [publishedListingUrl, setPublishedListingUrl] = useState<string | null>(null);
  const [retrySnapshotPayload, setRetrySnapshotPayload] = useState<DisclosureSnapshotPayload | null>(null);

  const datasetReady = dataset.status === "preview_ready";
  const flaggedColumns = piiScan?.column_results ?? [];
  const piiState = piiScanState === "running" ? "running" : getPiiSignal(piiScan, piiFailed);
  const allFlaggedColumnsSaved = flaggedColumns.length === 0 || flaggedColumns.every((column) => Boolean(piiActions[column.column]));
  const canContinuePrivacy = Boolean(piiScan) && (flaggedColumns.length === 0 || allFlaggedColumnsSaved || privacyAttested);
  const persistedPrivacySatisfied = privacyAttested || (Boolean(piiScan) && allFlaggedColumnsSaved);
  const draftListingId = initialDraftListingId ?? metadata?.listing_id ?? null;

  useEffect(() => {
    const active = activeStep === 2 && Boolean(metadata);
    setEmbeddedSurfaceActive(active);
    return () => setEmbeddedSurfaceActive(false);
  }, [activeStep, metadata, setEmbeddedSurfaceActive]);

  const runPiiScan = useCallback(async (showFailureToast = true) => {
    if (!datasetReady) return;
    setPiiFailed(false);
    setPiiScanState("running");
    try {
      const scan = await piiApi.scan(dataset.id);
      setPiiScan(scan);
      setPiiScanState("passed");
    } catch (e) {
      setPiiFailed(true);
      setPiiScanState("failed");
      if (showFailureToast) {
        toast({
          title: "Privacy scan failed",
          description: e instanceof Error ? e.message : "Could not scan this dataset for personal data.",
          variant: "destructive",
        });
      }
    }
  }, [dataset.id, datasetReady]);

  useEffect(() => {
    let cancelled = false;

    async function loadPrivacyState() {
      try {
        const config = await piiApi.getConfig(dataset.id);
        if (!cancelled) {
          setPiiActions(config.column_actions || {});
          setPrivacyAttested(Boolean(config.privacy_attested));
        }
      } catch {
        // Config is optional until the seller makes a privacy decision.
      }

      try {
        const scan = await piiApi.getScan(dataset.id);
        if (!cancelled) {
          setPiiScan(scan);
          autoPiiScanDatasetRef.current = dataset.id;
          autoPiiScanAttemptedDatasetIds.add(dataset.id);
          setPiiScanState("passed");
        }
      } catch {
        if (cancelled) return;
        if (
          datasetReady &&
          autoPiiScanDatasetRef.current !== dataset.id &&
          !autoPiiScanAttemptedDatasetIds.has(dataset.id)
        ) {
          autoPiiScanDatasetRef.current = dataset.id;
          autoPiiScanAttemptedDatasetIds.add(dataset.id);
          await runPiiScan(false);
        } else {
          setPiiScanState("pending");
        }
      }

      if (!cancelled) setPrivacyStateHydrated(true);
    }

    loadPrivacyState();
    return () => {
      cancelled = true;
    };
  }, [dataset.id, datasetReady, runPiiScan]);

  useEffect(() => {
    if (!privacyStateHydrated || hydrationStepResolvedRef.current) return;
    hydrationStepResolvedRef.current = true;
    if (persistedListingMetadata && persistedPrivacySatisfied) {
      setActiveStep(2);
    }
  }, [persistedListingMetadata, persistedPrivacySatisfied, privacyStateHydrated]);

  const savePrivacyConfig = async (actions: Record<string, PIIColumnAction>, attested: boolean) => {
    setSavingPrivacy(true);
    try {
      const saved = await piiApi.saveConfig(dataset.id, actions, attested);
      setPiiActions(saved.column_actions || {});
      setPrivacyAttested(Boolean(saved.privacy_attested));
      return true;
    } catch (e) {
      toast({
        title: "Privacy decision not saved",
        description: e instanceof Error ? e.message : "Could not save the PII review decision.",
        variant: "destructive",
      });
      return false;
    } finally {
      setSavingPrivacy(false);
    }
  };

  const handleRunPiiScan = async () => {
    await runPiiScan();
  };

  const handleColumnAction = async (column: string, action: PIIColumnAction) => {
    const previous = piiActions;
    const next = { ...piiActions, [column]: action };
    setPiiActions(next);
    const saved = await savePrivacyConfig(next, privacyAttested);
    if (!saved) setPiiActions(previous);
  };

  const handleAttestationChange = async (checked: boolean) => {
    const previous = privacyAttested;
    setPrivacyAttested(checked);
    const saved = await savePrivacyConfig(piiActions, checked);
    if (!saved) setPrivacyAttested(previous);
  };

  const handleContinuePrivacy = async () => {
    if (!canContinuePrivacy) return;
    if (privacyAttested) {
      const saved = await savePrivacyConfig(piiActions, true);
      if (!saved) return;
    }
    setActiveStep(2);
  };

  const handleGenerateMetadata = useCallback(async () => {
    setMetadataState("running");
    try {
      const generated = await datasetsApi.getListingMetadata(dataset.id);
      setMetadata(generated);
      setMetadataApproved(false);
      setActiveMetadataField("title");
      setForm((current) => ({
        title: generated.title || filenameToTitle(dataset.original_filename) || dataset.original_filename,
        description: generated.description || "",
        priceUsd: current.priceUsd,
        category: getPrimaryCategory(generated),
        tags: generated.tags || [],
      }));
      setMetadataState("passed");
    } catch (e) {
      setMetadataState("failed");
      toast({
        title: "Metadata generation failed",
        description: e instanceof Error ? e.message : "Could not generate listing metadata.",
        variant: "destructive",
      });
    }
  }, [dataset.id, dataset.original_filename]);

  useEffect(() => {
    if (
      activeStep !== 2 ||
      metadata ||
      metadataState === "running" ||
      autoMetadataDatasetRef.current === dataset.id
    ) {
      return;
    }

    autoMetadataDatasetRef.current = dataset.id;
    handleGenerateMetadata();
  }, [activeStep, dataset.id, metadata, metadataState, handleGenerateMetadata]);

  useEffect(() => {
    if (activeStep !== 3) return;
    let cancelled = false;
    setSampleLoading(true);
    setSampleError(null);
    datasetsApi.getDisclosureSample(dataset.id, 100)
      .then((response) => {
        if (cancelled) return;
        setPreparedSample(prepareDisclosureSample(response.sample || []));
      })
      .catch((e) => {
        if (cancelled) return;
        setPreparedSample(prepareDisclosureSample([]));
        setSampleError(e instanceof Error ? e.message : "Could not load disclosure preview rows.");
        setSampleDecision("none");
      })
      .finally(() => {
        if (!cancelled) setSampleLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeStep, dataset.id]);

  const handleApproveMetadata = () => {
    if (!metadata) return;
    setMetadataApproved(true);
    setApprovedMetadataDraft(buildApprovedMetadataDraft(form, metadata, dataset));
  };

  const handleContinueMetadata = () => {
    if (!metadata || !metadataApproved || !form.title.trim() || !form.description.trim()) return;
    setApprovedMetadataDraft(buildApprovedMetadataDraft(form, metadata, dataset));
    setFinalDisclosureConfirmed(false);
    setActiveStep(3);
  };

  const focusNextMetadataField = () => {
    const currentIndex = guidedMetadataFields.findIndex((field) => field.value === activeMetadataField);
    const nextField = guidedMetadataFields[currentIndex + 1];
    if (nextField) {
      setActiveMetadataField(nextField.value);
      return;
    }
    handleApproveMetadata();
  };

  const handleAcceptAllMetadata = () => {
    if (!metadata || !form.title.trim() || !form.description.trim()) return;
    setMetadataApproved(true);
    setApprovedMetadataDraft(buildApprovedMetadataDraft(form, metadata, dataset));
    setFinalDisclosureConfirmed(false);
    setActiveStep(3);
  };

  const handleChangeMetadataField = () => {
    const field = guidedMetadataFields.find((item) => item.value === activeMetadataField);
    if (!field || !draftListingId) return;

    const currentValue = activeMetadataField === "tags" ? form.tags.join(", ") : String(form[activeMetadataField] || "");
    const toolInstructionByField: Record<GuidedMetadataField, string> = {
      title: `After the seller confirms the requested change, call update_listing_title with listing_id "${draftListingId}".`,
      description: `After the seller confirms the requested change, call update_listing_description with listing_id "${draftListingId}".`,
      category: `After the seller confirms the requested change, call set_listing_category with listing_id "${draftListingId}".`,
      tags: `After the seller confirms the requested change, call regenerate_listing_metadata with listing_id "${draftListingId}", fields ["tags"], and apply true.`,
    };

    sendMessage(
      [
        `I want to change the listing ${field.label.toLowerCase()} for draft listing ${draftListingId} from dataset ${dataset.id}.`,
        `Active field: ${activeMetadataField}.`,
        `Current ${field.label.toLowerCase()}: ${currentValue || "(empty)"}`,
        "Please keep this conversational: ask a concise clarifying question before applying the edit if the requested change is not already specific.",
        toolInstructionByField[activeMetadataField],
        "Apply only this active field through the existing listing edit engine and do not defer because the draft listing id is already available.",
      ].join("\n")
    );
  };

  const addTag = () => {
    const tag = tagInput.trim().toLowerCase();
    if (!tag || form.tags.includes(tag) || form.tags.length >= 20) {
      setTagInput("");
      return;
    }
    setMetadataApproved(false);
    setForm({ ...form, tags: [...form.tags, tag] });
    setTagInput("");
  };

  const newPublishOperationId = () => {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return crypto.randomUUID();
    }
    return `op-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  };

  const submitDisclosureSnapshot = async (listingId: string, payload: DisclosureSnapshotPayload, listingUrl = publishedListingUrl) => {
    const request = { dataset_id: dataset.id, ...payload } satisfies DisclosureSnapshotProxyRequest;
    await marketplaceApi.createDisclosureSnapshot(
      listingId,
      request
    );
    setPublishedListingId(listingId);
    setRetrySnapshotPayload(null);
    setDisclosureFailure(null);
    setPublishComplete(true);
    toast({
      title: "Dataset published to ai.market",
      description: (
        <a href={listingUrl || `https://ai.market/listing/${encodeURIComponent(listingId)}`} target="_blank" rel="noopener noreferrer" className="underline">
          View listing on ai.market
        </a>
      ),
    });
    try {
      const refreshed = await datasetsApi.get(dataset.id);
      onDatasetRefresh?.(refreshed);
    } catch (error) {
      // A refresh failure must not turn a stored snapshot into a disclosure retry.
      console.warn("Published dataset refresh failed", error);
    }
  };

  const buildCurrentDisclosurePayload = (sourcePublishOperationId: string) => {
    const approvedFields = approvedMetadataDraft;
    if (!approvedFields) {
      throw new Error("Metadata approval expired. Review and approve Step 2 again.");
    }
    const approvedSample = sampleDecision === "approved_rows" ? preparedSample.sample : null;
    return buildDisclosureSnapshotPayload({
      approvedFields,
      sampleDecision,
      approvedSample,
      confirmed: finalDisclosureConfirmed,
      sourcePublishOperationId,
    });
  };

  const handleRetryDisclosureSnapshot = async () => {
    if (!publishedListingId || !retrySnapshotPayload || publishing) return;
    setPublishing(true);
    setDisclosureFailure(null);
    try {
      await submitDisclosureSnapshot(publishedListingId, retrySnapshotPayload);
    } catch (e) {
      const failure = classifyDisclosureSnapshotFailure(e);
      setDisclosureFailure(failure);
      toast({
        title: failure.title,
        description: failure.description,
        variant: "destructive",
      });
    } finally {
      setPublishing(false);
    }
  };

  const handleReviewDisclosureDecision = () => {
    setDisclosureFailure(null);
    setFinalDisclosureConfirmed(false);
    setActiveStep(3);
  };

  const handlePublish = async () => {
    if (publishing || publishedListingId) return;
    setDisclosureFailure(null);
    const price = Number.parseFloat(form.priceUsd);
    if (!form.title.trim()) {
      toast({ title: "Title required", description: "Add a title for this listing.", variant: "destructive" });
      return;
    }
    if (!form.description.trim()) {
      toast({ title: "Description required", description: "Add listing details for buyers.", variant: "destructive" });
      return;
    }
    if (!Number.isFinite(price) || price < 25) {
      toast({ title: "Price required", description: "Set a price of at least $25.", variant: "destructive" });
      return;
    }
    if (!approvedMetadataDraft || !metadataApproved) {
      toast({ title: "Review metadata again", description: "Approved metadata changed before publish.", variant: "destructive" });
      setActiveStep(2);
      return;
    }
    if (sampleDecision === "approved_rows" && !preparedSample.sample) {
      toast({ title: "Sample unavailable", description: "Choose no sample rows or reload the disclosure preview.", variant: "destructive" });
      return;
    }
    if (!finalDisclosureConfirmed) {
      toast({ title: "Confirmation required", description: "Confirm the public disclosure notice before publishing.", variant: "destructive" });
      return;
    }

    setPublishing(true);
    const sourcePublishOperationId = newPublishOperationId();
    let disclosurePayload: DisclosureSnapshotPayload;
    try {
      disclosurePayload = buildCurrentDisclosurePayload(sourcePublishOperationId);
    } catch (e) {
      toast({
        title: "Disclosure decision incomplete",
        description: e instanceof Error ? e.message : "Complete the disclosure decision before publishing.",
        variant: "destructive",
      });
      setPublishing(false);
      return;
    }

    try {
      const publishResponse = await marketplaceApi.publish({
        vz_dataset_id: dataset.id,
        title: form.title.trim(),
        description: form.description.trim(),
        tags: form.tags,
        category: form.category,
        price_cents: Math.round(price * 100),
        row_count: metadata?.row_count ?? dataset.metadata?.row_count ?? null,
        column_names: metadata?.column_summary?.map((column) => column.name) ?? dataset.metadata?.columns?.map((column) => column.name) ?? null,
        column_types: metadata?.column_summary?.map((column) => column.type) ?? dataset.metadata?.columns?.map((column) => column.type) ?? null,
        file_format: metadata?.file_format || dataset.file_type,
        file_size_bytes: metadata?.size_bytes || dataset.metadata?.size_bytes || null,
      });
      const listingId = publishResponse.listing_id;
      if (!listingId) {
        throw new Error("ai.market did not return a listing_id.");
      }
      setPublishedListingId(listingId);
      setPublishedListingUrl(publishResponse.marketplace_url ?? null);
      setRetrySnapshotPayload(disclosurePayload);
      try {
        await submitDisclosureSnapshot(listingId, disclosurePayload, publishResponse.marketplace_url);
      } catch (snapshotError) {
        const failure = classifyDisclosureSnapshotFailure(snapshotError);
        setDisclosureFailure(failure);
        toast({
          title: failure.title,
          description: failure.description,
          variant: "destructive",
        });
      }
    } catch (e) {
      toast({
        title: "Publish failed",
        description: e instanceof Error ? e.message : "Failed to publish listing.",
        variant: "destructive",
      });
    } finally {
      setPublishing(false);
    }
  };

  const setFormPatch = (patch: Partial<ListingEditorValue>) => {
    setMetadataApproved(false);
    setApprovedMetadataDraft(null);
    setForm({ ...form, ...patch });
  };

  const handleStep3FormChange = (next: ListingEditorValue) => {
    const metadataChanged =
      next.title !== form.title ||
      next.description !== form.description ||
      next.category !== form.category ||
      next.tags.join("\u0000") !== form.tags.join("\u0000");
    setForm(next);
    if (metadataChanged) {
      setMetadataApproved(false);
      setApprovedMetadataDraft(null);
      setFinalDisclosureConfirmed(false);
      setActiveStep(2);
      toast({
        title: "Metadata approval reset",
        description: "Review and approve the changed public fields before publishing.",
      });
    }
  };

  useEffect(() => {
    const updatedListing = listingDraftUpdates[dataset.id];
    if (!updatedListing) return;

    setMetadataApproved(false);
    setForm((current) => ({
      ...current,
      title: updatedListing.title ?? current.title,
      description: updatedListing.description ?? current.description,
      category: (updatedListing.auto_metadata?.category as string) || current.category,
      tags: Array.isArray(updatedListing.tags) ? updatedListing.tags : current.tags,
    }));
  }, [dataset.id, listingDraftUpdates]);

  const piiFlaggedColumnNames = new Set(flaggedColumns.map((column) => column.column));
  const approvedSample: ApprovedSample | null = preparedSample.sample;
  const sampleColumnsWithPii = approvedSample?.columns.filter((column) => piiFlaggedColumnNames.has(column)) ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <Button variant="ghost" size="sm" onClick={() => navigate(backPath)} className="gap-2">
          <ArrowLeft className="w-4 h-4" />
          Back
        </Button>
        <Button variant="outline" size="sm" className="text-destructive hover:text-destructive" onClick={onDelete} disabled={isDeleting}>
          <Trash2 className="w-4 h-4 mr-2" />
          Delete
        </Button>
      </div>

      <div>
        <h1 className="text-2xl font-bold text-foreground">{dataset.original_filename}</h1>
        <p className="text-sm text-muted-foreground">
          Review privacy findings, approve allAI metadata, then publish.
        </p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Listing Flow</CardTitle>
          <CardDescription>Each step must be completed before the next one unlocks.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-3">
          <div className="rounded-md border p-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <StepIcon state={piiState} />
              1. Privacy review
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {!datasetReady && "Waiting for dataset preview"}
              {piiState === "passed" && "Passed"}
              {piiState === "flagged" && `${piiScan?.columns_with_pii ?? 0} column${piiScan?.columns_with_pii === 1 ? "" : "s"} flagged`}
              {piiState === "not_run" && "Not run"}
              {piiState === "pending" && datasetReady && "Preparing scan"}
              {piiState === "running" && "Scanning"}
            </p>
          </div>
          <div className="rounded-md border p-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <StepIcon state={metadataState} />
              2. Metadata review
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {metadataState === "running" && "Generating description, tags, classifications, and scores"}
              {metadataState === "passed" && "Ready"}
              {metadataState === "failed" && "Failed"}
              {metadataState === "pending" && "Waiting for privacy review"}
            </p>
          </div>
          <div className="rounded-md border p-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <StepIcon state={publishComplete ? "passed" : "not_run"} />
              3. Listing Details and Disclosure
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {publishComplete ? "Complete" : publishedListingId ? "Listing published, disclosure snapshot pending" : activeStep === 3 ? "Editor unlocked" : "Waiting for metadata approval"}
            </p>
          </div>
        </CardContent>
      </Card>

      {activeStep === 1 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldAlert className="h-4 w-4 text-primary" />
              Step 1: Privacy Review
            </CardTitle>
            <CardDescription>Scan for reported personal-data findings and choose how to handle each flagged column.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {!datasetReady && (
              <div className="rounded-md border bg-muted/30 p-3 text-sm text-muted-foreground">
                File registration is complete. Preview preparation is finishing before the privacy scan can run.
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2">
              {(piiScan || piiFailed) && (
                <Button onClick={handleRunPiiScan} disabled={!datasetReady || piiScanState === "running" || savingPrivacy} size="sm" className="gap-2">
                  {piiScanState === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldAlert className="h-4 w-4" />}
                  Run scan again
                </Button>
              )}
              {!piiScan && piiScanState === "running" && (
                <Badge variant="secondary" className="gap-1">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  Running privacy scan
                </Badge>
              )}
              {piiFailed && <Badge variant="secondary">Scan failed</Badge>}
              {piiScan && flaggedColumns.length === 0 && <Badge className="bg-haven-success/20 text-haven-success border-haven-success/30">No personal data detected</Badge>}
            </div>

            {flaggedColumns.length > 0 && (
              <div className="space-y-3">
                {flaggedColumns.map((column) => (
                  <div key={column.column} className="rounded-md border p-3">
                    <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                      <div className="space-y-1">
                        <div className="font-medium">{column.column}</div>
                        <div className="flex flex-wrap gap-1.5">
                          {column.pii_types.map((type) => (
                            <Badge key={type} variant="secondary">{type}</Badge>
                          ))}
                          <Badge variant="outline">{column.risk_level} risk</Badge>
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {PII_ACTIONS.map((action) => (
                          <Button
                            key={action.value}
                            type="button"
                            variant={piiActions[column.column] === action.value ? "default" : "outline"}
                            size="sm"
                            disabled={savingPrivacy || activeStep > 1}
                            onClick={() => handleColumnAction(column.column, action.value)}
                          >
                            {action.label}
                          </Button>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}

                <div className="flex items-start gap-3 rounded-md border p-3">
                  <Checkbox
                    id="privacy-attestation"
                    checked={privacyAttested}
                    disabled={savingPrivacy || activeStep > 1}
                    onCheckedChange={(checked) => handleAttestationChange(checked === true)}
                  />
                  <Label htmlFor="privacy-attestation" className="text-sm font-normal leading-5">
                    I have reviewed the reported personal-data findings and choose to publish this listing as-is.
                  </Label>
                </div>
              </div>
            )}

            <Button onClick={handleContinuePrivacy} disabled={activeStep > 1 || !canContinuePrivacy || savingPrivacy} size="sm">
              Continue to metadata
            </Button>
          </CardContent>
        </Card>
      )}

      {activeStep === 2 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <CheckCircle2 className="h-4 w-4 text-primary" />
              Step 2: Metadata Review
            </CardTitle>
            <CardDescription>Generate, edit, and approve the allAI listing draft.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {!metadata && (
              <Button onClick={handleGenerateMetadata} disabled={metadataState === "running"} size="sm" className="gap-2">
                {metadataState === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Store className="h-4 w-4" />}
                Generate allAI draft
              </Button>
            )}

            {metadata && (
              <>
                <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
                  <div className="space-y-4">
                    <div className="flex flex-wrap items-center gap-2">
                      {guidedMetadataFields.map((field) => (
                        <Button
                          key={field.value}
                          type="button"
                          size="sm"
                          variant={activeMetadataField === field.value ? "default" : "outline"}
                          onClick={() => setActiveMetadataField(field.value)}
                        >
                          {field.label}
                        </Button>
                      ))}
                    </div>

                    <div className={cn(
                      "space-y-2 rounded-md border p-3 transition-colors",
                      activeMetadataField === "title" ? "border-primary bg-primary/5" : "border-transparent"
                    )}>
                      <Label htmlFor="metadata-title">Title</Label>
                      <Input id="metadata-title" value={form.title} onChange={(event) => setFormPatch({ title: event.target.value })} disabled={activeStep > 2} />
                    </div>
                    <div className={cn(
                      "space-y-2 rounded-md border p-3 transition-colors",
                      activeMetadataField === "description" ? "border-primary bg-primary/5" : "border-transparent"
                    )}>
                      <Label htmlFor="metadata-description">Description</Label>
                      <Textarea id="metadata-description" value={form.description} onChange={(event) => setFormPatch({ description: event.target.value })} rows={5} disabled={activeStep > 2} />
                    </div>
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className={cn(
                        "space-y-2 rounded-md border p-3 transition-colors",
                        activeMetadataField === "category" ? "border-primary bg-primary/5" : "border-transparent"
                      )}>
                        <Label htmlFor="metadata-category">Category</Label>
                        <Select value={form.category} onValueChange={(category) => setFormPatch({ category })} disabled={activeStep > 2}>
                          <SelectTrigger id="metadata-category" className="bg-background border-border">
                            <SelectValue placeholder="Choose a category" />
                          </SelectTrigger>
                          <SelectContent>
                            {categoryOptions.map((category) => (
                              <SelectItem key={category.value} value={category.value}>{category.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="grid grid-cols-2 gap-3 text-sm">
                        <div className="rounded-md border p-3">
                          <span className="text-muted-foreground">Privacy score</span>
                          <p className="font-medium">{metadata.privacy_score == null ? "Not available" : `${metadata.privacy_score}/10`}</p>
                        </div>
                        <div className="rounded-md border p-3">
                          <span className="text-muted-foreground">Freshness score</span>
                          <p className="font-medium">{Math.round(metadata.freshness_score * 100)}%</p>
                        </div>
                        <div className="rounded-md border border-dashed p-3">
                          <span className="text-muted-foreground">Discoverability</span>
                          <p className="font-medium text-muted-foreground">
                            {METADATA_REVIEW_INSIGHTS_ENABLED ? "Coming soon" : "Coming soon"}
                          </p>
                        </div>
                      </div>
                    </div>
                    <div className={cn(
                      "space-y-2 rounded-md border p-3 transition-colors",
                      activeMetadataField === "tags" ? "border-primary bg-primary/5" : "border-transparent"
                    )}>
                      <Label htmlFor="metadata-tags">Tags</Label>
                      <div className="flex gap-2">
                        <Input
                          id="metadata-tags"
                          value={tagInput}
                          onChange={(event) => setTagInput(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              addTag();
                            }
                          }}
                          disabled={activeStep > 2}
                        />
                        <Button type="button" variant="outline" onClick={addTag} disabled={activeStep > 2 || !tagInput.trim()}>Add</Button>
                      </div>
                      <div className="flex flex-wrap gap-1.5 pt-1">
                        {form.tags.map((tag) => (
                          <Badge key={tag} variant="secondary" className="gap-1">
                            {tag}
                            <button
                              type="button"
                              onClick={() => setFormPatch({ tags: form.tags.filter((item) => item !== tag) })}
                              className="hover:text-destructive"
                              aria-label={`Remove tag ${tag}`}
                              disabled={activeStep > 2}
                            >
                              <XCircle className="h-3 w-3" />
                            </button>
                          </Badge>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-md border border-dashed p-3 text-sm">
                      <span className="text-muted-foreground">Why allAI chose this</span>
                      <p className="mt-1 text-muted-foreground">Coming soon</p>
                    </div>
                  </div>

                  <div className="space-y-3">
                    <FieldReviewCard
                      activeField={activeMetadataField}
                      draftListingId={draftListingId}
                      onLooksGood={focusNextMetadataField}
                      onChangeIt={handleChangeMetadataField}
                      controlsDisabled={!allieAvailable || activeStep > 2}
                    />
                    {allieAvailable ? (
                      <ChatPanel
                        embedded
                        title="allAI review"
                        subtitle={`Reviewing ${guidedMetadataFields.find((field) => field.value === activeMetadataField)?.label.toLowerCase() || activeMetadataField}`}
                      />
                    ) : (
                      <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                        allAI is unavailable for this session.
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap gap-2">
                  <Button onClick={handleAcceptAllMetadata} disabled={activeStep > 2 || !form.title.trim() || !form.description.trim()} size="sm">
                    Accept all & continue
                  </Button>
                  {metadataApproved && (
                    <Button onClick={handleContinueMetadata} disabled={activeStep > 2 || !form.title.trim() || !form.description.trim()} size="sm" variant="outline">
                      Continue to publish
                    </Button>
                  )}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {activeStep === 3 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Store className="h-4 w-4 text-primary" />
              Step 3: Listing Details and Disclosure
            </CardTitle>
            <CardDescription>Confirm listing details, sample disclosure, and the public AI discovery notice.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <ListingEditorForm
              value={form}
              onChange={handleStep3FormChange}
              tagInput={tagInput}
              onTagInputChange={setTagInput}
              disabled={publishing}
            />

            <div className="space-y-3 rounded-md border p-3">
              <div>
                <h3 className="text-sm font-medium">Public sample</h3>
                <p className="text-xs text-muted-foreground">
                  Choose whether real preview rows become public. The displayed set is the complete public sample.
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant={sampleDecision === "none" ? "default" : "outline"}
                  disabled={publishing}
                  onClick={() => {
                    setSampleDecision("none");
                    setFinalDisclosureConfirmed(false);
                  }}
                >
                  No sample rows
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={sampleDecision === "approved_rows" ? "default" : "outline"}
                  disabled={publishing || sampleLoading || !approvedSample}
                  onClick={() => {
                    setSampleDecision("approved_rows");
                    setFinalDisclosureConfirmed(false);
                  }}
                >
                  Publish these real sample rows
                </Button>
              </div>

              {sampleLoading && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Loading disclosure preview
                </div>
              )}
              {sampleError && (
                <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                  {sampleError} No sample rows will be submitted unless the preview loads.
                </div>
              )}
              {approvedSample && (
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                    <Badge variant="outline">{approvedSample.rows.length} row{approvedSample.rows.length === 1 ? "" : "s"}</Badge>
                    <Badge variant="outline">{approvedSample.columns.length} column{approvedSample.columns.length === 1 ? "" : "s"}</Badge>
                    {preparedSample.truncatedRows && <Badge variant="secondary">Rows truncated to public limit</Badge>}
                    {preparedSample.truncatedColumns && <Badge variant="secondary">Columns truncated to public limit</Badge>}
                    {preparedSample.truncatedForBytes && <Badge variant="secondary">Sample reduced to 250 KB</Badge>}
                  </div>
                  {sampleColumnsWithPii.length > 0 && (
                    <div className="rounded-md border border-yellow-500/30 bg-yellow-500/5 p-3 text-sm">
                      Personal-data flagged columns in this sample: {sampleColumnsWithPii.join(", ")}. Choose no sample rows or confirm the exact row content below before publishing.
                    </div>
                  )}
                  <ScrollArea className="w-full rounded-md border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          {approvedSample.columns.map((column) => (
                            <TableHead key={column}>
                              <span className="inline-flex items-center gap-1">
                                {column}
                                {piiFlaggedColumnNames.has(column) && <Badge variant="secondary">PII</Badge>}
                              </span>
                            </TableHead>
                          ))}
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {approvedSample.rows.map((row, index) => (
                          <TableRow key={approvedSample.row_refs[index]}>
                            {approvedSample.columns.map((column) => (
                              <TableCell key={column} className="max-w-[220px] truncate">
                                {String(row[column] ?? "")}
                              </TableCell>
                            ))}
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    <ScrollBar orientation="horizontal" />
                  </ScrollArea>
                </div>
              )}
            </div>

            <div className="rounded-md border p-3 text-sm">
              <h3 className="font-medium">Disclosure summary</h3>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                <div>
                  <span className="text-muted-foreground">Approved title</span>
                  <p className="font-medium">{approvedMetadataDraft?.title || form.title}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Sample choice</span>
                  <p className="font-medium">{sampleDecision === "none" ? "No sample rows" : "Publish these real sample rows"}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Category</span>
                  <p className="font-medium">{approvedMetadataDraft?.category || form.category}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Schema columns</span>
                  <p className="font-medium">{approvedMetadataDraft?.schema.length ?? 0}</p>
                </div>
              </div>
            </div>

            {disclosureFailure && (
              <DisclosureSnapshotFailurePanel
                failure={disclosureFailure}
                publishing={publishing}
                canRetry={Boolean(retrySnapshotPayload)}
                onRetry={handleRetryDisclosureSnapshot}
                onReview={handleReviewDisclosureDecision}
              />
            )}

            <div className="flex items-start gap-3 rounded-md border p-3">
              <Checkbox
                id="ai-training-disclosure-confirmation"
                checked={finalDisclosureConfirmed}
                disabled={publishing}
                onCheckedChange={(checked) => setFinalDisclosureConfirmed(checked === true)}
              />
              <Label htmlFor="ai-training-disclosure-confirmation" className="text-sm font-normal leading-5">
                {AIM_CHANNEL_DISCLOSURE_CONFIRMATION_COPY}
              </Label>
            </div>

            <Button
              onClick={handlePublish}
              disabled={Boolean(publishedListingId) || publishing || !finalDisclosureConfirmed || !approvedMetadataDraft || (sampleDecision === "approved_rows" && !approvedSample)}
              size="sm"
              className="gap-2"
            >
              {publishing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Store className="h-4 w-4" />}
              {publishedListingId ? "Published" : publishing ? "Publishing..." : "Publish to ai.market"}
            </Button>
          </CardContent>
        </Card>
      )}

      <DataVerificationFlow
        datasetId={dataset.id}
        sourceName={dataset.original_filename}
        listingId={publishedListingId}
      />
    </div>
  );
}

const DatasetDetail = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const backPath = location.state?.from === "/list-data" ? "/list-data" : "/datasets";
  const { isPublished, getPublishedData, unpublishDataset } = useMarketplace();
  const { onboarding_required } = useAuth();
  const { hasFeature } = useMode();
  const channel = useChannel();
  const [currentPage, setCurrentPage] = useState(1);
  const [publishModalOpen, setPublishModalOpen] = useState(false);
  const rowsPerPage = 10;

  // Delete debounce
  const [isDeleting, setIsDeleting] = useState(false);

  // API data states
  const [apiDataset, setApiDataset] = useState<ApiDataset | null>(null);
  const [sampleData, setSampleData] = useState<Record<string, unknown>[]>([]);
  const [statistics, setStatistics] = useState<DatasetStatisticsResponse | null>(null);
  const [readiness, setReadiness] = useState<DatasetReadinessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch dataset from API
  useEffect(() => {
    if (!id) {
      setError("No dataset ID provided");
      setLoading(false);
      return;
    }

    const fetchDataset = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await datasetsApi.get(id);
        setApiDataset(data);

        // Fetch sample data and statistics in parallel
        const [sampleRes, statsRes, readinessRes] = await Promise.allSettled([
          datasetsApi.getSample(id, 20),
          datasetsApi.getStatistics(id),
          datasetsApi.getReadiness(id),
        ]);

        if (sampleRes.status === "fulfilled") {
          setSampleData(sampleRes.value.sample);
        }
        if (statsRes.status === "fulfilled") {
          setStatistics(statsRes.value);
        }
        if (readinessRes.status === "fulfilled") {
          setReadiness(readinessRes.value);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load dataset");
      } finally {
        setLoading(false);
      }
    };

    fetchDataset();
  }, [id]);

  useEffect(() => {
    if (!apiDataset || apiDataset.status === "preview_ready" || apiDataset.status === "error") {
      return undefined;
    }

    const timer = window.setInterval(async () => {
      try {
        const status = await datasetsApi.getStatus(apiDataset.id);
        if (status.status === "preview_ready" || status.status === "error") {
          const refreshed = await datasetsApi.get(apiDataset.id);
          setApiDataset(refreshed);
        }
      } catch {
        // Keep the current processing view and retry on the next tick.
      }
    }, 3000);

    return () => window.clearInterval(timer);
  }, [apiDataset]);

  // Show loading skeleton
  if (loading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-4 w-48" />
        <div className="flex items-center gap-4">
          <Skeleton className="w-12 h-12 rounded-lg" />
          <div className="space-y-2">
            <Skeleton className="h-6 w-64" />
            <Skeleton className="h-4 w-40" />
          </div>
        </div>
        <div className="grid grid-cols-6 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-24 rounded-lg" />
          ))}
        </div>
      </div>
    );
  }

  // Show error / not found
  if (error || !apiDataset) {
    return (
      <div className="flex flex-col items-center justify-center py-16 space-y-4">
        <Database className="w-16 h-16 text-muted-foreground" />
        <h2 className="text-xl font-semibold text-foreground">
          Dataset not found
        </h2>
        <p className="text-muted-foreground">
          {error || "The dataset you're looking for doesn't exist."}
        </p>
        <Button variant="secondary" onClick={() => navigate("/datasets")}>
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Datasets
        </Button>
      </div>
    );
  }

  const handleDelete = async () => {
    if (!window.confirm("Are you sure you want to delete this dataset? This action cannot be undone.")) {
      return;
    }
    setIsDeleting(true);
    try {
      await datasetsApi.delete(apiDataset.id);
      toast({
        title: "Dataset deleted",
        description: "The dataset has been permanently removed",
      });
      navigate("/datasets");
    } catch {
      toast({
        title: "Delete failed",
        description: "Failed to delete dataset. Please try again or check system health.",
        variant: "destructive",
      });
    } finally {
      setIsDeleting(false);
    }
  };

  if (apiDataset.status !== "error" && !apiDataset.listing_id) {
    return (
      <ListingPreparation
        dataset={apiDataset}
        onDatasetRefresh={setApiDataset}
        draftListingId={apiDataset.listing_id ?? null}
        backPath={backPath}
        onDelete={handleDelete}
        isDeleting={isDeleting}
      />
    );
  }

  // Convert API dataset to frontend format
  const dataset = mapApiDatasetToFrontend(apiDataset);
  const Icon = getFileIcon(dataset.type);

  // Build schema from API data
  const schema: ColumnSchema[] = apiDataset.metadata?.columns?.map((col) => ({
    name: col.name,
    dataType: mapApiTypeToSchemaType(col.type),
    nonNullCount: dataset.rows,
    nullPercentage: 0,
    sampleValues: [],
  })) || [];

  // Build stats from API response
  const stats = statistics?.statistics?.map((s) => ({
    name: s.column,
    dataType: mapApiTypeToSchemaType(s.type),
    uniqueCount: s.unique_count,
    min: s.min,
    max: s.max,
    mean: s.mean,
    median: s.median,
    stdDev: s.std,
    mostCommon: s.top_values?.map((v) => ({ value: v.value, count: v.count })),
  })) || [];

  const totalPages = Math.ceil(dataset.rows / rowsPerPage);
  const startRow = (currentPage - 1) * rowsPerPage + 1;
  const endRow = Math.min(currentPage * rowsPerPage, dataset.rows);

  // Check marketplace context for published status
  const datasetIsPublished = isPublished(dataset.id) || dataset.marketplace?.isPublished;
  const publishedData = getPublishedData(dataset.id);
  const marketplaceData = publishedData || dataset.marketplace;

  const handlePublishSuccess = () => {
    if (onboarding_required) {
      toast({
        title: "Dataset published",
        description: sellerSetupRequiredDescription,
        action: sellerSetupToastAction(),
        duration: 15000,
      });
      openSellerSetup();
      return;
    }

    toast({
      title: "Dataset published",
      description: "Your dataset is now live on the marketplace",
    });
  };

  const handleUnpublish = () => {
    unpublishDataset(dataset.id);
    toast({
      title: "Dataset unpublished",
      description: "Your dataset has been removed from the marketplace",
    });
  };

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-1 text-sm">
        <Link
          to="/datasets"
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          Datasets
        </Link>
        <ChevronRightIcon className="w-4 h-4 text-muted-foreground" />
        <span className="text-foreground font-medium">{dataset.name}</span>
      </nav>

      {/* Header */}
      <div className="flex flex-col gap-4">
        <Button
          variant="ghost"
          size="sm"
          className="w-fit -ml-2 text-muted-foreground hover:text-foreground"
            onClick={() => navigate(backPath)}
        >
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Datasets
        </Button>

        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-lg bg-secondary flex items-center justify-center">
              <Icon className="w-6 h-6 text-primary" />
            </div>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold text-foreground">
                  {dataset.name}
                </h1>
                {datasetIsPublished && (
                  <Badge className="bg-[hsl(var(--haven-success))]/20 text-[hsl(var(--haven-success))] border-[hsl(var(--haven-success))]/30">
                    Published
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-3 mt-1">
                {dataset.status === "preview_ready" ? (
                  <Badge
                    variant="secondary"
                    className="bg-haven-success/20 text-haven-success border-haven-success/30"
                  >
                    Ready
                  </Badge>
                ) : dataset.status === "error" ? (
                  <Badge
                    variant="secondary"
                    className="bg-destructive/20 text-destructive border-destructive/30"
                  >
                    Failed
                  </Badge>
                ) : (
                  <Badge
                    variant="secondary"
                    className="bg-haven-warning/20 text-haven-warning border-haven-warning/30 gap-1"
                  >
                    <Loader2 className="w-3 h-3 animate-spin" />
                    Processing
                  </Badge>
                )}
                <span className="text-sm text-muted-foreground">
                  {formatNumber(dataset.rows)} rows • {dataset.columns} columns
                </span>
                {hasFeature("marketplace") && datasetIsPublished && marketplaceData && (
                  <span className="text-sm font-medium text-primary">
                    ${marketplaceData.price}
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {dataset.status === "preview_ready" && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => navigate(`/sql?dataset=${dataset.id}`)}
              >
                <Code className="w-4 h-4 mr-2" />
                Query
              </Button>
            )}
            {hasFeature("marketplace") && dataset.status === "preview_ready" && !datasetIsPublished && (
              <Button
                variant={channel === "marketplace" || channel === "aim-data" ? "default" : "ghost"}
                size="sm"
                onClick={() => setPublishModalOpen(true)}
                className={`gap-2${
                  channel === "marketplace" || channel === "aim-data" ? " ring-2 ring-primary/30" : ""
                }`}
              >
                <Upload className="w-4 h-4" />
                {channel === "marketplace" || channel === "aim-data" ? "Publish to ai.market" : "Publish"}
              </Button>
            )}
            <Button variant="outline" size="sm" className="text-destructive hover:text-destructive" onClick={handleDelete} disabled={isDeleting}>
              <Trash2 className="w-4 h-4 mr-2" />
              Delete
            </Button>
          </div>
        </div>
      </div>

      {apiDataset.status === "preview_ready" && (
        <DataVerificationFlow
          datasetId={apiDataset.id}
          sourceName={apiDataset.original_filename}
          listingId={apiDataset.listing_id}
        />
      )}

      {/* Error/failed status message */}
      {dataset.status === "error" && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="py-6">
            <div className="flex items-center gap-3">
              <Database className="w-8 h-8 text-destructive" />
              <div>
                <h3 className="text-base font-semibold text-foreground">Processing Failed</h3>
                <p className="text-sm text-muted-foreground mt-1">
                  {apiDataset.error || "This dataset failed during processing. You can delete it and re-upload."}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Processing status message */}
      {dataset.status === "processing" && (
        <Card className="border-primary/50 bg-primary/5">
          <CardContent className="py-6">
            <div className="flex items-center gap-3">
              <Loader2 className="w-8 h-8 text-primary animate-spin" />
              <div>
                <h3 className="text-base font-semibold text-foreground">Processing Dataset</h3>
                <p className="text-sm text-muted-foreground mt-1">
                  This dataset is still being processed. Data will be available once processing completes.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Tabs — only shown when dataset is available */}
      {dataset.status === "preview_ready" && (
      <Tabs defaultValue="overview" className="space-y-6">
        <TabsList className="bg-secondary">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="schema">Schema</TabsTrigger>
          <TabsTrigger value="sample">Sample Data</TabsTrigger>
          <TabsTrigger value="statistics">Statistics</TabsTrigger>
          <TabsTrigger value="readiness">Readiness</TabsTrigger>
          {hasFeature("marketplace") && datasetIsPublished && (
            <TabsTrigger value="marketplace">Marketplace</TabsTrigger>
          )}
        </TabsList>

        {/* Overview Tab */}
        <TabsContent value="overview" className="space-y-6">
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
                    <Rows3 className="w-5 h-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">Total Rows</p>
                    <p className="text-sm font-semibold text-foreground truncate">
                      {formatNumber(dataset.rows)}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
                    <Columns3 className="w-5 h-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">
                      Total Columns
                    </p>
                    <p className="text-sm font-semibold text-foreground truncate">
                      {formatNumber(dataset.columns)}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
                    <HardDrive className="w-5 h-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">File Size</p>
                    <p className="text-sm font-semibold text-foreground truncate">
                      {dataset.size}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
                    <Calendar className="w-5 h-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">Created</p>
                    <p className="text-sm font-semibold text-foreground truncate">
                      {formatDate(dataset.createdAt)}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
                    <Clock className="w-5 h-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">Modified</p>
                    <p className="text-sm font-semibold text-foreground truncate">
                      {formatDate(dataset.modifiedAt)}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
                    <FileType className="w-5 h-5 text-primary" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs text-muted-foreground">File Type</p>
                    <p className="text-sm font-semibold text-foreground uppercase truncate">
                      {dataset.type}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          {dataset.status === "preview_ready" && (
            <Card className="bg-card border-border">
              <CardContent className="py-4">
                <p className="text-sm text-muted-foreground">
                  <Clock className="w-4 h-4 inline mr-2" />
                  Processed in{" "}
                  <span className="text-foreground font-medium">
                    {dataset.processingTime} seconds
                  </span>
                </p>
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {/* Schema Tab */}
        <TabsContent value="schema">
          <Card className="bg-card border-border overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent border-border">
                  <TableHead>Column Name</TableHead>
                  <TableHead>Data Type</TableHead>
                  <TableHead>Non-Null Count</TableHead>
                  <TableHead>Null %</TableHead>
                  <TableHead>Sample Values</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {schema.map((col) => (
                  <TableRow
                    key={col.name}
                    className="border-border hover:bg-secondary/50"
                  >
                    <TableCell className="font-mono text-sm">
                      {col.name}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className={DataTypeColors[col.dataType]}
                      >
                        {col.dataType}
                      </Badge>
                    </TableCell>
                    <TableCell>{formatNumber(col.nonNullCount)}</TableCell>
                    <TableCell>
                      <span
                        className={
                          col.nullPercentage > 0
                            ? "text-haven-warning"
                            : "text-haven-success"
                        }
                      >
                        {col.nullPercentage}%
                      </span>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {col.sampleValues.join(", ")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        </TabsContent>

        {/* Sample Data Tab */}
        <TabsContent value="sample" className="space-y-4">
          <Card className="bg-card border-border overflow-hidden">
            <ScrollArea className="w-full">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent border-border">
                    {(sampleData[0] ? Object.keys(sampleData[0]) : []).map((key) => (
                      <TableHead key={key} className="whitespace-nowrap">
                        {key}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sampleData.map((row, index) => (
                    <TableRow
                      key={index}
                      className="border-border hover:bg-secondary/50"
                    >
                      {Object.values(row).map((value, cellIndex) => (
                        <TableCell
                          key={cellIndex}
                          className="whitespace-nowrap"
                        >
                          {value === null ? (
                            <span className="text-muted-foreground italic">
                              null
                            </span>
                          ) : (
                            String(value)
                          )}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <ScrollBar orientation="horizontal" />
            </ScrollArea>
          </Card>

          {/* Pagination */}
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              Showing rows {formatNumber(startRow)}-{formatNumber(endRow)} of{" "}
              {formatNumber(dataset.rows)}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={currentPage === 1}
                onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
              >
                <ChevronLeft className="w-4 h-4 mr-1" />
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={currentPage >= totalPages}
                onClick={() =>
                  setCurrentPage((p) => Math.min(totalPages, p + 1))
                }
              >
                Next
                <ChevronRight className="w-4 h-4 ml-1" />
              </Button>
            </div>
          </div>
        </TabsContent>

        {/* Statistics Tab */}
        <TabsContent value="statistics" className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {stats.map((stat) => (
              <Card key={stat.name} className="bg-card border-border">
                <CardHeader className="pb-3">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-base font-mono">
                      {stat.name}
                    </CardTitle>
                    <Badge
                      variant="outline"
                      className={DataTypeColors[stat.dataType]}
                    >
                      {stat.dataType}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent>
                  {stat.dataType === "integer" || stat.dataType === "float" ? (
                    <div className="grid grid-cols-5 gap-4 text-center">
                      <div>
                        <p className="text-xs text-muted-foreground">Min</p>
                        <p className="text-sm font-semibold text-foreground">
                          {stat.min?.toLocaleString()}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Max</p>
                        <p className="text-sm font-semibold text-foreground">
                          {stat.max?.toLocaleString()}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Mean</p>
                        <p className="text-sm font-semibold text-foreground">
                          {stat.mean?.toLocaleString(undefined, {
                            maximumFractionDigits: 2,
                          })}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Median</p>
                        <p className="text-sm font-semibold text-foreground">
                          {stat.median?.toLocaleString()}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Std Dev</p>
                        <p className="text-sm font-semibold text-foreground">
                          {stat.stdDev?.toLocaleString(undefined, {
                            maximumFractionDigits: 2,
                          })}
                        </p>
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <p className="text-sm">
                        <span className="text-muted-foreground">
                          Unique values:{" "}
                        </span>
                        <span className="font-semibold text-foreground">
                          {stat.uniqueCount?.toLocaleString()}
                        </span>
                      </p>
                      {stat.mostCommon && (
                        <div>
                          <p className="text-xs text-muted-foreground mb-2">
                            Most common values:
                          </p>
                          <div className="space-y-1">
                            {stat.mostCommon.map((item, i) => (
                              <div
                                key={i}
                                className="flex items-center justify-between text-sm"
                              >
                                <span className="text-foreground truncate max-w-[200px]">
                                  {item.value}
                                </span>
                                <span className="text-muted-foreground">
                                  {item.count.toLocaleString()}
                                </span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        </TabsContent>

        {/* Readiness Tab — BQ-VZ-DATA-READINESS */}
        <TabsContent value="readiness" className="space-y-6">
          {readiness ? (
            <>
              {/* Quality Scorecard */}
              {readiness.quality_scorecard && (
                <Card className="bg-card border-border">
                  <CardHeader>
                    <CardTitle className="text-lg">Quality Scorecard</CardTitle>
                    <CardDescription>
                      Overall Grade: <Badge variant={readiness.quality_scorecard.grade === "A" || readiness.quality_scorecard.grade === "B" ? "default" : "destructive"} className="ml-1">{readiness.quality_scorecard.grade}</Badge>
                      <span className="ml-2 text-muted-foreground">({(readiness.quality_scorecard.overall_score * 100).toFixed(0)}%)</span>
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {(["completeness", "validity", "consistency", "uniqueness"] as const).map((dim) => {
                      const d = readiness.quality_scorecard![dim];
                      return (
                        <div key={dim}>
                          <div className="flex justify-between text-sm mb-1">
                            <span className="capitalize">{dim}</span>
                            <span className={d.score >= 0.8 ? "text-green-400" : d.score >= 0.5 ? "text-yellow-400" : "text-red-400"}>
                              {(d.score * 100).toFixed(0)}%
                            </span>
                          </div>
                          <Progress value={d.score * 100} className="h-2" />
                          {d.details.length > 0 && (
                            <ul className="mt-1 text-xs text-muted-foreground">
                              {d.details.slice(0, 3).map((detail, i) => (
                                <li key={i}>{detail}</li>
                              ))}
                            </ul>
                          )}
                        </div>
                      );
                    })}
                  </CardContent>
                </Card>
              )}

              {/* PII Risk */}
              {readiness.pii_risk && (
                <Card className="bg-card border-border">
                  <CardHeader>
                    <CardTitle className="text-lg">PII Risk Assessment</CardTitle>
                    <CardDescription>
                      Risk: <Badge variant={readiness.pii_risk.overall_risk === "none" ? "default" : readiness.pii_risk.overall_risk === "high" ? "destructive" : "secondary"} className="ml-1">{readiness.pii_risk.overall_risk}</Badge>
                      <span className="ml-2 text-muted-foreground">Privacy Score: {readiness.pii_risk.privacy_score}/10</span>
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="grid grid-cols-2 gap-4 text-sm">
                      <div><span className="text-muted-foreground">Columns with PII:</span> {readiness.pii_risk.columns_with_pii}</div>
                      <div><span className="text-muted-foreground">Clean columns:</span> {readiness.pii_risk.columns_clean}</div>
                    </div>
                    {readiness.pii_risk.column_results && readiness.pii_risk.column_results.length > 0 && (
                      <div className="mt-4 space-y-2">
                        {readiness.pii_risk.column_results.map((col) => (
                          <div key={col.column} className="flex items-center justify-between text-sm border-b border-border pb-2">
                            <span className="font-mono">{col.column}</span>
                            <div className="flex gap-1">
                              {col.pii_types.map((t) => (
                                <Badge key={t} variant="outline" className="text-xs">{t}</Badge>
                              ))}
                              <Badge variant={col.risk_level === "high" ? "destructive" : "secondary"} className="text-xs">{col.risk_level}</Badge>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              )}

              {/* Statistical Profile */}
              {readiness.statistical_profile && (
                <Card className="bg-card border-border">
                  <CardHeader>
                    <CardTitle className="text-lg">Statistical Profile</CardTitle>
                    <CardDescription>
                      {readiness.statistical_profile.row_count.toLocaleString()} rows, {readiness.statistical_profile.column_count} columns
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-3">
                      {readiness.statistical_profile.columns.slice(0, 20).map((col) => (
                        <div key={col.column_name} className="border-b border-border pb-2">
                          <div className="flex justify-between text-sm">
                            <span className="font-mono">{col.column_name}</span>
                            <span className="text-muted-foreground">{col.dtype}</span>
                          </div>
                          <div className="grid grid-cols-3 gap-2 text-xs text-muted-foreground mt-1">
                            <div>Distinct: ~{col.hll_distinct_estimate.toLocaleString()}</div>
                            <div>Null: {(col.null_rate * 100).toFixed(1)}%</div>
                            {col.quantiles && <div>Median: {col.quantiles.p50}</div>}
                          </div>
                          {col.frequent_items && col.frequent_items.length > 0 && (
                            <div className="flex gap-1 mt-1 flex-wrap">
                              {col.frequent_items.slice(0, 5).map((fi, i) => (
                                <Badge key={i} variant="outline" className="text-xs font-mono">
                                  {String(fi.value).slice(0, 20)}
                                </Badge>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </CardContent>
                </Card>
              )}

              {!readiness.quality_scorecard && !readiness.pii_risk && !readiness.statistical_profile && (
                <Card className="bg-card border-border">
                  <CardContent className="p-8 text-center text-muted-foreground">
                    No readiness data available yet. Run the processing pipeline first.
                  </CardContent>
                </Card>
              )}
            </>
          ) : (
            <Card className="bg-card border-border">
              <CardContent className="p-8 text-center text-muted-foreground">
                <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" />
                Loading readiness report...
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {/* Marketplace Tab */}
        {datasetIsPublished && (
          <TabsContent value="marketplace" className="space-y-6">
            {/* Listing Status */}
            <Card className="bg-card border-border">
              <CardContent className="py-6">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <div className="w-12 h-12 rounded-full bg-[hsl(var(--haven-success))]/20 flex items-center justify-center">
                      <TrendingUp className="w-6 h-6 text-[hsl(var(--haven-success))]" />
                    </div>
                    <div>
                      <h3 className="text-lg font-semibold text-foreground">Live on Marketplace</h3>
                      <p className="text-sm text-muted-foreground">
                        Listed at ${marketplaceData?.price || 450}
                      </p>
                    </div>
                  </div>
                  <Button variant="outline" className="gap-2">
                    <ExternalLink className="w-4 h-4" />
                    View Listing
                  </Button>
                </div>
              </CardContent>
            </Card>

            {/* Stats Grid */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Card className="bg-card border-border">
                <CardContent className="p-6">
                  <div className="flex items-center gap-4">
                    <div className="w-12 h-12 rounded-lg bg-secondary flex items-center justify-center">
                      <Eye className="w-6 h-6 text-primary" />
                    </div>
                    <div>
                      <p className="text-sm text-muted-foreground">Views</p>
                      <p className="text-2xl font-bold text-foreground">
                        {marketplaceData?.views || 145}
                      </p>
                    </div>
                  </div>
                </CardContent>
              </Card>

              <Card className="bg-card border-border">
                <CardContent className="p-6">
                  <div className="flex items-center gap-4">
                    <div className="w-12 h-12 rounded-lg bg-secondary flex items-center justify-center">
                      <ShoppingCart className="w-6 h-6 text-primary" />
                    </div>
                    <div>
                      <p className="text-sm text-muted-foreground">Purchases</p>
                      <p className="text-2xl font-bold text-foreground">
                        {marketplaceData?.purchases || 3}
                      </p>
                    </div>
                  </div>
                </CardContent>
              </Card>

              <Card className="bg-card border-border">
                <CardContent className="p-6">
                  <div className="flex items-center gap-4">
                    <div className="w-12 h-12 rounded-lg bg-[hsl(var(--haven-success))]/20 flex items-center justify-center">
                      <DollarSign className="w-6 h-6 text-[hsl(var(--haven-success))]" />
                    </div>
                    <div>
                      <p className="text-sm text-muted-foreground">Earnings</p>
                      <p className="text-2xl font-bold text-[hsl(var(--haven-success))]">
                        ${(marketplaceData?.earnings || 1080).toLocaleString()}
                      </p>
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>

            {/* Actions */}
            <div className="flex gap-3">
              <Button variant="outline" className="gap-2">
                Update Listing
              </Button>
              <Button 
                variant="outline" 
                className="gap-2 text-destructive hover:text-destructive"
                onClick={handleUnpublish}
              >
                Unpublish
              </Button>
            </div>
          </TabsContent>
        )}
      </Tabs>
      )}

      {/* Publish Modal */}
      <PublishModal
        open={publishModalOpen}
        onOpenChange={setPublishModalOpen}
        dataset={dataset}
        onPublishSuccess={handlePublishSuccess}
      />
    </div>
  );
};

export default DatasetDetail;
