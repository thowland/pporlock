/** Exclusion-list arithmetic. REQ PXY-013/014/016. */
import { describe, expect, it } from 'vitest';
import {
  appendHost,
  describeEntry,
  exclusionComment,
  findExclusion,
  globMatches,
  normalizeEntry,
  normalizeHost,
} from './exclusions';
import type { ExclusionEntry } from '../api/types';

const SHIPPED: ExclusionEntry[] = [
  { pattern: '*.apple.com', comment: 'update: macOS software update', source: 'default' },
  { pattern: 'ocsp.digicert.com', comment: 'update: revocation', source: 'default' },
  { pattern: '17.0.0.0/8', comment: 'pinning: Apple push', source: 'default' },
];

describe('normalizeHost', () => {
  it('lowercases and drops the trailing root dot, as the daemon does', () => {
    expect(normalizeHost('  CDN.Example.COM.  ')).toBe('cdn.example.com');
  });
});

describe('globMatches  # REQ PXY-013', () => {
  it.each([
    ['*.apple.com', 'gs.apple.com', true],
    ['*.apple.com', 'a.b.apple.com', true],
    // fnmatch does not let a leading `*.` match the bare domain, and neither
    // do we — otherwise the UI would claim a host was covered when it is not.
    ['*.apple.com', 'apple.com', false],
    ['*.apple.com', 'notapple.com', false],
    ['cdn.example.com', 'cdn.example.com', true],
    ['cdn.example.com', 'cdn.example.com.evil.test', false],
    ['analytics?.example.com', 'analytics7.example.com', true],
    ['analytics?.example.com', 'analytics77.example.com', false],
    ['*', 'anything.test', true],
    ['*example*', 'www.example.org', true],
  ])('%s vs %s', (pattern, host, expected) => {
    expect(globMatches(pattern, host)).toBe(expected);
  });

  it('is case-insensitive on both sides', () => {
    expect(globMatches('*.APPLE.com', 'GS.apple.COM')).toBe(true);
  });
});

describe('findExclusion', () => {
  it('reports the entry that already covers a host', () => {
    expect(findExclusion(SHIPPED, 'gs.apple.com')?.pattern).toBe('*.apple.com');
  });

  it('returns null for a host nothing covers', () => {
    expect(findExclusion(SHIPPED, 'cdn.example.com')).toBeNull();
  });

  it('ignores blank patterns and an empty host', () => {
    expect(findExclusion([{ pattern: '  ' }], 'x.test')).toBeNull();
    expect(findExclusion(SHIPPED, '   ')).toBeNull();
  });

  it('does not claim a CIDR entry covers a hostname', () => {
    // The daemon matches CIDRs against the peer IP, which the UI does not
    // have. Claiming coverage here would suppress a legitimate exclusion.
    expect(findExclusion(SHIPPED, '17.0.0.0')).toBeNull();
  });
});

describe('appendHost  # REQ PXY-016', () => {
  it('appends without disturbing the shipped list', () => {
    const outcome = appendHost(SHIPPED, 'cdn.example.com', 'flow table');
    expect(outcome.status).toBe('added');
    // The 33 shipped entries are what keep pinned hosts and OS updates
    // working; a PUT that dropped them would break the machine quietly.
    expect(outcome.entries.slice(0, 3)).toEqual(SHIPPED);
    expect(outcome.entries).toHaveLength(4);
    expect(outcome.entry).toEqual({
      pattern: 'cdn.example.com',
      comment: 'added from the flow table (cdn.example.com)',
      source: 'user',
    });
  });

  it('never adds a duplicate, and says which entry already covers the host', () => {
    const outcome = appendHost(SHIPPED, 'gs.apple.com', 'flow table');
    expect(outcome.status).toBe('already');
    expect(outcome.entries).toBe(SHIPPED);
    expect(outcome.entry.pattern).toBe('*.apple.com');
  });

  it('normalizes the host it stores', () => {
    const outcome = appendHost([], 'CDN.Example.com.', 'flow detail');
    expect(outcome.entries[0]?.pattern).toBe('cdn.example.com');
  });

  it('records a reason, because an unexplained exclusion looks like a bug', () => {
    expect(exclusionComment('x.test', 'flow detail')).toBe('added from the flow detail (x.test)');
    expect(appendHost([], 'x.test', 'flow table').entry.comment).not.toBe('');
  });
});

describe('normalizeEntry / describeEntry', () => {
  it('fills in the fields the contract leaves optional', () => {
    expect(normalizeEntry({ pattern: 'x.test' })).toEqual({
      pattern: 'x.test',
      comment: '',
      source: 'user',
    });
  });

  it('describes an entry with its source and reason', () => {
    expect(describeEntry(SHIPPED[0]!)).toBe(
      '*.apple.com (default) — update: macOS software update',
    );
    expect(describeEntry({ pattern: 'x.test', source: 'user' })).toBe('x.test (user)');
  });
});
