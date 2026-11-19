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

interface FastApiValidationItem {
  loc?: Array<string | number>;
  msg?: string;
  type?: string;
}

function isHtmlPayload(text: string, contentType: string): boolean {
  if (contentType.toLowerCase().includes("text/html")) {
    return true;
  }
  const trimmed = text.trimStart().toLowerCase();
  return (
    trimmed.startsWith("<!doctype html") ||
    trimmed.startsWith("<html") ||
    trimmed.startsWith("<head") ||
    trimmed.startsWith("<body")
  );
}

function formatHttpHtmlError(status: number): string {
  switch (status) {
    case 502:
      return "Ошибка сервера (HTTP 502): сервис временно недоступен";
    case 503:
      return "Ошибка сервера (HTTP 503): сервис временно недоступен";
    case 504:
      return "Ошибка сервера (HTTP 504): превышено время ожидания ответа";
    default:
      return `Ошибка сервера (HTTP ${status})`;
  }
}

function formatFastApiDetail(detail: unknown, status: number): string {
  if (typeof detail === "string") {
    const cleaned = detail.trim();
    return cleaned || `Ошибка HTTP ${status}`;
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        if (typeof entry === "string") return entry.trim();
        if (entry && typeof entry === "object") {
          const item = entry as FastApiValidationItem;
          const rawMsg = typeof item.msg === "string" ? item.msg.trim() : "";
          const cleanMsg = rawMsg.replace(/^Value error,\s*/i, "");
          const rawLoc = Array.isArray(item.loc) ? item.loc : [];
          const filteredLoc = rawLoc.filter(
            (seg, idx) =>
              !(
                idx === 0 &&
                typeof seg === "string" &&
                ["body", "query", "path", "header"].includes(seg)
              ),
          );
          const fieldPath = filteredLoc.map(String).join(".");
          if (fieldPath && cleanMsg) {
            return `${fieldPath}: ${cleanMsg}`;
          }
          if (cleanMsg) {
            return cleanMsg;
          }
        }
        return "";
      })
      .filter(Boolean);
    if (parts.length > 0) {
      return parts.join("; ");
    }
  }
  if (detail !== null && detail !== undefined) {
    try {
      return JSON.stringify(detail);
    } catch {
      return `Ошибка HTTP ${status}`;
    }
  }
  return `Ошибка HTTP ${status}`;
}

async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
  tokenOverride?: string,
): Promise<T> {
  const token =
    tokenOverride !== undefined ? tokenOverride.trim() : getStoredToken();
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

  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      headers,
    });
  } catch {
    throw new ApiError(
      "Нет связи с сервером. Проверьте подключение и доступность API.",
      0,
    );
  }

  let data: unknown = null;
  let htmlError = false;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.toLowerCase().includes("application/json")) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  } else {
    const text = await response.text();
    if (isHtmlPayload(text, contentType)) {
      htmlError = true;
    } else if (text.trim()) {
      data = { detail: text.trim() };
    }
  }

  if (!response.ok) {
    if (htmlError) {
      throw new ApiError(formatHttpHtmlError(response.status), response.status);
    }
    const detail =
      data && typeof data === "object" && "detail" in data
        ? formatFastApiDetail((data as { detail: unknown }).detail, response.status)
        : `Ошибка HTTP ${response.status}`;
    throw new ApiError(detail, response.status);
  }

  if (htmlError) {
    throw new ApiError(
      `Неожиданный HTML-ответ от сервера (HTTP ${response.status})`,
      response.status,
    );
  }

  return data as T;
}

export const api = {
  getSettings(tokenOverride?: string): Promise<SettingsResponse> {
    return apiFetch<SettingsResponse>("/api/v1/settings", {}, tokenOverride);
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
