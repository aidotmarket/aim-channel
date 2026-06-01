/**
 * BQ-VZ-SHARED-SEARCH: Portal API Client
 *
 * Separate from admin API — calls /api/portal/* endpoints.
 * Uses portal JWT (Bearer token) instead of X-API-Key.
 */

import { getApiUrl } from "@/lib/api";

const PORTAL_TOKEN_KEY = "vectoraiz_portal_token";

// ---------------------------------------------------------------------------
// Token management
// ---------------------------------------------------------------------------

export function getPortalToken(): string | null {
  return sessionStorage.getItem(PORTAL_TOKEN_KEY);
}

export function setPortalToken(token: string): void {
  sessionStorage.setItem(PORTAL_TOKEN_KEY, token);
}

export function clearPortalToken(): void {
  sessionStorage.removeItem(PORTAL_TOKEN_KEY);
}

// ---------------------------------------------------------------------------
// Fetch helper (portal-specific, no X-API-Key)
// ---------------------------------------------------------------------------

async function portalFetch<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${getApiUrl()}${endpoint}`;
  const token = getPortalToken();

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(url, { ...options, headers, credentials: "omit" });

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const error = new Error(body.detail || `Portal API error: ${response.status}`);
    (error as any).status = response.status;
    throw error;
  }

  return response.json();
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PortalPublicConfig {
  enabled: boolean;
  tier: "open" | "code" | "sso";
  name: string;
}

export interface PortalAuthResponse {
  token: string;
  expires_at: string;
  tier: string;
}

export interface PortalDataset {
  dataset_id: string;
  name: string;
  description: string | null;
  row_count: number;
}

export interface PortalSSOUserInfo {
  email: string | null;
  name: string | null;
  subject: string | null;
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export const portalApi = {
  /** Get public portal config (no auth) */
  getConfig: () => portalFetch<PortalPublicConfig>("/api/portal/config"),

  /** Authenticate with access code */
  authWithCode: async (code: string): Promise<PortalAuthResponse> => {
    const res = await portalFetch<PortalAuthResponse>("/api/portal/auth/code", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    setPortalToken(res.token);
    return res;
  },

  /** Initiate SSO login (redirects to IdP) */
  initiateSSO: () => {
    window.location.href = `${getApiUrl()}/api/portal/auth/sso/authorize`;
  },

  /** Get SSO user info */
  getSSOUserInfo: () => portalFetch<PortalSSOUserInfo>("/api/portal/auth/sso/userinfo"),

  /** Logout SSO session */
  ssoLogout: async (): Promise<{ message: string; end_session_url: string | null }> => {
    const res = await portalFetch<{ message: string; end_session_url: string | null }>(
      "/api/portal/auth/sso/logout",
      { method: "POST" }
    );
    clearPortalToken();
    return res;
  },

  /** List portal-visible datasets */
  getDatasets: () =>
    portalFetch<{ datasets: PortalDataset[] }>("/api/portal/datasets"),
};
