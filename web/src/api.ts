const TOKEN_KEY = "feedsentry_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);
  const token = getToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (
    options.body &&
    typeof options.body === "string" &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, { ...options, headers });

  if (response.status === 401) {
    clearToken();
    if (window.location.pathname !== "/login") {
      window.location.assign("/login");
    }
  }

  return response;
}

export async function apiJson<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await apiFetch(path, options);
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (body.detail != null) {
        detail = JSON.stringify(body.detail);
      }
    } catch {
      /* keep default */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export type SourceView = {
  id: string;
  kind: string;
  feed_url: string;
  enabled: boolean;
  title: string | null;
  page_url: string | null;
  route: string | null;
  initialized_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  next_check_at: string | null;
  last_error: string | null;
};

export type SystemStatus = {
  sources: number;
  enabled_sources: number;
  pending_events: number;
  failed_events: number;
  config_error: string | null;
  source_statuses: SourceView[];
  last_tick_at: string | null;
  status_counts: Record<string, number>;
};

export type CandidateView = {
  candidate_id: string;
  title: string;
  page_url: string;
  feed_url: string;
};

export type AddSourceResult = {
  source: SourceView;
  created: boolean;
  baseline_initialized: boolean;
};

export type SourcesResponse = {
  sources: SourceView[];
};

export type DiscoverResponse = {
  candidates: CandidateView[];
};

export type CheckSourceResponse = {
  created_events: number;
};

export type ChangedResponse = {
  changed: boolean;
};

export type RemovedResponse = {
  removed: boolean;
};

export async function getStatus(): Promise<SystemStatus> {
  return apiJson<SystemStatus>("/api/status");
}

export async function listSources(): Promise<SourcesResponse> {
  return apiJson<SourcesResponse>("/api/sources");
}

export async function setSourceEnabled(
  id: string,
  enabled: boolean,
): Promise<ChangedResponse> {
  return apiJson<ChangedResponse>(
    `/api/sources/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    },
  );
}

export async function removeSource(id: string): Promise<RemovedResponse> {
  return apiJson<RemovedResponse>(
    `/api/sources/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
}

export async function checkSource(id: string): Promise<CheckSourceResponse> {
  return apiJson<CheckSourceResponse>(
    `/api/sources/${encodeURIComponent(id)}/check`,
    { method: "POST" },
  );
}

export async function addFeed(url: string): Promise<AddSourceResult> {
  return apiJson<AddSourceResult>("/api/feeds", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function discoverFeeds(
  pageUrl: string,
): Promise<DiscoverResponse> {
  return apiJson<DiscoverResponse>("/api/feeds/discover", {
    method: "POST",
    body: JSON.stringify({ page_url: pageUrl }),
  });
}

export async function subscribeFeed(
  candidateId: string,
): Promise<AddSourceResult> {
  return apiJson<AddSourceResult>("/api/feeds/subscribe", {
    method: "POST",
    body: JSON.stringify({ candidate_id: candidateId }),
  });
}

export function errorDetail(err: unknown, fallback = "操作失败"): string {
  if (err instanceof ApiError) {
    return err.detail;
  }
  if (err instanceof TypeError) {
    return "无法连接后端，请确认服务已启动";
  }
  if (err instanceof Error && err.message) {
    return err.message;
  }
  return fallback;
}
