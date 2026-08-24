import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import { App } from './App.jsx';
import { PortalAuthProvider } from './lib/auth.jsx';
import './styles.css';

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <PortalAuthProvider>
        <App />
      </PortalAuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
