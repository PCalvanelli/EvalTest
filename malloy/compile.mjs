// Malloy → DuckDB SQL compiler sidecar for the eval playground.
//
// Protocol: JSON lines on stdin/stdout.
//   1. {"type": "init", "ddl": "<CREATE SCHEMA/TABLE ...>", "model": "<.malloy text>"}  → {"id": "init", "ok": true}
//   2. {"id": <any>, "query": "<malloy query>"}                                          → {"id", "sql"} | {"id", "error"}
// Tables are created EMPTY in an in-memory DuckDB: Malloy only needs column types to compile. Python
// executes the SQL against the real warehouse, so the two DuckDB versions never share a file.

import readline from 'node:readline';
import { SingleConnectionRuntime } from '@malloydata/malloy';
import { DuckDBConnection } from '@malloydata/db-duckdb';

// Keep stdout clean for the protocol; anything a library logs goes to stderr.
console.log = console.info = console.warn = (...args) => process.stderr.write(args.join(' ') + '\n');

const send = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');

let model = null;

async function init({ ddl, model: modelText }) {
  const connection = new DuckDBConnection({ name: 'duckdb', databasePath: ':memory:' });
  for (const stmt of ddl.split(';').map((s) => s.trim()).filter(Boolean)) {
    await connection.runRawSQL(stmt);
  }
  const runtime = new SingleConnectionRuntime({ connection });
  model = runtime.loadModel(modelText);
  await model.getModel(); // surface model compile errors at startup
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    send({ id: null, error: 'Invalid JSON request' });
    continue;
  }
  if (msg.type === 'init') {
    try {
      await init(msg);
      send({ id: 'init', ok: true });
    } catch (e) {
      send({ id: 'init', error: String(e?.message ?? e) });
    }
    continue;
  }
  if (!model) {
    send({ id: msg.id, error: 'Sidecar not initialized' });
    continue;
  }
  try {
    const sql = await model.loadQuery(msg.query).getSQL();
    send({ id: msg.id, sql });
  } catch (e) {
    send({ id: msg.id, error: String(e?.message ?? e) });
  }
}
