import { Navigate, Route, Routes } from 'react-router-dom';

import { Rail, TabBar } from './components/Nav.jsx';
import { Screen, Skeleton } from './components/ui.jsx';
import { useAuth } from './lib/auth.jsx';
import { ApplicationsProvider, useApplications } from './lib/store.jsx';
import { ApplicationDetailPage, ApplicationsPage } from './pages/Applications.jsx';
import { DiscoverPage } from './pages/Discover.jsx';
import { DocumentsPage } from './pages/Documents.jsx';
import { HomePage } from './pages/Home.jsx';
import { InboxPage } from './pages/Inbox.jsx';
import { NetworkPage } from './pages/Network.jsx';
import { ProfilePage } from './pages/Profile.jsx';
import { LoginPage, MagicLinkPage } from './pages/SignIn.jsx';

function SignedIn({ profile }) {
  const { needsYou } = useApplications();
  return (
    <div className="app">
      <Rail badge={needsYou} profile={profile} />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/discover" element={<DiscoverPage />} />
        <Route path="/applications" element={<ApplicationsPage />} />
        <Route path="/applications/:applicationId" element={<ApplicationDetailPage />} />
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/network" element={<NetworkPage />} />
        {/* The tab was called Networking until the People tab existed; old
            links out of texts and emails still point at it. */}
        <Route path="/networking" element={<Navigate to="/network" replace />} />
        <Route path="/documents" element={<DocumentsPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <TabBar badge={needsYou} />
    </div>
  );
}

export function App() {
  const { signedIn, booting, profile } = useAuth();

  if (booting) {
    return (
      <Screen>
        <div className="skel" style={{ height: 132, marginBottom: 16 }} />
        <Skeleton />
      </Screen>
    );
  }

  if (!signedIn) {
    return (
      <Routes>
        {/* Always reachable: a magic link has to work whether or not an old
            session is still sitting in localStorage. */}
        <Route path="/m/:token" element={<MagicLinkPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <ApplicationsProvider>
      <Routes>
        <Route path="/m/:token" element={<MagicLinkPage />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<SignedIn profile={profile} />} />
      </Routes>
    </ApplicationsProvider>
  );
}
