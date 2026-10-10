import { describe, expect, it } from "vitest";

import { createServerClock, remainingMs } from "@/lib/serverClock";
import { crossedThresholds, timeWarningMessage, warningText } from "@/lib/useServerClock";

function fakePerf(start = 1000) {
  let now = start;
  return { now: () => now, advance: (ms: number) => { now += ms; } };
}

describe("serverClock", () => {
  it("is unsynced until it has a sample", () => {
    const clock = createServerClock(fakePerf().now);
    expect(clock.isSynced()).toBe(false);
    expect(clock.now()).toBeNull();
  });

  it("advances with the monotonic clock from the server's reading, not the device wall clock", () => {
    const perf = fakePerf();
    const clock = createServerClock(perf.now);
    const realDateNow = Date.now;
    clock.sample("2026-06-01T10:00:00Z");
    Date.now = () => Date.parse("1999-01-01T00:00:00Z"); // the user sets the device clock back 27 years
    perf.advance(30_000);
    expect(clock.now()).toBe(Date.parse("2026-06-01T10:00:30Z"));
    Date.now = realDateNow;
  });

  it("adds half the round trip to the server's reading", () => {
    const clock = createServerClock(fakePerf().now);
    clock.sample("2026-06-01T10:00:00Z", 400);
    expect(clock.now()).toBe(Date.parse("2026-06-01T10:00:00Z") + 200);
  });

  it("ignores an older response that arrives late, but accepts a later one", () => {
    const perf = fakePerf();
    const clock = createServerClock(perf.now);
    clock.sample("2026-06-01T10:00:10Z");
    clock.sample("2026-06-01T09:59:00Z"); // stale
    expect(clock.now()).toBe(Date.parse("2026-06-01T10:00:10Z"));
    perf.advance(1000);
    clock.sample("2026-06-01T10:00:20Z");
    expect(clock.now()).toBe(Date.parse("2026-06-01T10:00:20Z"));
  });

  it("ignores an unparseable sample", () => {
    const clock = createServerClock(fakePerf().now);
    clock.sample("not a time");
    expect(clock.isSynced()).toBe(false);
  });

  it("re-anchors when the server moves the clock forward (resync after a long sleep)", () => {
    const perf = fakePerf();
    const clock = createServerClock(perf.now);
    clock.sample("2026-06-01T10:00:00Z");
    perf.advance(1000); // the tab was frozen: perf barely moved but the server did
    clock.sample("2026-06-01T10:05:00Z");
    expect(clock.now()).toBe(Date.parse("2026-06-01T10:05:00Z"));
  });
});

describe("remainingMs", () => {
  const now = Date.parse("2026-06-01T10:00:00Z");
  it("is the time to the deadline, floored at zero", () => {
    expect(remainingMs("2026-06-01T10:10:00Z", now)).toBe(600_000);
    expect(remainingMs("2026-06-01T09:00:00Z", now)).toBe(0);
  });
  it("is null when either side is unknown", () => {
    expect(remainingMs(null, now)).toBeNull();
    expect(remainingMs("2026-06-01T10:10:00Z", null)).toBeNull();
    expect(remainingMs("garbage", now)).toBeNull();
  });
});

describe("time warnings", () => {
  it("warns at the ten minute default and only once a threshold is crossed", () => {
    expect(timeWarningMessage(11 * 60_000, [10])).toBeNull();
    expect(timeWarningMessage(10 * 60_000, [10])).toBe("Less than 10 minutes remain.");
    expect(timeWarningMessage(9 * 60_000 + 59_000, [10])).toBe("Less than 10 minutes remain.");
  });

  it("uses the tightest crossed threshold, so the text changes once per threshold", () => {
    const minutes = [30, 10, 1];
    const texts = [31, 25, 12, 9, 5, 2, 0.5].map((m) => timeWarningMessage(m * 60_000, minutes));
    expect(texts).toEqual([null, warningText(30), warningText(30), warningText(10), warningText(10), warningText(10), warningText(1)]);
    // within one threshold the string is identical every second, so a live region does not re-announce it
    expect(new Set([9, 8.5, 8, 7.2].map((m) => timeWarningMessage(m * 60_000, minutes))).size).toBe(1);
  });

  it("says nothing at zero (that is 'time is up', handled separately) or when the time is unknown", () => {
    expect(timeWarningMessage(0, [10])).toBeNull();
    expect(timeWarningMessage(null, [10])).toBeNull();
  });

  it("honours an empty list and unsorted input", () => {
    expect(timeWarningMessage(60_000, [])).toBeNull();
    expect(crossedThresholds(4 * 60_000, [5, 30, 10])).toEqual([30, 10, 5]);
  });

  it("pluralises correctly", () => {
    expect(warningText(1)).toBe("Less than 1 minute remains.");
    expect(warningText(5)).toBe("Less than 5 minutes remain.");
  });
});
