import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { request as httpRequest } from 'node:http';
import { test } from 'node:test';

import { config } from '../src/config.js';
import { createIngestServer, signatureIsValid } from '../src/server.js';

function withConfig(overrides, fn) {
  const previous = {
    webhookSecret: config.webhookSecret,
    apiKey: config.apiKey,
    allowUnsigned: process.env.INGEST_ALLOW_UNSIGNED,
  };
  Object.assign(config, overrides);
  if ('allowUnsigned' in overrides) {
    if (overrides.allowUnsigned == null) {
      delete process.env.INGEST_ALLOW_UNSIGNED;
    } else {
      process.env.INGEST_ALLOW_UNSIGNED = overrides.allowUnsigned;
    }
  }
  try {
    return fn();
  } finally {
    config.webhookSecret = previous.webhookSecret;
    config.apiKey = previous.apiKey;
    if (previous.allowUnsigned === undefined) {
      delete process.env.INGEST_ALLOW_UNSIGNED;
    } else {
      process.env.INGEST_ALLOW_UNSIGNED = previous.allowUnsigned;
    }
  }
}

test('unsigned is allowed only when there is no secret and no API key', () => {
  withConfig({ webhookSecret: '', apiKey: '', allowUnsigned: null }, () => {
    assert.equal(signatureIsValid('{}', undefined), true);
  });
});

test('a live API key with no secret is not an open write', () => {
  withConfig({ webhookSecret: '', apiKey: 'jb_live', allowUnsigned: null }, () => {
    assert.equal(signatureIsValid('{}', undefined), false);
    assert.equal(signatureIsValid('{}', 'deadbeef'), false);
  });
});

test('INGEST_ALLOW_UNSIGNED opts a keyed local box back into unsigned mode', () => {
  withConfig({ webhookSecret: '', apiKey: 'jb_live', allowUnsigned: '1' }, () => {
    assert.equal(signatureIsValid('{}', undefined), true);
  });
});

test('an oversize body gets a 413 back, not a reset connection', async () => {
  // Destroying the request the moment the limit is hit took the socket down
  // with the response still unwritten: the partner saw ECONNRESET, which is
  // indistinguishable from the service being down, so their retry re-posted
  // the same oversize body instead of shrinking the batch.
  const server = createIngestServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();

  try {
    const status = await new Promise((resolve, reject) => {
      const req = httpRequest(
        { host: '127.0.0.1', port, method: 'POST', path: '/ingest', headers: { 'Content-Type': 'application/json' } },
        (res) => {
          res.resume();
          resolve(res.statusCode);
        },
      );
      // Whatever the server does to the socket after answering is its business;
      // failing to finish the upload must not lose us the response.
      req.on('error', (error) => reject(new Error(`no response: ${error.code || error.message}`)));
      req.on('response', () => req.destroy());
      req.setTimeout(5000, () => reject(new Error('no response — the socket was reset')));

      const megabyte = Buffer.alloc(1024 * 1024, 0x61);
      let sent = 0;
      const pump = () => {
        // Just past MAX_BODY_BYTES, then stop: the point is the response, not
        // how much we can push before the server hangs up.
        while (sent < 7 && !req.destroyed) {
          sent += 1;
          if (!req.write(megabyte)) {
            req.once('drain', pump);
            return;
          }
        }
      };
      pump();
    });

    assert.equal(status, 413);
  } finally {
    server.close();
  }
});

test('a configured secret is verified, never skipped', () => {
  const secret = 's3cret';
  const body = '{"name":"Ada"}';
  const signature = createHmac('sha256', secret).update(body).digest('hex');
  withConfig({ webhookSecret: secret, apiKey: 'jb_live', allowUnsigned: '1' }, () => {
    assert.equal(signatureIsValid(body, signature), true);
    assert.equal(signatureIsValid(body, `sha256=${signature}`), true);
    assert.equal(signatureIsValid(body, 'nope'), false);
    assert.equal(signatureIsValid(body, undefined), false);
  });
});
