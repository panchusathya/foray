import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { test } from 'node:test';

import { config } from '../src/config.js';
import { signatureIsValid } from '../src/server.js';

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
