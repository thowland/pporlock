/**
 * Recognising masked values (SPEC-0 §9.1, REQ CAP-042).
 *
 * A masked value is the literal string:
 *
 *     «redacted:sha1=<4 hex>,len=<bytes>»
 *
 * The format is fixed precisely so all three clients render it the same way,
 * so this parser is deliberately the twin of `extension/src/shared/redaction.ts`
 * — same anchored pattern, same presentation string. Two clients that disagree
 * about what a masked value looks like would let one of them show a secret the
 * other hid.
 *
 * Parsing rather than substring-matching on `«redacted` is what makes the two
 * facts the format preserves usable: the original byte length, and whether two
 * masked values are the *same* secret.
 */

export interface MaskedValue {
  /** First four hex characters of the SHA-1 of the original value. */
  sha1: string;
  /** Byte length of the original value. */
  len: number;
}

// Anchored, and both fields are bounded: an unanchored or greedy pattern would
// happily match a value that merely *contains* the marker, which is exactly how
// an attacker-supplied header value would fake a redaction.
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
 * designed to answer it without revealing either value.
 */
export function describeMasked(value: string): string {
  const masked = parseMasked(value);
  if (masked === null) return value;
  return `redacted · ${masked.len} bytes · #${masked.sha1}`;
}

/**
 * The `field_path` naming one header occurrence, for `GET /flows/{id}?unmask=`.
 *
 * The daemon's grammar (`daemon/src/pporlock/capture/redact.py:resolve_field`)
 * is `<request|response>.headers.<name>[.<occurrence>]`, where the occurrence
 * is a **zero-based index among the headers of that name** — not the row index
 * in the header list. The suffix is omitted for the first occurrence, which is
 * what the daemon defaults to.
 */
export function headerFieldPath(
  side: 'request' | 'response',
  name: string,
  occurrence: number,
): string {
  const base = `${side}.headers.${name.toLowerCase()}`;
  return occurrence <= 0 ? base : `${base}.${occurrence}`;
}
