import { useEffect, useState } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { BottomNav } from './components/BottomNav.jsx';
import { Screen } from './components/ui.jsx';
import { api } from './lib/api.js';
import { useAuth } from './lib/auth.jsx';
import { ApplicationDetailPage, ApplicationsPage } from './pages/Applications.jsx';
import { DiscoverPage } from './pages/Discover.jsx';
import { NetworkingPage } from './pages/Networking.jsx';
import { DocumentsPage, ProfilePage } from './pages/Profile.jsx';
import { LoginPage, MagicLinkPage } from './pages/SignIn.jsx';

export function App() {
  const { signedIn, booting } = useAuth();
  const location = useLocation();
  const [needsReview, setNeedsReview] = useState(0);

  useEffect(() => {
    if (!signedIn) return;
    api
      .applications()
      .then((data) =>
        setNeedsReview(
          (data.applications || []).filter((a) =>
            ['prepared', 'awaiting_consent'].includes(a.status),
          ).length,
        ),
      )
      .catch(() => setNeedsReview(0));
  }, [signedIn, location.pathname]);

  if (booting) {
    return (
      <Screen>
        <div className="skel" style={{ height: 140 }} />
      </Screen>
    );
  }

  return (
    <div className="app">
      <Routes>
        {/* Always reachable: a magic link has to work whether or not an old
            session is still sitting in localStorage. */}
        <Route path="/m/:token" element={<MagicLinkPage />} />
        <Route path="/login" element={signedIn ? <Navigate to="/" replace /> : <LoginPage />} />

        {signedIn ? (
          <>
            <Route path="/" element={<DiscoverPage />} />
            <Route path="/applications" element={<ApplicationsPage />} />
            <Route path="/applications/:applicationId" element={<ApplicationDetailPage />} />
            <Route path="/networking" element={<NetworkingPage />} />
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/profile" element={<ProfilePage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </>
        ) : (
          <Route path="*" element={<Navigate to="/login" replace />} />
        )}
      </Routes>

      {signedIn && <BottomNav badge={needsReview} />}
    </div>
  );
}
