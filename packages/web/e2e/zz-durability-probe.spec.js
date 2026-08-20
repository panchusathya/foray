import { expect, test } from '@playwright/test';

import { candidateFixtures, createOrg, uniqueEmail } from './support.js';

/**
 * TEMPORARY DIAGNOSTIC — delete with deploy/tests/signup_durability_probe.py
 * once the cause is fixed.
 *
 * Every observed failure has the same shape: `seedCandidates` 401s with
 * {"detail":"Not authenticated"} on the call immediately after a successful
 * `createOrg`. The standalone Python probe (deploy/tests) does the same two
 * calls back to back, 40 times, and never reproduces it — so plain
 * signup-then-immediately-use is NOT the trigger, and the difference must be in
 * how this suite makes the calls rather than in the sequence itself.
 *
 * The remaining differences between that probe and the specs are all
 * client-side: Playwright's APIRequestContext (connection reuse, cookie jar,
 * its own header handling) versus urllib opening a fresh connection each time,
 * and the three-candidate fixture payload versus a one-candidate one. This
 * probe removes both differences by driving the real helpers in a loop, so it
 * differs from a failing spec only in repetition.
 *
 * Named zz- so it sorts last and cannot perturb the specs that matter.
 * It never fails the run: it records what it saw and asserts only that it ran.
 */

const ITERATIONS = Number(process.env.PROBE_ITERATIONS ?? 25);

test.describe('durability probe (temporary)', () => {
  test('createOrg then seedCandidates, repeatedly', async ({ request }) => {
    test.setTimeout(180_000);

    const outcomes = { ok: 0, bulk401: 0, otherFail: 0, recovered: 0, lost: 0 };
    const notes = [];

    for (let i = 0; i < ITERATIONS; i += 1) {
      let org;
      try {
        org = await createOrg(request, `probe${i}`);
      } catch (error) {
        outcomes.otherFail += 1;
        notes.push(`[${i}] createOrg failed: ${String(error).slice(0, 160)}`);
        continue;
      }

      // Deliberately the same call the failing specs make, same payload shape.
      const response = await request.post('/v1/candidates/bulk', {
        headers: org.headers,
        data: { candidates: candidateFixtures(`probe${i}`) },
      });

      if (response.ok()) {
        outcomes.ok += 1;
        continue;
      }

      const status = response.status();
      const body = (await response.text()).slice(0, 160);
      notes.push(`[${i}] REPRODUCED bulk -> ${status} ${body}`);
      if (status === 401) outcomes.bulk401 += 1;
      else outcomes.otherFail += 1;

      // Late or lost? Re-issue the identical request for a few seconds.
      const started = Date.now();
      let recovered = false;
      while (Date.now() - started < 4000) {
        await new Promise((resolve) => setTimeout(resolve, 100));
        const retry = await request.post('/v1/candidates/bulk', {
          headers: org.headers,
          data: { candidates: candidateFixtures(`probe${i}`) },
        });
        if (retry.ok()) {
          recovered = true;
          notes.push(`[${i}]   -> succeeded after ${Date.now() - started} ms (late write)`);
          break;
        }
      }
      if (recovered) outcomes.recovered += 1;
      else {
        outcomes.lost += 1;
        notes.push(`[${i}]   -> still failing after 4s (lost write)`);
      }
    }

    // A signup whose email already exists returns 409, which would look like a
    // different bug entirely; prove the generator is not colliding.
    const sample = new Set([uniqueEmail('probe'), uniqueEmail('probe'), uniqueEmail('probe')]);

    const report = [
      `iterations: ${ITERATIONS}`,
      `bulk ok: ${outcomes.ok}`,
      `bulk 401 straight after createOrg: ${outcomes.bulk401}`,
      `other failures: ${outcomes.otherFail}`,
      `recovered on retry (late write): ${outcomes.recovered}`,
      `never recovered (lost write): ${outcomes.lost}`,
      `uniqueEmail distinct over 3 calls: ${sample.size === 3}`,
      ...notes,
    ].join('\n');

    console.log(`\n=== durability probe ===\n${report}\n`);
    test.info().annotations.push({ type: 'probe', description: report });

    // Records, never gates: the point is the evidence above.
    expect(ITERATIONS).toBeGreaterThan(0);
  });
});
