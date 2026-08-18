#!/usr/bin/env node
/**
 * juicebox-ingest — candidate ingestion CLI.
 *
 *   juicebox-ingest file ./people.jsonl [--source ats-export] [--dry-run]
 *   juicebox-ingest github 'language:python location:"San Francisco"' [--limit 25]
 *   juicebox-ingest serve [--port 8787]
 *   juicebox-ingest health
 */

import { fileCandidates } from './connectors/file.js';
import { githubCandidates } from './connectors/github.js';
import { config } from './config.js';
import { log } from './log.js';
import { runPipeline } from './pipeline.js';
import { healthCheck } from './sinks/apiSink.js';
import { serve } from './server.js';

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token.startsWith('--')) {
      const key = token.slice(2);
      const next = argv[index + 1];
      if (next === undefined || next.startsWith('--')) {
        flags[key] = true;
      } else {
        flags[key] = next;
        index += 1;
      }
    } else {
      positional.push(token);
    }
  }
  return { positional, flags };
}

const USAGE = `juicebox-ingest <command> [args]

Commands:
  file <path>            Import a .jsonl or .csv export
  github <query>         Source engineers from the GitHub public API
  serve                  Run the inbound webhook receiver
  health                 Check connectivity to the Juicebox API

Flags:
  --source <name>        Override the recorded source (file)
  --limit <n>            Max profiles to fetch (github, default 30)
  --batch-size <n>       Candidates per API call
  --dry-run              Normalise and log without writing
  --port <n>             Webhook port (serve)

Environment:
  JUICEBOX_API_URL       default http://localhost:8000
  JUICEBOX_API_KEY       required for anything that writes
  GITHUB_TOKEN           raises GitHub rate limits
`;

async function main(argv) {
  const { positional, flags } = parseArgs(argv);
  const [command, ...rest] = positional;

  if (!command || flags.help) {
    process.stdout.write(USAGE);
    return 0;
  }

  const batchSize = flags['batch-size'] ? Number(flags['batch-size']) : config.batchSize;
  const dryRun = Boolean(flags['dry-run']);

  switch (command) {
    case 'file': {
      const path = rest[0];
      if (!path) throw new Error('usage: juicebox-ingest file <path>');
      const totals = await runPipeline(
        fileCandidates(path, { source: flags.source }),
        { batchSize, dryRun },
      );
      log.info('file import complete', totals);
      return totals.failed > 0 ? 1 : 0;
    }

    case 'github': {
      const query = rest.join(' ');
      if (!query) throw new Error('usage: juicebox-ingest github <query>');
      const limit = flags.limit ? Number(flags.limit) : 30;
      const totals = await runPipeline(githubCandidates(query, { limit }), { batchSize, dryRun });
      log.info('github import complete', totals);
      return totals.failed > 0 ? 1 : 0;
    }

    case 'serve': {
      serve(flags.port ? Number(flags.port) : config.webhookPort);
      return new Promise(() => {}); // run until the process is signalled
    }

    case 'health': {
      const status = await healthCheck();
      log.info('api health', status);
      return 0;
    }

    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

const isEntrypoint = import.meta.url === `file://${process.argv[1]}`.replace(/\\/g, '/');
if (isEntrypoint || process.argv[1]?.endsWith('index.js')) {
  main(process.argv.slice(2))
    .then((code) => {
      if (typeof code === 'number') process.exit(code);
    })
    .catch((error) => {
      log.error('ingest failed', { error: error.message });
      process.exit(1);
    });
}

export { main };
