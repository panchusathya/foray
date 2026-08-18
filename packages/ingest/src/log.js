/** Structured logging — one JSON object per line, so it pipes into anything. */

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold = LEVELS[process.env.LOG_LEVEL ?? 'info'] ?? LEVELS.info;

function emit(level, message, fields) {
  if (LEVELS[level] < threshold) return;
  const line = { ts: new Date().toISOString(), level, msg: message, ...fields };
  const stream = level === 'error' ? process.stderr : process.stdout;
  stream.write(`${JSON.stringify(line)}\n`);
}

export const log = {
  debug: (message, fields = {}) => emit('debug', message, fields),
  info: (message, fields = {}) => emit('info', message, fields),
  warn: (message, fields = {}) => emit('warn', message, fields),
  error: (message, fields = {}) => emit('error', message, fields),
};
