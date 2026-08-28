/**
 * The module editor (SPEC-2 §7.2, REQ WUI-006, API-027, WUI-007).
 *
 * `module.yaml` is the single source of truth. The rule builder tab is a
 * generator that writes back into that same text — not a parallel model of it —
 * which is why the YAML tab always shows exactly what will be installed.
 *
 * Validation has two sources and they are deliberately different in kind:
 * local YAML parse errors (instant, syntax only) and `POST /validate` (the
 * daemon's own schema and Python check, authoritative). Both are normalised
 * into the same marker shape before they reach the editor.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ApiClient } from '../../api/client';
import {
  MODULE_PY,
  MODULE_YAML,
  moduleFile,
  type ModuleDetail,
  type ValidationIssue,
} from '../../api/types';
import { CodeEditor, type EditorLoader } from '../editor/CodeEditor';
import type { EditorMarker } from '../editor/types';
import { appendRule, findRuleIndex, readRules, writeRule, yamlIssues } from '../../lib/module-yaml';
import { draftToRule, emptyDraft, ruleToDraft, type RuleDraft } from '../../lib/rule-draft';
import { RuleBuilder } from '../rules/RuleBuilder';

type Tab = typeof MODULE_YAML | typeof MODULE_PY | 'builder';

interface Props {
  api: ApiClient;
  name: string;
  onBack?: (() => void) | undefined;
  /** Test seam: swaps the lazily loaded Monaco implementation. */
  editorLoad?: EditorLoader | undefined;
}

function issueToMarker(issue: ValidationIssue): EditorMarker {
  return {
    line: typeof issue.line === 'number' && issue.line > 0 ? issue.line : 1,
    column: typeof issue.column === 'number' && issue.column > 0 ? issue.column : 1,
    message: issue.message,
    severity: issue.severity === 'warning' ? 'warning' : 'error',
    code: issue.code,
  };
}

/** A validation finding with no file named belongs to the manifest. */
function issueFile(issue: ValidationIssue): string {
  return issue.file === null || issue.file === undefined || issue.file === ''
    ? MODULE_YAML
    : issue.file;
}

export function ModuleEditor({ api, name, onBack, editorLoad }: Props) {
  const [detail, setDetail] = useState<ModuleDetail | null>(null);
  const [yamlText, setYamlText] = useState('');
  const [pyText, setPyText] = useState('');
  const [tab, setTab] = useState<Tab>(MODULE_YAML);
  const [dirty, setDirty] = useState(false);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [status, setStatus] = useState<{ kind: 'ok' | 'error' | 'info'; text: string } | null>(
    null,
  );
  const [draft, setDraft] = useState<RuleDraft>(() => emptyDraft());
  const [selectedRule, setSelectedRule] = useState<string>('');

  const load = useCallback(async () => {
    try {
      const loaded = await api.getModule(name);
      setDetail(loaded);
      setYamlText(moduleFile(loaded, MODULE_YAML));
      setPyText(moduleFile(loaded, MODULE_PY));
      setDirty(false);
      setStatus(null);
    } catch (cause) {
      setStatus({
        kind: 'error',
        text: cause instanceof Error ? cause.message : `Could not open ${name}.`,
      });
    }
  }, [api, name]);

  useEffect(() => {
    void load();
  }, [load]);

  // Unsaved-changes protection on navigation (SPEC-2 §7.2).
  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  const files = useMemo(
    () => ({ [MODULE_YAML]: yamlText, [MODULE_PY]: pyText }),
    [yamlText, pyText],
  );

  const yamlMarkers = useMemo<EditorMarker[]>(
    () => [
      ...yamlIssues(yamlText).map((issue) => ({
        line: issue.line,
        column: issue.column,
        message: issue.message,
        severity: 'error' as const,
        code: 'yaml_syntax',
      })),
      ...issues.filter((issue) => issueFile(issue) === MODULE_YAML).map(issueToMarker),
    ],
    [yamlText, issues],
  );

  const pyMarkers = useMemo<EditorMarker[]>(
    () => issues.filter((issue) => issueFile(issue) === MODULE_PY).map(issueToMarker),
    [issues],
  );

  const validate = useCallback(async () => {
    try {
      const result = await api.validateModule(files);
      setIssues([...result.errors, ...(result.warnings ?? [])]);
      setStatus(
        result.ok
          ? { kind: 'ok', text: 'Valid. Nothing was installed.' }
          : { kind: 'error', text: `${result.errors.length} validation error(s).` },
      );
      return result.ok;
    } catch (cause) {
      setStatus({
        kind: 'error',
        text: cause instanceof Error ? cause.message : 'Validation failed.',
      });
      return false;
    }
  }, [api, files]);

  const save = useCallback(
    async (reload: boolean) => {
      try {
        await api.replaceModule(name, files);
        setDirty(false);
        if (!reload) {
          setStatus({ kind: 'ok', text: 'Saved. Modules were not reloaded.' });
          return;
        }
        const result = await api.reloadModules();
        // The reload result is the answer to "did my edit actually take", so it
        // is surfaced in full rather than reduced to a checkmark. It is set
        // *after* the refetch, which clears the status line of its own accord.
        const message =
          result.errors.length > 0
            ? ({
                kind: 'error',
                text: `Reloaded ${result.loaded} module(s); ${result.errors.length} failed: ${result.errors
                  .map((error) => `${error.code}: ${error.message}`)
                  .join('; ')}`,
              } as const)
            : ({
                kind: 'ok',
                text: `Saved and reloaded. ${result.loaded} loaded, ${result.enabled} enabled, ${result.quarantined} quarantined.`,
              } as const);
        await load();
        setStatus(message);
      } catch (cause) {
        setStatus({ kind: 'error', text: cause instanceof Error ? cause.message : 'Save failed.' });
      }
    },
    [api, files, name, load],
  );

  const rules = useMemo(() => readRules(yamlText), [yamlText]);

  const openRule = (ruleName: string) => {
    setSelectedRule(ruleName);
    const rule = rules.find((candidate) => candidate.name === ruleName);
    setDraft(rule ? ruleToDraft(rule) : emptyDraft());
  };

  /** Write the draft back into `module.yaml`, leaving every other rule alone. */
  const applyDraft = () => {
    const rule = draftToRule(draft);
    if (rule.name.trim() === '') {
      setStatus({ kind: 'error', text: 'A rule needs a name before it can be written.' });
      return;
    }
    const index = findRuleIndex(yamlText, selectedRule === '' ? rule.name : selectedRule);
    const next = index >= 0 ? writeRule(yamlText, index, rule) : appendRule(yamlText, rule);
    if (next === yamlText) {
      setStatus({ kind: 'info', text: 'No change — the rule is already exactly this.' });
    } else {
      setYamlText(next);
      setDirty(true);
      setStatus({ kind: 'ok', text: `Wrote "${rule.name}" into ${MODULE_YAML}.` });
    }
    setSelectedRule(rule.name);
  };

  return (
    <div className="moduleeditor">
      <div className="viewbar">
        {onBack !== undefined && (
          <button type="button" className="action" onClick={onBack}>
            ← Modules
          </button>
        )}
        <h2>{name}</h2>
        {detail !== null && <span className="pill dim">{detail.state}</span>}
        {dirty && (
          <span className="pill warn" title="Unsaved changes">
            unsaved
          </span>
        )}
        <span className="spacer" style={{ flex: 1 }} />
        <button type="button" className="action" onClick={() => void validate()}>
          Validate
        </button>
        <button type="button" className="action" onClick={() => void save(false)}>
          Save
        </button>
        <button type="button" className="action primary" onClick={() => void save(true)}>
          Save and reload
        </button>
      </div>

      {status !== null && (
        <div className={`banner ${status.kind}`} role="status">
          {status.text}
        </div>
      )}

      <div className="tabs" role="tablist" aria-label="Module files">
        {([MODULE_YAML, MODULE_PY, 'builder'] as Tab[]).map((candidate) => (
          <button
            key={candidate}
            type="button"
            role="tab"
            aria-selected={tab === candidate}
            className={tab === candidate ? 'tab active' : 'tab'}
            onClick={() => setTab(candidate)}
          >
            {candidate === 'builder' ? 'rule builder' : candidate}
          </button>
        ))}
      </div>

      {tab === MODULE_YAML && (
        <CodeEditor
          value={yamlText}
          language="yaml"
          markers={yamlMarkers}
          ariaLabel="module.yaml editor"
          onChange={(next) => {
            setYamlText(next);
            setDirty(true);
          }}
          onSave={() => void save(true)}
          load={editorLoad}
        />
      )}

      {tab === MODULE_PY && (
        <CodeEditor
          value={pyText}
          language="python"
          markers={pyMarkers}
          ariaLabel="module.py editor"
          onChange={(next) => {
            setPyText(next);
            setDirty(true);
          }}
          onSave={() => void save(true)}
          load={editorLoad}
        />
      )}

      {tab === 'builder' && (
        <div className="builderpane">
          <div className="viewbar">
            <label className="rb-inline">
              <span>Rule</span>
              <select
                aria-label="Rule to edit"
                value={selectedRule}
                onChange={(event) => openRule(event.target.value)}
              >
                <option value="">(new rule)</option>
                {rules.map((rule) => (
                  <option key={rule.name} value={rule.name}>
                    {rule.name}
                  </option>
                ))}
              </select>
            </label>
            <span className="spacer" style={{ flex: 1 }} />
            <button type="button" className="action primary" onClick={applyDraft}>
              Apply to {MODULE_YAML}
            </button>
          </div>
          <RuleBuilder draft={draft} onChange={setDraft} moduleName={name} />
        </div>
      )}
    </div>
  );
}
