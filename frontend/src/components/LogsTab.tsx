import React, { useCallback, useEffect, useState } from "react";
import {
  ArrowLeftRight,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  RefreshCw,
  Search,
} from "lucide-react";
import { api, ApiError } from "../api";
import type { SyncLogItem } from "../types";

interface LogsTabProps {
  onUnauthorized: () => void;
}

const PAGE_LIMIT = 25;

function formatDateTime(iso: string): string {
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

function getLogBadge(item: SyncLogItem) {
  const skippedEvents = [
    "sync_skipped",
    "anti_loop_skipped",
    "callback_duplicate_skipped",
    "callback_state_update",
  ];
  if (item.level === "ERROR") {
    return (
      <span className="inline-flex items-center rounded-full bg-red-50 px-2.5 py-0.5 text-xs font-semibold text-red-700">
        ERROR
      </span>
    );
  }
  if (item.level === "WARNING") {
    return (
      <span className="inline-flex items-center rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-semibold text-amber-700">
        WARN
      </span>
    );
  }
  if (skippedEvents.includes(item.event_type)) {
    return (
      <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-600">
        SKIPPED
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
      SUCCESS
    </span>
  );
}

function getDirectionLabel(direction: string): string {
  if (direction === "crm_to_komtet" || direction === "retailcrm_to_komtet") {
    return "RetailCRM → КОМТЕТ";
  }
  if (direction === "komtet_to_crm" || direction === "komtet_to_retailcrm") {
    return "КОМТЕТ → RetailCRM";
  }
  return direction;
}

export const LogsTab: React.FC<LogsTabProps> = ({ onUnauthorized }) => {
  const [logs, setLogs] = useState<SyncLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("all");
  const [directionFilter, setDirectionFilter] = useState("all");
  const [orderIdInput, setOrderIdInput] = useState("");
  const [appliedOrderId, setAppliedOrderId] = useState<number | undefined>(
    undefined,
  );
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await api.getLogs({
        limit: PAGE_LIMIT,
        offset,
        status: statusFilter,
        direction: directionFilter,
        order_id: appliedOrderId,
      });
      setLogs(res.items);
      setTotal(res.total);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Не удалось загрузить журнал событий",
      );
    } finally {
      setLoading(false);
    }
  }, [offset, statusFilter, directionFilter, appliedOrderId, onUnauthorized]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  const handleOrderSearch = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = orderIdInput.trim();
    if (!trimmed) {
      setAppliedOrderId(undefined);
    } else {
      const num = parseInt(trimmed, 10);
      setAppliedOrderId(!Number.isNaN(num) && num > 0 ? num : undefined);
    }
    setOffset(0);
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              Журнал событий синхронизации ({total})
            </h2>
            <p className="text-xs text-slate-500">
              Кликните на любую запись журнала для просмотра JSON-пейлоада и деталей события
            </p>
          </div>

          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:flex lg:flex-wrap lg:items-center">
            {/* Фильтр по статусу */}
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setOffset(0);
              }}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs focus:border-blue-600 focus:outline-none lg:w-auto lg:py-1.5"
            >
              <option value="all">Статус: Все (all)</option>
              <option value="success">Успешные (success)</option>
              <option value="error">Ошибки (error)</option>
              <option value="skipped">Пропущенные (skipped)</option>
            </select>

            {/* Фильтр по направлению */}
            <select
              value={directionFilter}
              onChange={(e) => {
                setDirectionFilter(e.target.value);
                setOffset(0);
              }}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs focus:border-blue-600 focus:outline-none lg:w-auto lg:py-1.5"
            >
              <option value="all">Направление: Все (all)</option>
              <option value="retailcrm_to_komtet">
                RetailCRM → КОМТЕТ (retailcrm_to_komtet)
              </option>
              <option value="komtet_to_retailcrm">
                КОМТЕТ → RetailCRM (komtet_to_retailcrm)
              </option>
            </select>

            {/* Поиск по order_id */}
            <form
              onSubmit={handleOrderSearch}
              className="flex w-full items-stretch lg:w-auto"
            >
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-slate-400 lg:top-2" />
                <input
                  type="number"
                  min={1}
                  inputMode="numeric"
                  value={orderIdInput}
                  onChange={(e) => setOrderIdInput(e.target.value)}
                  placeholder="Поиск по ID заказа..."
                  className="w-full rounded-l-xl border border-slate-300 py-2 pl-8 pr-2.5 text-xs focus:border-blue-600 focus:outline-none lg:w-44 lg:py-1.5"
                />
              </div>
              <button
                type="submit"
                className="shrink-0 rounded-r-xl border border-l-0 border-slate-300 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-100 lg:py-1.5"
              >
                Найти
              </button>
            </form>

            {/* Кнопка Обновить */}
            <button
              type="button"
              onClick={() => void loadLogs()}
              disabled={loading}
              className="inline-flex w-full items-center justify-center gap-1.5 rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60 lg:w-auto lg:py-1.5"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 text-blue-600 ${loading ? "animate-spin" : ""}`}
              />
              Обновить
            </button>
          </div>
        </div>

        {errorMsg && (
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-800">
            {errorMsg}
          </div>
        )}

        {/* Мобильные карточки (< md) */}
        <div className="mt-4 space-y-3 md:hidden">
          {logs.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-8 text-center text-xs text-slate-500">
              {loading
                ? "Загрузка журнала..."
                : "Записей, подходящих под выбранные фильтры, не найдено"}
            </div>
          ) : (
            logs.map((log) => {
              const isExpanded = expandedId === log.id;
              return (
                <div
                  key={log.id}
                  onClick={() => setExpandedId(isExpanded ? null : log.id)}
                  className="min-w-0 cursor-pointer overflow-hidden rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm transition hover:border-slate-300"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      {getLogBadge(log)}
                      <span className="text-[11px] text-slate-500">
                        {formatDateTime(log.created_at)}
                      </span>
                    </div>
                    <span className="shrink-0 text-slate-400">
                      {isExpanded ? (
                        <ChevronUp className="h-4 w-4" />
                      ) : (
                        <ChevronDown className="h-4 w-4" />
                      )}
                    </span>
                  </div>

                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    <span className="inline-flex items-center gap-1 font-medium text-slate-700">
                      <ArrowLeftRight className="h-3.5 w-3.5 text-slate-400" />
                      {getDirectionLabel(log.direction)}
                    </span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
                      {log.event_type}
                    </span>
                    {(log.retailcrm_order_id || log.komtet_order_id) && (
                      <span className="font-mono text-[11px] font-medium text-slate-600">
                        {log.retailcrm_order_id
                          ? `CRM #${log.retailcrm_order_id}`
                          : "—"}
                        {log.komtet_order_id
                          ? ` / K #${log.komtet_order_id}`
                          : ""}
                      </span>
                    )}
                  </div>

                  <div
                    className={`mt-2 break-words text-xs text-slate-800 ${
                      isExpanded ? "" : "line-clamp-2"
                    }`}
                  >
                    {log.message}
                  </div>

                  {isExpanded && (
                    <div
                      onClick={(e) => e.stopPropagation()}
                      className="mt-3 space-y-2.5 border-t border-slate-100 pt-3 text-xs"
                    >
                      {log.error_details && (
                        <div>
                          <div className="font-semibold text-red-700">
                            Детали ошибки:
                          </div>
                          <pre className="mt-1 max-w-full overflow-x-auto rounded-lg border border-red-200 bg-red-50 p-2.5 font-mono text-[11px] text-red-900">
                            {log.error_details}
                          </pre>
                        </div>
                      )}

                      {log.payload && (
                        <div>
                          <div className="font-semibold text-slate-700">
                            JSON Payload:
                          </div>
                          <pre className="mt-1 max-h-80 max-w-full overflow-auto rounded-lg border border-slate-200 bg-slate-900 p-2.5 font-mono text-[11px] text-slate-100">
                            {JSON.stringify(log.payload, null, 2)}
                          </pre>
                        </div>
                      )}

                      {!log.error_details && !log.payload && (
                        <div className="text-[11px] text-slate-400">
                          Дополнительный JSON payload отсутствует.
                        </div>
                      )}
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
                <th className="px-3 py-2.5">Время</th>
                <th className="px-3 py-2.5">Статус</th>
                <th className="px-3 py-2.5">Направление</th>
                <th className="px-3 py-2.5">Событие</th>
                <th className="px-3 py-2.5">Заказ CRM / КОМТЕТ</th>
                <th className="px-3 py-2.5">Сообщение</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {logs.length === 0 ? (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-8 text-center text-xs text-slate-500"
                  >
                    {loading
                      ? "Загрузка журнала..."
                      : "Записей, подходящих под выбранные фильтры, не найдено"}
                  </td>
                </tr>
              ) : (
                logs.map((log) => {
                  const isExpanded = expandedId === log.id;
                  return (
                    <React.Fragment key={log.id}>
                      <tr
                        onClick={() =>
                          setExpandedId(isExpanded ? null : log.id)
                        }
                        className="cursor-pointer hover:bg-slate-50"
                      >
                        <td className="px-2 py-3 text-center text-slate-400">
                          {isExpanded ? (
                            <ChevronUp className="h-4 w-4" />
                          ) : (
                            <ChevronDown className="h-4 w-4" />
                          )}
                        </td>
                        <td className="whitespace-nowrap px-3 py-3 text-xs text-slate-500">
                          {formatDateTime(log.created_at)}
                        </td>
                        <td className="px-3 py-3">{getLogBadge(log)}</td>
                        <td className="whitespace-nowrap px-3 py-3 text-xs font-medium text-slate-700">
                          <span className="inline-flex items-center gap-1">
                            <ArrowLeftRight className="h-3.5 w-3.5 text-slate-400" />
                            {getDirectionLabel(log.direction)}
                          </span>
                        </td>
                        <td className="px-3 py-3 font-mono text-xs text-slate-700">
                          {log.event_type}
                        </td>
                        <td className="whitespace-nowrap px-3 py-3 font-mono text-xs text-slate-700">
                          {log.retailcrm_order_id
                            ? `CRM #${log.retailcrm_order_id}`
                            : "—"}
                          {log.komtet_order_id
                            ? ` / K #${log.komtet_order_id}`
                            : ""}
                        </td>
                        <td className="max-w-md px-3 py-3 text-xs text-slate-800">
                          <div className="line-clamp-1">{log.message}</div>
                        </td>
                      </tr>

                      {isExpanded && (
                        <tr className="bg-slate-50/90">
                          <td colSpan={7} className="px-6 py-4">
                            <div className="space-y-3 text-xs">
                              <div>
                                <div className="font-semibold text-slate-700">
                                  Полный текст сообщения:
                                </div>
                                <div className="mt-1 break-words rounded-lg border border-slate-200 bg-white p-2.5 text-slate-800">
                                  {log.message}
                                </div>
                              </div>

                              {log.error_details && (
                                <div>
                                  <div className="font-semibold text-red-700">
                                    Детали ошибки:
                                  </div>
                                  <pre className="mt-1 max-w-full overflow-x-auto rounded-lg border border-red-200 bg-red-50 p-2.5 font-mono text-[11px] text-red-900">
                                    {log.error_details}
                                  </pre>
                                </div>
                              )}

                              {log.payload && (
                                <div>
                                  <div className="font-semibold text-slate-700">
                                    JSON Payload:
                                  </div>
                                  <pre className="mt-1 max-h-80 max-w-full overflow-auto rounded-lg border border-slate-200 bg-slate-900 p-3 font-mono text-[11px] text-slate-100">
                                    {JSON.stringify(log.payload, null, 2)}
                                  </pre>
                                </div>
                              )}
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
