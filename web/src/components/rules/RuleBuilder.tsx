/**
 * The rule builder form (SPEC-2 §7.3, REQ WUI-007).
 *
 * Fields mirror SPEC-0 §5.3/§5.4 one for one — match criteria on the left,
 * action and its parameters on the right — because a form that invents its own
 * vocabulary teaches the user something they then have to unlearn when they
 * open the YAML.
 *
 * The component is purely controlled: it holds no rule state of its own. That
 * is what lets the editor keep the YAML as the single source of truth and stay
 * synchronised with this view (§7.3).
 */
import { useMemo, useState } from 'react';
import {
  RULE_ACTIONS,
  parseExtraYaml,
  previewYaml,
  type HeaderEditsDraft,
  type MultiField,
  type Pair,
  type RuleAction,
  type RuleDraft,
} from '../../lib/rule-draft';
import { deepEqual, stringifyRule, stringifyYaml } from '../../lib/module-yaml';

const REDIRECT_KEYS = ['scheme', 'host', 'port', 'path', 'query'] as const;
type RedirectKey = (typeof REDIRECT_KEYS)[number];

const HEADER_SIDES = ['request', 'response'] as const;
type HeaderSide = (typeof HEADER_SIDES)[number];

interface Props {
  draft: RuleDraft;
  onChange: (next: RuleDraft) => void;
  /** Rendered as a hint under the name field; the module the rule belongs to. */
  moduleName?: string | undefined;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="rb-field">
      <span className="rb-label">{label}</span>
      {children}
      {hint !== undefined && <span className="rb-hint">{hint}</span>}
    </label>
  );
}

function PairEditor({
  legend,
  pairs,
  allowPresenceOnly,
  onChange,
}: {
  legend: string;
  pairs: Pair[];
  allowPresenceOnly?: boolean;
  onChange: (next: Pair[]) => void;
}) {
  const update = (index: number, patch: Partial<Pair>) =>
    onChange(pairs.map((pair, i) => (i === index ? { ...pair, ...patch } : pair)));

  return (
    <fieldset className="rb-pairs">
      <legend>{legend}</legend>
      {pairs.map((pair, index) => (
        <div className="rb-pair" key={index}>
          <input
            type="text"
            aria-label={`${legend} key ${index + 1}`}
            value={pair.key}
            onChange={(e) => update(index, { key: e.target.value })}
          />
          <input
            type="text"
            aria-label={`${legend} value ${index + 1}`}
            value={pair.value}
            disabled={allowPresenceOnly === true && pair.presenceOnly === true}
            onChange={(e) => update(index, { value: e.target.value })}
          />
          {allowPresenceOnly === true && (
            <label className="rb-inline">
              <input
                type="checkbox"
                aria-label={`${legend} ${index + 1} presence only`}
                checked={pair.presenceOnly === true}
                onChange={(e) => update(index, { presenceOnly: e.target.checked })}
              />
              presence
            </label>
          )}
          <button
            type="button"
            className="action"
            aria-label={`Remove ${legend} ${index + 1}`}
            onClick={() => onChange(pairs.filter((_, i) => i !== index))}
          >
            ×
          </button>
        </div>
      ))}
      <button
        type="button"
        className="action"
        onClick={() => onChange([...pairs, { key: '', value: '' }])}
      >
        Add {legend}
      </button>
    </fieldset>
  );
}

function ListEditor({
  legend,
  values,
  onChange,
}: {
  legend: string;
  values: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="rb-pairs">
      <legend>{legend}</legend>
      {values.map((value, index) => (
        <div className="rb-pair" key={index}>
          <input
            type="text"
            aria-label={`${legend} ${index + 1}`}
            value={value}
            onChange={(e) => onChange(values.map((v, i) => (i === index ? e.target.value : v)))}
          />
          <button
            type="button"
            className="action"
            aria-label={`Remove ${legend} ${index + 1}`}
            onClick={() => onChange(values.filter((_, i) => i !== index))}
          >
            ×
          </button>
        </div>
      ))}
      <button type="button" className="action" onClick={() => onChange([...values, ''])}>
        Add {legend}
      </button>
    </fieldset>
  );
}

export function RuleBuilder({ draft, onChange, moduleName }: Props) {
  const preview = useMemo(() => {
    try {
      return previewYaml(draft);
    } catch {
      return stringifyRule({ name: draft.name, action: draft.action });
    }
  }, [draft]);

  const setMatch = (patch: Partial<RuleDraft['match']>) =>
    onChange({ ...draft, match: { ...draft.match, ...patch, present: true } });

  /*
   * Computed-key access below is over `RedirectKey` / `HeaderSide`, both closed
   * literal unions declared in this file. No property name here originates in
   * a URL, a header, or anything else the user does not type into this form.
   */
  /* eslint-disable security/detect-object-injection */
  const redirectValue = (key: RedirectKey) => draft.redirect[key];
  const setRedirect = (key: RedirectKey, value: string): RuleDraft => ({
    ...draft,
    redirect: { ...draft.redirect, [key]: value },
  });
  const headerSide = (side: HeaderSide) => draft.headers[side];
  const setHeaderSide = (side: HeaderSide, patch: Partial<HeaderEditsDraft>): RuleDraft => ({
    ...draft,
    headers: { ...draft.headers, [side]: { ...draft.headers[side], ...patch, present: true } },
  });
  /* eslint-enable security/detect-object-injection */

  const multi = (
    label: string,
    field: MultiField,
    apply: (next: MultiField) => Partial<RuleDraft['match']>,
  ) => (
    <Field label={label} hint="Comma-separated for a list.">
      <input
        type="text"
        aria-label={label}
        value={field.text}
        onChange={(e) => setMatch(apply({ ...field, text: e.target.value }))}
      />
    </Field>
  );

  return (
    <div className="rulebuilder">
      <div className="rb-columns">
        <section className="rb-column" aria-label="Rule identity and match criteria">
          <h3>Match</h3>
          <Field
            label="Rule name"
            hint={moduleName === undefined ? 'Unique within the module.' : `In ${moduleName}.`}
          >
            <input
              type="text"
              aria-label="Rule name"
              value={draft.name}
              onChange={(e) => onChange({ ...draft, name: e.target.value })}
            />
          </Field>

          <label className="rb-inline">
            <input
              type="checkbox"
              aria-label="Rule enabled"
              checked={draft.enabled}
              onChange={(e) =>
                onChange({ ...draft, enabled: e.target.checked, enabledPresent: true })
              }
            />
            Enabled
          </label>

          <Field label="Host" hint="Glob, case-insensitive, matched against the full host.">
            <input
              type="text"
              aria-label="Host"
              value={draft.match.host}
              onChange={(e) => setMatch({ host: e.target.value })}
            />
          </Field>
          <Field label="Path" hint="Regex, re.search — anchor explicitly when you mean it.">
            <input
              type="text"
              aria-label="Path"
              value={draft.match.path}
              onChange={(e) => setMatch({ path: e.target.value })}
            />
          </Field>
          {multi('Method', draft.match.method, (method) => ({ method }))}
          {multi('Destination', draft.match.dest, (dest) => ({ dest }))}
          {multi('Status', draft.match.status, (status) => ({ status }))}
          <Field label="Content type" hint="Response-side only.">
            <input
              type="text"
              aria-label="Content type"
              value={draft.match.content_type}
              onChange={(e) => setMatch({ content_type: e.target.value })}
            />
          </Field>

          <PairEditor
            legend="Query criterion"
            pairs={draft.match.query}
            onChange={(query) => setMatch({ query })}
          />
          <PairEditor
            legend="Request header criterion"
            pairs={draft.match.request_headers}
            allowPresenceOnly
            onChange={(request_headers) => setMatch({ request_headers })}
          />
        </section>

        <section className="rb-column" aria-label="Action and parameters">
          <h3>Action</h3>
          <Field label="Action">
            <select
              aria-label="Action"
              value={draft.action}
              onChange={(e) => onChange({ ...draft, action: e.target.value as RuleAction })}
            >
              {RULE_ACTIONS.map((action) => (
                <option key={action} value={action}>
                  {action}
                </option>
              ))}
            </select>
          </Field>

          {draft.action === 'block' && (
            <>
              <Field label="Block mode" hint="stub returns a benign body; kill drops the flow.">
                <select
                  aria-label="Block mode"
                  value={draft.block.mode}
                  onChange={(e) =>
                    onChange({ ...draft, block: { ...draft.block, mode: e.target.value } })
                  }
                >
                  <option value="">(default: stub)</option>
                  <option value="stub">stub</option>
                  <option value="kill">kill</option>
                </select>
              </Field>
              <Field label="Stub" hint="auto, or a named stub from the shipped library.">
                <input
                  type="text"
                  aria-label="Stub"
                  value={draft.block.stub}
                  onChange={(e) =>
                    onChange({ ...draft, block: { ...draft.block, stub: e.target.value } })
                  }
                />
              </Field>
            </>
          )}

          {draft.action === 'map_local' && (
            <>
              <Field label="File" hint="Relative to the module's assets/ directory.">
                <input
                  type="text"
                  aria-label="File"
                  value={draft.mapLocal.file}
                  onChange={(e) =>
                    onChange({ ...draft, mapLocal: { ...draft.mapLocal, file: e.target.value } })
                  }
                />
              </Field>
              <Field label="Content type override">
                <input
                  type="text"
                  aria-label="Content type override"
                  value={draft.mapLocal.content_type}
                  onChange={(e) =>
                    onChange({
                      ...draft,
                      mapLocal: { ...draft.mapLocal, content_type: e.target.value },
                    })
                  }
                />
              </Field>
              <Field label="Status">
                <input
                  type="text"
                  aria-label="Status override"
                  value={draft.mapLocal.status}
                  onChange={(e) =>
                    onChange({ ...draft, mapLocal: { ...draft.mapLocal, status: e.target.value } })
                  }
                />
              </Field>
            </>
          )}

          {draft.action === 'redirect' &&
            REDIRECT_KEYS.map((key) => (
              <Field key={key} label={`Redirect ${key}`}>
                <input
                  type="text"
                  aria-label={`Redirect ${key}`}
                  value={redirectValue(key)}
                  onChange={(e) => onChange(setRedirect(key, e.target.value))}
                />
              </Field>
            ))}

          {draft.action === 'headers' &&
            HEADER_SIDES.map((side) => (
              <div key={side} className="rb-side">
                <h4>{side} headers</h4>
                <PairEditor
                  legend={`${side} add`}
                  pairs={headerSide(side).add}
                  onChange={(add) => onChange(setHeaderSide(side, { add }))}
                />
                <PairEditor
                  legend={`${side} set`}
                  pairs={headerSide(side).set}
                  onChange={(set) => onChange(setHeaderSide(side, { set }))}
                />
                <ListEditor
                  legend={`${side} remove`}
                  values={headerSide(side).remove}
                  onChange={(remove) => onChange(setHeaderSide(side, { remove }))}
                />
              </div>
            ))}

          {draft.action === 'body' && (
            <Field
              label="Transform"
              hint="A named registry entry — transforms are never expressions in YAML."
            >
              <input
                type="text"
                aria-label="Transform"
                value={draft.body.transform}
                onChange={(e) => onChange({ ...draft, body: { transform: e.target.value } })}
              />
            </Field>
          )}

          {/* Anything the form does not model is shown rather than dropped. */}
          <ExtraEditor extra={draft.extra} onChange={(extra) => onChange({ ...draft, extra })} />
        </section>
      </div>

      <section className="rb-preview" aria-label="Emitted YAML preview">
        <h3>Emitted YAML</h3>
        <pre>{preview}</pre>
      </section>
    </div>
  );
}

/**
 * The escape hatch for keys the form does not model.
 *
 * It keeps its own text state because half-typed YAML does not parse, and a
 * strictly derived value would refuse the user's keystrokes mid-word. Invalid
 * text is held locally and simply not committed upward.
 */
function ExtraEditor({
  extra,
  onChange,
}: {
  extra: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const serialised = Object.keys(extra).length === 0 ? '' : stringifyYaml(extra);
  const [text, setText] = useState(serialised);
  const [committed, setCommitted] = useState<Record<string, unknown>>(extra);

  // Reseed only when the value changed somewhere other than this textarea —
  // a different rule was opened. Comparing the *parsed* value rather than the
  // serialised text is what stops a half-typed line being rewritten under the
  // cursor: `a:` parses to `{a: null}`, which would otherwise reformat itself
  // to `a: null` mid-keystroke.
  if (!deepEqual(committed, extra)) {
    setCommitted(extra);
    setText(serialised);
  }

  const parsed = parseExtraYaml(text);
  return (
    <Field
      label="Other keys (YAML)"
      hint="Keys this form does not model — inline stub specs, transform lists."
    >
      <textarea
        aria-label="Other keys as YAML"
        className="rb-extra"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          const next = parseExtraYaml(event.target.value);
          if (next !== null) {
            setCommitted(next);
            onChange(next);
          }
        }}
      />
      {parsed === null && <span className="rb-error">Not valid YAML — not applied yet.</span>}
    </Field>
  );
}
