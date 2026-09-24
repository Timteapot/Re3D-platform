import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  ApiError,
  login as loginRequest,
  logout as logoutRequest,
  refreshSession,
  register as registerRequest,
} from "./api";
import { requestJson, requestResponse } from "../api/http";
import type { LoginInput, RegisterInput, User } from "./types";

type AuthStatus = "loading" | "anonymous" | "authenticated";

interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  accessToken: string | null;
  restoreError: string | null;
  login: (input: LoginInput) => Promise<void>;
  register: (input: RegisterInput) => Promise<User>;
  logout: () => Promise<void>;
  retryRestore: () => Promise<void>;
  request: <T>(path: string, init?: RequestInit) => Promise<T>;
  fetchAuthorized: (path: string, init?: RequestInit) => Promise<Response>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function readableError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "无法连接认证服务，请检查后端是否已启动。";
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<User | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  const clearSession = useCallback(() => {
    setUser(null);
    setAccessToken(null);
    setStatus("anonymous");
  }, []);

  const retryRestore = useCallback(async () => {
    setStatus("loading");
    setRestoreError(null);
    try {
      const session = await refreshSession();
      setUser(session.user);
      setAccessToken(session.access_token);
      setStatus("authenticated");
    } catch (error) {
      clearSession();
      if (!(error instanceof ApiError && error.status === 401)) {
        setRestoreError(readableError(error));
      }
    }
  }, [clearSession]);

  useEffect(() => {
    void retryRestore();
  }, [retryRestore]);

  const login = useCallback(async (input: LoginInput) => {
    const session = await loginRequest(input);
    setUser(session.user);
    setAccessToken(session.access_token);
    setRestoreError(null);
    setStatus("authenticated");
  }, []);

  const register = useCallback((input: RegisterInput) => registerRequest(input), []);

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const request = useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      if (accessToken === null) {
        throw new ApiError(401, "当前会话没有可用的访问令牌");
      }
      try {
        return await requestJson<T>(path, init, accessToken);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 401)) {
          throw error;
        }
        try {
          const session = await refreshSession();
          setUser(session.user);
          setAccessToken(session.access_token);
          setStatus("authenticated");
          return await requestJson<T>(path, init, session.access_token);
        } catch (refreshError) {
          clearSession();
          throw refreshError;
        }
      }
    },
    [accessToken, clearSession],
  );

  const fetchAuthorized = useCallback(
    async (path: string, init: RequestInit = {}): Promise<Response> => {
      if (accessToken === null) {
        throw new ApiError(401, "当前会话没有可用的访问令牌");
      }
      let response = await requestResponse(path, init, accessToken);
      if (response.status !== 401) return response;
      try {
        const session = await refreshSession();
        setUser(session.user);
        setAccessToken(session.access_token);
        setStatus("authenticated");
        response = await requestResponse(path, init, session.access_token);
        return response;
      } catch (refreshError) {
        clearSession();
        throw refreshError;
      }
    },
    [accessToken, clearSession],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      accessToken,
      restoreError,
      login,
      register,
      logout,
      retryRestore,
      request,
      fetchAuthorized,
    }),
    [accessToken, fetchAuthorized, login, logout, register, request, restoreError, retryRestore, status, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
