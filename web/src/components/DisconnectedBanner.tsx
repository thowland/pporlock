/**
 * Disconnected state (REQ WUI-013).
 *
 * The distinction between "no flows because nothing is happening" and "no flows
 * because we are not connected" must never require inference.
 */
import type { Connection } from '../hooks/useDaemonState';

export function DisconnectedBanner({
  connection,
  onRetry,
}: {
  connection: Connection;
  onRetry: () => void;
}) {
  if (connection === 'connected') return null;

  if (connection === 'unauthorized') {
    return (
      <div className="banner">
        <div className="msg">
          <strong>Not authorized.</strong> The daemon is running but this page has no valid token.
          Reload the page; if that does not help, restart the daemon so it can reissue one.
        </div>
        <button type="button" className="action" onClick={onRetry}>
          retry
        </button>
      </div>
    );
  }

  return (
    <div className="banner">
      <div className="msg">
        <strong>Cannot reach the daemon.</strong> It may be stopped, or listening on a different
        port. Check with <code>pporlock doctor</code>, and start it with <code>pporlock run</code>.
      </div>
      <button type="button" className="action" onClick={onRetry}>
        retry
      </button>
    </div>
  );
}
