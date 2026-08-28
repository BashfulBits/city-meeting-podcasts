import { DatabaseSync } from "node:sqlite";

/**
 * An in-memory node:sqlite-backed stand-in for a Durable Object's SQLite storage, close enough
 * to the real ctx.storage shape (a `.sql.exec()` accessor plus `.transactionSync()`) for
 * coordinator.js's actual code paths to run against unmodified.
 */
export function createMockSqlStorage() {
  const db = new DatabaseSync(":memory:");

  const sql = {
    exec(query, ...params) {
      const trimmed = query.trim();
      const upper = trimmed.toUpperCase();
      if (
        upper.startsWith("SELECT") ||
        upper.startsWith("PRAGMA") ||
        upper.startsWith("EXPLAIN")
      ) {
        const stmt = db.prepare(query);
        return stmt.all(...params);
      }
      if (params.length > 0) {
        db.prepare(query).run(...params);
      } else {
        db.exec(query);
      }
      return [];
    },
  };

  // Mirrors ctx.storage.transactionSync's real contract: a synchronous callback, committed on
  // normal return, rolled back if it throws.
  // https://developers.cloudflare.com/durable-objects/api/storage-api/
  const storage = {
    sql,
    transactionSync(callback) {
      db.exec("BEGIN");
      let result;
      try {
        result = callback();
      } catch (err) {
        db.exec("ROLLBACK");
        throw err;
      }
      db.exec("COMMIT");
      return result;
    },
  };

  return { sql, storage, db };
}

/**
 * A createMockSqlStorage() that also records every statement the coordinator executes, so a test
 * can attribute rows read per statement after the fact. Recording is off until `recorder.start()`
 * so table seeding is never counted; `recorder.reset()` clears what has been captured.
 */
export function createRecordingSqlStorage() {
  const { sql, storage, db } = createMockSqlStorage();
  const recorder = { statements: [], recording: false };
  recorder.start = () => {
    recorder.recording = true;
  };
  recorder.reset = () => {
    recorder.statements = [];
  };

  const innerExec = sql.exec.bind(sql);
  sql.exec = (query, ...params) => {
    if (recorder.recording) {
      recorder.statements.push({ query: query.trim().replace(/\s+/g, " "), params });
    }
    return innerExec(query, ...params);
  };

  return { sql, storage, db, recorder };
}

/**
 * Estimate rows read for one statement from its query plan.
 *
 * Two costly shapes, both seen in the 2026-08-27 Durable Objects rows-read overage:
 *
 *   - a SCAN, which reads the whole table; and
 *   - an index SEARCH constrained ONLY on a low-cardinality `state` column, which walks every
 *     row in that state. This one is the subtle case: the plan says SEARCH, so it *looks*
 *     indexed, but `WHERE state IN ('completed','failed') AND updated_at < ?` against an index
 *     keyed (state, priority, created_at) still reads every terminal row before filtering. That
 *     is precisely the trap that wiring up purgePendingBatch hit.
 *
 * Any seek that additionally constrains a second column is treated as bounded.
 *
 * Deliberately approximate: this is a *scale sensitivity* probe, not Cloudflare's billing meter.
 * It is exact for the unbounded-scan class and conservative elsewhere.
 */
export function estimateRowsRead(db, { query, params }) {
  if (/^(CREATE|BEGIN|COMMIT|ROLLBACK|PRAGMA|EXPLAIN)/.test(query.toUpperCase())) return 0;

  let plan;
  try {
    plan = db.prepare(`EXPLAIN QUERY PLAN ${query}`).all(...params).map((r) => r.detail);
  } catch {
    return 0; // not explainable; nothing to attribute
  }

  const countOf = (table) => db.prepare(`SELECT COUNT(*) c FROM ${table}`).get().c;

  /** Rows in `table` matching the statement's own `state` predicate, however it is written. */
  const stateRows = (table) => {
    const eq = query.match(/state\s*=\s*'(\w+)'/);
    const inList = query.match(/state\s+IN\s*\(([^)]*)\)/i);
    let states = null;
    if (eq) states = [eq[1]];
    else if (inList) states = [...inList[1].matchAll(/'(\w+)'/g)].map((m) => m[1]);
    // Unparseable state predicate: assume the worst rather than under-reporting.
    if (!states || states.length === 0) return countOf(table);
    return states.reduce(
      (sum, state) =>
        sum + db.prepare(`SELECT COUNT(*) c FROM ${table} WHERE state = ?`).get(state).c,
      0
    );
  };

  let rows = 0;
  for (const detail of plan) {
    const scan = detail.match(/^SCAN (\w+)/);
    if (scan) {
      rows += countOf(scan[1]);
      continue;
    }
    const search = detail.match(/^SEARCH (\w+) USING (?:COVERING )?INDEX \w+ \(([^)]*)\)/);
    if (search) {
      const [, table, constraints] = search;
      rows += constraints.trim() === "state=?" ? stateRows(table) : 1;
      continue;
    }
    if (/^SEARCH /.test(detail)) rows += 1;
  }
  return rows;
}

/** Tables whose row count grows with traffic and so must never be fully scanned. */
export const GROWABLE_TABLES = ["jobs", "job_models", "bundles", "attempts"];
