/**
 * Extension entry placeholder.
 *
 * Sprint 0 proves the toolchain builds. The MV3 manifest, service worker,
 * proxy controller, and fail-safe land in Sprint 5 (SPEC-3 §3, §4).
 */
import { DEFAULT_CONTROL_ORIGIN, assertLoopbackOrigin } from './shared/control-origin';

export const controlOrigin = assertLoopbackOrigin(DEFAULT_CONTROL_ORIGIN);
