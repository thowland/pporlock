/**
 * Backtick-quoted spans rendered as code.
 *
 * The error catalogue is written as plain strings — it has to be, because
 * `extension-errors.test.ts` compares it against a source file, and because a
 * table of JSX fragments is much harder to read than a table of sentences. But
 * "Start it: `pporlock run`" has to *look* like a command, and shipping literal
 * backticks in an interface is the tell of a string that was written for a
 * terminal and rendered in a browser.
 *
 * Deliberately not a markdown renderer. It understands exactly one thing, and
 * an unbalanced backtick leaves the text alone rather than swallowing the rest
 * of the sentence.
 */
export function Ticked({ text }: { text: string }) {
  const parts = text.split('`');
  // An odd number of backticks means one is unmatched; the split would then
  // mark the tail as code, so the whole string is left as written instead.
  if (parts.length % 2 === 0) return <>{text}</>;
  return (
    <>
      {parts.map((part, index) =>
        // Keyed by index, which is safe here in the one case it usually is
        // not: the array comes from splitting a constant string, so it never
        // reorders and its length never changes between renders.
        index % 2 === 1 ? <code key={index}>{part}</code> : <span key={index}>{part}</span>,
      )}
    </>
  );
}
