import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  GitCompareArrows,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  XCircle,
} from "lucide-react";
import { api, ApiError } from "../api";
import type {
  DictionariesResponse,
  MappingItem,
  SettingsResponse,
} from "../types";

interface MappingsTabProps {
  settings: SettingsResponse | null;
  onSettingsUpdated: (updated: SettingsResponse) => void;
  onUnauthorized: () => void;
}

const KOMTET_PAYMENT_OPTIONS = [
  { code: "cash", name: "Наличными курьеру (cash)" },
  { code: "card", name: "Банковской картой курьеру (card)" },
  { code: "prepayment", name: "Предоплата онлайн (prepayment)" },
];

const KOMTET_VAT_OPTIONS = [
  { code: "22", name: "НДС 22%" },
  { code: "20", name: "НДС 20%" },
  { code: "10", name: "НДС 10%" },
  { code: "0", name: "НДС 0%" },
  { code: "no", name: "Без НДС (no)" },
];

const DEFAULT_VAT_ROWS: Array<{ crmCode: string; crmName: string; komtetCode: string }> = [
  { crmCode: "22", crmName: "НДС 22%", komtetCode: "22" },
  { crmCode: "20", crmName: "НДС 20%", komtetCode: "20" },
  { crmCode: "10", crmName: "НДС 10%", komtetCode: "10" },
  { crmCode: "0", crmName: "НДС 0%", komtetCode: "0" },
  { crmCode: "none", crmName: "Без НДС", komtetCode: "no" },
];

export const MappingsTab: React.FC<MappingsTabProps> = ({
  settings,
  onSettingsUpdated,
  onUnauthorized,
}) => {
  const [dictionaries, setDictionaries] = useState<DictionariesResponse | null>(
    null,
  );
  const [loadingDicts, setLoadingDicts] = useState(false);
  const [saving, setSaving] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const [triggerStatuses, setTriggerStatuses] = useState<string[]>([]);
  const [cancelStatuses, setCancelStatuses] = useState<string[]>([]);
  const [finalStatus, setFinalStatus] = useState<string>("complete");

  // Directional mapping states: retailcrm_code -> komtet_code
  const [courierMap, setCourierMap] = useState<Record<string, string>>({});
  const [paymentMap, setPaymentMap] = useState<Record<string, string>>({});
  const [vatMap, setVatMap] = useState<Record<string, string>>({});

  // Custom courier rows when CRM couriers dictionary is empty or manager wants manual entry
  const [customCouriers, setCustomCouriers] = useState<
    Array<{ code: string; name: string }>
  >([]);
  const [newCourierCode, setNewCourierCode] = useState("");
  const [newCourierName, setNewCourierName] = useState("");

  useEffect(() => {
    if (!settings) return;
    setTriggerStatuses(settings.trigger_statuses || []);
    setCancelStatuses(settings.cancel_statuses || []);
    setFinalStatus(settings.final_status || "complete");
  }, [settings]);

  const loadData = useCallback(async () => {
    setLoadingDicts(true);
    setErrorMsg(null);
    try {
      const [dicts, mappingsResp] = await Promise.all([
        api.getDictionaries(),
        api.getMappings(),
      ]);
      setDictionaries(dicts);

      const cMap: Record<string, string> = {};
      const pMap: Record<string, string> = {};
      const vMap: Record<string, string> = {
        "22": "22",
        "20": "20",
        "10": "10",
        "0": "0",
        none: "no",
      };

      const knownCrmCourierIds = new Set(
        dicts.retailcrm_couriers.map((c) => String(c.id)),
      );
      const extraCouriers: Array<{ code: string; name: string }> = [];

      for (const item of mappingsResp.items) {
        if (!item.is_active) continue;
        if (item.mapping_type === "courier") {
          cMap[item.retailcrm_code] = item.komtet_code;
          if (!knownCrmCourierIds.has(item.retailcrm_code)) {
            extraCouriers.push({
              code: item.retailcrm_code,
              name: item.retailcrm_name || `Курьер #${item.retailcrm_code}`,
            });
          }
        } else if (item.mapping_type === "payment_type") {
          pMap[item.retailcrm_code] = item.komtet_code;
        } else if (item.mapping_type === "vat") {
          vMap[item.retailcrm_code] = item.komtet_code;
        }
      }

      setCourierMap(cMap);
      setPaymentMap(pMap);
      setVatMap(vMap);
      setCustomCouriers(extraCouriers);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Не удалось загрузить справочники и маппинг",
      );
    } finally {
      setLoadingDicts(false);
    }
  }, [onUnauthorized]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const toggleStatusInList = (
    code: string,
    current: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>,
  ) => {
    if (current.includes(code)) {
      setter(current.filter((c) => c !== code));
    } else {
      setter([...current, code]);
    }
  };

  const handleAddCustomCourier = () => {
    const code = newCourierCode.trim();
    if (!code) return;
    const name = newCourierName.trim() || `Курьер #${code}`;
    if (
      !customCouriers.some((c) => c.code === code) &&
      !dictionaries?.retailcrm_couriers.some((c) => String(c.id) === code)
    ) {
      setCustomCouriers([...customCouriers, { code, name }]);
    }
    setNewCourierCode("");
    setNewCourierName("");
  };

  const handleRemoveCustomCourier = (code: string) => {
    setCustomCouriers(customCouriers.filter((c) => c.code !== code));
    const next = { ...courierMap };
    delete next[code];
    setCourierMap(next);
  };

  const handleSaveAll = async () => {
    setSaving(true);
    setErrorMsg(null);
    setSuccessMsg(null);
    try {
      const items: MappingItem[] = [];

      // 1) Couriers
      const allCrmCouriers = [
        ...(dictionaries?.retailcrm_couriers.map((c) => ({
          code: String(c.id),
          name:
            [c.lastName, c.firstName, c.patronymic]
              .filter(Boolean)
              .join(" ")
              .trim() || `Курьер #${c.id}`,
        })) || []),
        ...customCouriers,
      ];

      const komtetCourierPool = [
        ...(dictionaries?.komtet_couriers || []),
        ...(dictionaries?.komtet_employees || []),
      ];

      for (const crmCourier of allCrmCouriers) {
        const komtetId = (courierMap[crmCourier.code] || "").trim();
        if (!komtetId) continue;
        const matchedEmp = komtetCourierPool.find(
          (emp) => String(emp.id) === komtetId,
        );
        items.push({
          mapping_type: "courier",
          retailcrm_code: crmCourier.code,
          retailcrm_name: crmCourier.name,
          komtet_code: komtetId,
          komtet_name: matchedEmp ? matchedEmp.name : `ID ${komtetId}`,
          is_active: true,
        });
      }

      // 2) Payment types
      const crmPaymentTypes =
        dictionaries?.retailcrm_payment_types &&
        dictionaries.retailcrm_payment_types.length > 0
          ? dictionaries.retailcrm_payment_types
          : [
              { code: "cash", name: "Наличные" },
              { code: "bank-card", name: "Банковская карта" },
              { code: "oplachen", name: "Оплачен" },
            ];

      for (const pt of crmPaymentTypes) {
        const komtetCode = (paymentMap[pt.code] || "").trim();
        if (!komtetCode) continue;
        const opt = KOMTET_PAYMENT_OPTIONS.find((o) => o.code === komtetCode);
        items.push({
          mapping_type: "payment_type",
          retailcrm_code: pt.code,
          retailcrm_name: pt.name || pt.code,
          komtet_code: komtetCode,
          komtet_name: opt ? opt.name : komtetCode,
          is_active: true,
        });
      }

      // 3) VAT rates
      for (const row of DEFAULT_VAT_ROWS) {
        const komtetVat = vatMap[row.crmCode] || row.komtetCode;
        const opt = KOMTET_VAT_OPTIONS.find((o) => o.code === komtetVat);
        items.push({
          mapping_type: "vat",
          retailcrm_code: row.crmCode,
          retailcrm_name: row.crmName,
          komtet_code: komtetVat,
          komtet_name: opt ? opt.name : komtetVat,
          is_active: true,
        });
      }

      const [, updatedSettings] = await Promise.all([
        api.saveMappings(items, ["courier", "payment_type", "vat"]),
        api.updateSettings({
          trigger_statuses: triggerStatuses,
          cancel_statuses: cancelStatuses,
          final_status: finalStatus.trim() || "complete",
        }),
      ]);

      onSettingsUpdated(updatedSettings);
      setSuccessMsg("Статусы и таблицы маппинга успешно сохранены");
      setTimeout(() => setSuccessMsg(null), 4000);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Ошибка сохранения маппинга и статусов",
      );
    } finally {
      setSaving(false);
    }
  };

  const statusesList =
    dictionaries?.retailcrm_statuses &&
    dictionaries.retailcrm_statuses.length > 0
      ? dictionaries.retailcrm_statuses
      : [
          { code: "new", name: "Новый (new)" },
          { code: "paid", name: "Оплачен (paid)" },
          { code: "send-to-delivery", name: "Передан в доставку (send-to-delivery)" },
          { code: "delivering", name: "Доставляется (delivering)" },
          { code: "complete", name: "Выполнен (complete)" },
          { code: "cancel-other", name: "Отменен (cancel-other)" },
          { code: "no-product", name: "Нет в наличии (no-product)" },
          { code: "otklonen", name: "Отклонен (otklonen)" },
        ];

  const paymentTypesList =
    dictionaries?.retailcrm_payment_types &&
    dictionaries.retailcrm_payment_types.length > 0
      ? dictionaries.retailcrm_payment_types
      : [
          { code: "cash", name: "Наличные" },
          { code: "bank-card", name: "Банковская карта" },
          { code: "oplachen", name: "Оплачен (предоплата)" },
        ];

  const komtetStaffList =
    dictionaries?.komtet_couriers && dictionaries.komtet_couriers.length > 0
      ? dictionaries.komtet_couriers
      : dictionaries?.komtet_employees || [];

  const crmCouriersList = [
    ...(dictionaries?.retailcrm_couriers.map((c) => ({
      code: String(c.id),
      name:
        [c.lastName, c.firstName, c.patronymic]
          .filter(Boolean)
          .join(" ")
          .trim() || `Курьер #${c.id}`,
      active: c.active !== false,
      isCustom: false,
    })) || []),
    ...customCouriers.map((c) => ({
      code: c.code,
      name: c.name,
      active: true,
      isCustom: true,
    })),
  ];

  return (
    <div className="space-y-6">
      {/* Верхняя панель обновления справочников */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div>
          <h2 className="text-base font-semibold text-slate-900">
            Справочники API и сопоставление сущностей
          </h2>
          <p className="text-xs text-slate-500">
            Загрузка живых статусов, курьеров и типов оплат из RetailCRM и КОМТЕТ Кассы
          </p>
        </div>

        <button
          type="button"
          onClick={() => void loadData()}
          disabled={loadingDicts}
          className="inline-flex items-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          <RefreshCw
            className={`h-4 w-4 text-blue-600 ${loadingDicts ? "animate-spin" : ""}`}
          />
          Обновить справочники из API
        </button>
      </div>

      {/* Баннер предупреждений из API (например, SRV00) */}
      {dictionaries && dictionaries.warnings.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
            <div className="space-y-1">
              <div className="font-semibold">
                Предупреждения при загрузке справочников API:
              </div>
              <ul className="list-inside list-disc space-y-1 text-xs">
                {dictionaries.warnings.map((w, idx) => (
                  <li key={idx}>{w}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}

      {errorMsg && (
        <div className="flex items-center gap-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          <XCircle className="h-5 w-5 shrink-0 text-red-600" />
          <span>{errorMsg}</span>
        </div>
      )}

      {successMsg && (
        <div className="flex items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm font-medium text-emerald-800">
          <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" />
          <span>{successMsg}</span>
        </div>
      )}

      {/* Секция 1: Статусы заказов RetailCRM */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h3 className="text-base font-semibold text-slate-900">
          1. Статусы заказов RetailCRM
        </h3>
        <p className="mt-0.5 text-xs text-slate-500">
          Выберите статусы, при переходе в которые заказ выгружается в КОМТЕТ, отменяется или закрывается после пробития чека
        </p>

        <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-2">
          {/* Триггерные статусы */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-700">
                Триггерные статусы отправки в КОМТЕТ ({triggerStatuses.length})
              </span>
            </div>
            <div className="max-h-60 space-y-1.5 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50/50 p-3">
              {statusesList.map((st) => {
                const checked = triggerStatuses.includes(st.code);
                return (
                  <label
                    key={st.code}
                    className={`flex cursor-pointer items-center justify-between rounded-lg px-2.5 py-1.5 text-xs transition ${
                      checked
                        ? "bg-blue-50 font-medium text-blue-900"
                        : "hover:bg-slate-100 text-slate-700"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() =>
                          toggleStatusInList(
                            st.code,
                            triggerStatuses,
                            setTriggerStatuses,
                          )
                        }
                        className="h-4 w-4 rounded border-slate-300 text-blue-600"
                      />
                      <span>{st.name}</span>
                    </div>
                    <span className="font-mono text-[11px] text-slate-400">
                      {st.code}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>

          {/* Статусы отмены */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-700">
                Статусы отмены заказа в КОМТЕТ ({cancelStatuses.length})
              </span>
            </div>
            <div className="max-h-60 space-y-1.5 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50/50 p-3">
              {statusesList.map((st) => {
                const checked = cancelStatuses.includes(st.code);
                return (
                  <label
                    key={st.code}
                    className={`flex cursor-pointer items-center justify-between rounded-lg px-2.5 py-1.5 text-xs transition ${
                      checked
                        ? "bg-red-50 font-medium text-red-900"
                        : "hover:bg-slate-100 text-slate-700"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() =>
                          toggleStatusInList(
                            st.code,
                            cancelStatuses,
                            setCancelStatuses,
                          )
                        }
                        className="h-4 w-4 rounded border-slate-300 text-red-600"
                      />
                      <span>{st.name}</span>
                    </div>
                    <span className="font-mono text-[11px] text-slate-400">
                      {st.code}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
        </div>

        {/* Финальный статус */}
        <div className="mt-4 max-w-md">
          <label className="mb-1 block text-xs font-semibold text-slate-700">
            Финальный статус заказа в RetailCRM после фискализации чека (state = done)
          </label>
          <select
            value={finalStatus}
            onChange={(e) => setFinalStatus(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
          >
            {statusesList.map((st) => (
              <option key={st.code} value={st.code}>
                {st.name} ({st.code})
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Секция 2: Таблица маппинга Курьеров */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-base font-semibold text-slate-900">
              2. Маппинг курьеров (RetailCRM ↔ КОМТЕТ Касса)
            </h3>
            <p className="mt-0.5 text-xs text-slate-500">
              Выберите сотрудника КОМТЕТ из списка или укажите его числовой ID вручную
            </p>
          </div>
        </div>

        <div className="mt-4 overflow-x-auto">
          <table className="w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs font-semibold text-slate-600">
                <th className="px-4 py-2.5">Курьер в RetailCRM</th>
                <th className="px-4 py-2.5">Сотрудник в КОМТЕТ (из списка)</th>
                <th className="px-4 py-2.5">ID курьера КОМТЕТ (вручную)</th>
                <th className="w-12 px-2 py-2.5"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {crmCouriersList.length === 0 ? (
                <tr>
                  <td
                    colSpan={4}
                    className="px-4 py-6 text-center text-xs text-slate-500"
                  >
                    В справочнике RetailCRM пока нет курьеров. Добавьте связку вручную ниже.
                  </td>
                </tr>
              ) : (
                crmCouriersList.map((c) => {
                  const currentVal = courierMap[c.code] || "";
                  return (
                    <tr key={c.code} className="hover:bg-slate-50/70">
                      <td className="px-4 py-2.5">
                        <div className="font-medium text-slate-900">
                          {c.name}
                        </div>
                        <div className="font-mono text-xs text-slate-400">
                          ID: {c.code}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <select
                          value={
                            komtetStaffList.some(
                              (emp) => String(emp.id) === currentVal,
                            )
                              ? currentVal
                              : ""
                          }
                          onChange={(e) =>
                            setCourierMap({
                              ...courierMap,
                              [c.code]: e.target.value,
                            })
                          }
                          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs focus:border-blue-600 focus:outline-none"
                        >
                          <option value="">— Не выбран / Ввод вручную —</option>
                          {komtetStaffList.map((emp) => (
                            <option key={String(emp.id)} value={String(emp.id)}>
                              {emp.name} (ID: {emp.id}
                              {emp.type ? `, ${emp.type}` : ""})
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-2.5">
                        <input
                          type="text"
                          value={currentVal}
                          onChange={(e) =>
                            setCourierMap({
                              ...courierMap,
                              [c.code]: e.target.value,
                            })
                          }
                          placeholder="Числовой ID курьера КОМТЕТ"
                          className="w-full rounded-lg border border-slate-300 px-3 py-1.5 font-mono text-xs focus:border-blue-600 focus:outline-none"
                        />
                      </td>
                      <td className="px-2 py-2.5 text-right">
                        {c.isCustom && (
                          <button
                            type="button"
                            onClick={() => handleRemoveCustomCourier(c.code)}
                            className="rounded-lg p-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                            title="Удалить строку"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Добавление курьера вручную */}
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
          <input
            type="text"
            value={newCourierCode}
            onChange={(e) => setNewCourierCode(e.target.value)}
            placeholder="ID курьера в RetailCRM"
            className="w-44 rounded-lg border border-slate-300 px-3 py-1.5 text-xs focus:border-blue-600 focus:outline-none"
          />
          <input
            type="text"
            value={newCourierName}
            onChange={(e) => setNewCourierName(e.target.value)}
            placeholder="Имя курьера (опционально)"
            className="w-56 rounded-lg border border-slate-300 px-3 py-1.5 text-xs focus:border-blue-600 focus:outline-none"
          />
          <button
            type="button"
            onClick={handleAddCustomCourier}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
          >
            <Plus className="h-3.5 w-3.5" />
            Добавить курьера вручную
          </button>
        </div>
      </div>

      {/* Секция 3 и 4: Типы оплат и Ставки НДС */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Таблица маппинга Типов оплат */}
        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h3 className="text-base font-semibold text-slate-900">
            3. Маппинг типов оплат
          </h3>
          <p className="mt-0.5 text-xs text-slate-500">
            Сопоставление способов оплаты RetailCRM с типами расчетов КОМТЕТ Кассы
          </p>

          <div className="mt-4 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-xs font-semibold text-slate-600">
                  <th className="px-3 py-2">Тип оплаты RetailCRM</th>
                  <th className="px-3 py-2">Способ оплаты КОМТЕТ</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {paymentTypesList.map((pt) => (
                  <tr key={pt.code} className="hover:bg-slate-50/70">
                    <td className="px-3 py-2.5">
                      <div className="font-medium text-slate-900">
                        {pt.name}
                      </div>
                      <div className="font-mono text-xs text-slate-400">
                        {pt.code}
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      <select
                        value={paymentMap[pt.code] || ""}
                        onChange={(e) =>
                          setPaymentMap({
                            ...paymentMap,
                            [pt.code]: e.target.value,
                          })
                        }
                        className="w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs focus:border-blue-600 focus:outline-none"
                      >
                        <option value="">— По умолчанию (card) —</option>
                        {KOMTET_PAYMENT_OPTIONS.map((opt) => (
                          <option key={opt.code} value={opt.code}>
                            {opt.name}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Таблица маппинга Ставок НДС */}
        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h3 className="text-base font-semibold text-slate-900">
            4. Маппинг ставок НДС
          </h3>
          <p className="mt-0.5 text-xs text-slate-500">
            Поддержка ставок НДС 22%, 20%, 10%, 0% и «Без НДС» по 54-ФЗ
          </p>

          <div className="mt-4 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-xs font-semibold text-slate-600">
                  <th className="px-3 py-2">Ставка в RetailCRM</th>
                  <th className="px-3 py-2">Ставка в КОМТЕТ Кассе</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {DEFAULT_VAT_ROWS.map((row) => (
                  <tr key={row.crmCode} className="hover:bg-slate-50/70">
                    <td className="px-3 py-2.5">
                      <div className="font-medium text-slate-900">
                        {row.crmName}
                      </div>
                      <div className="font-mono text-xs text-slate-400">
                        код: {row.crmCode}
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      <select
                        value={vatMap[row.crmCode] || row.komtetCode}
                        onChange={(e) =>
                          setVatMap({
                            ...vatMap,
                            [row.crmCode]: e.target.value,
                          })
                        }
                        className="w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs focus:border-blue-600 focus:outline-none"
                      >
                        {KOMTET_VAT_OPTIONS.map((opt) => (
                          <option key={opt.code} value={opt.code}>
                            {opt.name}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Кнопка сохранения */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => void handleSaveAll()}
          disabled={saving}
          className="inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60"
        >
          {saving ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          Сохранить маппинг и статусы
        </button>

        <div className="inline-flex items-center gap-1.5 text-xs text-slate-500">
          <GitCompareArrows className="h-4 w-4 text-slate-400" />
          Изменения применяются мгновенно для всех новых синхронизаций
        </div>
      </div>
    </div>
  );
};
