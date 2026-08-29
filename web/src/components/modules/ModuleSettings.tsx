/**
 * A module's own settings, as a form the module's author declared.
 *
 * The module library could always turn a module on and reorder it. Everything
 * else about it lived in a YAML file, so "browse as Googlebot instead of
 * GPTBot" meant opening the editor and finding the right line — an interaction
 * the library exists to avoid for `enabled`, and no more reasonable here.
 *
 * The form is rendered from the daemon's declaration (`ModuleSetting`), never
 * from anything hard-coded here: this component knows about six field types and
 * nothing whatsoever about user-agent switching. A module that declares nothing
 * has no settings control at all.
 *
 * **Nothing is sent until Save.** Editing the live config a keystroke at a time
 * would fire a PATCH per character and, worse, would briefly send whatever a
 * half-typed field contained to a module that is modifying live traffic. The
 * daemon replaces the whole override map in one call, so Save is also what
 * makes "reset to default" expressible — a field left at its default is simply
 * not sent.
 */
import { useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { ModuleDetail, ModuleSetting, ModuleSettingValue } from '../../api/types';

interface Props {
  api: ApiClient;
  name: string;
  onClose: () => void;
  /** Called after a successful save, so the library can pick up the new state. */
  onSaved: () => void;
}

type Values = Record<string, ModuleSettingValue>;

/** The value in force for a field, falling back to what the author declared. */
function valueOf(setting: ModuleSetting, values: Values): ModuleSettingValue {
  const held = Object.prototype.hasOwnProperty.call(values, setting.key)
    ? values[setting.key]
    : undefined;
  if (held !== undefined) return held;
  if (setting.default !== undefined && setting.default !== null) return setting.default;
  return setting.type === 'boolean' ? false : setting.type === 'string_list' ? [] : '';
}

/**
 * Equality by JSON, which is enough here: every value is a string, boolean,
 * integer or array of strings, and the daemon refuses anything else.
 */
function same(a: ModuleSettingValue | undefined, b: ModuleSettingValue | undefined): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/**
 * What to send: only the fields that differ from their declared default.
 *
 * Sending everything would work, but it would freeze today's defaults into the
 * user's state — a later version of the module that improved a default would
 * never reach anyone who had opened this dialog once.
 */
export function changedFrom(settings: ModuleSetting[], values: Values): Values {
  const out: Values = {};
  for (const setting of settings) {
    const current = valueOf(setting, values);
    if (!same(current, setting.default)) out[setting.key] = current;
  }
  return out;
}

export function ModuleSettings({ api, name, onClose, onSaved }: Props) {
  const [detail, setDetail] = useState<ModuleDetail | null>(null);
  const [values, setValues] = useState<Values>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setProblem(null);
    api.getModule(name).then(
      (result) => {
        if (!live) return;
        setDetail(result);
        setValues({ ...(result.config ?? {}) });
      },
      (error: unknown) => {
        if (live) setProblem(error instanceof Error ? error.message : String(error));
      },
    );
    return () => {
      live = false;
    };
  }, [api, name]);

  const settings = detail?.settings ?? [];

  const set = (key: string, value: ModuleSettingValue) => {
    setValues((previous) => ({ ...previous, [key]: value }));
  };

  const save = async () => {
    setSaving(true);
    setProblem(null);
    try {
      await api.patchModule(name, { config: changedFrom(settings, values) });
      onSaved();
      onClose();
    } catch (error) {
      // Kept open on failure. The daemon writes nothing when it refuses, so
      // closing would discard edits the user still has the only copy of.
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="modsettings" aria-label={`${name} settings`}>
      <header className="report-head">
        <h2>{name} settings</h2>
        <button type="button" className="action" onClick={onClose}>
          close
        </button>
      </header>

      {problem !== null && (
        <div className="banner error" role="alert">
          {problem}
        </div>
      )}

      {detail === null && problem === null && <p className="dim setting-empty">Loading…</p>}

      {detail !== null && settings.length === 0 && (
        <p className="dim setting-empty">This module declares no settings.</p>
      )}

      {settings.map((setting) => (
        <SettingField
          key={setting.key}
          setting={setting}
          value={valueOf(setting, values)}
          disabled={saving}
          onChange={(value) => set(setting.key, value)}
        />
      ))}

      {settings.length > 0 && (
        <footer className="setting-actions">
          {/* Module code is trusted and unsandboxed (REQ MOD-031), and a
              setting is an input to it. Every authoring surface says so; a
              surface that changes what module code does is one of them. */}
          <p className="dim">
            Settings are read by module code, which runs unsandboxed with full access to your
            intercepted traffic.
          </p>
          <button type="button" className="action" disabled={saving} onClick={() => setValues({})}>
            Reset to defaults
          </button>
          <button
            type="button"
            className="action primary"
            disabled={saving}
            onClick={() => void save()}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </footer>
      )}
    </section>
  );
}

function SettingField({
  setting,
  value,
  disabled,
  onChange,
}: {
  setting: ModuleSetting;
  value: ModuleSettingValue;
  disabled: boolean;
  onChange: (value: ModuleSettingValue) => void;
}) {
  const describedBy = setting.description ? `${setting.key}-help` : undefined;

  return (
    <div className="setting">
      <label className="setting-label" htmlFor={`setting-${setting.key}`}>
        {setting.label}
      </label>

      <Control
        setting={setting}
        value={value}
        disabled={disabled}
        describedBy={describedBy}
        onChange={onChange}
      />

      {setting.description !== undefined && setting.description !== '' && (
        <p className="dim setting-help" id={describedBy}>
          {setting.description}
        </p>
      )}
    </div>
  );
}

function Control({
  setting,
  value,
  disabled,
  describedBy,
  onChange,
}: {
  setting: ModuleSetting;
  value: ModuleSettingValue;
  disabled: boolean;
  describedBy: string | undefined;
  onChange: (value: ModuleSettingValue) => void;
}) {
  const id = `setting-${setting.key}`;
  const described = describedBy === undefined ? {} : { 'aria-describedby': describedBy };

  if (setting.type === 'boolean') {
    return (
      <input
        id={id}
        type="checkbox"
        checked={value === true}
        disabled={disabled}
        {...described}
        onChange={(event) => onChange(event.target.checked)}
      />
    );
  }

  if (setting.type === 'enum') {
    return (
      <select
        id={id}
        value={String(value)}
        disabled={disabled}
        {...described}
        onChange={(event) => onChange(event.target.value)}
      >
        {(setting.options ?? []).map((option) => (
          <option key={option.value} value={option.value} title={option.description}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }

  if (setting.type === 'integer') {
    return (
      <IntegerField
        id={id}
        setting={setting}
        value={typeof value === 'number' ? value : 0}
        disabled={disabled}
        described={described}
        onChange={onChange}
      />
    );
  }

  if (setting.type === 'string_list') {
    // One per line rather than a comma-separated box: the values are host
    // globs and header names, which may legitimately contain a comma, and a
    // separator that appears inside the data is not a separator.
    return (
      <textarea
        id={id}
        rows={4}
        value={(Array.isArray(value) ? value : []).join('\n')}
        disabled={disabled}
        placeholder={setting.placeholder}
        {...described}
        onChange={(event) => onChange(event.target.value.split('\n'))}
      />
    );
  }

  if (setting.type === 'text') {
    return (
      <textarea
        id={id}
        rows={3}
        value={String(value)}
        disabled={disabled}
        placeholder={setting.placeholder}
        {...described}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }

  return (
    <input
      id={id}
      type="text"
      value={String(value)}
      disabled={disabled}
      placeholder={setting.placeholder}
      {...described}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

/**
 * An integer field that survives being emptied.
 *
 * A number input wired straight to state turns an empty box into `0` and then
 * refuses to let the user type a new value in front of it. This holds the text
 * and reports a number only when the text is one, so clearing the field leaves
 * the last good value in place rather than silently sending zero — which for a
 * bounded setting would be refused, and for an unbounded one would be wrong.
 */
function IntegerField({
  id,
  setting,
  value,
  disabled,
  described,
  onChange,
}: {
  id: string;
  setting: ModuleSetting;
  value: number;
  disabled: boolean;
  described: Record<string, string>;
  onChange: (value: ModuleSettingValue) => void;
}) {
  const [text, setText] = useState(String(value));
  const [seen, setSeen] = useState(value);

  if (seen !== value) {
    setSeen(value);
    setText(String(value));
  }

  return (
    <input
      id={id}
      type="number"
      value={text}
      disabled={disabled}
      min={setting.min}
      max={setting.max}
      {...described}
      onChange={(event) => {
        setText(event.target.value);
        const next = Number(event.target.value);
        if (event.target.value.trim() !== '' && Number.isInteger(next)) onChange(next);
      }}
    />
  );
}
