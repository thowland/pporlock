/**
 * The module-authoring guides, from the page where you would want them.
 *
 * The documentation is good and nothing in the product pointed at it. Someone
 * looking at an empty module library has no way to discover that a tutorial,
 * a cookbook and a full reference exist — the library says "author it here" and
 * then leaves them with a YAML editor.
 *
 * The guides open on GitHub rather than in the app. The daemon serves the
 * built web UI, not the repository, so rendering them here would mean either
 * bundling six markdown files into the UI or teaching the control server to
 * serve `docs/` — both larger than the problem. What is in the dialog instead
 * is the part that is genuinely useful without leaving the page: the shape of a
 * module on disk, which is the thing everyone has to look up once.
 */
import { Modal } from '../Modal';
import { docUrl, GUIDES } from '../../lib/about';

export function GuidesDialog({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Writing modules" onClose={onClose}>
      <p className="modal-lede">
        A module is a directory: a manifest, optional YAML rules, optional Python. Nothing is
        enabled by the act of creating it — enabling is always a separate, deliberate step.
      </p>

      <pre className="modal-code">
        {`modules/my-module/
  module.yaml     name, version, priority, settings
  rules.yaml      declarative match/action rules
  hooks.py        optional: request/response hooks
  assets/         files map_local may serve — and nothing outside it`}
      </pre>

      <div className="banner warn" role="note">
        Module code is trusted and unsandboxed, and a dry run executes it too. Read a module before
        you enable it.
      </div>

      <ul className="guides">
        {GUIDES.map((guide) => (
          <li key={guide.file}>
            <a href={docUrl(guide.file)} target="_blank" rel="noreferrer">
              {guide.title}
            </a>
            <p>{guide.blurb}</p>
            <code>{guide.file}</code>
          </li>
        ))}
      </ul>

      <p className="modal-foot">
        These open on GitHub. Every one of them is also in <code>docs/</code> in your own checkout.
      </p>
    </Modal>
  );
}
