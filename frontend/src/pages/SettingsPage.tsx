import { useState, useEffect, useCallback } from "react";
import {
  HardDrive,
  Eye,
  EyeOff,
  FolderOpen,
  Trash2,
  ExternalLink,
  RefreshCw,
  CheckCircle,
  XCircle,
  Cpu,
  ShoppingBag,
  Server,
  Loader2,
  Wifi,
  WifiOff,
  KeyRound,
  Plus,
  Copy,
  LogOut,
  ArrowUpCircle,
  Download,
  Terminal,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/hooks/use-toast";
import { getApiUrl, systemApi } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { useBrand } from "@/contexts/BrandContext";
import { useMode } from "@/contexts/ModeContext";
import ConnectivitySettings from "@/components/ConnectivitySettings";

// Empty string = same-origin (relative URLs). Works on Railway, Docker, etc.
const DEFAULT_API_URL = '';

const SettingsPage = () => {
  const brand = useBrand();
  const defaultDataDirectory = `~/${brand.productName.toLowerCase().replace(/\s+/g, "-")}/data`;
  const manualUpdateCommands = `cd your-${brand.installDirectoryName}-directory
docker compose -f docker-compose.customer.yml pull ${brand.dockerComposeServiceName}
docker compose -f docker-compose.customer.yml up -d ${brand.dockerComposeServiceName}`;
  // Dirty state tracking for Save button
  const [isDirty, setIsDirty] = useState(false);

  // Backend URL state — empty string means "same origin" (relative URLs)
  const [apiUrl, setApiUrl] = useState(() =>
    localStorage.getItem('vectoraiz_api_url') ?? DEFAULT_API_URL
  );
  const [backendTestStatus, setBackendTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
  const [backendErrorMessage, setBackendErrorMessage] = useState('');

  // Processing state
  const [memoryLimit, setMemoryLimit] = useState([12]);
  const [concurrentUploads, setConcurrentUploads] = useState(() =>
    localStorage.getItem('vectoraiz_concurrent_uploads') ?? 'auto'
  );
  const [recommendedConcurrent, setRecommendedConcurrent] = useState<number | null>(null);
  const [systemCores, setSystemCores] = useState<number | null>(null);
  const [systemMemGb, setSystemMemGb] = useState<number | null>(null);
  
  // Storage state
  const [dataDirectory, setDataDirectory] = useState(defaultDataDirectory);
  
  // Marketplace state
  const [marketplaceKey, setMarketplaceKey] = useState("");
  const [showMarketplaceKey, setShowMarketplaceKey] = useState(false);
  const [marketplaceConnected, setMarketplaceConnected] = useState(false);

  // PII Settings state (BQ-VZ-DATA-READINESS)
  const [piiThreshold, setPiiThreshold] = useState([0.5]);
  const [piiExcludedPatterns, setPiiExcludedPatterns] = useState("");
  const [piiSettingsLoading, setPiiSettingsLoading] = useState(false);

  // Local API Keys management state
  const { logout } = useAuth();
  const { hasFeature } = useMode();
  interface ApiKeyInfo { key_id: string; label: string; scopes: string[]; created_at: string; last_used_at: string | null; revoked: boolean; }
  const [localKeys, setLocalKeys] = useState<ApiKeyInfo[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [newKeyLabel, setNewKeyLabel] = useState("");
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [showCreatedKeyDialog, setShowCreatedKeyDialog] = useState(false);

  // Software update state
  interface VersionInfo {
    current: string;
    latest: string | null;
    update_available: boolean;
    latest_published_at: string | null;
    changelog_url: string;
    can_auto_update: boolean;
    error?: string;
  }
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);
  const [versionLoading, setVersionLoading] = useState(false);
  const [updateStatus, setUpdateStatus] = useState<"idle" | "updating" | "done" | "error">("idle");
  const [updateMessage, setUpdateMessage] = useState("");
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const fetchLocalKeys = useCallback(async () => {
    setKeysLoading(true);
    try {
      const storedKey = localStorage.getItem('vectoraiz_api_key');
      if (!storedKey) return;
      const res = await fetch(`${getApiUrl()}/api/auth/keys`, {
        headers: { 'X-API-Key': storedKey },
      });
      if (res.ok) {
        const data = await res.json();
        setLocalKeys(data);
      }
    } catch {
      // Silently fail
    } finally {
      setKeysLoading(false);
    }
  }, []);

  const fetchVersionInfo = useCallback(async (force = false) => {
    setVersionLoading(true);
    try {
      const url = `${getApiUrl()}/api/version${force ? "?force=true" : ""}`;
      const res = await fetch(url);
      if (res.ok) {
        const data: VersionInfo = await res.json();
        setVersionInfo(data);
        setLastChecked(new Date());
      }
    } catch {
      // Silently fail
    } finally {
      setVersionLoading(false);
    }
  }, []);

  const triggerUpdate = async () => {
    setUpdateStatus("updating");
    setUpdateMessage("Pulling latest image... This may take a minute.");
    try {
      const storedKey = localStorage.getItem("vectoraiz_api_key");
      if (!storedKey) {
        setUpdateStatus("error");
        setUpdateMessage("Authentication required. Please sign in.");
        return;
      }
      const res = await fetch(`${getApiUrl()}/api/version/update`, {
        method: "POST",
        headers: { "X-API-Key": storedKey },
      });
      const data = await res.json();
      if (data.status === "updating") {
        setUpdateStatus("done");
        setUpdateMessage("Update downloaded. Restarting...");
        // Auto-refresh after 10 seconds to pick up the new version
        setTimeout(() => window.location.reload(), 10000);
      } else if (data.status === "up_to_date") {
        setUpdateStatus("idle");
        toast({ title: "Up to date", description: data.message });
      } else if (data.status === "docker_not_available") {
        setUpdateStatus("error");
        setUpdateMessage(data.message);
      } else {
        setUpdateStatus("error");
        setUpdateMessage(data.message || "Update failed.");
      }
    } catch (e) {
      setUpdateStatus("error");
      setUpdateMessage(e instanceof Error ? e.message : "Update request failed.");
    }
  };

  const createLocalKey = async () => {
    try {
      const storedKey = localStorage.getItem('vectoraiz_api_key');
      if (!storedKey) return;
      const res = await fetch(`${getApiUrl()}/api/auth/keys`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': storedKey },
        body: JSON.stringify({ label: newKeyLabel.trim() || 'My API Key' }),
      });
      if (res.ok) {
        const data = await res.json();
        setCreatedKey(data.full_key);
        setShowCreatedKeyDialog(true);
        setNewKeyLabel("");
        fetchLocalKeys();
      } else {
        const err = await res.json().catch(() => ({ detail: 'Failed to create key' }));
        toast({ title: "Error", description: err.detail, variant: "destructive" });
      }
    } catch {
      toast({ title: "Error", description: "Failed to create API key", variant: "destructive" });
    }
  };

  const revokeLocalKey = async (keyId: string) => {
    try {
      const storedKey = localStorage.getItem('vectoraiz_api_key');
      if (!storedKey) return;
      const res = await fetch(`${getApiUrl()}/api/auth/keys/${keyId}`, {
        method: 'DELETE',
        headers: { 'X-API-Key': storedKey },
      });
      if (res.ok) {
        toast({ title: "Key revoked", description: `API key ${keyId} has been revoked.` });
        fetchLocalKeys();
      } else {
        const err = await res.json().catch(() => ({ detail: 'Failed to revoke key' }));
        toast({ title: "Error", description: err.detail, variant: "destructive" });
      }
    } catch {
      toast({ title: "Error", description: "Failed to revoke API key", variant: "destructive" });
    }
  };

  // Resolve display/test URL: empty string → current origin
  const resolvedUrl = apiUrl || window.location.origin;

  // Test backend connection on mount
  useEffect(() => {
    const testInitialConnection = async () => {
      try {
        const url = apiUrl || window.location.origin;
        const response = await fetch(`${url}/api/health`);
        if (response.ok) {
          setBackendTestStatus('success');
        }
      } catch {
        // Silently fail on initial check
      }
    };
    testInitialConnection();
  }, []);

  // Fetch local API keys on mount
  useEffect(() => {
    fetchLocalKeys();
  }, [fetchLocalKeys]);

  // Fetch version info on mount
  useEffect(() => {
    fetchVersionInfo();
  }, [fetchVersionInfo]);

  // Fetch system info for recommended concurrent uploads
  useEffect(() => {
    systemApi.info().then((info) => {
      const rec = info.system.recommended_concurrent_uploads;
      setRecommendedConcurrent(rec);
      setSystemCores(info.system.cpu_cores);
      setSystemMemGb(info.system.memory_gb);
      localStorage.setItem('vectoraiz_recommended_concurrent', String(rec));
    }).catch(() => {
      // Fallback: leave recommendation as null
    });
  }, []);

  // Fetch PII settings on mount (BQ-VZ-DATA-READINESS)
  useEffect(() => {
    const url = apiUrl || window.location.origin;
    const accessToken = localStorage.getItem('aim_data_access_token');
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (accessToken) headers['Authorization'] = `Bearer ${accessToken}`;
    fetch(`${url}/api/pii/settings`, { headers })
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data) {
          setPiiThreshold([data.score_threshold ?? 0.5]);
          setPiiExcludedPatterns((data.excluded_patterns ?? []).join(", "));
        }
      })
      .catch(() => {});
  }, [apiUrl]);

  const savePiiSettings = async () => {
    setPiiSettingsLoading(true);
    try {
      const url = apiUrl || window.location.origin;
      const accessToken = localStorage.getItem('aim_data_access_token');
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (accessToken) headers['Authorization'] = `Bearer ${accessToken}`;
      const body = {
        score_threshold: piiThreshold[0],
        excluded_patterns: piiExcludedPatterns.split(",").map((s: string) => s.trim()).filter(Boolean),
        entity_overrides: {},
      };
      await fetch(`${url}/api/pii/settings`, { method: 'PUT', headers, body: JSON.stringify(body) });
    } finally {
      setPiiSettingsLoading(false);
    }
  };

  // Validate URL format
  const isValidUrl = (url: string): boolean => {
    try {
      const parsed = new URL(url);
      return parsed.protocol === 'http:' || parsed.protocol === 'https:';
    } catch {
      return false;
    }
  };

  const testBackendConnection = async () => {
    // When apiUrl is non-empty, validate it's a proper URL
    if (apiUrl && !isValidUrl(apiUrl)) {
      setBackendTestStatus('error');
      setBackendErrorMessage('Invalid URL format. Use http:// or https://');
      return;
    }

    setBackendTestStatus('testing');
    setBackendErrorMessage('');

    const testUrl = apiUrl || window.location.origin;

    try {
      const response = await fetch(`${testUrl}/api/health`, {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
      });

      if (response.ok) {
        setBackendTestStatus('success');
        // Auto-save on successful connection
        if (apiUrl) {
          localStorage.setItem('vectoraiz_api_url', apiUrl);
        } else {
          localStorage.removeItem('vectoraiz_api_url');
        }
        toast({
          title: "Backend connected",
          description: `Successfully connected to ${brand.name} backend.`,
        });
      } else {
        setBackendTestStatus('error');
        setBackendErrorMessage(`Server returned ${response.status}`);
      }
    } catch (e) {
      setBackendTestStatus('error');
      setBackendErrorMessage(e instanceof Error ? e.message : 'Connection failed');
    }
  };

  const handleResetBackendUrl = () => {
    setApiUrl(DEFAULT_API_URL);
    localStorage.removeItem('vectoraiz_api_url');
    setBackendTestStatus('idle');
    toast({
      title: "Reset to default",
      description: "Backend URL reset to same-origin (auto-detect)",
    });
  };

  const handleSaveBackendUrl = () => {
    if (apiUrl) {
      localStorage.setItem('vectoraiz_api_url', apiUrl);
    } else {
      localStorage.removeItem('vectoraiz_api_url');
    }
    toast({
      title: "Backend URL saved",
      description: apiUrl ? "Your backend URL has been saved." : "Using same-origin (auto-detect).",
    });
  };

  const handleSaveSettings = () => {
    setIsDirty(false);
    toast({
      title: "Settings saved successfully",
      description: "Your configuration has been updated.",
    });
  };

  const handleClearCache = () => {
    toast({
      title: "Cache cleared",
      description: "All cached data has been removed.",
    });
  };

  return (
    <div className="space-y-6 max-w-3xl pb-20">
      <div>
        <p className="text-muted-foreground">
          Configure {brand.settingsTitle} to match your workflow
        </p>
      </div>

      {/* Section 2: Processing Settings */}
      <Card className="bg-card border-border">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
              <Cpu className="w-5 h-5 text-primary" />
            </div>
            <div>
              <CardTitle className="text-foreground">Processing</CardTitle>
              <CardDescription>Configure data processing settings</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <Label className="text-foreground">Memory Limit</Label>
              <span className="text-sm font-mono text-primary">{memoryLimit[0]} GB</span>
            </div>
            <Slider
              value={memoryLimit}
              onValueChange={(v) => { setMemoryLimit(v); setIsDirty(true); }}
              min={4}
              max={32}
              step={2}
              className="w-full"
            />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>4 GB</span>
              <span>32 GB</span>
            </div>
            <p className="text-sm text-muted-foreground">
              Maximum memory for data processing. Higher values allow larger files.
            </p>
          </div>

          <div className="space-y-2 pt-2 border-t border-border">
            <Label className="text-foreground">Concurrent Uploads</Label>
            <Select value={concurrentUploads} onValueChange={(v) => {
              setConcurrentUploads(v);
              setIsDirty(true);
              if (v === 'auto') {
                localStorage.removeItem('vectoraiz_concurrent_uploads');
              } else {
                localStorage.setItem('vectoraiz_concurrent_uploads', v);
              }
            }}>
              <SelectTrigger className="bg-background border-border w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Auto{recommendedConcurrent ? ` (${recommendedConcurrent})` : ''}</SelectItem>
                <SelectItem value="1">1</SelectItem>
                <SelectItem value="2">2</SelectItem>
                <SelectItem value="3">3</SelectItem>
                <SelectItem value="4">4</SelectItem>
                <SelectItem value="5">5</SelectItem>
                <SelectItem value="6">6</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Number of files uploaded simultaneously.
              {systemCores !== null && systemMemGb !== null && (
                <> Detected: {systemCores} cores, {systemMemGb} GB RAM.</>
              )}
            </p>
          </div>
        </CardContent>
      </Card>

      {/* PII Detection Settings (BQ-VZ-DATA-READINESS) */}
      <Card className="bg-card border-border">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
              <Eye className="w-5 h-5 text-primary" />
            </div>
            <div>
              <CardTitle className="text-foreground">PII Detection</CardTitle>
              <p className="text-sm text-muted-foreground">Configure PII scanning thresholds and exclusions</p>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label>Confidence Threshold: {piiThreshold[0].toFixed(2)}</Label>
            <Slider
              value={piiThreshold}
              onValueChange={setPiiThreshold}
              min={0.1}
              max={1.0}
              step={0.05}
            />
            <p className="text-xs text-muted-foreground">
              Higher values reduce false positives. Lower values catch more potential PII.
            </p>
          </div>
          <div className="space-y-2">
            <Label>Excluded Patterns (comma-separated)</Label>
            <Input
              placeholder="SENSOR-, DEV-, TEST-"
              value={piiExcludedPatterns}
              onChange={(e) => setPiiExcludedPatterns(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Patterns that should not be flagged as PII (e.g. sensor IDs, device codes).
            </p>
          </div>
          <Button onClick={savePiiSettings} disabled={piiSettingsLoading} size="sm">
            {piiSettingsLoading ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : null}
            Save PII Settings
          </Button>
        </CardContent>
      </Card>

      {/* Section 3: Storage */}
      <Card className="bg-card border-border">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
              <HardDrive className="w-5 h-5 text-primary" />
            </div>
            <div>
              <CardTitle className="text-foreground">Storage</CardTitle>
              <CardDescription>Configure data storage location</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="data-dir" className="text-foreground">Data Directory</Label>
            <div className="flex gap-2">
              <Input
                id="data-dir"
                type="text"
                value={dataDirectory}
                onChange={(e) => { setDataDirectory(e.target.value); setIsDirty(true); }}
                className="bg-background border-border text-foreground font-mono"
              />
              <Button variant="outline" className="gap-2">
                <FolderOpen className="w-4 h-4" />
                Browse
              </Button>
            </div>
          </div>

          <div className="space-y-2 pt-2 border-t border-border">
            <div className="flex items-center justify-between">
              <Label className="text-foreground">Current Usage</Label>
              <span className="text-sm text-muted-foreground">2.4 GB of 100 GB used</span>
            </div>
            <Progress value={2.4} max={100} className="h-2" />
          </div>

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="outline" className="gap-2 text-destructive hover:text-destructive">
                <Trash2 className="w-4 h-4" />
                Clear Cache
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Clear Cache</AlertDialogTitle>
                <AlertDialogDescription>
                  This will remove all cached data including temporary files and processed data. 
                  Your original datasets will not be affected. This action cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={handleClearCache} className="bg-destructive hover:bg-destructive/90">
                  Clear Cache
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </CardContent>
      </Card>

      {/* Section 6: Software Updates & About */}
      <Card className="bg-card border-border">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-secondary flex items-center justify-center">
              <ArrowUpCircle className="w-5 h-5 text-primary" />
            </div>
            <div>
              <CardTitle className="text-foreground">Software Updates</CardTitle>
              <CardDescription>Application information and updates</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Version info */}
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-muted-foreground">Current Version</span>
              <span className="text-foreground font-mono">{versionInfo?.current?.replace(/^v/, '') ?? "..."}</span>
            </div>
            <div className="flex justify-between text-sm items-center">
              <span className="text-muted-foreground">Latest Version</span>
              <div className="flex items-center gap-2">
                <span className="text-foreground font-mono">{versionInfo?.latest ?? "..."}</span>
                {versionInfo?.update_available && (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-[hsl(var(--haven-warning))]/15 text-[hsl(var(--haven-warning))]">
                    Update available
                  </span>
                )}
                {versionInfo && !versionInfo.update_available && !versionInfo.error && (
                  <span className="inline-flex items-center gap-1 text-xs text-[hsl(var(--haven-success))]">
                    <CheckCircle className="w-3.5 h-3.5" />
                    Up to date
                  </span>
                )}
              </div>
            </div>
            {lastChecked && (
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Last Checked</span>
                <span className="text-muted-foreground text-xs">
                  {lastChecked.toLocaleTimeString()}
                </span>
              </div>
            )}
          </div>

          {/* Error state */}
          {versionInfo?.error && (
            <div className="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/30 rounded-lg">
              <XCircle className="w-4 h-4 text-destructive mt-0.5 flex-shrink-0" />
              <p className="text-sm text-destructive">
                Could not check for updates: {versionInfo.error}
              </p>
            </div>
          )}

          {/* Update status messages */}
          {updateStatus === "updating" && (
            <div className="flex items-center gap-2 p-3 bg-primary/10 border border-primary/30 rounded-lg">
              <Loader2 className="w-4 h-4 text-primary animate-spin flex-shrink-0" />
              <p className="text-sm text-primary">{updateMessage}</p>
            </div>
          )}
          {updateStatus === "done" && (
            <div className="flex items-center gap-2 p-3 bg-[hsl(var(--haven-success))]/10 border border-[hsl(var(--haven-success))]/30 rounded-lg">
              <CheckCircle className="w-4 h-4 text-[hsl(var(--haven-success))] flex-shrink-0" />
              <p className="text-sm text-[hsl(var(--haven-success))]">{updateMessage}</p>
            </div>
          )}
          {updateStatus === "error" && (
            <div className="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/30 rounded-lg">
              <XCircle className="w-4 h-4 text-destructive mt-0.5 flex-shrink-0" />
              <p className="text-sm text-destructive whitespace-pre-wrap">{updateMessage}</p>
            </div>
          )}

          {/* Manual update instructions (when Docker socket not available) */}
          {versionInfo && !versionInfo.can_auto_update && versionInfo.update_available && (
            <div className="space-y-2 p-3 bg-secondary/50 rounded-lg">
              <div className="flex items-center gap-2">
                <Terminal className="w-4 h-4 text-muted-foreground" />
                <span className="text-sm font-medium text-foreground">Manual update</span>
              </div>
              <pre className="text-xs font-mono bg-background p-2 rounded border border-border overflow-x-auto">
{manualUpdateCommands}
              </pre>
            </div>
          )}

          {/* Action buttons */}
          <div className="flex gap-2">
            <Button
              variant="outline"
              className="gap-2"
              disabled={versionLoading}
              onClick={() => fetchVersionInfo(true)}
            >
              {versionLoading ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
              Check Now
            </Button>
            {versionInfo?.update_available && versionInfo?.can_auto_update && (
              <Button
                className="gap-2"
                disabled={updateStatus === "updating" || updateStatus === "done"}
                onClick={triggerUpdate}
              >
                {updateStatus === "updating" ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Download className="w-4 h-4" />
                )}
                Update to {versionInfo.latest}
              </Button>
            )}
          </div>

          {/* About footer */}
          <div className="pt-3 border-t border-border space-y-3">
            <p className="text-sm text-muted-foreground">
              {brand.name}{versionInfo?.current ? ` ${versionInfo.current.replace(/^v/, '')}` : ""}
            </p>
            <div className="flex flex-wrap gap-4">
              <a
                href={brand.documentationUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
              >
                Documentation
                <ExternalLink className="w-3 h-3" />
              </a>
              <a
                href={brand.issueTrackerUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
              >
                Report Issue
                <ExternalLink className="w-3 h-3" />
              </a>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Sticky Save Button */}
      <div className="fixed bottom-0 left-0 right-0 p-4 bg-background/95 backdrop-blur border-t border-border">
        <div className="max-w-3xl mx-auto flex justify-end">
          <Button onClick={handleSaveSettings} disabled={!isDirty} className="gap-2">
            Save Settings
          </Button>
        </div>
      </div>
    </div>
  );
};

export default SettingsPage;
