import { useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';

/**
 * A module's report, fetched with the bearer token and rendered safely.
 *
 * OI-30. The first version of this was an `<a href="/modules/x/report">`, which
 * cannot work: a navigation carries no Authorization header, so every click
 * returned `missing or invalid bearer token`. Putting the token in the URL is
 * forbidden — it would end up in history, referrers and the audit log — so the
 * UI fetches it and displays the result.
 *
 * **HTML goes in a sandboxed iframe, not into this document.** The body is
 * module-authored and this page holds the bearer token. `sandbox=""` with no
 * `allow-scripts` and no `allow-same-origin` puts it in a unique opaque origin
 * with scripting disabled, so it can render a table and nothing else.
 *
 * A blob: URL opened in a tab would have been simpler and is the trap here:
 * blob URLs inherit the creating origin, so module HTML would have run
 * same-origin with the UI. The iframe is the safe shape, and the reason it is
 * embedded rather than opened in a tab.
 */
export function ModuleReport({
  api,
  name,
  onClose,
}: {
  api: ApiClient;
  name: string;
  onClose: () => void;
}) {
  const [report, setReport] = useState<{ contentType: string; body: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setReport(null);
    setProblem(null);
    api
      .getModuleReport(name)
      .then((result) => {
        if (live) setReport(result);
      })
      .catch((error: unknown) => {
        if (live) setProblem(error instanceof Error ? error.message : String(error));
      });
    return () => {
      live = false;
    };
  }, [api, name]);

  const isHtml = report?.contentType.startsWith('text/html') ?? false;

  return (
    <section className="report" aria-label={`${name} report`}>
      <header className="report-head">
        <h2>{name}</h2>
        <button type="button" className="action" onClick={onClose}>
          close
        </button>
      </header>

      {problem !== null && <p className="err">{problem}</p>}
      {problem === null && report === null && <p className="dim">Loading…</p>}

      {report !== null &&
        (isHtml ? (
          <iframe
            title={`${name} report`}
            className="report-frame"
            // No allow-scripts and no allow-same-origin: an opaque origin that
            // cannot reach this page or the control API.
            sandbox=""
            srcDoc={report.body}
          />
        ) : (
          // Text, CSV and JSON are shown as text. Rendering them as markup
          // would be a way to smuggle HTML past the content-type allowlist.
          <pre className="report-text">{report.body}</pre>
        ))}
    </section>
  );
}
