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

const VALIDATION_FIELD_LABELS: Record<string, string> = {
  retailcrm_url: "Адрес кабинета RetailCRM",
  retailcrm_api_key: "API-ключ RetailCRM",
  retailcrm_default_site: "Код магазина в RetailCRM по умолчанию",
  komtet_base_url: "Адрес сервиса КОМТЕТ Касса",
  komtet_shop_id: "Идентификатор магазина КОМТЕТ",
  komtet_secret_key: "Секретный ключ магазина КОМТЕТ",
  komtet_queue_id: "ID кассовой очереди",
  komtet_api_version: "Версия интеграции КОМТЕТ",
  tax_system: "Система налогообложения (СНО)",
  default_vat: "Ставка НДС по умолчанию",
  payment_address: "Место расчётов",
  fallback_client_email: "Резервный email покупателя",
  company_inn: "ИНН организации / ИП",
  trigger_statuses: "Статусы для отправки заказа",
  cancel_statuses: "Статусы отмены заказа",
  allowed_delivery_codes: "Типы доставки для передачи в КОМТЕТ",
  final_status: "Статус после пробития чека",
  default_delivery_time_from: "Время доставки «с»",
  default_delivery_time_to: "Время доставки «до»",
  komtet_callback_url: "Адрес для обратных уведомлений от КОМТЕТ",
  require_mark_code: "Запрашивать сканирование маркировки (Честный ЗНАК)",
  verify_komtet_signature: "Проверка подлинности уведомлений",
  anti_loop_seconds: "Пауза против повторной отправки",
  treat_paid_status_as_prepaid: "Считать заказы в статусе «Оплачен» предоплаченными на 100%",
  auto_sync_enabled: "Автоматическая синхронизация",
  komtet_custom_field_code: "Код доп. поля для номера заказа КОМТЕТ",
  order_id: "ID заказа",
  komtet_code: "Код в КОМТЕТ",
  retailcrm_code: "Код в RetailCRM",
  items: "Список соответствий",
};

function formatHttpStatusError(status: number): string {
  switch (status) {
    case 400:
      return "Некорректные данные";
    case 401:
      return "Неверный ключ доступа";
    case 403:
      return "Доступ запрещён";
    case 404:
      return "Данные не найдены";
    case 422:
      return "Проверьте правильность заполнения полей";
    case 500:
      return "Внутренняя ошибка сервера";
    case 502:
    case 503:
      return "Сервис временно недоступен. Попробуйте повторить через минуту.";
    case 504:
      return "Превышено время ожидания ответа сервера.";
    default:
      return `Ошибка при обращении к серверу (код ${status})`;
  }
}

function translateValidationField(loc: Array<string | number>): string {
  const filtered = loc.filter(
    (seg, idx) =>
      !(
        idx === 0 &&
        typeof seg === "string" &&
        ["body", "query", "path", "header"].includes(seg)
      ),
  );
  if (filtered.length === 0) return "";
  const lastStr = [...filtered]
    .reverse()
    .find((seg): seg is string => typeof seg === "string");
  if (lastStr && VALIDATION_FIELD_LABELS[lastStr]) {
    return VALIDATION_FIELD_LABELS[lastStr];
  }
  const fullPath = filtered.map(String).join(".");
  return VALIDATION_FIELD_LABELS[fullPath] || "";
}

function translateValidationMessage(msg: string): string {
  const cleaned = msg.replace(/^Value error,\s*/i, "").trim();
  if (!cleaned) return "";
  if (/^Input should be a valid integer/i.test(cleaned)) {
    return "укажите целое число";
  }
  if (/^Input should be a valid number/i.test(cleaned)) {
    return "укажите число";
  }
  if (/^Input should be a valid string/i.test(cleaned)) {
    return "укажите текстовое значение";
  }
  if (/^Field required$/i.test(cleaned)) {
    return "обязательное поле";
  }
  const gtMatch = cleaned.match(/^Input should be greater than or equal to (\d+)/i);
  if (gtMatch) {
    return `значение должно быть не меньше ${gtMatch[1]}`;
  }
  const ltMatch = cleaned.match(/^Input should be less than or equal to (\d+)/i);
  if (ltMatch) {
    return `значение должно быть не больше ${ltMatch[1]}`;
  }
  return cleaned;
}

function formatFastApiDetail(detail: unknown, status: number): string {
  if (typeof detail === "string") {
    const cleaned = detail.trim();
    return cleaned || formatHttpStatusError(status);
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        if (typeof entry === "string") return entry.trim();
        if (entry && typeof entry === "object") {
          const item = entry as FastApiValidationItem;
          const rawMsg = typeof item.msg === "string" ? item.msg.trim() : "";
          const cleanMsg = translateValidationMessage(rawMsg);
          const rawLoc = Array.isArray(item.loc) ? item.loc : [];
          const fieldLabel = translateValidationField(rawLoc);
          if (fieldLabel && cleanMsg) {
            return `${fieldLabel}: ${cleanMsg}`;
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
    return "Сервер вернул некорректный ответ. Попробуйте повторить действие.";
  }
  return formatHttpStatusError(status);
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
      "Нет связи с сервером. Проверьте подключение к интернету и попробуйте снова.",
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
      throw new ApiError(formatHttpStatusError(response.status), response.status);
    }
    const detail =
      data && typeof data === "object" && "detail" in data
        ? formatFastApiDetail((data as { detail: unknown }).detail, response.status)
        : formatHttpStatusError(response.status);
    throw new ApiError(detail, response.status);
  }

  if (htmlError) {
    throw new ApiError(
      "Сервер вернул неожиданный ответ. Обновите страницу или обратитесь к администратору.",
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
