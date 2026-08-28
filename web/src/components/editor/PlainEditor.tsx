/**
 * The textarea editor (SPEC-2 §7.2 fallback path).
 *
 * This is what renders while Monaco's chunk is in flight, and what renders
 * permanently if that chunk fails to load — a proxy debugging tool served from
 * loopback must never present an empty box where the code was. Markers cannot
 * be gutter decorations here, so they render as a list keyed by line, which is
 * also the accessible presentation of the same information.
 */
import type { CodeEditorProps } from './types';

export function PlainEditor({ value, language, markers, onChange, ariaLabel }: CodeEditorProps) {
  return (
    <div className="editor plain">
      <textarea
        className="editor-area"
        aria-label={ariaLabel}
        spellCheck={false}
        data-language={language}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <MarkerList markers={markers} />
    </div>
  );
}

export function MarkerList({ markers }: Pick<CodeEditorProps, 'markers'>) {
  if (markers.length === 0) return null;
  return (
    <ul className="marker-list" aria-label="Validation problems">
      {markers.map((marker, index) => (
        <li
          key={`${marker.line}-${marker.column}-${index}`}
          className={`marker ${marker.severity}`}
        >
          <span className="marker-pos">
            {marker.line}:{marker.column}
          </span>
          {marker.code !== undefined && marker.code !== '' && (
            <span className="marker-code">{marker.code}</span>
          )}
          <span className="marker-message">{marker.message}</span>
        </li>
      ))}
    </ul>
  );
}
