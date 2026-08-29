/**
 * Exclusion-list arithmetic (REQ PXY-013/014/016).
 *
 * `PUT /exclusions` replaces the entire list — there is no append route — so
 * every caller that adds one host has to GET, append, and PUT, and a caller
 * that forgets the GET deletes the 33 shipped entries that keep pinned hosts
 * and OS updates working. That sequence lives here, once, rather than in each
 * component that offers the action.
 *
 * The matching here answers one question only: "would this host already be
 * tunnelled?", so the UI can say *already excluded* instead of appending a
 * duplicate. The daemon's `ExclusionList.decide` remains the authority on what
 * is actually excluded; this is a conservative echo of its glob half.
 */
import type { ExclusionEntry, ExclusionSource } from '../api/types';

/** Lowercased and with the trailing root dot dropped, as the daemon does. */
export function normalizeHost(host: string): string {
  return host.trim().toLowerCase().replace(/\.+$/, '');
}

/**
 * `fnmatch`-style match for `*` and `?`, iteratively.
 *
 * Deliberately not a compiled RegExp: a pattern comes from the daemon's list
 * and building a regular expression out of it is both a lint finding and an
 * unnecessary way to make user data executable. Character classes (`[a-z]`)
 * are treated as literals — the daemon supports them, we do not, and the
 * consequence is only that a host covered by a class pattern is not recognized
 * as already-excluded. The exact-pattern check below catches the duplicate
 * either way.
 */
export function globMatches(pattern: string, host: string): boolean {
  const p = pattern.trim().toLowerCase();
  const h = normalizeHost(host);
  let pi = 0;
  let hi = 0;
  let star = -1;
  let resume = 0;

  while (hi < h.length) {
    const pc = pi < p.length ? p.charAt(pi) : '';
    if (pi < p.length && (pc === '?' || pc === h.charAt(hi))) {
      pi += 1;
      hi += 1;
    } else if (pc === '*') {
      star = pi;
      pi += 1;
      resume = hi;
    } else if (star >= 0) {
      pi = star + 1;
      resume += 1;
      hi = resume;
    } else {
      return false;
    }
  }
  while (pi < p.length && p.charAt(pi) === '*') pi += 1;
  return pi === p.length;
}

/** The entry that already covers this host, or `null`. */
export function findExclusion(entries: ExclusionEntry[], host: string): ExclusionEntry | null {
  const target = normalizeHost(host);
  if (target === '') return null;
  for (const entry of entries) {
    const pattern = entry.pattern.trim();
    if (pattern === '') continue;
    if (pattern.toLowerCase() === target) return entry;
    if (globMatches(pattern, target)) return entry;
  }
  return null;
}

/** `comment` and `source` filled in, so the UI never renders `undefined`. */
export function normalizeEntry(entry: ExclusionEntry): Required<ExclusionEntry> {
  const source: ExclusionSource = entry.source ?? 'user';
  return { pattern: entry.pattern, comment: entry.comment ?? '', source };
}

/**
 * Why this entry exists, in the entry itself.
 *
 * The shipped list gives every one of its 33 entries a reason, on the grounds
 * that an exclusion nobody can explain is indistinguishable from a bug. An
 * entry added by a click is exactly the kind that will be unexplainable in a
 * month, so it says where it came from and for what host.
 */
export function exclusionComment(host: string, surface: string): string {
  return `added from the ${surface} (${normalizeHost(host)})`;
}

export type AppendOutcome =
  | { status: 'added'; entries: ExclusionEntry[]; entry: ExclusionEntry }
  | { status: 'already'; entries: ExclusionEntry[]; entry: ExclusionEntry };

/**
 * The list with `host` added, or the existing list and the entry that already
 * covers it. Never appends a duplicate, and never reorders or rewrites an entry
 * it did not add — the other entries travel through byte-identical, because a
 * PUT is a replacement and anything this function drops is gone.
 */
export function appendHost(
  entries: ExclusionEntry[],
  host: string,
  surface: string,
): AppendOutcome {
  const existing = findExclusion(entries, host);
  if (existing !== null) return { status: 'already', entries, entry: existing };
  const entry: ExclusionEntry = {
    pattern: normalizeHost(host),
    comment: exclusionComment(host, surface),
    source: 'user',
  };
  return { status: 'added', entries: [...entries, entry], entry };
}

/** How an entry describes itself in a sentence: `*.apple.com (default)`. */
export function describeEntry(entry: ExclusionEntry): string {
  const normalized = normalizeEntry(entry);
  const reason = normalized.comment === '' ? '' : ` — ${normalized.comment}`;
  return `${normalized.pattern} (${normalized.source})${reason}`;
}
