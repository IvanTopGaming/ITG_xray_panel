export function assertPanelRole(expected: string): void {
  const injected = String(window.__PANEL_ROLE__ || '')
    .trim()
    .toLowerCase();
  const actual = injected === 'worker' ? 'worker' : 'master';
  if (actual === expected) return;

  const message =
    `This is the ${expected} panel bundle, but the container was started with ` +
    `PANEL_ROLE=${injected || '(unset)'}, which resolves to ${actual}. ` +
    `Deploy the ${actual} image on this host, or fix PANEL_ROLE.`;

  document.body.innerHTML = '';
  const box = document.createElement('pre');
  box.style.cssText =
    'margin:2rem;padding:1.5rem;border:1px solid #ef4444;border-radius:.5rem;' +
    'color:#fca5a5;background:#1a0a0a;font:14px/1.6 monospace;white-space:pre-wrap';
  box.textContent = message;
  document.body.appendChild(box);
  throw new Error(message);
}
