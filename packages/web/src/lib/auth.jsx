import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { api, getSessionKind, getToken, setSessionKind, setToken } from './api.js';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(Boolean(getToken()));

  useEffect(() => {
    if (!getToken()) {
      setLoading(false);
      return;
    }
    const isClient = getSessionKind() === 'client';
    (isClient ? api.clientMe() : api.me())
      .then(setUser)
      .catch(() => {
        setToken('');
        setSessionKind('');
        setUser(null);
      })
      .finally(() => setLoading(false));
  }, []);

  // One form, two logins: an address is a recruiter, anything else is a
  // client-dashboard username (which can never contain "@").
  const login = useCallback(async (identifier, password) => {
    const isClient = !identifier.includes('@');
    const result = isClient
      ? await api.clientLogin({ username: identifier, password })
      : await api.login({ email: identifier, password });
    setToken(result.access_token);
    setSessionKind(isClient ? 'client' : '');
    setUser(result.user);
    return result.user;
  }, []);

  const signup = useCallback(async (payload) => {
    const result = await api.signup(payload);
    setToken(result.access_token);
    setSessionKind('');
    setUser(result.user);
    return result.user;
  }, []);

  const logout = useCallback(() => {
    setToken('');
    setSessionKind('');
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, signup, logout }),
    [user, loading, login, signup, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>');
  return context;
}
