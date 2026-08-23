const STORAGE_PREFIX = 'juicebox-intake-pending:';

export function mintClientMessageId(threadId) {
  const key = STORAGE_PREFIX + threadId;
  const existing = sessionStorage.getItem(key);
  if (existing) {
    return existing;
  }
  const id = crypto.randomUUID();
  sessionStorage.setItem(key, id);
  return id;
}

export function clearPendingMessageId(threadId) {
  sessionStorage.removeItem(STORAGE_PREFIX + threadId);
}
