import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowLeftRight,
  FileText,
  GitCompareArrows,
  KeyRound,
  ListOrdered,
  Loader2,
  Lock,
  LogOut,
  RefreshCw,
  Settings,
} from "lucide-react";
import { api, ApiError, getStoredToken, setStoredToken } from "./api";
import { ConnectionTab } from "./components/ConnectionTab";
import { LogsTab } from "./components/LogsTab";
import { MappingsTab } from "./components/MappingsTab";
import { OrdersTab } from "./components/OrdersTab";
import type { SettingsResponse } from "./types";

type ActiveTab = "connection" | "mappings" | "orders" | "logs";

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<ActiveTab>("connection");
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [tokenInput, setTokenInput] = useState(getStoredToken());
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [hasSavedToken, setHasSavedToken] = useState<boolean>(
    Boolean(getStoredToken()),
  );

  const handleUnauthorized = useCallback(() => {
    setNeedsAuth(true);
  }, []);

  const loadSettings = useCallback(async () => {
    setInitialLoading(true);
    setFatalError(null);
    try {
      const res = await api.getSettings();
      setSettings(res);
      setNeedsAuth(false);
      setAuthError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setStoredToken("");
        setHasSavedToken(false);
        setSettings(null);
        setNeedsAuth(true);
        return;
      }
      setSettings(null);
      setFatalError(
        err instanceof Error
          ? err.message
          : "Не удалось загрузить настройки с сервера",
      );
    } finally {
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const handleLoginSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const cleaned = tokenInput.trim();
    if (!cleaned) {
      setAuthError("Введите Admin API Token");
      return;
    }
    setAuthLoading(true);
    setAuthError(null);
    try {
      const res = await api.getSettings(cleaned);
      setStoredToken(cleaned);
      setHasSavedToken(true);
      setSettings(res);
      setNeedsAuth(false);
      setFatalError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setStoredToken("");
        setHasSavedToken(false);
        setAuthError("Неверный Admin API Token (401 Unauthorized)");
        setNeedsAuth(true);
      } else {
        setAuthError(
          err instanceof Error ? err.message : "Ошибка проверки токена",
        );
      }
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogoutOrSwitchToken = () => {
    setStoredToken("");
    setTokenInput("");
    setHasSavedToken(false);
    setSettings(null);
    setFatalError(null);
    setAuthError(null);
    setNeedsAuth(true);
  };

  const canRenderWorkspace =
    !needsAuth && !initialLoading && !fatalError && settings !== null;

  return (
    <div className="min-h-screen overflow-x-hidden bg-slate-50 text-slate-900">
      {/* Шапка */}
      <header className="border-b border-slate-200 bg-white shadow-sm">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3.5 sm:px-6 sm:py-4 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div className="flex min-w-0 items-start gap-3 sm:items-center">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-white shadow-sm">
              <ArrowLeftRight className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h1 className="text-base font-bold tracking-tight text-slate-900 sm:text-lg">
                  RetailCRM ↔ КОМТЕТ Касса Курьер
                </h1>
                <span className="shrink-0 rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-semibold text-blue-700">
                  v0.2
                </span>
              </div>
              <p className="mt-0.5 text-xs text-slate-500">
                Сервис двусторонней синхронизации заказов курьерской доставки и фискализации 54-ФЗ
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 sm:gap-3">
            {settings && (
              <span
                className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium ${
                  settings.auto_sync_enabled
                    ? "bg-emerald-50 text-emerald-700"
                    : "bg-slate-100 text-slate-600"
                }`}
              >
                <span
                  className={`h-2 w-2 shrink-0 rounded-full ${
                    settings.auto_sync_enabled
                      ? "bg-emerald-500"
                      : "bg-slate-400"
                  }`}
                />
                {settings.auto_sync_enabled
                  ? "Автосинхронизация активна"
                  : "Автосинхронизация на паузе"}
              </span>
            )}

            <button
              type="button"
              onClick={handleLogoutOrSwitchToken}
              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              {hasSavedToken ? (
                <>
                  <LogOut className="h-3.5 w-3.5 shrink-0 text-slate-500" />
                  Выйти / Сменить токен
                </>
              ) : (
                <>
                  <KeyRound className="h-3.5 w-3.5 shrink-0 text-slate-500" />
                  Ввести токен
                </>
              )}
            </button>
          </div>
        </div>

        {/* Навигационные вкладки */}
        {canRenderWorkspace && (
          <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
            <nav
              aria-label="Основные разделы"
              className="-mx-4 -mb-px flex gap-1 overflow-x-auto whitespace-nowrap px-4 no-scrollbar sm:mx-0 sm:gap-6 sm:px-0"
            >
              <button
                type="button"
                onClick={() => setActiveTab("connection")}
                className={`inline-flex min-h-[44px] shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium transition sm:px-0 ${
                  activeTab === "connection"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <Settings className="h-4 w-4 shrink-0" />
                Подключение и параметры
              </button>

              <button
                type="button"
                onClick={() => setActiveTab("mappings")}
                className={`inline-flex min-h-[44px] shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium transition sm:px-0 ${
                  activeTab === "mappings"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <GitCompareArrows className="h-4 w-4 shrink-0" />
                Статусы и маппинг
              </button>

              <button
                type="button"
                onClick={() => setActiveTab("orders")}
                className={`inline-flex min-h-[44px] shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium transition sm:px-0 ${
                  activeTab === "orders"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <ListOrdered className="h-4 w-4 shrink-0" />
                Заказы и ручной тест
              </button>

              <button
                type="button"
                onClick={() => setActiveTab("logs")}
                className={`inline-flex min-h-[44px] shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium transition sm:px-0 ${
                  activeTab === "logs"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <FileText className="h-4 w-4 shrink-0" />
                Журнал логов
              </button>
            </nav>
          </div>
        )}
      </header>

      {/* Основной контент */}
      <main className="mx-auto max-w-7xl px-4 py-5 sm:px-6 sm:py-6 lg:px-8">
        {needsAuth ? (
          <div className="mx-auto mt-6 max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-sm sm:mt-12 sm:p-8">
            <div className="flex flex-col items-center text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
                <Lock className="h-6 w-6" />
              </div>
              <h2 className="mt-4 text-lg font-bold text-slate-900">
                Авторизация в панели управления
              </h2>
              <p className="mt-1 text-xs text-slate-500">
                Введите <span className="font-mono">ADMIN_API_TOKEN</span>, заданный в переменных окружения сервера. Токен сохраняется в{" "}
                <span className="font-mono">localStorage</span> вашего браузера только после успешной проверки.
              </p>
            </div>

            <form onSubmit={handleLoginSubmit} className="mt-6 space-y-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Admin API Token
                </label>
                <input
                  type="password"
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  placeholder="Введите секретный токен администратора"
                  autoFocus
                  disabled={authLoading}
                  className="w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20 disabled:opacity-60"
                />
              </div>

              {authError && (
                <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {authError}
                </div>
              )}

              <button
                type="submit"
                disabled={authLoading}
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:opacity-60"
              >
                {authLoading && <Loader2 className="h-4 w-4 animate-spin" />}
                Войти в панель настроек
              </button>
            </form>
          </div>
        ) : initialLoading ? (
          <div className="mx-auto mt-10 flex max-w-md flex-col items-center rounded-2xl border border-slate-200 bg-white p-8 text-center shadow-sm sm:mt-16">
            <Loader2 className="h-8 w-8 animate-spin text-blue-600" />
            <h2 className="mt-4 text-base font-semibold text-slate-900">
              Загрузка конфигурации...
            </h2>
            <p className="mt-1 text-xs text-slate-500">
              Получаем актуальные параметры интеграции с сервера
            </p>
          </div>
        ) : fatalError || !settings ? (
          <div className="mx-auto mt-8 max-w-lg rounded-2xl border border-red-200 bg-white p-6 text-center shadow-sm sm:mt-14 sm:p-8">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-red-50 text-red-600">
              <AlertTriangle className="h-6 w-6" />
            </div>
            <h2 className="mt-4 text-base font-bold text-slate-900">
              Не удалось загрузить настройки сервиса
            </h2>
            <p className="mt-2 break-words text-sm text-slate-600">
              {fatalError || "Сервер вернул пустой ответ"}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Редактирование временно заблокировано, чтобы избежать перезаписи настроек пустыми значениями.
            </p>
            <div className="mt-6 flex flex-col justify-center gap-2.5 sm:flex-row">
              <button
                type="button"
                onClick={() => void loadSettings()}
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 sm:w-auto"
              >
                <RefreshCw className="h-4 w-4" />
                Повторить
              </button>
              <button
                type="button"
                onClick={handleLogoutOrSwitchToken}
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-5 py-2.5 text-sm font-medium text-slate-700 hover:bg-slate-50 sm:w-auto"
              >
                <KeyRound className="h-4 w-4 text-slate-500" />
                Сменить токен
              </button>
            </div>
          </div>
        ) : (
          <>
            {activeTab === "connection" && (
              <ConnectionTab
                settings={settings}
                onSettingsUpdated={setSettings}
                onUnauthorized={handleUnauthorized}
              />
            )}
            {activeTab === "mappings" && (
              <MappingsTab
                settings={settings}
                onSettingsUpdated={setSettings}
                onUnauthorized={handleUnauthorized}
              />
            )}
            {activeTab === "orders" && (
              <OrdersTab onUnauthorized={handleUnauthorized} />
            )}
            {activeTab === "logs" && (
              <LogsTab onUnauthorized={handleUnauthorized} />
            )}
          </>
        )}
      </main>
    </div>
  );
};

export default App;
