import { describe, expect, it } from 'vitest';
import { loadDbHandle } from './wasm-db-handle';

/**
 * Real-module regression test for the Node build of
 * `loadDbHandle`. Lives in a separate file (NOT
 * `wasm-db-handle.test.ts`) because that file mocks
 * `@sqlite.org/sqlite-wasm` with `vi.hoisted` + `vi.mock` and
 * stubs globals in `beforeEach` / `afterEach` — neither is safe
 * to share with a test that imports the real package.
 *
 * The Node path is the one vitest's jsdom env takes by default
 * (jsdom exposes Node's `process`), so the loader picks
 * `loadNodeDb`, imports the package's node.mjs entry, opens an
 * in-memory OO1 DB, and wraps it in a `NodeDbHandle`. The point
 * of this test is to guard the shared `DbHandle` contract —
 * `exec` / `select` / `run` / `transaction` / `close` must all
 * behave correctly on a real DB. A regression here would break
 * every vitest suite that uses `WasmDataSource` against an
 * in-memory DB.
 *
 * The real module import is slow (~1s on a cold cache because
 * the WASM blob has to be parsed) so we set a generous timeout.
 */

describe('loadDbHandle Node path (real module)', () => {
  it(
    'opens an in-memory DB and execs/selects/runs/transacts through NodeDbHandle',
    async () => {
      const loaded = await loadDbHandle();
      expect(loaded.kind).toBe('node');
      expect(loaded.persisted).toBe(false);

      const { handle } = loaded;

      // CREATE TABLE — exec must accept multi-statement SQL and
      // surface the OO1 handle's return value.
      await handle.exec('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)');

      // INSERT via run with parameter binding.
      await handle.run('INSERT INTO t (name) VALUES (?)', ['alice']);
      await handle.run('INSERT INTO t (name) VALUES (?)', ['bob']);

      // SELECT must return rows as objects keyed by column name.
      const rows = await handle.select<{ id: number; name: string }>(
        'SELECT * FROM t ORDER BY id',
      );
      expect(rows).toEqual([
        { id: 1, name: 'alice' },
        { id: 2, name: 'bob' },
      ]);

      // SELECT with bind params.
      const alice = await handle.select<{ name: string }>(
        'SELECT name FROM t WHERE name = ?',
        ['alice'],
      );
      expect(alice).toEqual([{ name: 'alice' }]);

      // Transaction COMMIT — the inserted row must survive.
      await handle.transaction(async () => {
        await handle.run('INSERT INTO t (name) VALUES (?)', ['charlie']);
      });
      const afterCommit = await handle.select<{ name: string }>(
        'SELECT name FROM t WHERE name = ?',
        ['charlie'],
      );
      expect(afterCommit).toEqual([{ name: 'charlie' }]);

      // Transaction ROLLBACK — when the callback throws, the
      // surrounding write must NOT be visible. This is the
      // critical guarantee the production WasmDataSource relies
      // on for atomic sync deltas.
      await expect(
        handle.transaction(async () => {
          await handle.run('INSERT INTO t (name) VALUES (?)', ['dave']);
          throw new Error('intentional rollback');
        }),
      ).rejects.toThrow('intentional rollback');
      const afterRollback = await handle.select<{ name: string }>(
        'SELECT name FROM t WHERE name = ?',
        ['dave'],
      );
      expect(afterRollback).toEqual([]);

      handle.close();
    },
    15_000,
  );
});
