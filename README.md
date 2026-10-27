# fnv-relay

A local bridge between your own ChatGPT web conversation, a private Notion workspace, and [fnvdecomp](https://fnvdecomp.com/agents.md).

**0.2 preview:** initial target is Ubuntu/Linux, including Ubuntu on WSL2. Native Windows support has not been verified. Complete the setup and health checks before starting work; preview tests are not evidence of a successful live contribution.

The web agent reads a lane in Notion and writes C++ candidates there. Your local relay retrieves them, runs the official checker with your own game executable and compiler, and submits only independently checked `MATCH` results under your FNV account. The relay has no model API dependency and uses no third-party coordination server. You normally do not need to forward source files by hand.

You need Python 3.11.8+, Wine, a supported Linux sandbox environment, a legitimately purchased PC copy of Fallout: New Vegas, the official checker/compiler and shared files, an FNV account/key, and your own Notion workspace. Your ChatGPT account needs access to the Notion tools used in the web conversation. Account features and usage limits still apply; this does not promise unlimited agent use or require a separate Codex subscription or model API key.

## Start here

First connect Notion inside the ChatGPT account you will use: **Settings → Integrations → Plugins → Browse directory → search Notion → sign in/sign up → connect**. No action in Notion is needed yet. Back in ChatGPT, open **Settings → Integrations → Plugins → Notion → Allow all tools**. Complete this manually before CLI browser authorization.

On Ubuntu 24.04 x86_64 (including WSL2), the [bootstrap script](docs/BOOTSTRAP.md) installs Wine, extracts Microsoft's original compiler, and sets up the relay. Supply your own game:

```sh
bash scripts/bootstrap-ubuntu.sh --game '/path/to/FalloutNV.exe'
```

You approve FNV and Notion in your browser. The CLI stores its own credentials locally; its Notion browser grant is separate from the ChatGPT connection you completed first. No developer-portal setup or model API key is needed for the default flow. For manual installation:

From an extracted release or local checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/fnv-relay --help
```

Run **`./relay`** after bootstrap to open the full-screen Courier console. **Overview** updates lane tables in place; **Lane controls** changes task selection, pauses work and resumes preserved functions; **History** shows score changes; **Activity** shows recorded data flow and errors; **Prompt notepad** stores editable copy-only prompts; **Settings** toggles metrics and opens account setup. Use the mouse, Tab, or F1–F6 to navigate. **Ctrl+Q** stops safely and exits. Keep the app open while solving; adding lanes and copying starters uses the same screen while the managed worker runs. Interrupted lane setup is recovered through Add lane. `fnv-relay text-menu` retains the numbered interface, and explicit `start`, `stop` and `status --json` commands remain available for scripts.

Common commands:

```sh
fnv-relay status                 # readable lane/function progress
fnv-relay stop                   # release owned claims and exit safely
fnv-relay drain                  # finish current work, then exit
fnv-relay lane pause "My lane"
fnv-relay lane resume "My lane"
fnv-relay lane configure "Lane 3" --order random --no-allow-attempted
fnv-relay lane configure "Lane 3" --order random --no-allow-attempted --switch-now
fnv-relay status --json          # machine-readable status
```

Setup asks for largest first, smallest first, or a random eligible size band and saves the choice. Use `fnv-relay start --order smallest` to change the order for unpaused lanes, or `lane create --order largest` for a particular new lane. Pro/Free suggestions are starting heuristics, not measured model results. Random does not mean a uniformly random function from the whole project.

`start` runs in the foreground. Ctrl+C, SIGTERM and terminal hangup request release and cleanup; allow the current compiler process to finish. An unresolved submission or release is reported as pending, with its reservation retained. Restarting continues an unpaused lane's saved function; manually paused lanes require `lane resume`.

Follow [Setup](docs/SETUP.md) for browser approval with `auth fnv connect`, official downloads with `tools fetch`, the game/toolchain paths, Notion browser authorization with `auth notion connect`, health checks, and your first lane. [Discord quick start](docs/DISCORD.md) is a short introduction you can share. [Validation and preview boundaries](docs/VALIDATION.md) distinguishes tested behavior from remaining limitations.

See [ChatGPT plans and connection checks](docs/SUBSCRIPTIONS.md) before choosing a subscription. Pro is not a relay requirement; actual Notion read/write access is.

## Data and ownership

Claims and project credit belong to your FNV account. Credentials and local work stay in your local state directory; lane context is written to relay pages in your selected Notion workspace. Keep that parent page private in your own workspace. Notion is an external service, so content written there is subject to its policies.

Do not publish state directories, private page links, tokens, context packs, candidate source, game files, compiler binaries, or debug traces as part of this package. Follow the [FNV contributor terms](https://fnvdecomp.com/legal.html) and official agent rules. This package's MIT license covers its own code only; it does not license the game, Microsoft compiler, checker, or project context.

Official setup references were checked on 2026-10-24. Follow current upstream instructions when they change.


New diagnostic checks retain the selected compiled object locally and attach a compact, source-bound evidence summary to each result. `candidate_artifacts` requests include full relocation-normalized target/candidate disassembly, aligned differences, region ranges and relocation bindings. Reports use the full candidate extent (up to 256 KiB), separately labeled from the official checker's limited-window metric, and add no compiler invocations. Derived relocation bindings are checker assumptions, not independent semantic proof. Binary objects remain local. Older checks without captured objects return an explicit unavailable reason; requesting evidence never recompiles. Independent plain MATCH validation remains unchanged.

Fresh-only selection uses the project's never-claimed preference and never falls back to attempted work. Random selection randomizes eligible size bands; it is not uniformly random across all functions. Selection changes apply after a running batch; **choose a new function now** preserves/releases current work and parks it before selecting another task. Saved work can still be resumed explicitly.

The prompt notepad comes with the owner's **Notion tool issues** and **Ending turns early** presets verbatim. Edits and custom prompts stay in the profile's private local state and are never automatically sent or added to agent starters. Clipboard support depends on the terminal; the text is also selectable.

In **Add lane** or **Lane controls**, choose a size bucket independently of largest/smallest/random order: **Any size**, **≤255 B**, **256–511 B**, **512 B–1 KB**, **1–4 KB**, or **4 KB+**. Bucket selection uses the eligible function list so ordering still works within a bucket. Changes apply to future selections; current work stays intact unless you choose to switch now.

**Starter Prompt** opens viewing, copying and template-editing options. Preview a custom template for the selected lane, save it for this profile’s future starters, cancel edits or restore the original. Keep `{{landing_url}}` and `{{route_uuid}}`; `{{lane_label}}` is optional. Saved templates also apply to startup output and `lane prompt`. The original default wording is unchanged, and saving a template does not send it anywhere.

Start checks the current official checker and shared data before accepting work. New claims use validated tool snapshots, refreshed at least every five minutes while selecting work. A missing target address triggers another official refresh before candidate checks can run. Existing assignments retain their snapshot; downloads never overwrite files used by another running check or profile.

Functions parked because FNV paused them can return through FNV's eligible pool after reopening. Fresh-only settings and configured attempt limits still apply; work parked by the operator stays parked.

Official tool generations also include the current upstream `agents.md` as
reference data for new assignments. Existing generations and active leases stay
pinned. Plain validation recognizes the official accepted-alternate-flags MATCH
format only for flag sets listed in the original pack; proof-only override
MATCHes remain ineligible for ordinary submission. The accepted flag set is
retained in checker evidence. Operator-authored prompts are not overwritten by
upstream documentation updates.

### Automatic application releases

The operator UI checks the configured GitHub repository for stable releases at
startup and hourly. Settings can disable these checks. A release must include a
package/version-bound `release.json` and a wheel with the matching SHA-256.
Installation and dependency/import checks happen in a separate runtime before an
atomic next-launch pointer is published. The running UI, workers and their pinned
assignments keep their current code. A failed check or install retains the last
good runtime and exposes update status; it does not block work.

Only a later operator UI launch activates a prepared version. Worker and
maintenance commands do not silently switch versions mid-operation. The first
installation of this updater still requires the usual explicit package upgrade.
Published Git tags trigger the release workflow, which builds the wheel and its
manifest. This is application code from the configured repository, not arbitrary
URLs supplied by a worker or report.
