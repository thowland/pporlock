# Installing pporlock

**REQ DOC-001.** This is the whole path from an empty machine to intercepted
traffic visible in the UI. It is written to be followed literally, in order.

pporlock is macOS-only and Chrome-only by design. It never touches your system
proxy settings — only Chrome's, and only through the extension (REQ SCP-001).
Every other application on the machine is unaffected, which is also why turning
pporlock off cannot break anything else.

---

## 0. What you are about to install

Four pieces, and it is worth knowing which is which before anything goes wrong:

| Piece | What it is | Where it lives |
|---|---|---|
| **daemon** | The proxy itself, plus the control API and the web UI it serves | a Python process you start with `pporlock run` |
| **web UI** | Where you read provenance and author modules | served by the daemon at `http://127.0.0.1:8081/` |
| **extension** | The only thing that can point Chrome at the proxy | loaded unpacked into Chrome |
| **CA certificate** | What lets the proxy decrypt HTTPS | your **login** keychain, not the System keychain |

The CA is the part that deserves a moment's thought. Trusting it means any
process running as you that can reach the proxy can read your HTTPS traffic to
non-excluded hosts. That is the entire point of the tool, and it is also a real
change to your machine's trust posture. §7 tells you exactly how to undo it.

---

## 1. Prerequisites

```bash
# Python 3.12 and uv
brew install uv
uv python install 3.12

# Node 20+, for building the web UI and the extension
node --version   # must be >= 20
```

Chrome must be installed at the usual location. `pporlock doctor` checks this
and will say so if it is not.

---

## 2. Build and install

```bash
git clone https://github.com/thowland/pporlock.git pporlock
cd pporlock

make setup     # python venv + node dependencies for web/ and extension/
make web       # builds the web UI the daemon serves
make extension # builds the unpacked extension into extension/dist/
```

`make setup` is safe to re-run.

### Putting `pporlock` on your PATH

`make setup` builds the daemon into `daemon/.venv/` but does **not** put its
CLI on your `PATH`. Every `pporlock ...` command from here on assumes you have
done one of these two things:

```bash
make install                           # installs `pporlock` into ~/.local/bin
```

which is `uv tool install --editable ./daemon`, plus a check that the result is
actually on your `PATH`. After a dependency or entry-point change, `make
reinstall` forces it — a plain re-install no-ops when the tool is already there,
which is how you end up running yesterday's requirements without being told.

**`--editable` is not optional here.** A plain `uv tool install` copies the
daemon into its own venv, and from there it cannot see this checkout — so it
cannot find the web UI that `make web` builds into `web/dist`, and rebuilding
does not help. The symptom is a daemon that starts fine and reports the web UI
missing however many times you build it. Editable keeps the CLI pointed at the
repo, which is also what you want on a single-user single-machine tool you will
be editing.

Confirm `~/.local/bin` is on your `PATH` — `uv tool install` will say so if it
is not — and check with:

```bash
pporlock version
```

Or, to run it from the repo without installing anything, prefix every command
in this guide with `uv run`:

```bash
cd daemon && uv run pporlock version
```

The install form is assumed below, because §5 loads a Chrome extension that
talks to a daemon you will want to start from any directory.

---

## 3. Generate and trust the CA

The certificate authority does not exist until the daemon has run once, so this
is two steps rather than one:

```bash
pporlock run        # let it start, then ctrl-c
pporlock install    # trusts the CA it just generated
```

`pporlock install` puts the CA in your **login keychain**, not the System
keychain. That means no admin password, and the blast radius is your user
account rather than the whole machine. It is a deliberate trade: a per-user
trust decision is the right granularity for a per-user tool.

If you would rather not trust the CA at all, `pporlock install --no-ca` skips
it. HTTPS interception will not work, but the proxy still runs and plain HTTP
still flows through it.

---

## 4. Disable QUIC in Chrome

**This step is not optional, and skipping it produces the single most confusing
failure mode in the whole system.**

Chrome speaks QUIC (HTTP/3) over UDP to sites that offer it — most large sites
do. A proxy sees none of it. The symptom is that some sites appear in the flow
table and some simply do not, with no error anywhere, and nothing in the UI can
tell you why.

Go to `chrome://flags/#enable-quic`, set **Experimental QUIC protocol** to
**Disabled**, and relaunch Chrome.

`pporlock doctor` checks this (`chrome_quic_disabled`) and fails if it is still
on, precisely because a silent partial capture is worse than a loud failure.

---

## 5. Load the extension and pair

### 5.1 Load it

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. **Load unpacked** → select `extension/dist/`

The extension asks for no broad permissions at install. It requests `proxy`,
`storage`, `tabs`, `alarms`, `webRequest`, `notifications`, and host access to
**loopback only**. It cannot read any page and cannot reach any host that is not
this machine.

Per-tab attribution is the one feature that needs broad host access, because
Chrome only reports requests to an extension that already has access to them.
That grant is optional, is requested from the options page when you ask for the
feature, and everything else works without it.

### 5.2 Pair

The extension deliberately cannot read the daemon's token file (REQ API-012), so
you bridge the two with a short-lived code:

```bash
pporlock run     # in one terminal, leave it running
pporlock pair    # in another
```

`pporlock pair` prints a code. Type it into the extension popup. The code is
single-use and expires; a wrong entry closes the window and you run `pair`
again.

---

## 6. Verify

```bash
pporlock doctor
```

Seventeen checks run. The table below covers the ones that most often fail on a
fresh install; `pporlock doctor` prints all of them with their own explanations.

Not every check must *pass*: `launchd_installed` and `extension_paired` report
**warn** on a machine that has done neither, which is the ordinary state after
following this guide. Treat a `fail` as blocking and a `warn` as a question.

| Check | What a failure means |
|---|---|
| `mitmproxy_present` | The Python environment is not set up — re-run `make setup` |
| `config_valid` | `~/.pporlock/config.yaml` has an error; the message names the field |
| `ca_present` | Run `pporlock run` once to generate the CA |
| `ca_trusted` | Run `pporlock install` |
| `port_proxy_free` | Something else holds port 8080 — set `proxy.listen_port` |
| `port_control_free` | Something else holds port 8081 — set `control.listen_port` |
| `chrome_installed` | Chrome is not where pporlock expects it |
| `chrome_quic_disabled` | **§4 above.** This is the one people skip |
| `exclusions_load` | The exclusion list is malformed |
| `state_dir` | `~/.pporlock` is missing or not writable |

`pporlock doctor --fix` repairs what can be repaired automatically and re-runs
the checks so you see the result rather than a claim about it.

Then, the real verification: with the daemon running and the extension toggle
on, visit any HTTPS site. You should see flows in the table at
`http://127.0.0.1:8081/`, and **no certificate warning in Chrome**. A
certificate warning means the CA is not trusted — go back to §3.

---

## 7. Uninstalling

**REQ DOC-005.** Complete, and honest about what it leaves behind.

```bash
pporlock uninstall
```

This removes CA trust. It then prints what remains, because a tool that says
"done" while leaving your data on disk is lying:

- `~/.pporlock/` — your modules, profiles, recorded sessions, and the pairing
  token
- `~/.mitmproxy/` — the CA key material itself

To remove those too:

```bash
pporlock uninstall --purge    # also deletes ~/.pporlock
rm -rf ~/.mitmproxy           # deletes the CA key material
```

Two more things are not the daemon's to remove, and you should do them by hand:

- **Chrome's proxy setting** — turn the extension toggle off, or remove the
  extension from `chrome://extensions`. Removing the extension returns Chrome to
  a direct connection automatically.
- **The QUIC flag** — set `chrome://flags/#enable-quic` back to Default if you
  want QUIC back.

After all of the above, nothing of pporlock remains except the cloned
repository.

---

## Where to go next

- [Module authoring](module-authoring.md) — writing rules and Python modules
- [Troubleshooting](troubleshooting.md) — "the page is subtly wrong", from
  provenance to cause
