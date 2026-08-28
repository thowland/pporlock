/**
 * Recognising masked values (SPEC-0 §9.1, REQ CAP-042).
 *
 * A masked value is the literal string:
 *
 *     «redacted:sha1=<4 hex>,len=<bytes>»
 *
 * The format is fixed precisely so all three clients render it the same way.
 * Parsing it rather than substring-matching on `«redacted` means the panel can
 * show the two things the format preserves — the length, and whether two
 * masked values are the *same* secret — without ever seeing the secret.
 *
 * The extension deliberately has no unmask path. Unmasking is live-ring-only,
 * web-UI-only, per-value (SPEC-0 §9.3); adding it here would create a second
 * disclosure surface with none of that ceremony.
 */

export interface MaskedValue {
  /** First four hex characters of the SHA-1 of the original value. */
  sha1: string;
  /** Byte length of the original value. */
  len: number;
}

// Anchored, and both fields are bounded: an unanchored or greedy pattern here
// would happily match a value that merely contains the marker.
const MASK_RE = /^«redacted:sha1=([0-9a-f]{4}),len=(\d{1,12})»$/;

export function parseMasked(value: string): MaskedValue | null {
  const match = MASK_RE.exec(value);
  if (!match?.[1] || !match[2]) return null;
  return { sha1: match[1], len: Number.parseInt(match[2], 10) };
}

export function isMasked(value: string): boolean {
  return parseMasked(value) !== null;
}

/**
 * What to show in place of the value.
 *
 * The fingerprint is included because "are these two cookies the same cookie"
 * is the question redaction most often gets in the way of, and the format was
 * designed to answer it.
 */
export function describeMasked(value: string): string {
  const masked = parseMasked(value);
  if (masked === null) return value;
  return `redacted · ${masked.len} bytes · #${masked.sha1}`;
}
