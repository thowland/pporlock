/**
 * The editor contract (SPEC-2 §7.2, REQ WUI-006).
 *
 * Monaco and the plain fallback implement exactly this, so nothing above the
 * editor knows which one is mounted.
 */
export interface EditorMarker {
  /** 1-based, matching both Monaco and the daemon's validation output. */
  line: number;
  column: number;
  message: string;
  severity: 'error' | 'warning';
  code?: string | undefined;
}

export type EditorLanguage = 'yaml' | 'python';

export interface CodeEditorProps {
  value: string;
  language: EditorLanguage;
  markers: EditorMarker[];
  onChange: (next: string) => void;
  /** Every editor is an interactive control and needs a name (REQ WUI-015). */
  ariaLabel: string;
  onSave?: (() => void) | undefined;
}
