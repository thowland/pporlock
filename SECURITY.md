# Security

pporlock terminates TLS, holds session cookies in memory, runs unsandboxed user
code, and can rewrite any page the browser loads. It installs a certificate
authority into the operating user's login keychain. Those are its features.
This file is about the boundaries it does and does not claim.

## What is in scope

A vulnerability report is welcome for anything that breaks one of these, all of
which the project treats as load-bearing:

- **Listeners bind loopback only.** A non-loopback configuration value is
  rejected at startup, and the check is asserted in code rather than documented.
- **The control API requires a bearer token**, and the token never appears in a
  URL, an error body, or the audit log.
- **The origin policy.** Only the paired extension and the daemon's own web UI
  may drive the control API.
- **Redaction happens at write time.** A session file on disk must never contain
  an unredacted secret — headers, JSON body keys, or query strings.
- **Unmasking is live-ring-only, web-UI-only, and one value at a time.** The MCP
  interface has no unmask capability at all.
- **Asset confinement.** `map_local` and module asset paths resolve inside the
  module's own `assets/`, with symlinks resolved before the containment check.
- **The MCP server cannot enable a module**, including one it has just written.

## What is deliberately not a boundary

**Module code is trusted and unsandboxed.** There is no jail, no import
allowlist and no resource limit. A module runs with the privileges of the user
who started the daemon and can do anything that user can. **Dry run executes it
too** — it is a rehearsal of a modification, not a sandbox. The guardrails that
exist — error isolation, failure quarantine, a per-flow time budget — contain
*mistakes*, not hostility.

So "a module can read your files" is not a vulnerability in pporlock; it is the
stated trust model, and the mitigation is reading module code before enabling
it. Where the project sandboxes something anyway — module-authored report HTML
is served under a `sandbox` CSP and rendered in a sandboxed frame — that is a
refusal to add a *convenient* path, not a claim of containment.

**The CA is a real change to the machine's trust posture.** Trusting it means
any process running as that user which can reach the proxy can read HTTPS
traffic to non-excluded hosts. `docs/install.md` §6 covers removing it.

**Single user, single machine.** pporlock has no multi-user model, no
authorisation levels, and no notion of an untrusted operator. It is a tool
someone runs against their own browsing.

## Reporting

Email **th@wdogsystems.com**. Please include what you did, what happened, and
what you expected. If the issue is a working exploit, describe the class of
problem rather than attaching a ready-made one.

There is no bounty and no guaranteed response time. This is a personal project.

## Using it

Point it at systems you are authorised to inspect. A tool that can modify any
response can break things, and the person operating it chooses the target.
