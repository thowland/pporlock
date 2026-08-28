/**
 * Profiles (SPEC-2 §8.1, REQ WUI-009).
 *
 * `default` always exists and is neither deletable nor renamable (REQ MOD-041).
 * The daemon enforces that; the UI states it rather than merely failing, so the
 * rule is learnable without hitting an error.
 *
 * Activation is called out because it changes traffic behaviour immediately —
 * a profile carrying a dev toggle is flagged before it is activated, not after
 * (REQ MOD-044, WUI-012).
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { ProfileSummary } from '../../api/types';

export const DEFAULT_PROFILE = 'default';

interface Props {
  api: ApiClient;
  /** From `GET /state`; the list endpoint also reports it, and they agree. */
  activeProfile?: string | undefined;
  onActivated?: ((name: string) => void) | undefined;
}

function activeToggles(profile: ProfileSummary): string[] {
  const toggles = profile.dev_toggles ?? {};
  return Object.entries(toggles)
    .filter(([, on]) => on === true)
    .map(([toggle]) => toggle);
}

export function ProfilesView({ api, activeProfile, onActivated }: Props) {
  const [profiles, setProfiles] = useState<ProfileSummary[] | null>(null);
  const [active, setActive] = useState<string>(activeProfile ?? DEFAULT_PROFILE);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState('');

  const refresh = useCallback(async () => {
    try {
      const list = await api.listProfiles();
      setProfiles(list.profiles);
      setActive(list.active);
      setError(null);
    } catch (cause) {
      setProfiles([]);
      setError(cause instanceof Error ? cause.message : 'Could not list profiles.');
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const guard = async (work: () => Promise<void>, fallback: string) => {
    try {
      await work();
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : fallback);
    }
  };

  const create = () => {
    const name = newName.trim();
    if (name === '') {
      setError('A profile needs a name.');
      return;
    }
    void guard(async () => {
      await api.createProfile({ name, modules: [] });
      setNewName('');
    }, `Could not create ${name}.`);
  };

  const remove = (profile: ProfileSummary) => {
    if (profile.name === DEFAULT_PROFILE) return;
    void guard(() => api.deleteProfile(profile.name), `Could not delete ${profile.name}.`);
  };

  const activate = (profile: ProfileSummary) => {
    void guard(async () => {
      await api.activateProfile(profile.name);
      setActive(profile.name);
      onActivated?.(profile.name);
    }, `Could not activate ${profile.name}.`);
  };

  if (profiles === null) return <div className="empty">Loading profiles…</div>;

  return (
    <div className="profiles">
      <div className="viewbar">
        <h2>Profiles</h2>
        <span className="spacer" style={{ flex: 1 }} />
        <input
          type="text"
          aria-label="New profile name"
          placeholder="new profile name"
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
        />
        <button type="button" className="action primary" onClick={create}>
          Create profile
        </button>
      </div>

      {error !== null && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      <table className="profilelist">
        <thead>
          <tr>
            <th>Name</th>
            <th className="num">Modules</th>
            <th>Dev toggles</th>
            <th>Description</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {profiles.map((profile) => {
            const isActive = profile.name === active;
            const isDefault = profile.name === DEFAULT_PROFILE;
            const toggles = activeToggles(profile);
            return (
              <tr key={profile.name} className={isActive ? 'selected' : undefined}>
                <td>
                  {profile.name}
                  {isActive && (
                    <span className="pill ok" title="Currently active">
                      active
                    </span>
                  )}
                  {isDefault && (
                    <span className="pill dim" title="default cannot be renamed or deleted">
                      built-in
                    </span>
                  )}
                </td>
                <td className="num dim">{profile.modules?.length ?? 0}</td>
                <td>
                  {toggles.length === 0 ? (
                    <span className="faint">—</span>
                  ) : (
                    <span className="pill devtoggle">⚠ {toggles.join(' + ')}</span>
                  )}
                </td>
                <td className="dim">{profile.description ?? ''}</td>
                <td>
                  <button
                    type="button"
                    className="action"
                    aria-label={`Activate ${profile.name}`}
                    disabled={isActive}
                    onClick={() => activate(profile)}
                  >
                    Activate
                  </button>
                  <button
                    type="button"
                    className="action"
                    aria-label={`Delete ${profile.name}`}
                    // REQ MOD-041: the default profile is not deletable, and
                    // the reason is on the control rather than in an error.
                    disabled={isDefault}
                    title={isDefault ? 'The default profile cannot be deleted' : undefined}
                    onClick={() => remove(profile)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
