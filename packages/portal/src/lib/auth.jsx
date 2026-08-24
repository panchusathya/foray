import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { ApiError, api, getToken, setToken } from './api.js';

const AuthContext = createContext(null);

export function PortalAuthProvider({ children }) {
  const [profile, setProfile] = useState(null);
  const [booting, setBooting] = useState(true);

  const load = useCallback(async () => {
    if (!getToken()) {
      setProfile(null);
      setBooting(false);
      return;
    }
    try {
      const data = await api.me();
      setProfile(data);
    } catch (error) {
      // A 401 means the session expired or was revoked; anything else is
      // transient and must not log somebody out of a working session.
      if (error instanceof ApiError && error.status === 401) {
        setToken('');
        setProfile(null);
      }
    } finally {
      setBooting(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const signIn = useCallback(
    async (token) => {
      setToken(token);
      setBooting(true);
      await load();
    },
    [load],
  );

  const signOut = useCallback(() => {
    setToken('');
    setProfile(null);
  }, []);

  const value = useMemo(
    () => ({ profile, booting, signedIn: Boolean(profile), signIn, signOut, reload: load }),
    [profile, booting, signIn, signOut, load],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside PortalAuthProvider');
  return value;
}
