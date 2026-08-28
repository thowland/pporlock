/**
 * The intent picker (SPEC-2 §7.4, REQ WUI-008).
 *
 * Two clicks from a flow row to a pre-filled rule: one to open this menu, one
 * to choose an intent. Anything longer and the path from "that request broke my
 * page" to "I have a rule for it" stops being the obvious thing to do.
 *
 * The intent is resolved by the daemon (`POST /flows/{id}/suggest-rule`) so the
 * UI, the DevTools panel, and MCP all propose the same rule. When that call
 * fails the local derivation stands in — a degraded suggestion beats a dead
 * button on a tool whose whole job is to keep working when things break.
 */
import { useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { FlowRecord, Rule, RuleIntent } from '../../api/types';
import { INTENT_LABELS, ruleFromFlow } from '../../lib/rule-from-flow';

const INTENTS: RuleIntent[] = ['block', 'map_local', 'redirect', 'headers'];

interface Props {
  api: ApiClient;
  flow: FlowRecord;
  onRule: (rule: Rule) => void;
}

export function CreateRuleMenu({ api, flow, onRule }: Props) {
  const [open, setOpen] = useState(false);

  const choose = async (intent: RuleIntent) => {
    setOpen(false);
    try {
      const suggestion = await api.suggestRule(flow.flow_id, intent);
      onRule(suggestion.rule);
    } catch {
      onRule(ruleFromFlow(flow, intent));
    }
  };

  return (
    <span className="rulemenu">
      <button
        type="button"
        className="action"
        aria-label={`Create rule from flow ${flow.flow_id}`}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={(event) => {
          event.stopPropagation();
          setOpen(!open);
        }}
      >
        rule…
      </button>
      {open && (
        <span className="rulemenu-items" role="menu" aria-label="Rule intent">
          {INTENTS.map((intent) => {
            // `intent` comes from the closed INTENTS tuple in this file.
            // eslint-disable-next-line security/detect-object-injection
            const label = INTENT_LABELS[intent];
            return (
              <button
                key={intent}
                type="button"
                role="menuitem"
                className="action"
                onClick={(event) => {
                  event.stopPropagation();
                  void choose(intent);
                }}
              >
                {label}
              </button>
            );
          })}
        </span>
      )}
    </span>
  );
}
