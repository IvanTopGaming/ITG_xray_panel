import React from 'react';
import ReactDOM from 'react-dom/client';
import { assertPanelRole } from '@ui/lib/assertPanelRole';
import App from './App.tsx';
import '@ui/index.css';

assertPanelRole(__EXPECTED_PANEL_ROLE__);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
