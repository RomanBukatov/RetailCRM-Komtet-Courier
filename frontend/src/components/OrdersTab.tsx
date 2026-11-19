import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Loader2,
  RefreshCw,
  Send,
  XCircle,
} from "lucide-react";
import { api, ApiError } from "../api";
import type { ManualSyncResponse, OrderSyncItem } from "../types";

interface OrdersTabProps {
  onUnauthorized: () => void;
}

const PAGE_LIMIT = 20;

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

function getSyncStatusBadge(status: string) {
  switch (status) {
    case "completed":
    case "paid":
      return (
        <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
          Выполнен ({status})
        </span>
      );
    case "synced":
      return (
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-semibold text-blue-700">
          Синхронизирован
        </span>
      );
    case "canceled":
      return (
        <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-700">
          Отменен
        </span>
      );
    case "error":
      return (
        <span className="inline-flex items-center rounded-full bg-red-50 px-2.5 py-0.5 text-xs font-semibold text-red-700">
          Ошибка
        </span>
      );
    default:
      return (
        <span className="inline-flex items-center rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-semibold text-amber-700">
          {status}
        </span>
      );
  }
}

export const OrdersTab: React.FC<OrdersTabProps> = ({ onUnauthorized }) => {
  const [manualOrderId, setManualOrderId] = useState("");
  const [ignoreStatusFilter, setIgnoreStatusFilter] = useState(false);
  const [syncingManual, setSyncingManual] = useState(false);
  const [manualResult, setManualResult] = useState<ManualSyncResponse | null>(
    null,
  );
  const [manualError, setManualError] = useState<string | null>(null);

  const [orders, setOrders] = useState<OrderSyncItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const loadOrders = useCallback(
    async (newOffset = offset, newStatus = statusFilter) => {
      setLoading(true);
      setListError(null);
      try {
        const res = await api.getOrdersSync({
          limit: PAGE_LIMIT,
          offset: newOffset,
          sync_status: newStatus || undefined,
        });
        setOrders(res.items);
        setTotal(res.total);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onUnauthorized();
          return;
        }
        setListError(
          err instanceof Error
            ? err.message
            : "Не удалось загрузить таблицу заказов",
        );
      } finally {
        setLoading(false);
      }
    },
    [offset, statusFilter, onUnauthorized],
  );

  useEffect(() => {
    void loadOrders(offset, statusFilter);
  }, [loadOrders, offset, statusFilter]);

  const handleManualSync = async (e: React.FormEvent) => {
    e.preventDefault();
    const parsedId = parseInt(manualOrderId.trim(), 10);
    if (Number.isNaN(parsedId) || parsedId <= 0) {
      setManualError("Введите корректный положительный ID заказа RetailCRM");
      return;
    }

    setSyncingManual(true);
    setManualError(null);
    setManualResult(null);
    try {
      const res = await api.manualSyncOrder(parsedId, ignoreStatusFilter);
      setManualResult(res);
      await loadOrders(0, statusFilter);
      setOffset(0);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      setManualError(
        err instanceof Error ? err.message : "Ошибка при ручной отправке заказа",
      );
    } finally {
      setSyncingManual(false);
    }
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      {/* Блок ручной отправки заказа */}
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
        <h2 className="text-base font-semibold text-slate-900">
          Ручная синхронизация заказа из RetailCRM в КОМТЕТ Кассу
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Укажите внутренний ID заказа в RetailCRM для принудительного создания или обновления заказа у курьеров
        </p>

        <form
          onSubmit={handleManualSync}
          className="mt-4 grid grid-cols-1 gap-3 md:flex md:flex-wrap md:items-center"
        >
          <input
            type="number"
            min={1}
            inputMode="numeric"
            value={manualOrderId}
            onChange={(e) => setManualOrderId(e.target.value)}
            placeholder="ID заказа RetailCRM (например: 1200)"
            className="w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20 md:w-72 md:py-2"
          />

          <label className="flex cursor-pointer items-center gap-2 py-1 text-xs font-medium text-slate-700">
            <input
              type="checkbox"
              checked={ignoreStatusFilter}
              onChange={(e) => setIgnoreStatusFilter(e.target.checked)}
              className="h-4 w-4 shrink-0 rounded border-slate-300 text-blue-600"
            />
            Игнорировать фильтр триггерных статусов
          </label>

          <button
            type="submit"
            disabled={syncingManual}
            className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60 md:w-auto md:py-2"
          >
            {syncingManual ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
            Отправить заказ в КОМТЕТ вручную
          </button>
        </form>

        {manualError && (
          <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-red-200 bg-red-50 p-3.5 text-xs text-red-800">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
            <span className="min-w-0 break-words">{manualError}</span>
          </div>
        )}

        {manualResult && (
          <div
            className={`mt-4 rounded-xl border p-4 text-xs ${
              manualResult.ok
                ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                : "border-red-200 bg-red-50 text-red-900"
            }`}
          >
            <div className="flex items-start gap-2.5">
              {manualResult.ok ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
              ) : (
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
              )}
              <div className="min-w-0 space-y-1">
                <div className="font-semibold">
                  Результат синхронизации заказа #
                  {manualResult.result.retailcrm_order_id}:{" "}
                  <span className="uppercase">
                    {manualResult.result.status}
                  </span>
                  {manualResult.result.reason
                    ? ` (${manualResult.result.reason})`
                    : ""}
                </div>
                {manualResult.result.komtet_order_id && (
                  <div>
                    ID заказа в КОМТЕТ Кассе:{" "}
                    <span className="font-mono font-semibold">
                      #{manualResult.result.komtet_order_id}
                    </span>
                  </div>
                )}
                {manualResult.result.error && (
                  <div className="break-words text-red-800">
                    Ошибка: {manualResult.result.error}
                  </div>
                )}
                {manualResult.result.warning && (
                  <div className="break-words text-amber-800">
                    Предупреждение: {manualResult.result.warning}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Таблица / карточки синхронизированных заказов */}
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="text-base font-semibold text-slate-900">
              Синхронизированные заказы ({total})
            </h3>
            <p className="text-xs text-slate-500">
              Двусторонняя связка заказов RetailCRM и КОМТЕТ Кассы. Нажмите на запись для просмотра подробностей.
            </p>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:flex sm:flex-wrap sm:items-center">
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setOffset(0);
              }}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs focus:border-blue-600 focus:outline-none sm:w-auto sm:py-1.5"
            >
              <option value="">Все статусы связки</option>
              <option value="synced">Синхронизирован (synced)</option>
              <option value="completed">Выполнен (completed)</option>
              <option value="canceled">Отменен (canceled)</option>
              <option value="error">Ошибка (error)</option>
              <option value="pending">В ожидании (pending)</option>
            </select>

            <button
              type="button"
              onClick={() => void loadOrders(offset, statusFilter)}
              disabled={loading}
              className="inline-flex w-full items-center justify-center gap-1.5 rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60 sm:w-auto sm:py-1.5"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 text-blue-600 ${loading ? "animate-spin" : ""}`}
              />
              Обновить
            </button>
          </div>
        </div>

        {listError && (
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-800">
            {listError}
          </div>
        )}

        {/* Мобильные карточки (< md) */}
        <div className="mt-4 space-y-3 md:hidden">
          {orders.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-8 text-center text-xs text-slate-500">
              {loading
                ? "Загрузка заказов..."
                : "Записей синхронизации заказов пока нет"}
            </div>
          ) : (
            orders.map((ord) => {
              const isExpanded = expandedId === ord.id;
              return (
                <div
                  key={ord.id}
                  onClick={() => setExpandedId(isExpanded ? null : ord.id)}
                  className="min-w-0 cursor-pointer overflow-hidden rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm transition hover:border-slate-300"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 font-mono text-sm font-semibold text-slate-900">
                        <span>CRM #{ord.retailcrm_order_id}</span>
                        {ord.retailcrm_order_number &&
                          ord.retailcrm_order_number !==
                            String(ord.retailcrm_order_id) && (
                            <span className="font-sans text-xs font-normal text-slate-500">
                              (№{ord.retailcrm_order_number})
                            </span>
                          )}
                      </div>
                      {ord.retailcrm_site && (
                        <div className="text-[11px] text-slate-400">
                          {ord.retailcrm_site}
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {getSyncStatusBadge(ord.sync_status)}
                      <span className="text-slate-400">
                        {isExpanded ? (
                          <ChevronUp className="h-4 w-4" />
                        ) : (
                          <ChevronDown className="h-4 w-4" />
                        )}
                      </span>
                    </div>
                  </div>

                  <div className="mt-2.5 grid grid-cols-2 gap-2 border-t border-slate-100 pt-2.5 text-xs">
                    <div>
                      <div className="text-[11px] text-slate-400">
                        ID в КОМТЕТ
                      </div>
                      <div className="font-mono font-medium text-slate-800">
                        {ord.komtet_order_id ? `#${ord.komtet_order_id}` : "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] text-slate-400">
                        Статус КОМТЕТ
                      </div>
                      <div className="font-mono text-slate-700">
                        {ord.komtet_state || "—"}
                      </div>
                    </div>
                    <div className="col-span-2">
                      <div className="text-[11px] text-slate-400">Обновлен</div>
                      <div className="text-slate-600">
                        {formatDateTime(ord.updated_at)}
                      </div>
                    </div>
                  </div>

                  {ord.last_error && !isExpanded && (
                    <div className="mt-2 line-clamp-2 rounded-lg bg-red-50 px-2.5 py-1.5 text-xs text-red-700">
                      {ord.last_error}
                    </div>
                  )}

                  {isExpanded && (
                    <div
                      onClick={(e) => e.stopPropagation()}
                      className="mt-3 space-y-2.5 border-t border-slate-100 pt-3 text-xs"
                    >
                      {ord.last_error && (
                        <div>
                          <div className="font-semibold text-red-700">
                            Последняя ошибка:
                          </div>
                          <pre className="mt-1 max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-lg border border-red-200 bg-red-50 p-2.5 font-mono text-[11px] text-red-900">
                            {ord.last_error}
                          </pre>
                        </div>
                      )}
                      <div>
                        <div className="font-semibold text-slate-700">
                          JSON связки заказа:
                        </div>
                        <pre className="mt-1 max-h-64 max-w-full overflow-auto rounded-lg border border-slate-200 bg-slate-900 p-2.5 font-mono text-[11px] text-slate-100">
                          {JSON.stringify(ord, null, 2)}
                        </pre>
                      </div>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Десктопная таблица (md+) */}
        <div className="mt-4 hidden overflow-x-auto md:block">
          <table className="w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs font-semibold text-slate-600">
                <th className="w-8 px-2 py-2.5"></th>
                <th className="px-4 py-2.5">ID заказа RetailCRM</th>
                <th className="px-4 py-2.5">ID заказа КОМТЕТ</th>
                <th className="px-4 py-2.5">Статус синхронизации</th>
                <th className="px-4 py-2.5">Статус в КОМТЕТ</th>
                <th className="px-4 py-2.5">Обновлен</th>
                <th className="px-4 py-2.5">Последняя ошибка</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-8 text-center text-xs text-slate-500"
                  >
                    {loading
                      ? "Загрузка заказов..."
                      : "Записей синхронизации заказов пока нет"}
                  </td>
                </tr>
              ) : (
                orders.map((ord) => {
                  const isExpanded = expandedId === ord.id;
                  return (
                    <React.Fragment key={ord.id}>
                      <tr
                        onClick={() =>
                          setExpandedId(isExpanded ? null : ord.id)
                        }
                        className="cursor-pointer hover:bg-slate-50/70"
                      >
                        <td className="px-2 py-3 text-center text-slate-400">
                          {isExpanded ? (
                            <ChevronUp className="h-4 w-4" />
                          ) : (
                            <ChevronDown className="h-4 w-4" />
                          )}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs font-semibold text-slate-900">
                          #{ord.retailcrm_order_id}
                          {ord.retailcrm_order_number &&
                          ord.retailcrm_order_number !==
                            String(ord.retailcrm_order_id) ? (
                            <span className="ml-1.5 font-sans font-normal text-slate-500">
                              (№{ord.retailcrm_order_number})
                            </span>
                          ) : null}
                          {ord.retailcrm_site && (
                            <div className="font-sans text-[11px] font-normal text-slate-400">
                              {ord.retailcrm_site}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs text-slate-700">
                          {ord.komtet_order_id ? `#${ord.komtet_order_id}` : "—"}
                        </td>
                        <td className="px-4 py-3">
                          {getSyncStatusBadge(ord.sync_status)}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs text-slate-600">
                          {ord.komtet_state || "—"}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-xs text-slate-500">
                          {formatDateTime(ord.updated_at)}
                        </td>
                        <td className="max-w-xs px-4 py-3 text-xs text-red-600">
                          {ord.last_error ? (
                            <span
                              className="line-clamp-2"
                              title={ord.last_error}
                            >
                              {ord.last_error}
                            </span>
                          ) : (
                            <span className="text-slate-400">—</span>
                          )}
                        </td>
                      </tr>

                      {isExpanded && (
                        <tr className="bg-slate-50/90">
                          <td colSpan={7} className="px-6 py-4">
                            <div className="space-y-3 text-xs">
                              {ord.last_error && (
                                <div>
                                  <div className="font-semibold text-red-700">
                                    Полный текст ошибки:
                                  </div>
                                  <pre className="mt-1 max-w-full overflow-x-auto whitespace-pre-wrap rounded-lg border border-red-200 bg-red-50 p-2.5 font-mono text-[11px] text-red-900">
                                    {ord.last_error}
                                  </pre>
                                </div>
                              )}
                              <div>
                                <div className="font-semibold text-slate-700">
                                  JSON связки заказа:
                                </div>
                                <pre className="mt-1 max-h-72 max-w-full overflow-auto rounded-lg border border-slate-200 bg-slate-900 p-3 font-mono text-[11px] text-slate-100">
                                  {JSON.stringify(ord, null, 2)}
                                </pre>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Пагинация */}
        {total > PAGE_LIMIT && (
          <div className="mt-4 flex flex-col gap-2 border-t border-slate-100 pt-3 text-xs text-slate-600 sm:flex-row sm:items-center sm:justify-between">
            <div>
              Показано {offset + 1}–{Math.min(offset + PAGE_LIMIT, total)} из{" "}
              {total}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={offset === 0 || loading}
                onClick={() => setOffset(Math.max(0, offset - PAGE_LIMIT))}
                className="inline-flex flex-1 items-center justify-center gap-1 rounded-lg border border-slate-300 px-3 py-1.5 hover:bg-slate-50 disabled:opacity-40 sm:flex-none sm:py-1"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
                Назад
              </button>
              <button
                type="button"
                disabled={offset + PAGE_LIMIT >= total || loading}
                onClick={() => setOffset(offset + PAGE_LIMIT)}
                className="inline-flex flex-1 items-center justify-center gap-1 rounded-lg border border-slate-300 px-3 py-1.5 hover:bg-slate-50 disabled:opacity-40 sm:flex-none sm:py-1"
              >
                Вперед
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
