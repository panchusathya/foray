/** Drain an async iterator of candidates into the API in bounded batches. */

import { config } from './config.js';
import { log } from './log.js';
import { pushCandidates } from './sinks/apiSink.js';

export async function runPipeline(iterator, { batchSize = config.batchSize, dryRun = false } = {}) {
  const totals = { read: 0, created: 0, updated: 0, failed: 0, batches: 0 };
  let buffer = [];

  const flush = async () => {
    if (buffer.length === 0) return;
    if (dryRun) {
      log.info('dry run batch', { size: buffer.length, sample: buffer[0]?.full_name });
      totals.batches += 1;
      buffer = [];
      return;
    }
    const result = await pushCandidates(buffer);
    totals.created += result.created;
    totals.updated += result.updated;
    totals.failed += result.failed;
    totals.batches += result.batches;
    buffer = [];
  };

  for await (const candidate of iterator) {
    totals.read += 1;
    buffer.push(candidate);
    if (buffer.length >= batchSize) await flush();
  }
  await flush();

  return totals;
}
