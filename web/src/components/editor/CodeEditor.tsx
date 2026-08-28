/**
 * The editor mount point (SPEC-2 §7.2, §2.3, §12).
 *
 * Monaco is bundled locally — never CDN-loaded — and loaded *lazily*, so the
 * traffic view does not pay its bundle cost. Until the chunk resolves, and
 * forever if it fails to, the plain textarea editor stands in behind the same
 * `CodeEditorProps` interface. Failing to load an editor must not cost the user
 * their ability to read and change the file.
 */
import { useEffect, useState } from 'react';
import type { ComponentType } from 'react';
import { PlainEditor } from './PlainEditor';
import type { CodeEditorProps } from './types';

export type EditorLoader = () => Promise<ComponentType<CodeEditorProps>>;

const loadMonaco: EditorLoader = () =>
  import('./monaco/MonacoEditor').then((module) => module.MonacoEditor);

export function CodeEditor({
  load,
  ...props
}: CodeEditorProps & { load?: EditorLoader | undefined }) {
  const loader = load ?? loadMonaco;
  const [Impl, setImpl] = useState<ComponentType<CodeEditorProps> | null>(null);

  useEffect(() => {
    let cancelled = false;
    loader()
      .then((component) => {
        // Stored through an updater so React does not mistake a component for
        // a lazy-initialiser function.
        if (!cancelled) setImpl(() => component);
      })
      .catch((error: unknown) => {
        console.warn('pporlock: falling back to the plain editor —', error);
      });
    return () => {
      cancelled = true;
    };
  }, [loader]);

  if (Impl !== null) return <Impl {...props} />;
  return <PlainEditor {...props} />;
}
