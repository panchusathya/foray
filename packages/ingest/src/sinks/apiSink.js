/**
 * Ships normalised candidates to the Juicebox API.
 *
 * Batched, retried with exponential backoff + jitter, and bounded concurrency.
 * 4xx responses are not retried — a malformed batch will stay malformed, and
 * hammering the API with it just delays the error the operator needs to see.
 */

import { config, assertApiCredentials } from '../config.js';
import { log } from '../log.js';
import { dedupe } from '../normalize.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function chunk(items, size) {
  const batches = [];
  for (let index = 0; index < items.length; index += size) {
    batches.push(items.slice(index, index + size));
  }
  return batches;
}

async function postBatch(batch, attempt = 1) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.requestTimeoutMs);

  try {
    const response = await fetch(`${config.apiBaseUrl}/v1/candidates/bulk`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': config.apiKey,
      },
      body: JSON.stringify({ candidates: batch }),
      signal: controller.signal,
    });

    if (response.ok) return response.json();

    const body = await response.text();
    // Client errors are permanent; only server errors and rate limits retry.
    if (response.status < 500 && response.status !== 429) {
      throw new Error(`api rejected batch (${response.status}): ${body.slice(0, 400)}`);
    }
    throw Object.assign(new Error(`api error ${response.status}: ${body.slice(0, 200)}`), {
      retryable: true,
    });
  } catch (error) {
    const retryable = error.retryable || error.name === 'AbortError' || error.name === 'TypeError';
    if (!retryable || attempt > config.maxRetries) throw error;

    const backoff = config.retryBaseMs * 2 ** (attempt - 1);
    const delay = backoff + Math.floor(Math.random() * config.retryBaseMs);
    log.warn('retrying batch', { attempt, delay, error: error.message });
    await sleep(delay);
    return postBatch(batch, attempt + 1);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * @param {object[]} candidates
 * @returns {Promise<{created:number, updated:number, failed:number, batches:number}>}
 */
export async function pushCandidates(candidates) {
  assertApiCredentials();

  const unique = dedupe(candidates);
  if (unique.length === 0) return { created: 0, updated: 0, failed: 0, batches: 0 };

  const batches = chunk(unique, config.batchSize);
  const totals = { created: 0, updated: 0, failed: 0, batches: batches.length };

  let cursor = 0;
  const workers = Array.from({ length: Math.min(config.concurrency, batches.length) }, async () => {
    while (cursor < batches.length) {
      const index = cursor;
      cursor += 1;
      const batch = batches[index];
      try {
        const result = await postBatch(batch);
        totals.created += result.created ?? 0;
        totals.updated += result.updated ?? 0;
        log.info('batch accepted', {
          batch: index + 1,
          of: batches.length,
          created: result.created,
          updated: result.updated,
        });
      } catch (error) {
        totals.failed += batch.length;
        log.error('batch failed', { batch: index + 1, size: batch.length, error: error.message });
      }
    }
  });

  await Promise.all(workers);
  return totals;
}

export async function healthCheck() {
  const response = await fetch(`${config.apiBaseUrl}/health`);
  if (!response.ok) throw new Error(`api unhealthy: ${response.status}`);
  return response.json();
}
