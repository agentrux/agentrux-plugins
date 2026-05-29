/**
 * TTL-expired cursor recovery.
 *
 * A persisted pull cursor (`?after=<evt>`) is only valid inside a topic's
 * retention window. Once the pinned event ages out, `pipe_router` answers 404
 * with a `ttl_expired` / `cursor_advance` signal. Before this fix the gateway
 * had no path to reconcile the cursor, so the poller looped on that 404 every
 * `pollIntervalMs` forever (the observed GCP stuck-gateway symptom).
 *
 * These tests pin:
 *  1. the real detection helpers parse both the FastAPI `{detail:{...}}` wrap
 *     and the bare `next_action` form;
 *  2. a faithful mirror of the drain + re-anchor loop self-heals and respects
 *     the configured delivery semantics (chat → skip-to-latest;
 *     durable → oldest-available, with a skip-to-latest fallback that breaks
 *     a same-cursor loop).
 */

import {
  ApiError,
  isTtlExpiredCursor,
  extractOldestAvailable,
} from "../http-client";

// Server response shape from pipe_router._ttl_expired_cursor_response, after
// FastAPI wraps the HTTPException payload under `detail`.
function ttlExpiredBody(oldest: string | null): any {
  return {
    detail: {
      error: "NOT_FOUND",
      message: "after cursor refers to a TTL-expired event",
      details: { reason: "ttl_expired", oldest_available_evt_id: oldest },
      next_action: "cursor_advance",
    },
  };
}

describe("ttl_expired detection helpers", () => {
  test("recognizes a 404 ttl_expired ApiError (FastAPI detail wrap)", () => {
    const err = new ApiError(404, ttlExpiredBody("evt_oldest"));
    expect(isTtlExpiredCursor(err)).toBe(true);
    expect(extractOldestAvailable(err)).toBe("evt_oldest");
  });

  test("recognizes the bare next_action form (no detail wrap)", () => {
    const err = new ApiError(404, { next_action: "cursor_advance", details: {} });
    expect(isTtlExpiredCursor(err)).toBe(true);
    expect(extractOldestAvailable(err)).toBeNull();
  });

  test("null oldest_available is surfaced as null (empty topic)", () => {
    const err = new ApiError(404, ttlExpiredBody(null));
    expect(isTtlExpiredCursor(err)).toBe(true);
    expect(extractOldestAvailable(err)).toBeNull();
  });

  test("unrelated 404 is NOT treated as a cursor signal", () => {
    const err = new ApiError(404, { detail: { error: "NOT_FOUND", message: "topic gone" } });
    expect(isTtlExpiredCursor(err)).toBe(false);
  });

  test("non-404 and non-ApiError are ignored", () => {
    expect(isTtlExpiredCursor(new ApiError(500, { detail: {} }))).toBe(false);
    expect(isTtlExpiredCursor(new Error("boom"))).toBe(false);
    expect(extractOldestAvailable(new Error("boom"))).toBeNull();
  });
});

// The REAL re-anchor decision function from gateway.ts (now exported and
// dependency-injected via fetchLatest, so no HTTP and no inline mirror).
import { reanchorExpiredCursor } from "../gateway";

interface Waterline { sequence_number: number; event_id: string }

describe("drain loop self-heals on ttl_expired", () => {
  // A topic where the persisted cursor "evt_stale" has aged out. Live events
  // start at evt_100; the server reports evt_100 as oldest_available.
  const LIVE = [
    { event_id: "evt_100", sequence_number: 100, payload: { message: "a" } },
    { event_id: "evt_101", sequence_number: 101, payload: { message: "b" } },
  ];
  const LATEST = LIVE[LIVE.length - 1];

  function makePull(staleCursor: string) {
    // Returns events after a given cursor; throws ttl_expired when the cursor
    // is the stale one. desc+limit=1 (afterEventId=null) returns latest.
    return async (afterEventId: string | null, order: "asc" | "desc") => {
      if (order === "desc") return [LATEST];
      if (afterEventId === staleCursor) {
        throw new ApiError(404, ttlExpiredBody("evt_100"));
      }
      return LIVE.filter((e) => e.sequence_number > seqOf(afterEventId));
    };
  }

  function seqOf(eventId: string | null): number {
    if (!eventId) return 0;
    const m = LIVE.find((e) => e.event_id === eventId);
    return m ? m.sequence_number : 0;
  }

  async function runDrain(durable: boolean): Promise<{ waterline: Waterline; processed: string[]; iterations: number }> {
    let waterline: Waterline = { sequence_number: 0, event_id: "evt_stale" };
    const processed: string[] = [];
    const pull = makePull("evt_stale");
    const pullLatest = async () => {
      const b = await pull(null, "desc");
      return b.length ? b[0] : null;
    };

    let iterations = 0;
    while (true) {
      if (iterations++ > 50) throw new Error("drain did not terminate (infinite loop)");
      let batch: any[];
      try {
        batch = await pull(waterline.event_id || null, "asc");
      } catch (err) {
        if (isTtlExpiredCursor(err)) {
          waterline = await reanchorExpiredCursor(durable, err, waterline.event_id, pullLatest, "top_test");
          continue;
        }
        throw err;
      }
      if (batch.length === 0) break;
      for (const e of batch) {
        if (e.sequence_number <= waterline.sequence_number) continue;
        processed.push(e.event_id);
        waterline = { sequence_number: e.sequence_number, event_id: e.event_id };
      }
    }
    return { waterline, processed, iterations };
  }

  test("chat default: skips backlog to latest and terminates", async () => {
    const { waterline, processed } = await runDrain(false);
    // Skip-to-latest: cursor jumps to the newest event, no backlog replayed.
    expect(waterline.event_id).toBe(LATEST.event_id);
    expect(waterline.sequence_number).toBe(LATEST.sequence_number);
    expect(processed).toEqual([]);
  });

  test("durable: re-anchors to oldest available and replays the live window", async () => {
    const { waterline, processed } = await runDrain(true);
    // Durable re-anchor to evt_100 → ?after=evt_100 yields evt_101.
    expect(processed).toEqual(["evt_101"]);
    expect(waterline.event_id).toBe("evt_101");
  });

  test("durable with no progress (oldest == current) falls back to skip-to-latest", async () => {
    // Cursor already sits on the oldest-available value; re-anchoring there
    // again would loop, so we must skip to latest instead.
    let waterline: Waterline = { sequence_number: 0, event_id: "evt_100" };
    const err = new ApiError(404, ttlExpiredBody("evt_100"));
    const pullLatest = async () => ({ sequence_number: LATEST.sequence_number, event_id: LATEST.event_id });
    waterline = await reanchorExpiredCursor(true, err, waterline.event_id, pullLatest, "top_test");
    expect(waterline.event_id).toBe(LATEST.event_id);
  });
});
