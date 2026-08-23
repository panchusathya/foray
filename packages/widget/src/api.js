export class IntakeApiError extends Error {
  constructor(code, detail = '') {
    super(detail || code);
    this.code = code;
    this.detail = detail;
  }
}

function normalizeBaseUrl(baseUrl) {
  return (baseUrl || '').replace(/\/$/, '');
}

async function parseResponse(response) {
  if (response.status === 401) {
    throw new IntakeApiError('expired');
  }
  if (response.status === 429) {
    let detail = '';
    try {
      const body = await response.json();
      detail = body.detail || '';
    } catch {
      detail = '';
    }
    if (detail.includes('message limit')) {
      throw new IntakeApiError('thread_limit', detail);
    }
    if (detail.includes('new conversations')) {
      throw new IntakeApiError('org_limit', detail);
    }
    throw new IntakeApiError('rate_limited', detail);
  }
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      detail = body.detail || response.statusText;
    } catch {
      detail = response.statusText;
    }
    throw new IntakeApiError('failed', detail);
  }
  return response.json();
}

export async function startSession(token, baseUrl = '') {
  const response = await fetch(`${normalizeBaseUrl(baseUrl)}/v1/intake/chat/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  return parseResponse(response);
}

export async function sendMessage({
  baseUrl = '',
  threadId,
  token,
  body,
  clientMessageId,
}) {
  const response = await fetch(
    `${normalizeBaseUrl(baseUrl)}/v1/intake/chat/sessions/${threadId}/messages`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token,
        body,
        client_message_id: clientMessageId,
      }),
    },
  );
  return parseResponse(response);
}
