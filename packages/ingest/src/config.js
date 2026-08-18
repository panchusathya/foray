/**
 * Environment-driven config for the ingestion workers.
 *
 * Zero dependencies on purpose: this process often runs in a slim container
 * next to a cron scheduler, and an npm install is one more thing to break.
 */

const int = (value, fallback) => {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export const config = {
  apiBaseUrl: (process.env.JUICEBOX_API_URL ?? 'http://localhost:8000').replace(/\/$/, ''),
  apiKey: process.env.JUICEBOX_API_KEY ?? '',

  batchSize: int(process.env.INGEST_BATCH_SIZE, 200),
  concurrency: int(process.env.INGEST_CONCURRENCY, 3),
  maxRetries: int(process.env.INGEST_MAX_RETRIES, 4),
  retryBaseMs: int(process.env.INGEST_RETRY_BASE_MS, 500),
  requestTimeoutMs: int(process.env.INGEST_TIMEOUT_MS, 30000),

  webhookPort: int(process.env.INGEST_WEBHOOK_PORT, 8787),
  webhookSecret: process.env.INGEST_WEBHOOK_SECRET ?? '',

  githubToken: process.env.GITHUB_TOKEN ?? '',
  githubUserAgent: process.env.GITHUB_USER_AGENT ?? 'juicebox-ingest',
};

export function assertApiCredentials() {
  if (!config.apiKey) {
    throw new Error(
      'JUICEBOX_API_KEY is not set. Create one with POST /v1/auth/api-keys and export it.',
    );
  }
}
