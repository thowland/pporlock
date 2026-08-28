/**
 * Session list (SPEC-2 §8.2, REQ CAP-021, WUI-010).
 *
 * A session is a recording on disk, and the two things a user needs from this
 * list before opening one are whether it is still running and whether it is
 * complete. `dropped` is therefore a column rather than a detail: a session
 * that overflowed the writer is not a faithful recording (REQ CAP-023), and a
 * dry run against it is quietly answering a different question.
 *
 * Recording is opt-in and off by default (REQ CAP-020). Starting it is an
 * explicit act here, mirrored by the status bar's live indicator.
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { SessionMeta } from '../../api/types';
import { formatBytes, formatTime } from '../../lib/format';

interface Props {
  api: ApiClient;
  onOpen: (sessionId: string) => void;
  onDryRun: (sessionId: string) => void;
  /** The shell refreshes `GET /state` so the recording indicator stays true. */
  onChanged?: (() => void) | undefined;
}

export function SessionsView({ api, onOpen, onDryRun, onChanged }: Props) {
  const [sessions, setSessions] = useState<SessionMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [confirming, setConfirming] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
      setError(null);
    } catch (cause) {
      setSessions([]);
      setError(cause instanceof Error ? cause.message : 'Could not list sessions.');
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const guard = async (work: () => Promise<void>, fallback: string) => {
    try {
      await work();
      setError(null);
      await refresh();
      onChanged?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : fallback);
    }
  };

  const start = () => {
    const trimmed = name.trim();
    if (trimmed === '') {
      setError('A recording needs a name — it is how you will find it again.');
      return;
    }
    void guard(async () => {
      await api.startRecording(trimmed);
      setName('');
    }, `Could not start recording ${trimmed}.`);
  };

  const stop = (session: SessionMeta) =>
    void guard(
      () => api.stopRecording(session.session_id).then(() => undefined),
      'Could not stop.',
    );

  const remove = (session: SessionMeta) => {
    setConfirming(null);
    void guard(() => api.deleteSession(session.session_id), `Could not delete ${session.name}.`);
  };

  if (sessions === null) return <div className="empty">Loading sessions…</div>;

  return (
    <div className="sessions">
      <div className="viewbar">
        <h2>Sessions</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <input
          type="text"
          aria-label="New recording name"
          placeholder="recording name"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <button type="button" className="action primary" onClick={start}>
          Start recording
        </button>
      </div>

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      {sessions.length === 0 ? (
        <div className="empty">
          <h2>No recorded sessions</h2>
          <p>Recording is off by default. Name a session above and start it,</p>
          <p>then browse or dry-run against it once you stop.</p>
        </div>
      ) : (
        <table className="sessionlist">
          <caption className="sr-only">
            Recorded sessions, with flow counts, size on disk and the profile that was active.
          </caption>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">State</th>
              <th scope="col">Started</th>
              <th scope="col">Stopped</th>
              <th scope="col" className="num">
                Flows
              </th>
              <th scope="col" className="num">
                Size
              </th>
              <th scope="col">Profile</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => (
              <tr key={session.session_id}>
                <th scope="row" className="sessionname">
                  {session.name}
                  {session.dropped > 0 && (
                    // Not colour alone: the word "dropped" carries the meaning
                    // and the count says how bad it is (REQ WUI-015).
                    <span className="pill warn" title="The writer could not keep up">
                      ⚠ {session.dropped} dropped
                    </span>
                  )}
                </th>
                <td>
                  <span className={`pill ${session.state === 'recording' ? 'error' : 'dim'}`}>
                    {session.state === 'recording' ? '● recording' : 'stopped'}
                  </span>
                </td>
                <td className="dim">{formatTime(session.started_at)}</td>
                <td className="dim">
                  {session.stopped_at ? formatTime(session.stopped_at) : <span>—</span>}
                </td>
                <td className="num">{session.flow_count}</td>
                <td className="num dim">{formatBytes(session.size_bytes)}</td>
                <td className="dim">{session.profile}</td>
                <td className="rowactions">
                  <button
                    type="button"
                    className="action"
                    onClick={() => onOpen(session.session_id)}
                  >
                    Browse
                  </button>
                  <button
                    type="button"
                    className="action"
                    onClick={() => onDryRun(session.session_id)}
                  >
                    Dry run
                  </button>
                  {session.state === 'recording' ? (
                    <button type="button" className="action" onClick={() => stop(session)}>
                      Stop
                    </button>
                  ) : (
                    <>
                      <a
                        className="action"
                        href={api.sessionExportUrl(session.session_id, 'pporlock')}
                        download
                      >
                        Export
                      </a>
                      <a
                        className="action"
                        href={api.sessionExportUrl(session.session_id, 'har')}
                        download
                        // HAR has no place to put provenance, so an exported
                        // HAR silently loses the reason anything changed.
                        title="HAR cannot represent provenance — the record of what changed and why is lost"
                      >
                        Export HAR
                      </a>
                    </>
                  )}
                  <button
                    type="button"
                    className="action danger"
                    aria-label={`Delete ${session.name}`}
                    onClick={() => setConfirming(session.session_id)}
                  >
                    Delete
                  </button>
                  {confirming === session.session_id && (
                    <span className="confirm" role="alert">
                      Delete {session.name}? {formatBytes(session.size_bytes)} reclaimed.
                      <button
                        type="button"
                        className="action danger"
                        onClick={() => remove(session)}
                      >
                        Confirm delete
                      </button>
                      <button type="button" className="action" onClick={() => setConfirming(null)}>
                        Cancel
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
