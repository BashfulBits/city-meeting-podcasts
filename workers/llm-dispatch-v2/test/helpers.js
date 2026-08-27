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
      if (upper.startsWith("SELECT") || upper.startsWith("PRAGMA") || upper.startsWith("EXPLAIN")) {
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

  return { sql, storage };
}
