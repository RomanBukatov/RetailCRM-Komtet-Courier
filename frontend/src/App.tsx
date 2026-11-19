import React, { useCallback, useEffect, useState } from "react";
import {
  ArrowLeftRight,
  FileText,
  GitCompareArrows,
  KeyRound,
  ListOrdered,
  Lock,
  LogOut,
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
  const [needsAuth, setNeedsAuth] = useState(false);
  const [tokenInput, setTokenInput] = useState(getStoredToken());
  const [authError, setAuthError] = useState<string | null>(null);
  const [hasSavedToken, setHasSavedToken] = useState<boolean>(
    Boolean(getStoredToken()),
  );

  const loadSettings = useCallback(async () => {
    try {
      const res = await api.getSettings();
      setSettings(res);
      setNeedsAuth(false);
      setAuthError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setNeedsAuth(true);
        return;
      }
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
    setStoredToken(cleaned);
    setHasSavedToken(true);
    setAuthError(null);
    try {
      const res = await api.getSettings();
      setSettings(res);
      setNeedsAuth(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuthError("Неверный Admin API Token (401 Unauthorized)");
        setNeedsAuth(true);
      } else {
        setAuthError(
          err instanceof Error ? err.message : "Ошибка проверки токена",
        );
      }
    }
  };

  const handleLogoutOrSwitchToken = () => {
    setStoredToken("");
    setTokenInput("");
    setHasSavedToken(false);
    setNeedsAuth(true);
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      {/* Шапка */}
      <header className="border-b border-slate-200 bg-white shadow-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-600 text-white shadow-sm">
              <ArrowLeftRight className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-bold tracking-tight text-slate-900">
                  RetailCRM ↔ КОМТЕТ Касса Курьер
                </h1>
                <span className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-semibold text-blue-700">
                  v0.2
                </span>
              </div>
              <p className="text-xs text-slate-500">
                Сервис двусторонней синхронизации заказов курьерской доставки и фискализации 54-ФЗ
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {settings && (
              <span
                className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ${
                  settings.auto_sync_enabled
                    ? "bg-emerald-50 text-emerald-700"
                    : "bg-slate-100 text-slate-600"
                }`}
              >
                <span
                  className={`h-2 w-2 rounded-full ${
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
              className="inline-flex items-center gap-1.5 rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              {hasSavedToken ? (
                <>
                  <LogOut className="h-3.5 w-3.5 text-slate-500" />
                  Выйти / Сменить токен
                </>
              ) : (
                <>
                  <KeyRound className="h-3.5 w-3.5 text-slate-500" />
                  Ввести токен
                </>
              )}
            </button>
          </div>
        </div>

        {/* Навигационные вкладки */}
        {!needsAuth && (
          <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
            <nav className="-mb-px flex space-x-6 overflow-x-auto">
              <button
                type="button"
                onClick={() => setActiveTab("connection")}
                className={`inline-flex items-center gap-2 whitespace-nowrap border-b-2 py-3 text-sm font-medium transition ${
                  activeTab === "connection"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <Settings className="h-4 w-4" />
                Подключение и параметры
              </button>

              <button
                type="button"
                onClick={() => setActiveTab("mappings")}
                className={`inline-flex items-center gap-2 whitespace-nowrap border-b-2 py-3 text-sm font-medium transition ${
                  activeTab === "mappings"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <GitCompareArrows className="h-4 w-4" />
                Статусы и маппинг
              </button>

              <button
                type="button"
                onClick={() => setActiveTab("orders")}
                className={`inline-flex items-center gap-2 whitespace-nowrap border-b-2 py-3 text-sm font-medium transition ${
                  activeTab === "orders"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <ListOrdered className="h-4 w-4" />
                Заказы и ручной тест
              </button>

              <button
                type="button"
                onClick={() => setActiveTab("logs")}
                className={`inline-flex items-center gap-2 whitespace-nowrap border-b-2 py-3 text-sm font-medium transition ${
                  activeTab === "logs"
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
                }`}
              >
                <FileText className="h-4 w-4" />
                Журнал логов
              </button>
            </nav>
          </div>
        )}
      </header>

      {/* Основной контент */}
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        {needsAuth ? (
          <div className="mx-auto mt-12 max-w-md rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
            <div className="flex flex-col items-center text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
                <Lock className="h-6 w-6" />
              </div>
              <h2 className="mt-4 text-lg font-bold text-slate-900">
                Авторизация в панели управления
              </h2>
              <p className="mt-1 text-xs text-slate-500">
                Введите <span className="font-mono">ADMIN_API_TOKEN</span>, заданный в переменных окружения сервера. Токен сохраняется в{" "}
                <span className="font-mono">localStorage</span> вашего браузера.
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
                  className="w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm focus:border-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                />
              </div>

              {authError && (
                <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {authError}
                </div>
              )}

              <button
                type="submit"
                className="w-full rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700"
              >
                Войти в панель настроек
              </button>
            </form>
          </div>
        ) : (
          <>
            {activeTab === "connection" && (
              <ConnectionTab
                settings={settings}
                onSettingsUpdated={setSettings}
                onUnauthorized={() => setNeedsAuth(true)}
              />
            )}
            {activeTab === "mappings" && (
              <MappingsTab
                settings={settings}
                onSettingsUpdated={setSettings}
                onUnauthorized={() => setNeedsAuth(true)}
              />
            )}
            {activeTab === "orders" && (
              <OrdersTab onUnauthorized={() => setNeedsAuth(true)} />
            )}
            {activeTab === "logs" && (
              <LogsTab onUnauthorized={() => setNeedsAuth(true)} />
            )}
          </>
        )}
      </main>
    </div>
  );
};

export default App;
