import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
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
    <div className="space-y-6">
      {/* Блок ручной отправки заказа */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="text-base font-semibold text-slate-900">
          Ручная синхронизация заказа из RetailCRM в КОМТЕТ Кассу
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Укажите внутренний ID заказа в RetailCRM для принудительного создания или обновления заказа у курьеров
        </p>

        <form
          onSubmit={handleManualSync}
          className="mt-4 flex flex-wrap items-center gap-3"
        >
          <input
            type="number"
            min={1}
            value={manualOrderId}
            onChange={(e) => setManualOrderId(e.target.value)}
            placeholder="ID заказа RetailCRM (например: 1200)"
            className="w-72 rounded-xl border border-slate-300 px-3.5 py-2 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
          />

          <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-slate-700">
            <input
              type="checkbox"
              checked={ignoreStatusFilter}
              onChange={(e) => setIgnoreStatusFilter(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-blue-600"
            />
            Игнорировать фильтр триггерных статусов
          </label>

          <button
            type="submit"
            disabled={syncingManual}
            className="inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60"
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
          <div className="mt-4 flex items-center gap-2.5 rounded-xl border border-red-200 bg-red-50 p-3.5 text-xs text-red-800">
            <XCircle className="h-4 w-4 shrink-0 text-red-600" />
            <span>{manualError}</span>
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
              <div className="space-y-1">
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
                  <div className="text-red-800">
                    Ошибка: {manualResult.result.error}
                  </div>
                )}
                {manualResult.result.warning && (
                  <div className="text-amber-800">
                    Предупреждение: {manualResult.result.warning}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Таблица синхронизированных заказов */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-slate-900">
              Синхронизированные заказы ({total})
            </h3>
            <p className="text-xs text-slate-500">
              Двусторонняя связка заказов RetailCRM и КОМТЕТ Кассы
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setOffset(0);
              }}
              className="rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-xs focus:border-blue-600 focus:outline-none"
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
              className="inline-flex items-center gap-1.5 rounded-xl border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
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

        <div className="mt-4 overflow-x-auto">
          <table className="w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs font-semibold text-slate-600">
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
                    colSpan={6}
                    className="px-4 py-8 text-center text-xs text-slate-500"
                  >
                    {loading
                      ? "Загрузка заказов..."
                      : "Записей синхронизации заказов пока нет"}
                  </td>
                </tr>
              ) : (
                orders.map((ord) => (
                  <tr key={ord.id} className="hover:bg-slate-50/70">
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
                    <td className="px-4 py-3 text-xs text-slate-500">
                      {formatDateTime(ord.updated_at)}
                    </td>
                    <td className="max-w-xs px-4 py-3 text-xs text-red-600">
                      {ord.last_error ? (
                        <span className="line-clamp-2" title={ord.last_error}>
                          {ord.last_error}
                        </span>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Пагинация */}
        {total > PAGE_LIMIT && (
          <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-3 text-xs text-slate-600">
            <div>
              Показано {offset + 1}–{Math.min(offset + PAGE_LIMIT, total)} из{" "}
              {total}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={offset === 0 || loading}
                onClick={() => setOffset(Math.max(0, offset - PAGE_LIMIT))}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 hover:bg-slate-50 disabled:opacity-40"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
                Назад
              </button>
              <button
                type="button"
                disabled={offset + PAGE_LIMIT >= total || loading}
                onClick={() => setOffset(offset + PAGE_LIMIT)}
                className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 hover:bg-slate-50 disabled:opacity-40"
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
