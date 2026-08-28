/**
 * The Monaco implementation of `CodeEditorProps` (REQ WUI-006).
 *
 * Loaded only through `CodeEditor`'s dynamic import, so this module — and the
 * Monaco bundle it drags in — never reaches the traffic view.
 *
 * Validation markers come from `POST /validate` (REQ API-027) and from the
 * local YAML parse; both arrive here already normalised to 1-based line and
 * column, which is what `setModelMarkers` wants.
 */
import { useEffect, useRef } from 'react';
// Imported from the ESM entry points rather than the `monaco-editor` barrel:
// the barrel drags in every bundled language (abap, solidity, …). This UI needs
// two, and the chunk is a third of the size for it.
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api';
import 'monaco-editor/esm/vs/editor/editor.all.js';
import 'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution';
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution';
import { installMonacoEnvironment } from './workers';
import type { CodeEditorProps } from '../types';

installMonacoEnvironment();

const OWNER = 'pporlock';

export function MonacoEditor({
  value,
  language,
  markers,
  onChange,
  ariaLabel,
  onSave,
}: CodeEditorProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const onChangeRef = useRef(onChange);
  const onSaveRef = useRef(onSave);
  onChangeRef.current = onChange;
  onSaveRef.current = onSave;

  useEffect(() => {
    const element = host.current;
    if (element === null) return undefined;
    const instance = monaco.editor.create(element, {
      value,
      language,
      automaticLayout: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      tabSize: 2,
      fontSize: 12,
      ariaLabel,
      // The page is dark by default and follows the OS otherwise (app.css).
      theme: window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'vs' : 'vs-dark',
    });
    editor.current = instance;
    const changed = instance.onDidChangeModelContent(() => {
      onChangeRef.current(instance.getValue());
    });
    // Keyboard save is part of the accessibility contract (SPEC-2 §11).
    const save = instance.addAction({
      id: 'pporlock.save',
      label: 'Save',
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
      run: () => onSaveRef.current?.(),
    });
    return () => {
      save.dispose();
      changed.dispose();
      instance.getModel()?.dispose();
      instance.dispose();
      editor.current = null;
    };
    // Deliberately mounted once: `value` is seeded here and synchronised by the
    // effect below, so a keystroke does not tear down the editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [language, ariaLabel]);

  useEffect(() => {
    const instance = editor.current;
    if (instance !== null && instance.getValue() !== value) instance.setValue(value);
  }, [value]);

  useEffect(() => {
    const model = editor.current?.getModel();
    if (!model) return;
    monaco.editor.setModelMarkers(
      model,
      OWNER,
      markers.map((marker) => ({
        message: marker.code ? `${marker.code}: ${marker.message}` : marker.message,
        severity:
          marker.severity === 'warning'
            ? monaco.MarkerSeverity.Warning
            : monaco.MarkerSeverity.Error,
        startLineNumber: marker.line,
        startColumn: marker.column,
        endLineNumber: marker.line,
        endColumn: model.getLineMaxColumn(Math.min(marker.line, model.getLineCount())),
      })),
    );
  }, [markers]);

  return <div className="editor monaco" ref={host} data-testid="monaco-host" />;
}
