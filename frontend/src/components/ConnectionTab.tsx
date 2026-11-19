import React, { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Copy,
  KeyRound,
  Loader2,
  PlugZap,
  Save,
  ShieldCheck,
  Store,
  Truck,
  XCircle,
} from "lucide-react";
import { api, ApiError } from "../api";
import type {
  ApiVersion,
  ConnectionCheckResult,
  SettingsResponse,
  SettingsUpdateRequest,
} from "../types";

interface ConnectionTabProps {
  settings: SettingsResponse | null;
  onSettingsUpdated: (updated: SettingsResponse) => void;
  onUnauthorized: () => void;
}

const TAX_SYSTEM_OPTIONS = [
  { value: 0, label: "0 — ОСНО (Общая система налогообложения)" },
  { value: 1, label: "1 — УСН Доход" },
  { value: 2, label: "2 — УСН Доход минус Расход" },
  { value: 4, label: "4 — ЕСХН (Единый сельскохозяйственный налог)" },
  { value: 5, label: "5 — Патент (ПСН)" },
];

const VAT_OPTIONS = [
  { value: "22", label: "22% — НДС 22%" },
  { value: "20", label: "20% — НДС 20%" },
  { value: "10", label: "10% — НДС 10%" },
  { value: "7", label: "7% — НДС 7% (УСН)" },
  { value: "5", label: "5% — НДС 5% (УСН)" },
  { value: "0", label: "0% — НДС 0%" },
  { value: "no", label: "no — Без НДС" },
  { value: "122", label: "22/122 — расчётная ставка (122)" },
  { value: "120", label: "20/120 — расчётная ставка (120)" },
  { value: "110", label: "10/110 — расчётная ставка (110)" },
  { value: "107", label: "7/107 — расчётная ставка (107)" },
  { value: "105", label: "5/105 — расчётная ставка (105)" },
];

async function copyTextToClipboard(text: string): Promise<boolean> {
  if (
    typeof navigator !== "undefined" &&
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === "function" &&
    typeof window !== "undefined" &&
    window.isSecureContext
  ) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to legacy textarea fallback
    }
  }

  if (typeof document === "undefined") {
    return false;
  }

  let textarea: HTMLTextAreaElement | null = null;
  try {
    textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.top = "-9999px";
    textarea.style.left = "-9999px";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    if (textarea && textarea.parentNode) {
      textarea.parentNode.removeChild(textarea);
    }
  }
}

export const ConnectionTab: React.FC<ConnectionTabProps> = ({
  settings,
  onSettingsUpdated,
  onUnauthorized,
}) => {
  const [retailcrmUrl, setRetailcrmUrl] = useState("");
  const [retailcrmApiKey, setRetailcrmApiKey] = useState("");
  const [retailcrmDefaultSite, setRetailcrmDefaultSite] = useState("");

  const [komtetBaseUrl, setKomtetBaseUrl] = useState("https://kassa.komtet.ru");
  const [komtetShopId, setKomtetShopId] = useState("");
  const [komtetSecretKey, setKomtetSecretKey] = useState("");
  const [komtetQueueId, setKomtetQueueId] = useState("");
  const [komtetApiVersion, setKomtetApiVersion] = useState<ApiVersion>("v2");
  const [komtetCallbackUrl, setKomtetCallbackUrl] = useState("");

  const [taxSystem, setTaxSystem] = useState<number>(0);
  const [defaultVat, setDefaultVat] = useState("22");
  const [companyInn, setCompanyInn] = useState("");
  const [paymentAddress, setPaymentAddress] = useState("");
  const [deliveryTimeFrom, setDeliveryTimeFrom] = useState("09:00");
  const [deliveryTimeTo, setDeliveryTimeTo] = useState("21:00");
  const [antiLoopSeconds, setAntiLoopSeconds] = useState<number>(15);
  const [customFieldCode, setCustomFieldCode] = useState("komtet_id");
  const [autoSyncEnabled, setAutoSyncEnabled] = useState(true);
  const [verifyKomtetSignature, setVerifyKomtetSignature] = useState(true);

  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [checkResult, setCheckResult] = useState<ConnectionCheckResult | null>(
    null,
  );
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const copyTimerRef = useRef<number | null>(null);
  const saveTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!settings) return;
    setRetailcrmUrl(settings.retailcrm_url || "");
    setRetailcrmApiKey("");
    setRetailcrmDefaultSite(settings.retailcrm_default_site || "");
    setKomtetBaseUrl(settings.komtet_base_url || "https://kassa.komtet.ru");
    setKomtetShopId(settings.komtet_shop_id || "");
    setKomtetSecretKey("");
    setKomtetQueueId(
      settings.komtet_queue_id !== null && settings.komtet_queue_id !== undefined
        ? String(settings.komtet_queue_id)
        : "",
    );
    setKomtetApiVersion(settings.komtet_api_version || "v2");
    setKomtetCallbackUrl(settings.komtet_callback_url || "");
    setTaxSystem(
      [0, 1, 2, 4, 5].includes(settings.tax_system) ? settings.tax_system : 0,
    );
    setDefaultVat(settings.default_vat || "22");
    setCompanyInn(settings.company_inn || "");
    setPaymentAddress(settings.payment_address || "");
    setDeliveryTimeFrom(settings.default_delivery_time_from || "09:00");
    setDeliveryTimeTo(settings.default_delivery_time_to || "21:00");
    setAntiLoopSeconds(settings.anti_loop_seconds ?? 15);
    setCustomFieldCode(settings.komtet_custom_field_code || "komtet_id");
    setAutoSyncEnabled(settings.auto_sync_enabled ?? true);
    setVerifyKomtetSignature(settings.verify_komtet_signature ?? true);
  }, [settings]);

  const origin =
    typeof window !== "undefined"
      ? window.location.origin
      : "https://your-domain.ru";
  const crmWebhookUrl = `${origin}/webhooks/retailcrm`;
  const komtetWebhookUrl =
    komtetCallbackUrl.trim() || `${origin}/webhooks/komtet`;

  const handleCopy = async (key: string, text: string) => {
    const copied = await copyTextToClipboard(text);
    if (!copied) {
      setErrorMsg(
        "Не удалось скопировать URL в буфер обмена. Скопируйте значение вручную.",
      );
      return;
    }
    setCopiedKey(key);
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current);
    }
    copyTimerRef.current = window.setTimeout(() => {
      setCopiedKey(null);
      copyTimerRef.current = null;
    }, 2000);
  };

  const buildUpdatePayload = (): SettingsUpdateRequest => {
    const payload: SettingsUpdateRequest = {
      retailcrm_url: retailcrmUrl.trim(),
      retailcrm_default_site: retailcrmDefaultSite.trim(),
      komtet_base_url: komtetBaseUrl.trim(),
      komtet_shop_id: komtetShopId.trim(),
      komtet_api_version: komtetApiVersion,
      tax_system: taxSystem,
      default_vat: defaultVat,
      company_inn: companyInn.trim(),
      payment_address: paymentAddress.trim(),
      default_delivery_time_from: deliveryTimeFrom.trim(),
      default_delivery_time_to: deliveryTimeTo.trim(),
      komtet_callback_url: komtetCallbackUrl.trim(),
      verify_komtet_signature: verifyKomtetSignature,
      anti_loop_seconds: Number(antiLoopSeconds) || 0,
      auto_sync_enabled: autoSyncEnabled,
      komtet_custom_field_code: customFieldCode.trim() || "komtet_id",
    };

    if (retailcrmApiKey.trim()) {
      payload.retailcrm_api_key = retailcrmApiKey.trim();
    }
    if (komtetSecretKey.trim()) {
      payload.komtet_secret_key = komtetSecretKey.trim();
    }

    const trimmedQueue = komtetQueueId.trim();
    if (trimmedQueue === "") {
      payload.komtet_queue_id = null;
    } else if (!/^\d+$/.test(trimmedQueue) || Number(trimmedQueue) <= 0) {
      throw new Error(
        "Queue ID (ID очереди кассы) должен быть целым положительным числом или пустым.",
      );
    } else {
      payload.komtet_queue_id = Number(trimmedQueue);
    }

    return payload;
  };

  const showSaveSuccess = (message: string) => {
    setSaveSuccess(message);
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = window.setTimeout(() => {
      setSaveSuccess(null);
      saveTimerRef.current = null;
    }, 4000);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setErrorMsg(null);
    setSaveSuccess(null);
    try {
      const payload = buildUpdatePayload();
      const updated = await api.updateSettings(payload);
      onSettingsUpdated(updated);
      showSaveSuccess("Настройки успешно сохранены");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      setErrorMsg(
        err instanceof Error ? err.message : "Не удалось сохранить настройки",
      );
    } finally {
      setSaving(false);
    }
  };

  const handleTestConnection = async () => {
    setTesting(true);
    setErrorMsg(null);
    setSaveSuccess(null);
    try {
      // Save current form changes first so connection test uses fresh inputs
      const payload = buildUpdatePayload();
      const updated = await api.updateSettings(payload);
      onSettingsUpdated(updated);
      const res = await api.testConnection();
      setCheckResult(res);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Ошибка при проверке подключения",
      );
    } finally {
      setTesting(false);
    }
  };

  const vatOptions = VAT_OPTIONS.some((opt) => opt.value === defaultVat)
    ? VAT_OPTIONS
    : [...VAT_OPTIONS, { value: defaultVat, label: `${defaultVat} (текущая)` }];

  return (
    <form onSubmit={handleSave} className="space-y-5 sm:space-y-6">
      {errorMsg && (
        <div className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          <XCircle className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
          <div className="min-w-0">
            <div className="font-semibold">Ошибка сохранения или проверки</div>
            <div className="mt-0.5 break-words">{errorMsg}</div>
          </div>
        </div>
      )}

      {saveSuccess && (
        <div className="flex items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm font-medium text-emerald-800">
          <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" />
          <span>{saveSuccess}</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 sm:gap-6 lg:grid-cols-2">
        {/* Блок RetailCRM */}
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
          <div className="mb-4 flex items-center gap-2.5 border-b border-slate-100 pb-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-blue-50 text-blue-600">
              <Store className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-slate-900">
                Подключение к RetailCRM
              </h2>
              <p className="text-xs text-slate-500">
                Доступ к заказам, оплатам, справочникам и доп. полям (API v5)
              </p>
            </div>
          </div>

          <div className="space-y-4">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-700">
                URL кабинета RetailCRM
              </label>
              <input
                type="url"
                value={retailcrmUrl}
                onChange={(e) => setRetailcrmUrl(e.target.value)}
                placeholder="https://your-shop.retailcrm.ru"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
              />
            </div>

            <div>
              <div className="mb-1 flex flex-wrap items-center justify-between gap-1.5">
                <label className="text-xs font-medium text-slate-700">
                  API-ключ RetailCRM
                </label>
                {settings?.has_retailcrm_api_key && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                    <KeyRound className="h-3 w-3" />
                    Сохранен ({settings.retailcrm_api_key_masked})
                  </span>
                )}
              </div>
              <input
                type="password"
                value={retailcrmApiKey}
                onChange={(e) => setRetailcrmApiKey(e.target.value)}
                placeholder={
                  settings?.has_retailcrm_api_key
                    ? "Оставьте пустым, чтобы не менять сохраненный ключ"
                    : "Введите API-ключ RetailCRM"
                }
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
              />
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Код сайта по умолчанию (site)
                </label>
                <input
                  type="text"
                  value={retailcrmDefaultSite}
                  onChange={(e) => setRetailcrmDefaultSite(e.target.value)}
                  placeholder="Например: main-site"
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Код доп. поля ID заказа КОМТЕТ
                </label>
                <input
                  type="text"
                  value={customFieldCode}
                  onChange={(e) => setCustomFieldCode(e.target.value)}
                  placeholder="komtet_id"
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
              </div>
            </div>

            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="text-xs font-medium text-slate-700">
                    URL вебхука для триггера в RetailCRM
                  </div>
                  <div className="mt-0.5 break-all font-mono text-xs text-slate-600 sm:truncate">
                    {crmWebhookUrl}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void handleCopy("crm", crmWebhookUrl)}
                  className="inline-flex w-full shrink-0 items-center justify-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 sm:w-auto sm:py-1.5"
                >
                  {copiedKey === "crm" ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-emerald-600" />
                      Скопировано
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" />
                      Скопировать URL
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Блок КОМТЕТ Касса */}
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
          <div className="mb-4 flex items-center gap-2.5 border-b border-slate-100 pb-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <Truck className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-slate-900">
                Подключение к КОМТЕТ Касса Курьер
              </h2>
              <p className="text-xs text-slate-500">
                Реквизиты магазина, очереди и версия протокола заказов
              </p>
            </div>
          </div>

          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-5">
              <div className="sm:col-span-3">
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Базовый URL API КОМТЕТ
                </label>
                <input
                  type="url"
                  value={komtetBaseUrl}
                  onChange={(e) => setKomtetBaseUrl(e.target.value)}
                  placeholder="https://kassa.komtet.ru"
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
              </div>

              <div className="sm:col-span-2">
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Версия API
                </label>
                <select
                  value={komtetApiVersion}
                  onChange={(e) =>
                    setKomtetApiVersion(e.target.value as ApiVersion)
                  }
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                >
                  <option value="v2">API v2 (основная)</option>
                  <option value="v1">API v1 (устаревшая)</option>
                </select>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Shop ID (Идентификатор магазина)
                </label>
                <input
                  type="text"
                  value={komtetShopId}
                  onChange={(e) => setKomtetShopId(e.target.value)}
                  placeholder="Введите Shop ID"
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Queue ID (ID очереди кассы)
                </label>
                <input
                  type="number"
                  min={1}
                  inputMode="numeric"
                  value={komtetQueueId}
                  onChange={(e) => setKomtetQueueId(e.target.value)}
                  placeholder="Пусто — без привязки"
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
              </div>
            </div>

            <div>
              <div className="mb-1 flex flex-wrap items-center justify-between gap-1.5">
                <label className="text-xs font-medium text-slate-700">
                  Secret Key (Секретный ключ магазина)
                </label>
                {settings?.has_komtet_secret_key && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                    <KeyRound className="h-3 w-3" />
                    Сохранен ({settings.komtet_secret_key_masked})
                  </span>
                )}
              </div>
              <input
                type="password"
                value={komtetSecretKey}
                onChange={(e) => setKomtetSecretKey(e.target.value)}
                placeholder={
                  settings?.has_komtet_secret_key
                    ? "Оставьте пустым, чтобы не менять сохраненный секрет"
                    : "Введите Secret Key КОМТЕТ"
                }
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs font-medium text-slate-700">
                Публичный Callback URL для уведомлений от КОМТЕТ
              </label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  type="url"
                  value={komtetCallbackUrl}
                  onChange={(e) => setKomtetCallbackUrl(e.target.value)}
                  placeholder={`${origin}/webhooks/komtet`}
                  className="w-full min-w-0 rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
                <button
                  type="button"
                  onClick={() => void handleCopy("komtet", komtetWebhookUrl)}
                  className="inline-flex w-full shrink-0 items-center justify-center gap-1.5 rounded-lg border border-slate-300 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-100 sm:w-auto"
                >
                  {copiedKey === "komtet" ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-emerald-600" />
                      Скопировано
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" />
                      Скопировать URL
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Блок Фискализация и Доставка */}
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
        <div className="mb-4 flex items-center gap-2.5 border-b border-slate-100 pb-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-slate-900">
              Фискализация, доставка и защита
            </h2>
            <p className="text-xs text-slate-500">
              Параметры 54-ФЗ (СНО, НДС, ИНН, место расчетов), интервалы доставки и защита вебхуков
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="mb-1 block text-xs font-medium text-slate-700">
              Система налогообложения (СНО)
            </label>
            <select
              value={taxSystem}
              onChange={(e) => setTaxSystem(Number(e.target.value))}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            >
              {TAX_SYSTEM_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">
              Ставка НДС по умолчанию
            </label>
            <select
              value={defaultVat}
              onChange={(e) => setDefaultVat(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            >
              {vatOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">
              ИНН организации / ИП
            </label>
            <input
              type="text"
              inputMode="numeric"
              value={companyInn}
              onChange={(e) => setCompanyInn(e.target.value)}
              placeholder="10 или 12 цифр"
              maxLength={12}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            />
          </div>

          <div className="sm:col-span-2">
            <label className="mb-1 block text-xs font-medium text-slate-700">
              Адрес расчетов (URL магазина)
            </label>
            <input
              type="text"
              value={paymentAddress}
              onChange={(e) => setPaymentAddress(e.target.value)}
              placeholder="https://your-shop.ru"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">
              Интервал доставки (с — до)
            </label>
            <div className="flex items-center gap-2">
              <input
                type="time"
                value={deliveryTimeFrom}
                onChange={(e) => setDeliveryTimeFrom(e.target.value)}
                className="w-full min-w-0 rounded-lg border border-slate-300 px-2.5 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
              />
              <span className="shrink-0 text-slate-400">—</span>
              <input
                type="time"
                value={deliveryTimeTo}
                onChange={(e) => setDeliveryTimeTo(e.target.value)}
                className="w-full min-w-0 rounded-lg border border-slate-300 px-2.5 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
              />
            </div>
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">
              Окно защиты от петли (сек)
            </label>
            <input
              type="number"
              min={0}
              max={3600}
              inputMode="numeric"
              value={antiLoopSeconds}
              onChange={(e) => setAntiLoopSeconds(Number(e.target.value))}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            />
          </div>
        </div>

        <div className="mt-5 grid grid-cols-1 gap-4 border-t border-slate-100 pt-4 sm:grid-cols-2">
          <label className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 hover:bg-slate-100/70">
            <div className="min-w-0">
              <div className="text-sm font-medium text-slate-900">
                Автоматическая синхронизация
              </div>
              <div className="text-xs text-slate-500">
                Обрабатывать входящие вебхуки из RetailCRM в реальном времени
              </div>
            </div>
            <input
              type="checkbox"
              checked={autoSyncEnabled}
              onChange={(e) => setAutoSyncEnabled(e.target.checked)}
              className="h-5 w-5 shrink-0 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
          </label>

          <label className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3.5 hover:bg-slate-100/70">
            <div className="min-w-0">
              <div className="text-sm font-medium text-slate-900">
                Проверять подпись X-HMAC-Signature
              </div>
              <div className="text-xs text-slate-500">
                Криптографическая проверка подлинности обратных вебхуков от КОМТЕТ Кассы.{" "}
                <span className="font-medium text-slate-700">
                  В окружении production подпись проверяется принудительно.
                </span>
              </div>
            </div>
            <input
              type="checkbox"
              checked={verifyKomtetSignature}
              onChange={(e) => setVerifyKomtetSignature(e.target.checked)}
              className="h-5 w-5 shrink-0 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
          </label>
        </div>
      </div>

      {/* Кнопки действий */}
      <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:flex-wrap sm:items-center">
        <button
          type="submit"
          disabled={saving || testing}
          className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60 sm:w-auto"
        >
          {saving ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          Сохранить настройки
        </button>

        <button
          type="button"
          onClick={() => void handleTestConnection()}
          disabled={saving || testing}
          className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-5 py-2.5 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-60 sm:w-auto"
        >
          {testing ? (
            <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
          ) : (
            <PlugZap className="h-4 w-4 text-blue-600" />
          )}
          Проверить подключение и инициализировать komtet_id
        </button>
      </div>

      {/* Результаты проверки подключения */}
      {checkResult && (
        <div className="space-y-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
          <h3 className="text-sm font-semibold text-slate-900">
            Диагностика подключения и готовности интеграции
          </h3>

          {checkResult.warnings.length > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
              <div className="flex items-start gap-2.5">
                <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
                <div className="min-w-0 space-y-1">
                  <div className="font-semibold">
                    Обратите внимание на предупреждения API:
                  </div>
                  <ul className="list-inside list-disc space-y-1 break-words text-xs">
                    {checkResult.warnings.map((w, idx) => (
                      <li key={idx}>{w}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {/* Карточка 1: RetailCRM */}
            <div className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-slate-500">
                  RetailCRM API v5
                </span>
                {checkResult.retailcrm_ok ? (
                  <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                ) : (
                  <XCircle className="h-5 w-5 text-red-600" />
                )}
              </div>
              <div className="mt-2 text-sm font-semibold text-slate-900">
                {checkResult.retailcrm_ok ? "Подключено" : "Ошибка подключения"}
              </div>
              <div className="mt-1 break-words text-xs text-slate-600">
                {checkResult.retailcrm_message}
              </div>
              {checkResult.retailcrm_sites.length > 0 && (
                <div className="mt-2 break-words text-[11px] text-slate-500">
                  Сайты: {checkResult.retailcrm_sites.join(", ")}
                </div>
              )}
            </div>

            {/* Карточка 2: Поле komtet_id */}
            <div className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-slate-500">
                  Поле {customFieldCode || "komtet_id"}
                </span>
                {checkResult.komtet_field_exists ? (
                  <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                ) : (
                  <XCircle className="h-5 w-5 text-slate-400" />
                )}
              </div>
              <div className="mt-2 text-sm font-semibold text-slate-900">
                {checkResult.komtet_field_exists
                  ? checkResult.komtet_field_created
                    ? "Создано автоматически"
                    : "Поле уже существует"
                  : "Не проверено"}
              </div>
              <div className="mt-1 text-xs text-slate-600">
                Дополнительное поле заказа в RetailCRM для связки с КОМТЕТ
              </div>
            </div>

            {/* Карточка 3: Очередь КОМТЕТ */}
            <div className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-slate-500">
                  КОМТЕТ Касса (Очередь)
                </span>
                {checkResult.komtet_ok ? (
                  <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                ) : (
                  <XCircle className="h-5 w-5 text-red-600" />
                )}
              </div>
              <div className="mt-2 text-sm font-semibold text-slate-900">
                {checkResult.komtet_ok
                  ? checkResult.komtet_queue_active === true
                    ? "Очередь активна"
                    : checkResult.komtet_queue_active === false
                      ? "Очередь неактивна"
                      : "Авторизация успешна"
                  : "Ошибка авторизации"}
              </div>
              <div className="mt-1 break-words text-xs text-slate-600">
                {checkResult.komtet_message}
              </div>
            </div>

            {/* Карточка 4: Услуга КОМТЕТ Курьер */}
            <div className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-slate-500">
                  Услуга «КОМТЕТ Курьер»
                </span>
                {checkResult.komtet_delivery_service_active === true ? (
                  <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                ) : checkResult.komtet_delivery_service_active === false ? (
                  <AlertTriangle className="h-5 w-5 text-amber-500" />
                ) : (
                  <XCircle className="h-5 w-5 text-slate-400" />
                )}
              </div>
              <div className="mt-2 text-sm font-semibold text-slate-900">
                {checkResult.komtet_delivery_service_active === true
                  ? "Услуга активна"
                  : checkResult.komtet_delivery_service_active === false
                    ? "Требуется активация (SRV00)"
                    : "Не определено"}
              </div>
              <div className="mt-1 text-xs text-slate-600">
                {checkResult.komtet_delivery_service_active === false
                  ? "Активируйте тариф «Курьерская доставка» в ЛК КОМТЕТ Кассы"
                  : "Доступ к API заказов курьерской доставки"}
              </div>
            </div>
          </div>
        </div>
      )}
    </form>
  );
};
