/**
 * "Exclude this host" — one click from any flow (SPEC-2 §6.6, REQ PXY-016).
 *
 * Excluding is not a filter. The connection is tunnelled at the ClientHello and
 * never decrypted (REQ PXY-013/015): you keep seeing *that* a connection
 * happened and to where, and stop seeing anything about its content. It applies
 * to connections opened after the change, so the flow the user clicked from is
 * unaffected and so is the page in front of them until they reload — which is
 * why this confirms first and then says what will happen, rather than
 * succeeding silently and looking broken.
 *
 * Adding to the list is a normal operation — the shipped list has 33 entries
 * for exactly these reasons — so the confirmation states the consequence once
 * and does not scold. Undoing it lives in Settings, which the confirmation
 * says, because it is not reversible from the same click.
 */
import { useState } from 'react';
import type { ApiClient } from '../../api/client';
import { appendHost, describeEntry, normalizeHost } from '../../lib/exclusions';

type Phase =
  | { kind: 'idle' }
  | { kind: 'confirming' }
  | { kind: 'working' }
  | { kind: 'done'; message: string }
  | { kind: 'already'; message: string }
  | { kind: 'failed'; message: string };

export interface ExcludeHostActionProps {
  api: ApiClient;
  /** The host this flow talked to. Absent for a flow with neither request nor SNI. */
  host: string | null | undefined;
  /** Recorded in the entry's comment, so the list says where it came from. */
  surface: string;
}

export function ExcludeHostAction({ api, host, surface }: ExcludeHostActionProps) {
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
  const target = host === null || host === undefined ? '' : normalizeHost(host);

  // Nothing to exclude: a flow with no host at all. A disabled button would be
  // a control that never becomes usable, so there simply isn't one.
  if (target === '') return null;

  const exclude = async () => {
    setPhase({ kind: 'working' });
    try {
      // PUT replaces the whole list, so the current one is read first. Reading
      // it now rather than at mount also means a list changed elsewhere — the
      // extension, the CLI, another tab — is not clobbered by a stale copy.
      const current = await api.getExclusions();
      const outcome = appendHost(current.entries, target, surface);
      if (outcome.status === 'already') {
        setPhase({
          kind: 'already',
          message: `${target} is already excluded by ${describeEntry(outcome.entry)}. Nothing was changed.`,
        });
        return;
      }
      await api.putExclusions(outcome.entries);
      setPhase({
        kind: 'done',
        message:
          `${target} is now excluded. New TLS connections to it are tunnelled undecrypted from ` +
          `here on — this flow is unchanged, and connections the browser already has open stay ` +
          `decrypted until they close. Reload the page to see it take effect. Remove it again ` +
          `under Settings.`,
      });
    } catch (cause) {
      setPhase({
        kind: 'failed',
        message: cause instanceof Error ? cause.message : 'Could not update the exclusion list.',
      });
    }
  };

  return (
    <span className="excludeaction">
      <button
        type="button"
        className="action"
        aria-label={`Exclude ${target} from interception`}
        aria-expanded={phase.kind === 'confirming'}
        aria-haspopup="dialog"
        onClick={(event) => {
          event.stopPropagation();
          setPhase(phase.kind === 'confirming' ? { kind: 'idle' } : { kind: 'confirming' });
        }}
      >
        exclude…
      </button>

      {phase.kind === 'confirming' && (
        <span
          className="excludeaction-confirm"
          role="dialog"
          aria-label={`Exclude ${target}`}
          onClick={(event) => event.stopPropagation()}
        >
          <p>
            <b>{target}</b> will be tunnelled without being decrypted. You will still see that a
            connection happened and to where, and nothing about its content — no headers, no bodies,
            and no rules applied to it.
          </p>
          <p className="empty-small">
            It applies to new connections only: this flow stays as it is, and the page in front of
            you will not change until you reload. Undo it under Settings, not here.
          </p>
          <span className="excludeaction-buttons">
            <button
              type="button"
              className="action primary"
              onClick={(event) => {
                event.stopPropagation();
                void exclude();
              }}
            >
              Exclude this host
            </button>
            <button
              type="button"
              className="action"
              onClick={(event) => {
                event.stopPropagation();
                setPhase({ kind: 'idle' });
              }}
            >
              Cancel
            </button>
          </span>
        </span>
      )}

      {phase.kind === 'working' && <span className="faint"> excluding…</span>}

      {(phase.kind === 'done' || phase.kind === 'already' || phase.kind === 'failed') && (
        <span
          className={`excludeaction-result ${phase.kind === 'failed' ? 'bad' : ''}`}
          // The change is invisible in the traffic already on screen, so
          // silence would read as "nothing happened". A live region says what
          // will happen and when.
          role={phase.kind === 'failed' ? 'alert' : 'status'}
          onClick={(event) => event.stopPropagation()}
        >
          {phase.message}{' '}
          <button
            type="button"
            className="linkish"
            aria-label={`Dismiss the exclusion message for ${target}`}
            onClick={(event) => {
              event.stopPropagation();
              setPhase({ kind: 'idle' });
            }}
          >
            dismiss
          </button>
        </span>
      )}
    </span>
  );
}
