export type ApiVersion = "v1" | "v2";
export type MarkingMode = "off" | "catalog" | "all";

export interface SettingItem {
  key: string;
  value: unknown;
  value_type: string;
  is_secret: boolean;
  is_set: boolean;
  description?: string | null;
  updated_at?: string | null;
}

export interface SettingsResponse {
  retailcrm_url: string;
  retailcrm_api_key_masked: string;
  has_retailcrm_api_key: boolean;
  retailcrm_default_site: string;
  komtet_base_url: string;
  komtet_shop_id: string;
  komtet_secret_key_masked: string;
  has_komtet_secret_key: boolean;
  komtet_queue_id: number | null;
  komtet_api_version: ApiVersion;
  tax_system: number;
  default_vat: string;
  payment_address: string;
  fallback_client_email: string;
  company_inn: string;
  trigger_statuses: string[];
  cancel_statuses: string[];
  allowed_delivery_codes: string[];
  final_status: string;
  default_delivery_time_from: string;
  default_delivery_time_to: string;
  komtet_callback_url: string;
  require_mark_code: boolean;
  marking_mode: MarkingMode;
  verify_komtet_signature: boolean;
  anti_loop_seconds: number;
  treat_paid_status_as_prepaid: boolean;
  auto_sync_enabled: boolean;
  komtet_custom_field_code: string;
  items: SettingItem[];
}

export interface SettingsUpdateRequest {
  retailcrm_url?: string;
  retailcrm_api_key?: string;
  retailcrm_default_site?: string;
  komtet_base_url?: string;
  komtet_shop_id?: string;
  komtet_secret_key?: string;
  komtet_queue_id?: number | null;
  komtet_api_version?: ApiVersion;
  tax_system?: number;
  default_vat?: string;
  payment_address?: string;
  fallback_client_email?: string;
  company_inn?: string;
  trigger_statuses?: string[];
  cancel_statuses?: string[];
  allowed_delivery_codes?: string[];
  final_status?: string;
  default_delivery_time_from?: string;
  default_delivery_time_to?: string;
  komtet_callback_url?: string;
  require_mark_code?: boolean;
  marking_mode?: MarkingMode;
  verify_komtet_signature?: boolean;
  anti_loop_seconds?: number;
  treat_paid_status_as_prepaid?: boolean;
  auto_sync_enabled?: boolean;
  komtet_custom_field_code?: string;
}

export interface ConnectionCheckResult {
  retailcrm_ok: boolean;
  retailcrm_message: string;
  retailcrm_sites: string[];
  komtet_field_exists: boolean;
  komtet_field_created: boolean;
  komtet_ok: boolean;
  komtet_message: string;
  komtet_queue_active: boolean | null;
  komtet_delivery_service_active: boolean | null;
  warnings: string[];
}

export interface MappingItem {
  id?: number | null;
  mapping_type: "courier" | "payment_type" | "vat" | string;
  retailcrm_code: string;
  retailcrm_name?: string | null;
  komtet_code: string;
  komtet_name?: string | null;
  is_active: boolean;
  extra?: Record<string, unknown> | null;
}

export interface MappingsResponse {
  items: MappingItem[];
}

export interface CrmStatusDictItem {
  code: string;
  name: string;
  group?: string;
  active?: boolean;
}

export interface CrmPaymentTypeDictItem {
  code: string;
  name: string;
  active?: boolean;
}

export interface CrmCourierDictItem {
  id: number | string;
  firstName?: string;
  lastName?: string;
  patronymic?: string;
  active?: boolean;
  phone?: { number?: string } | string;
}

export interface CrmSiteDictItem {
  code: string;
  name: string;
}

export interface CrmDeliveryTypeDictItem {
  code: string;
  name: string;
  active?: boolean;
}

export interface KomtetEmployeeDictItem {
  id: number | string;
  name: string;
  type?: string;
  phone?: string;
}

export interface DictionariesResponse {
  retailcrm_statuses: CrmStatusDictItem[];
  retailcrm_payment_types: CrmPaymentTypeDictItem[];
  retailcrm_couriers: CrmCourierDictItem[];
  retailcrm_sites: CrmSiteDictItem[];
  retailcrm_delivery_types: CrmDeliveryTypeDictItem[];
  komtet_employees: KomtetEmployeeDictItem[];
  komtet_couriers: KomtetEmployeeDictItem[];
  komtet_queue: Record<string, unknown> | null;
  warnings: string[];
}

export interface OrderSyncItem {
  id: number;
  retailcrm_order_id: number;
  retailcrm_order_number?: string | null;
  retailcrm_site?: string | null;
  komtet_order_id?: number | null;
  komtet_external_id?: string | null;
  komtet_state?: string | null;
  sync_status: string;
  last_hash?: string | null;
  last_error?: string | null;
  last_synced_at?: string | null;
  last_komtet_callback_at?: string | null;
  updated_at: string;
}

export interface OrderSyncListResponse {
  items: OrderSyncItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ManualSyncResponse {
  ok: boolean;
  result: {
    status: string;
    reason?: string;
    retailcrm_order_id?: number;
    komtet_order_id?: number;
    error?: string;
    warning?: string;
  };
}

export interface SyncLogItem {
  id: number;
  direction: string;
  level: string;
  event_type: string;
  retailcrm_order_id?: number | null;
  komtet_order_id?: number | null;
  message: string;
  payload?: Record<string, unknown> | null;
  error_details?: string | null;
  created_at: string;
}

export interface SyncLogsResponse {
  items: SyncLogItem[];
  total: number;
  limit: number;
  offset: number;
}
