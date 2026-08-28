/**
 * Create-rule-from-flow, step two (SPEC-2 §7.4, REQ WUI-008).
 *
 * The builder is already pre-populated when this view mounts — that is the
 * second of the two budgeted clicks. Choosing where the rule lands comes after,
 * because deciding on a destination is a slower decision than recognising the
 * request you want to stop.
 *
 * A new module is created **disabled**, mirroring the daemon's rule that
 * creation never enables (REQ MCP-030). Enabling is presented as a separate,
 * deliberate step in the library rather than as a default.
 */
import { useEffect, useMemo, useState } from 'react';
import type { ApiClient } from '../../api/client';
import { MODULE_YAML, moduleFile, type ModuleStatus, type Rule } from '../../api/types';
import { appendRule, stringifyYaml } from '../../lib/module-yaml';
import { draftToRule, ruleToDraft, type RuleDraft } from '../../lib/rule-draft';
import { slugify } from '../../lib/rule-from-flow';
import { RuleBuilder } from './RuleBuilder';

interface Props {
  api: ApiClient;
  rule: Rule;
  onCreated: (moduleName: string) => void;
  onCancel: () => void;
}

/** A fresh manifest, disabled, with this one rule (SPEC-0 §5.2). */
export function newModuleYaml(name: string, rule: Rule): string {
  return stringifyYaml({
    name,
    version: '0.1.0',
    pporlock_api: '1',
    description: 'Created from a flow in the pporlock web UI.',
    enabled: false,
    priority: 100,
    rules: [rule],
  });
}

export function RuleFromFlowView({ api, rule, onCreated, onCancel }: Props) {
  const [draft, setDraft] = useState<RuleDraft>(() => ruleToDraft(rule));
  const [modules, setModules] = useState<ModuleStatus[]>([]);
  const [target, setTarget] = useState<string>('');
  const [newName, setNewName] = useState(() => slugify(rule.name ?? 'from-flow'));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(ruleToDraft(rule));
  }, [rule]);

  useEffect(() => {
    api.listModules().then(
      (page) => setModules(page.modules),
      () => setModules([]),
    );
  }, [api]);

  const creatingNew = useMemo(() => target === '', [target]);

  const submit = async () => {
    const emitted = draftToRule(draft);
    if (emitted.name.trim() === '') {
      setError('A rule needs a name.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (creatingNew) {
        const moduleName = slugify(newName);
        await api.createModule(moduleName, { [MODULE_YAML]: newModuleYaml(moduleName, emitted) });
        onCreated(moduleName);
      } else {
        const detail = await api.getModule(target);
        const next = appendRule(moduleFile(detail, MODULE_YAML), emitted);
        await api.replaceModule(target, { ...detail.files, [MODULE_YAML]: next });
        onCreated(target);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save the rule.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rulefromflow">
      <div className="viewbar">
        <button type="button" className="action" onClick={onCancel}>
          ← Traffic
        </button>
        <h2>New rule from flow</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <label className="rb-inline">
          <span>Destination</span>
          <select
            aria-label="Destination module"
            value={target}
            onChange={(event) => setTarget(event.target.value)}
          >
            <option value="">(new module)</option>
            {modules.map((module) => (
              <option key={module.name} value={module.name}>
                {module.name}
              </option>
            ))}
          </select>
        </label>
        {creatingNew && (
          <input
            type="text"
            aria-label="New module name"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
        )}
        <button
          type="button"
          className="action primary"
          disabled={saving}
          onClick={() => void submit()}
        >
          {creatingNew ? 'Create module (disabled)' : `Add to ${target}`}
        </button>
      </div>

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      <div className="banner info" role="note">
        A new module is created <strong>disabled</strong>. Enabling it is a separate step in the
        module library — creation never enables.
      </div>

      <RuleBuilder
        draft={draft}
        onChange={setDraft}
        moduleName={creatingNew ? slugify(newName) : target}
      />
    </div>
  );
}
