# OpenInstinct discovery operations

Before releasing a JuiceBox image, confirm the API and the general `worker`
service are both running. The API health check alone is insufficient: candidate
role discovery is consumed by the `worker` process, not the API.

1. Verify `docker compose ps api worker` reports both services healthy/running.
2. Verify the worker environment has a non-empty `EXA_API_KEY` and the same
   `OPENINSTINCT_SHARED_SECRET` as Foray.
3. Inspect worker logs for `jobs.discover.one`; queued/running jobs should
   drain, while `unavailable` identifies missing Exa configuration and
   `failed` carries the worker error.
4. Send one phone-scoped job-feed request. It should return `queued` or
   `running` while work is active, then `ready`, `empty`, `unavailable`, or
   `failed` — never a permanently true `searching` value after completion.
