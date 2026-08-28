/**
 * Service-worker state. SPEC-3 §3.1.
 *
 * MV3 terminates the worker aggressively, so the invariant these protect is:
 * nothing lives only in the worker's memory.
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_STATE, EMPTY_COUNTERS, StateStore } from './state';
import { FakeStorage } from '../test/fakes';

function store(): { store: StateStore; local: FakeStorage; session: FakeStorage } {
  const local = new FakeStorage();
  const session = new FakeStorage();
  return { store: new StateStore(local, session), local, session };
}

describe('durable state', () => {
  it('starts from defaults', async () => {
    expect(await store().store.load()).toEqual(DEFAULT_STATE);
  });

  it('defaults to proxy off and unpaired', async () => {
    const state = await store().store.load();
    expect(state.proxyEnabled).toBe(false);
    expect(state.paired).toBe(false);
    expect(state.token).toBeNull();
  });

  it('persists across instances, because the worker will be killed', async () => {
    const { store: s, local, session } = store();
    await s.save({ proxyEnabled: true, token: 'secret' });

    const revived = new StateStore(local, session);
    const state = await revived.load();
    expect(state.proxyEnabled).toBe(true);
    expect(state.token).toBe('secret');
  });

  it('merges a patch rather than replacing', async () => {
    const { store: s } = store();
    await s.save({ token: 'secret', paired: true });
    await s.save({ proxyEnabled: true });
    const state = await s.load();
    expect(state.token).toBe('secret');
    expect(state.proxyEnabled).toBe(true);
  });

  it('fills in keys added since the state was written', async () => {
    // A state written by an older version must not produce undefined fields
    // that the popup then renders.
    const { store: s, local } = store();
    await local.set({ 'pporlock.state': { proxyEnabled: true } });
    const state = await s.load();
    expect(state.proxyEnabled).toBe(true);
    expect(state.devToggles).toEqual({ anticache: false, anticomp: false });
    expect(state.failSafeTrippedAt).toBeNull();
  });

  it('tolerates a corrupt stored value', async () => {
    const { store: s, local } = store();
    await local.set({ 'pporlock.state': 'not an object' });
    expect(await s.load()).toEqual(DEFAULT_STATE);
  });

  it('clearToken drops both the token and the paired flag', async () => {
    const { store: s } = store();
    await s.save({ token: 'secret', paired: true });
    const state = await s.clearToken();
    expect(state.token).toBeNull();
    expect(state.paired).toBe(false);
  });
});

describe('per-tab counters', () => {
  it('start empty', async () => {
    expect(await store().store.getCounters(7)).toEqual(EMPTY_COUNTERS);
  });

  it('accumulate', async () => {
    const { store: s } = store();
    await s.bumpCounters(7, { requests: 1, blocked: 1 });
    await s.bumpCounters(7, { requests: 1, modified: 1 });
    expect(await s.getCounters(7)).toEqual({
      requests: 2,
      blocked: 1,
      modified: 1,
      warnings: 0,
      errors: 0,
    });
  });

  it('are kept separate per tab', async () => {
    const { store: s } = store();
    await s.bumpCounters(1, { requests: 5 });
    await s.bumpCounters(2, { requests: 3 });
    expect((await s.getCounters(1)).requests).toBe(5);
    expect((await s.getCounters(2)).requests).toBe(3);
  });

  it('reset on tab close so a reused id does not inherit', async () => {
    const { store: s } = store();
    await s.bumpCounters(7, { requests: 9 });
    await s.resetCounters(7);
    expect(await s.getCounters(7)).toEqual(EMPTY_COUNTERS);
  });

  it('live in session storage, not durable storage', async () => {
    // A browser restart should not carry per-tab tallies forward.
    const { store: s, local, session } = store();
    await s.bumpCounters(7, { requests: 1 });
    expect(session.data.size).toBe(1);
    expect(local.data.size).toBe(0);
  });
});
