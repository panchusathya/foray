import { IntakeWidget } from './widget.js';

const STYLE = `
.jb-intake{font:14px/1.4 system-ui,sans-serif;border:1px solid #d0d7de;border-radius:12px;max-width:360px;background:#fff;color:#1f2328;box-shadow:0 8px 24px rgba(0,0,0,.08);overflow:hidden}
.jb-intake-header{padding:12px 14px;font-weight:600;border-bottom:1px solid #d0d7de;background:#f6f8fa}
.jb-intake-error{padding:10px 14px;background:#fff8f0;color:#9a6700;border-bottom:1px solid #f0dcc2}
.jb-intake-messages{height:280px;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#fff}
.jb-intake-msg{max-width:85%;padding:8px 10px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.jb-intake-msg--outbound{align-self:flex-start;background:#f6f8fa}
.jb-intake-msg--inbound{align-self:flex-end;background:#ddf4ff}
.jb-intake-form{display:flex;gap:8px;padding:12px;border-top:1px solid #d0d7de}
.jb-intake-input{flex:1;padding:8px 10px;border:1px solid #d0d7de;border-radius:8px}
.jb-intake-send{padding:8px 12px;border:0;border-radius:8px;background:#0969da;color:#fff;font-weight:600;cursor:pointer}
.jb-intake-send:disabled,.jb-intake-input:disabled{opacity:.6;cursor:not-allowed}
`;

function injectStyles() {
  if (document.getElementById('juicebox-intake-widget-styles')) {
    return;
  }
  const style = document.createElement('style');
  style.id = 'juicebox-intake-widget-styles';
  style.textContent = STYLE;
  document.head.appendChild(style);
}

function mountFromScript(script) {
  const token = script.getAttribute('data-token') || '';
  const baseUrl = script.getAttribute('data-base-url') || '';
  const targetId = script.getAttribute('data-target') || 'juicebox-intake-widget';
  let mount = document.getElementById(targetId);
  if (!mount) {
    mount = document.createElement('div');
    mount.id = targetId;
    script.insertAdjacentElement('afterend', mount);
  }
  injectStyles();
  return new IntakeWidget({ token, baseUrl, mount });
}

export function init(options) {
  injectStyles();
  const mount =
    typeof options.mount === 'string'
      ? document.querySelector(options.mount)
      : options.mount;
  if (!mount) {
    throw new Error('JuiceboxIntakeWidget.init: mount element not found');
  }
  return new IntakeWidget({
    token: options.token,
    baseUrl: options.baseUrl || '',
    mount,
  });
}

const api = { init, IntakeWidget };

if (typeof window !== 'undefined') {
  window.JuiceboxIntakeWidget = api;
  const current = document.currentScript;
  if (current && current.getAttribute('data-token')) {
    mountFromScript(current);
  }
}

export default api;
