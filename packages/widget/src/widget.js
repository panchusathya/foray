import { IntakeApiError, sendMessage, startSession } from './api.js';
import { clearPendingMessageId, mintClientMessageId } from './ids.js';

const ERROR_COPY = {
  expired:
    'This conversation expired — please start again.',
  thread_limit:
    'This conversation has reached its message limit.',
  org_limit:
    'Too many new conversations were opened recently. Please try again later.',
  rate_limited:
    'Too many requests — please wait a moment and try again.',
  failed: 'Something went wrong. Please try again.',
};

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function renderMessages(container, messages) {
  container.innerHTML = messages
    .map(
      (message) =>
        `<div class="jb-intake-msg jb-intake-msg--${escapeHtml(message.direction)}">${escapeHtml(message.body)}</div>`,
    )
    .join('');
  container.scrollTop = container.scrollHeight;
}

export class IntakeWidget {
  constructor({ token, baseUrl = '', mount }) {
    this.token = token;
    this.baseUrl = baseUrl;
    this.mount = mount;
    this.threadId = '';
    this.threadToken = '';
    this.messages = [];
    this.busy = false;
    this.error = '';
    this._renderShell();
  }

  _renderShell() {
    this.mount.innerHTML = `
      <div class="jb-intake" data-testid="intake-widget">
        <div class="jb-intake-header">Careers chat</div>
        <div class="jb-intake-error" data-testid="intake-widget-error" hidden></div>
        <div class="jb-intake-messages" data-testid="intake-widget-messages"></div>
        <form class="jb-intake-form" data-testid="intake-widget-form">
          <input class="jb-intake-input" data-testid="intake-widget-input" type="text" maxlength="4000" placeholder="Type your message..." autocomplete="off" />
          <button class="jb-intake-send" data-testid="intake-widget-send" type="submit">Send</button>
        </form>
      </div>
    `;
    this.errorEl = this.mount.querySelector('[data-testid="intake-widget-error"]');
    this.messagesEl = this.mount.querySelector('[data-testid="intake-widget-messages"]');
    this.formEl = this.mount.querySelector('[data-testid="intake-widget-form"]');
    this.inputEl = this.mount.querySelector('[data-testid="intake-widget-input"]');
    this.formEl.addEventListener('submit', (event) => {
      event.preventDefault();
      void this._handleSend();
    });
    void this._bootstrap();
  }

  _setError(code) {
    this.error = code;
    if (!code) {
      this.errorEl.hidden = true;
      this.errorEl.textContent = '';
      return;
    }
    this.errorEl.hidden = false;
    this.errorEl.textContent = ERROR_COPY[code] || ERROR_COPY.failed;
  }

  _applySession(session) {
    this.threadId = session.thread_id;
    this.threadToken = session.thread_token;
    this.messages = session.messages || [];
    renderMessages(this.messagesEl, this.messages);
    if (typeof window !== 'undefined') {
      window.__jbThreadId = this.threadId;
      window.__jbThreadToken = this.threadToken;
    }
  }

  async _bootstrap() {
    this.busy = true;
    this.inputEl.disabled = true;
    try {
      const session = await startSession(this.token, this.baseUrl);
      this._applySession(session);
      this._setError('');
    } catch (error) {
      this._handleError(error);
    } finally {
      this.busy = false;
      this.inputEl.disabled = false;
      this.inputEl.focus();
    }
  }

  _handleError(error) {
    if (error instanceof IntakeApiError) {
      this._setError(error.code);
      if (error.code === 'expired') {
        this.inputEl.disabled = true;
        this.formEl.querySelector('button').disabled = true;
      }
      return;
    }
    this._setError('failed');
  }

  async _handleSend() {
    const body = this.inputEl.value.trim();
    if (!body || this.busy || !this.threadId) {
      return;
    }
    this.busy = true;
    this.inputEl.disabled = true;
    const clientMessageId = mintClientMessageId(this.threadId);
    try {
      const session = await sendMessage({
        baseUrl: this.baseUrl,
        threadId: this.threadId,
        token: this.threadToken,
        body,
        clientMessageId,
      });
      clearPendingMessageId(this.threadId);
      this.inputEl.value = '';
      this._applySession(session);
      this._setError('');
    } catch (error) {
      this._handleError(error);
    } finally {
      this.busy = false;
      if (this.error !== 'expired') {
        this.inputEl.disabled = false;
        this.inputEl.focus();
      }
    }
  }
}
