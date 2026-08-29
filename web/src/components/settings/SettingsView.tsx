/**
 * Settings — redaction (SPEC-2 §9, REQ CAP-044).
 *
 * The point of this screen is the word *effective*. `GET /config` returns the
 * configuration actually in force, defaults included, not the subset the user
 * typed. "Redaction is configurable" is otherwise a claim nobody can check, and
 * an unchecked claim about redaction is how a secret reaches disk.
 *
 * So the patterns are rendered from the daemon's answer, defaults are marked as
 * defaults, and anything the user added is marked as added. Both lists are
 * editable and written back with `PUT /config`.
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { DaemonConfig, RedactionConfig } from '../../api/types';
import { ExclusionsSection } from './ExclusionsSection';

/**
 * SPEC-0 §9.2. Held here only to answer "is this value a default?" — the list
 * that governs behaviour is always the daemon's.
 */
export const DEFAULT_HEADER_PATTERNS = [
  'cookie',
  'set-cookie',
  'authorization',
  'proxy-authorization',
  'x-api-key',
  'x-auth-token',
];

export const DEFAULT_JSON_KEY_PATTERNS = [
  'password',
  'token',
  'secret',
  'api_key',
  'apikey',
  'access_token',
  'refresh_token',
  'session',
  'auth',
  'credential',
];

/** An illustration of the mask format, using a value that is not a secret. */
const MASK_EXAMPLE = '«redacted:sha1=3f9a,len=182»';

function PatternList({
  legend,
  help,
  patterns,
  defaults,
  onChange,
}: {
  legend: string;
  help: string;
  patterns: string[];
  defaults: string[];
  onChange: (next: string[]) => void;
}) {
  const [draft, setDraft] = useState('');
  const known = new Set(defaults);
  const removed = defaults.filter((pattern) => !patterns.includes(pattern));

  return (
    <fieldset className="patternlist">
      <legend>{legend}</legend>
      <p className="empty-small">{help}</p>
      <ul>
        {patterns.map((pattern, index) => (
          <li key={`${pattern}-${index}`}>
            <code>{pattern}</code>
            {known.has(pattern) ? (
              <span className="pill dim">default</span>
            ) : (
              <span className="pill warn">added</span>
            )}
            <button
              type="button"
              className="action danger"
              aria-label={`Remove ${pattern} from ${legend}`}
              onClick={() => onChange(patterns.filter((_, at) => at !== index))}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
      {removed.length > 0 && (
        // Removing a default is the change most likely to put a secret on
        // disk, so it is stated rather than merely absent from the list.
        <p className="warn-strip">
          {`⚠ Removed from the defaults: ${removed.join(', ')}. Values matching these are no longer masked.`}
        </p>
      )}
      <div className="patternadd">
        <input
          type="text"
          aria-label={`Add a pattern to ${legend}`}
          placeholder="pattern"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          type="button"
          className="action"
          onClick={() => {
            const value = draft.trim().toLowerCase();
            if (value === '' || patterns.includes(value)) return;
            onChange([...patterns, value]);
            setDraft('');
          }}
        >
          Add
        </button>
      </div>
    </fieldset>
  );
}

export function SettingsView({ api }: { api: ApiClient }) {
  const [config, setConfig] = useState<DaemonConfig | null>(null);
  const [draft, setDraft] = useState<RedactionConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const found = await api.getConfig();
      setConfig(found);
      setDraft({
        enabled: found.redaction.enabled,
        header_patterns: [...found.redaction.header_patterns],
        json_key_patterns: [...found.redaction.json_key_patterns],
      });
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not read the configuration.');
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = () => {
    if (draft === null) return;
    setSaved(false);
    void api
      .putConfig({ redaction: draft })
      .then((next) => {
        setConfig(next);
        setSaved(true);
        setError(null);
      })
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : 'Could not save the configuration.'),
      );
  };

  if (error !== null && config === null) {
    return (
      <div className="settings">
        <div className="banner error" role="alert">
          {error}
        </div>
      </div>
    );
  }

  if (config === null || draft === null) return <div className="empty">Loading settings…</div>;

  return (
    <div className="settings">
      <div className="viewbar">
        <h2>Settings</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <button type="button" className="action primary" onClick={save}>
          Save redaction settings
        </button>
      </div>

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {saved && (
        <div className="banner ok" role="status">
          Saved. Redaction takes effect immediately.
        </div>
      )}

      <section className="settings-section">
        <h3>Redaction</h3>
        <p className="empty-small">
          This is the <b>effective</b> configuration — what is in force now, defaults included.
          Session data is redacted before it is written to disk, so changing these lists does not
          reach back into sessions already recorded.
        </p>

        <label className="settings-toggle">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
          />
          Redaction enabled
        </label>
        {!draft.enabled && (
          <div className="warn-strip">
            ⚠ With redaction off, cookies and authorization headers are written to session files in
            clear text and returned unmasked by the API.
          </div>
        )}

        <p className="empty-small">
          A masked value looks like <code className="masked-example">{MASK_EXAMPLE}</code> — the
          first four hex characters of the SHA-1 and the original byte length, which is enough to
          tell two values apart without revealing either.
        </p>

        <PatternList
          legend="Header patterns"
          help="Matched case-insensitively against header names."
          patterns={draft.header_patterns}
          defaults={DEFAULT_HEADER_PATTERNS}
          onChange={(header_patterns) => setDraft({ ...draft, header_patterns })}
        />

        <PatternList
          legend="JSON key patterns"
          help="Matched case-insensitively as a substring of a JSON body key."
          patterns={draft.json_key_patterns}
          defaults={DEFAULT_JSON_KEY_PATTERNS}
          onChange={(json_key_patterns) => setDraft({ ...draft, json_key_patterns })}
        />
      </section>

      {/* Exclusions save themselves as they are edited rather than joining the
          redaction draft: they are a different route (`PUT /exclusions`), and a
          shared Save button would imply that leaving the page without pressing
          it left the host decrypted. */}
      <ExclusionsSection api={api} />
    </div>
  );
}
