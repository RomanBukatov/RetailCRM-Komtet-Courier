import type {
  ConnectionCheckResult,
  DictionariesResponse,
  ManualSyncResponse,
  MappingItem,
  MappingsResponse,
  OrderSyncListResponse,
  SettingsResponse,
  SettingsUpdateRequest,
  SyncLogsResponse,
} from "./types";

const TOKEN_STORAGE_KEY = "komtet_retailcrm_admin_token";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getStoredToken(): string {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setStoredToken(token: string): void {
  try {
    const clean = token.trim();
    if (clean) {
      localStorage.setItem(TOKEN_STORAGE_KEY, clean);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // ignore storage errors
  }
}

async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getStoredToken();
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.headers as Record<string, string> | undefined),
  };

  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  let data: unknown = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  } else {
    const text = await response.text();
    data = text ? { detail: text } : null;
  }

  if (!response.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? typeof (data as { detail: unknown }).detail === "string"
          ? (data as { detail: string }).detail
          : JSON.stringify((data as { detail: unknown }).detail)
        : `Ошибка HTTP ${response.status}`;
    throw new ApiError(detail, response.status);
  }

  return data as T;
}

export const api = {
  getSettings(): Promise<SettingsResponse> {
    return apiFetch<SettingsResponse>("/api/v1/settings");
  },

  updateSettings(payload: SettingsUpdateRequest): Promise<SettingsResponse> {
    return apiFetch<SettingsResponse>("/api/v1/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  testConnection(): Promise<ConnectionCheckResult> {
    return apiFetch<ConnectionCheckResult>("/api/v1/settings/test-connection", {
      method: "POST",
    });
  },

  getDictionaries(): Promise<DictionariesResponse> {
    return apiFetch<DictionariesResponse>("/api/v1/dictionaries");
  },

  getMappings(mappingType?: string): Promise<MappingsResponse> {
    const query = mappingType
      ? `?mapping_type=${encodeURIComponent(mappingType)}`
      : "";
    return apiFetch<MappingsResponse>(`/api/v1/mappings${query}`);
  },

  saveMappings(
    items: MappingItem[],
    replaceTypes: string[],
  ): Promise<MappingsResponse> {
    return apiFetch<MappingsResponse>("/api/v1/mappings", {
      method: "PUT",
      body: JSON.stringify({
        items,
        replace_types: replaceTypes,
      }),
    });
  },

  getOrdersSync(params: {
    limit?: number;
    offset?: number;
    sync_status?: string;
  } = {}): Promise<OrderSyncListResponse> {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    if (params.sync_status) search.set("sync_status", params.sync_status);
    const qs = search.toString();
    return apiFetch<OrderSyncListResponse>(
      `/api/v1/orders-sync${qs ? `?${qs}` : ""}`,
    );
  },

  manualSyncOrder(
    orderId: number,
    ignoreStatusFilter = false,
  ): Promise<ManualSyncResponse> {
    const qs = ignoreStatusFilter ? "?ignore_status_filter=true" : "";
    return apiFetch<ManualSyncResponse>(
      `/api/v1/sync/manual/${encodeURIComponent(orderId)}${qs}`,
      { method: "POST" },
    );
  },

  getLogs(params: {
    limit?: number;
    offset?: number;
    status?: string;
    direction?: string;
    order_id?: number;
  } = {}): Promise<SyncLogsResponse> {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    if (params.status && params.status !== "all") {
      search.set("status", params.status);
    }
    if (params.direction && params.direction !== "all") {
      search.set("direction", params.direction);
    }
    if (params.order_id !== undefined && params.order_id > 0) {
      search.set("order_id", String(params.order_id));
    }
    const qs = search.toString();
    return apiFetch<SyncLogsResponse>(`/api/v1/logs${qs ? `?${qs}` : ""}`);
  },
};
