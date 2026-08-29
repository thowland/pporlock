/**
 * Settings — the ClientHello exclusion list (SPEC-2 §9, REQ PXY-014/016).
 *
 * This is the other half of the one-click action: the place where a user can
 * see what they excluded, why, and take it back. It renders the *effective*
 * list the daemon holds, defaults included, because "excluded" is otherwise a
 * claim nobody can check — and the first thing to suspect when a site
 * misbehaves is an exclusion nobody remembers.
 *
 * Removing is asymmetric on purpose. An entry the user added is theirs to
 * remove in one click. A shipped default is there because interception breaks
 * that host — a pinned client fails closed, an OS update stops arriving — so
 * removing one takes a second, deliberate confirmation that names the reason
 * recorded in the entry.
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { ExclusionEntry } from '../../api/types';
import { normalizeEntry } from '../../lib/exclusions';

export function ExclusionsSection({ api }: { api: ApiClient }) {
  const [entries, setEntries] = useState<ExclusionEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = await api.getExclusions();
      setEntries(found.entries);
      setLoadError(null);
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : 'Could not read the exclusion list.');
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Remove one pattern. The list is re-read first: `PUT` replaces everything,
   * and the copy on screen may be older than the daemon's if the extension or
   * another tab has added a host since it loaded.
   */
  const remove = async (pattern: string) => {
    setActionError(null);
    setNote(null);
    try {
      const current = await api.getExclusions();
      const next = current.entries.filter((entry) => entry.pattern !== pattern);
      const updated = await api.putExclusions(next);
      setEntries(updated.entries);
      setConfirming(null);
      setNote(
        `${pattern} is no longer excluded. Its traffic will be decrypted from the next connection on.`,
      );
    } catch (cause) {
      setActionError(
        cause instanceof Error ? cause.message : 'Could not update the exclusion list.',
      );
    }
  };

  return (
    <section className="settings-section">
      <h3>Exclusions</h3>
      <p className="empty-small">
        Connections to these hosts are tunnelled without being decrypted (REQ PXY-013). They still
        appear in the traffic table as passthrough flows — host, timing, byte counts — with no
        content. Changes take effect on new connections, without a daemon restart.
      </p>

      {loadError !== null && (
        // Deliberately not a live region: this section shares a screen with
        // the redaction form, and two alerts arguing for attention is worse
        // than one line of plain text where the list would have been.
        <p className="warn-strip">{`⚠ The exclusion list could not be read: ${loadError}`}</p>
      )}

      {actionError !== null && (
        <p className="warn-strip" role="alert">
          {`⚠ ${actionError}`}
        </p>
      )}

      {note !== null && (
        <p className="banner ok" role="status">
          {note}
        </p>
      )}

      {entries !== null && entries.length === 0 && (
        <p className="empty-small">The list is empty — every connection is being decrypted.</p>
      )}

      {entries !== null && entries.length > 0 && (
        <ul className="exclusionlist">
          {entries.map((raw, index) => {
            const entry = normalizeEntry(raw);
            const isDefault = entry.source === 'default';
            const pending = confirming === entry.pattern;
            return (
              <li key={`${entry.pattern}-${index}`}>
                <code>{entry.pattern}</code>
                <span className={isDefault ? 'pill dim' : 'pill warn'}>{entry.source}</span>
                <span className="exclusion-comment">
                  {entry.comment === '' ? (
                    // An exclusion nobody can explain is indistinguishable
                    // from a bug, so an empty comment is called out.
                    <span className="faint">no reason recorded</span>
                  ) : (
                    entry.comment
                  )}
                </span>
                {pending ? (
                  <>
                    <span className="warn-strip">
                      Shipped default. Interception is known to break this host — a pinned client
                      fails closed, an update stops arriving, or sensitive traffic is drawn into the
                      capture buffer. The reason recorded is above.
                    </span>
                    <button
                      type="button"
                      className="action danger"
                      aria-label={`Confirm removing the default exclusion ${entry.pattern}`}
                      onClick={() => void remove(entry.pattern)}
                    >
                      Remove anyway
                    </button>
                    <button
                      type="button"
                      className="action"
                      aria-label={`Keep ${entry.pattern}`}
                      onClick={() => setConfirming(null)}
                    >
                      Keep
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="action danger"
                    aria-label={`Remove ${entry.pattern} from the exclusion list`}
                    onClick={() => {
                      setNote(null);
                      // Their own entry goes in one click — one click added
                      // it. A shipped default asks first, because the reason
                      // it is there is a site that breaks without it.
                      if (isDefault) setConfirming(entry.pattern);
                      else void remove(entry.pattern);
                    }}
                  >
                    Remove
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
