/**
 * Inbound webhook receiver.
 *
 * Sits between third-party systems (an ATS, a scraper, a partner feed) and the
 * Juicebox API so those systems never hold a Juicebox API key.
 */

import { createHmac, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';

import { config } from './config.js';
import { log } from './log.js';
import { toCandidate } from './normalize.js';
import { pushCandidates } from './sinks/apiSink.js';

const MAX_BODY_BYTES = 5 * 1024 * 1024;

function allowUnsigned() {
  if (/^(1|true|yes)$/i.test(process.env.INGEST_ALLOW_UNSIGNED || '')) return true;
  // Local development has neither a secret nor an API key. A live key with
  // no secret is an open write into Juicebox — refuse rather than treat a
  // misconfiguration as unsigned mode.
  return !config.webhookSecret && !config.apiKey;
}

export function signatureIsValid(rawBody, provided) {
  if (!config.webhookSecret) return allowUnsigned();
  if (!provided) return false;
  const expected = createHmac('sha256', config.webhookSecret).update(rawBody).digest('hex');
  const a = Buffer.from(expected, 'utf8');
  const b = Buffer.from(provided.replace(/^sha256=/, ''), 'utf8');
  return a.length === b.length && timingSafeEqual(a, b);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let over = false;
    request.on('data', (chunk) => {
      if (over) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        over = true;
        // Stop collecting, but do NOT destroy the request: that takes the
        // socket down with the 413 still unwritten, and the caller gets an
        // ECONNRESET it cannot tell apart from the service being down. The
        // handler answers first, then drains (see drainRefused).
        request.pause();
        reject(new Error('payload too large'));
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    request.on('error', reject);
  });
}

// Throw away the rest of a body we have refused, rather than closing on the
// client mid-upload. Any close while it is still sending -- destroy(), or the
// destroySoon() Node does for us if the response says Connection: close --
// arrives as an RST, and an RST makes the peer discard everything it has not
// read yet, the 413 included. Draining costs nothing (the chunks are dropped,
// not buffered) and lets the request end normally, which is what gets the
// status code delivered. Bounded, so a client that ignores the answer and
// keeps streaming is still hung up on eventually.
function drainRefused(request, limit) {
  let dropped = 0;
  request.on('data', (chunk) => {
    dropped += chunk.length;
    if (dropped > limit) request.destroy();
  });
  request.resume();
}

function json(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  response.end(body);
}

export function createIngestServer() {
  return createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/healthz') {
      json(response, 200, { status: 'ok' });
      return;
    }
    if (request.method !== 'POST' || !request.url.startsWith('/ingest')) {
      json(response, 404, { error: 'not found' });
      return;
    }

    let rawBody;
    try {
      rawBody = await readBody(request);
    } catch (error) {
      // Answer first, then stop consuming. Destroying the request the moment
      // the limit was hit took the socket down with the 413 still unwritten,
      // and the partner ATS saw ECONNRESET -- indistinguishable from the
      // service being down, so its retry re-posted the same oversize body
      // forever instead of surfacing the size limit.
      json(response, 413, { error: error.message });
      drainRefused(request, MAX_BODY_BYTES);
      return;
    }

    if (!signatureIsValid(rawBody, request.headers['x-signature'])) {
      log.warn('rejected webhook with bad signature');
      json(response, 401, { error: 'invalid signature' });
      return;
    }

    let payload;
    try {
      payload = JSON.parse(rawBody);
    } catch (error) {
      json(response, 400, { error: `invalid json: ${error.message}` });
      return;
    }

    const source = new URL(request.url, 'http://localhost').searchParams.get('source') ?? 'webhook';
    const records = Array.isArray(payload) ? payload : payload.candidates ?? [payload];
    const candidates = records.map((record) => toCandidate(record, { source })).filter(Boolean);

    if (candidates.length === 0) {
      json(response, 422, { error: 'no identifiable candidates in payload' });
      return;
    }

    try {
      const result = await pushCandidates(candidates);
      log.info('webhook ingested', { source, ...result });
      json(response, 202, { accepted: candidates.length, ...result });
    } catch (error) {
      log.error('webhook ingest failed', { error: error.message });
      json(response, 502, { error: error.message });
    }
  });
}

export function serve(port = config.webhookPort) {
  const server = createIngestServer();
  server.listen(port, () => log.info('ingest webhook listening', { port }));
  return server;
}
