import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles/global.css';

function showBootError(message: string) {
  const root = document.getElementById('root');
  if (!root) return;
  root.innerHTML = '';
  const box = document.createElement('pre');
  box.setAttribute('role', 'alert');
  box.style.cssText = 'padding:24px;font:14px/1.5 monospace;color:#7a1f1f;background:#fdf1f1;white-space:pre-wrap;';
  box.textContent = `Thot n'a pas pu démarrer l'interface.\n${message}`;
  root.appendChild(box);
}

window.addEventListener('error', (event) => {
  if (document.getElementById('root')?.childNodes.length) return;
  showBootError(String(event.message || event.error || 'Erreur inconnue'));
});

try {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
} catch (error) {
  showBootError(error instanceof Error ? error.message : String(error));
}
