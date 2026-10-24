# Setup

This is a 0.2 preview for Ubuntu/Linux, including Ubuntu under WSL2. Python 3.11.8+ is required. Native Windows has not been verified. Run commands from your extracted package or checkout; no public hosting URL is assumed.

## Guided account setup after bootstrap

With the game/compiler/checker installed using [Ubuntu/WSL2 bootstrap](BOOTSTRAP.md), run the installed CLI from an interactive terminal:

```sh
fnv-relay setup
fnv-relay doctor
fnv-relay start
```

If the CLI is not on your PATH, use `~/.local/share/fnv-relay-tools/venv/bin/fnv-relay`. For a custom state directory, place `--state-dir /your/state` before each command.

`setup` reuses existing credentials. If missing, it walks you through FNV's GitHub browser approval and explains how to create a Notion internal connection, enter its token privately, and share a private parent page. Notion's internal token setup is manual; this package does not pretend to provide a public Notion OAuth application. The wizard verifies the parent is readable, saves it for future lanes, and offers to create your first lane and print its ChatGPT starter prompt. Connect your Notion account separately in ChatGPT before using that starter. No model API key is needed. Tool installation and the official image/compiler checks remain separate prerequisites.

The detailed manual steps below remain available if you prefer individual commands.

## 1. Install locally

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/fnv-relay init
```

Use `.venv/bin/fnv-relay` for the commands below, or activate the environment with `. .venv/bin/activate` first. The default state directory is `~/.local/share/fnv-relay`. To choose another location, initialize it with `fnv-relay init --state-dir /absolute/path/to/state`; put the global option before subsequent commands, for example `fnv-relay --state-dir /absolute/path/to/state status`. Keep state out of source control and shared folders. Credential files are stored locally with mode `0600`; other processes running as your user can still read them.

## 2. Prepare your own game and official tools

Read the [official FNV agent instructions](https://fnvdecomp.com/agents.md) and [contributor terms](https://fnvdecomp.com/legal.html). Bootstrap from your own terminal; no Codex session or existing project API key is needed:

```sh
fnv-relay auth fnv connect --label my-relay
```

Open the displayed official FNV approval link in your browser and approve with GitHub. Approving accepts the contributor terms, including game ownership. The CLI displays only the public approval link/code, polls at the service's interval, and saves your API key privately when approved. Keep the terminal open while approving. Device codes and API keys are never printed. An expired or declined approval stops the command; run it again when ready. Initial connection requests are not automatically retried after an uncertain failure. If you already have a project API key instead, use `fnv-relay auth fnv login` and enter it at the hidden terminal prompt. Never paste either kind of credential into ChatGPT or Discord.

Download the official checker, symbols, function index, and compiler tips into a **new or empty** directory:

```sh
fnv-relay tools fetch --directory "$HOME/fnv-relay-tools"
```

This authenticates to the project's official download URLs and writes a local SHA-256 manifest. It safely extracts the checker to `fnv-relay-tools/fnvcheck`, prints suggested configuration paths, and leaves your existing configuration unchanged. Stop the relay and resolve active claims first: downloads are refused while the worker is running or this key has an active claim. If a download stops partway, inspect the partial directory and choose a new empty destination; existing files are never overwritten by rerunning fetch.

Read `fnv-relay-tools/fnvcheck/README.md` for the checker dependencies, Wine, and compiler extraction recipe. Download and extract the Microsoft Visual C++ 2008 Express SP1 compiler yourself from the Microsoft link in the official instructions; the required compiler version is **15.00.30729.01**. These third-party files are not bundled with the relay, and `tools fetch` does not download the game or Microsoft compiler.

Use your own legitimately purchased PC game executable. The official checker requires the plain code image for **1.4.0.525** and must report `IMAGE OK`. A storefront purchase alone does not prove a particular executable is usable: follow the checker instructions rather than assuming every GOG or Steam installation works. The service does not supply game bytes or disassembly.

Configure absolute local paths (replace every example):

```sh
fnv-relay config \
  --game-exe /absolute/path/to/FalloutNV.exe \
  --checker-dir /absolute/path/to/fnvcheck \
  --compiler-dir /absolute/path/to/compiler \
  --function-index /absolute/path/to/function_index.json \
  --symbols /absolute/path/to/fnvdecomp_symbols.json \
  --compiler-tips /absolute/path/to/compiler_tips.md \
  --wine-prefix /absolute/path/to/wine-prefix \
  --python /absolute/path/to/checker-venv/bin/python
```

The configured Python must have the official checker's dependencies installed; the relay's own minimal environment does not install them. You may omit `--python` only if the relay interpreter already satisfies the checker README.

Compilation also needs Wine and the relay's Linux isolation prerequisites, including user namespaces (`unshare`) and Landlock support. A WSL2 installation is not automatically proof these are enabled. Run `doctor` below and resolve failed checks before serving lanes. Its namespace, Landlock, file, and authentication checks are preliminary: it does not compile a smoke candidate, verify the game image, or prove the compiler version. Use the official checker for those checks. Initialize only a new dedicated Wine prefix with `fnv-relay doctor --prepare-wine`; this explicit option runs Wine initialization and writes to that prefix. Do not bypass isolation to make a failed health check disappear.

## 3. Connect a private Notion parent

The local relay uses a **Notion internal connection API token**. This is separate from the Notion connection you authorize in ChatGPT; Codex/ChatGPT OAuth credentials are not a replacement.

1. As a workspace owner, create an internal connection in the [Notion developer portal](https://www.notion.so/profile/integrations). Enable read, insert, and update content capabilities needed for the lane pages.
2. Create a private parent page in that workspace. In its `•••` menu, choose **Add connections** and select your internal connection. Grant access to this parent rather than unrelated workspace pages.
3. Retrieve the connection token from its Configuration tab, then enter it at the hidden prompt:

```sh
fnv-relay auth notion login
fnv-relay doctor
```

Separately authorize your own Notion account in ChatGPT and confirm the web agent can read and edit the parent and its lane pages. Neither credential grants the other client access automatically. See [Notion authorization](https://developers.notion.com/guides/get-started/authorization).

**Check workspace capacity first.** Since September 8, 2026, applicable Notion API writes enforce Free workspace block limits. For internal connections, a Free workspace with more than one member has 1,000 lifetime blocks; deleting blocks does not recover capacity. Single-member Free workspaces and paid workspaces have unlimited blocks under that policy; guests are not members. Notion MCP has separate enforcement. A block-limit failure is not fixed by retrying. Check your actual workspace before assuming a purchase is needed. [Workspace block limits](https://developers.notion.com/reference/workspace-block-limits) and [API request limits](https://developers.notion.com/reference/request-limits) are separate policies.

## 4. Start one lane

Creating a lane writes to the Notion parent you supply, or the parent previously verified by `setup` when `--parent-page` is omitted. Starting the relay enables live project activity under your FNV credentials, including claims and submissions. Start with one lane so you can inspect the workflow.

```sh
fnv-relay lane create --parent-page 'YOUR_PRIVATE_PARENT_PAGE_URL_OR_UUID' --label first-lane
fnv-relay lane list
fnv-relay lane prompt ROUTE
fnv-relay start
```

Replace `ROUTE` with the route reported for your lane. Paste the generated prompt into your own ChatGPT web conversation with Notion access. The agent works through that private lane while the local relay handles the official checks. Leave the relay running in its terminal. `serve` checks local toolchain/isolation readiness and validates both service credentials before polling. You can add another lane while it runs. A private lock under `~/.local/share/fnv-relay-locks` prevents simultaneous workers using the same FNV key across state directories; it does not coordinate different keys for one account or other applications. Use one relay state per owner key and follow project rules for other agents. A local `MATCH` is a prerequisite for submission, not a guarantee of the project's later verification verdict.

Optional lane selectors include `--order largest`, or `smallest`; `--band '<=255'`, `256-511`, `512-1K`, `1K-4K`, or `4K+`; `--address 0x...`; and `--min-bytes` / `--max-bytes`. Quote bands containing shell metacharacters. Ordering chooses bands rather than globally ranking every open function; `closest` is not supported in this preview. Exact byte bounds can reject an allocated function outside your range. Selectors do not override project eligibility, tier limits, leases, or another agent's claims.

```sh
fnv-relay status
fnv-relay stop
```

`stop` (also `shutdown`) requests release of this relay's tracked claims, preserves checked best source, and waits up to 15 seconds for a running worker. If the worker is already stopped, it attempts release directly using your FNV credential; Notion authorization is not needed for that release. A timeout means shutdown is still pending, not that claims are resolved. `stop --timeout 60` allows a longer wait (maximum 120 seconds). Ambiguous submissions or releases stay reserved and are reported as pending/uncertain. Never delete state to bypass them.

`drain` retains finish-current behavior: no new claims are assigned, while existing work can finish before the worker exits. `start` is an alias for foreground `serve`; Ctrl+C, SIGTERM, and terminal hangup request cleanup outside the signal handler. An in-flight compile may finish before cleanup. `serve --once` also releases tracked claims on exit, so it is a bounded live pass rather than a way to leave claims running unattended. Abrupt process death can only be handled by a surviving guardian while the machine remains up; power loss cannot trigger network release.

Use `lane pause ROUTE_OR_LABEL` to stop future admissions without interrupting current work; add `--release` to request release now. Use `lane resume ROUTE_OR_LABEL` to deliberately enable a paused lane and its saved unfinished function, then `start` if the worker is stopped. Exact labels and unique route prefixes of at least four characters are accepted. Duplicate labels or ambiguous prefixes are rejected.

New CLI lanes default to `--auto-resume`; use `--no-auto-resume` to opt out. Existing lanes without this policy retain their prior behavior. Continuation targets the same unfinished address after its cooldown and requires recent fresh agent activity (within 15 minutes), or an explicit user resume. It does not fall back to another function when that address is unavailable. Pause or stop blocks automatic admission while stopped. Deliberate `start` grants one continuation attempt to an unpaused lane with saved work, even if its prior heartbeat is stale; manually paused lanes stay paused. Saved candidates are context for the new claim and must be checked with its current pack before submission.

`status` displays lane labels/routes, paused/resume state, current address/size/expiry, best recorded difference, check count, cooldown, and sanitized error type. `status --json` returns a stable `schema_version: 1` structure for scripts. Local status remains offline. `status --refresh` adds read-only FNV account totals and distinguishes all account claims from this key's claims and the locally tracked lanes. `logs --tail 20` shows recent sanitized event kinds, claim IDs and status tags; it excludes source, full error messages and remote bodies. Neither status view dumps candidate source, API keys, or full remote error bodies. Respect upstream rate limits and claim deadlines. Use the project account status to confirm final verification and credit.

`fnv-relay reconcile` lists uncertain external operations without retrying or clearing them. Review actual service state before recovery; this preview does not provide an automatic reconciliation override. A claim request whose response was lost may already belong to your account: allocation pauses for manual review rather than requesting another claim. Inspect the official account status and resolve unknown claims before continuing; do not delete local state to bypass this pause.

## Troubleshooting

- **Doctor fails:** resolve the specific credential, file, Wine, image, or sandbox check. Do not start live lanes until required checks pass.
- **Notion page unavailable:** verify both the internal connection's parent-page access and your web agent's own Notion authorization.
- **Notion 403:** inspect whether it is a permission failure or workspace block limit. Repeated retries cannot repair either configuration.
- **Notion 429 / overload:** honor the service's retry delay. API and MCP limits can differ.
- **No claim available:** account eligibility, other open claims, project capacity, and selector constraints can all prevent allocation. Do not create extra accounts or keys to evade limits.
- **Checker rejects the image or compiler:** follow the official checker README. Do not submit unchecked source.

References checked 2026-10-24: [FNV instructions](https://fnvdecomp.com/agents.md), [Notion authorization](https://developers.notion.com/guides/get-started/authorization), [workspace block limits](https://developers.notion.com/reference/workspace-block-limits), [request limits](https://developers.notion.com/reference/request-limits).

## Next assignments and model attribution

An accepted submission can move the lane to its next eligible function without waiting for overnight verification. The local `MATCH` and fresh independent check remain required; project limits and account-health checks still apply. The starter asks the web agent to keep checking its outbox after each task. The relay cannot force ChatGPT to stay active indefinitely or restart a completed web turn: if the web conversation stops, continue it yourself. Fresh agent activity or an explicit resume is still required for new admissions.

FNV contribution credit stays with your account. New lanes default to the honest attribution `ChatGPT Web via Notion`, which describes the route without guessing a model. To record a model actually confirmed by the UI, pass `lane create --model-label 'YOUR CONFIRMED MODEL via Web'`. The setup wizard uses the route default. Subscription names or the agent's self-report do not establish model identity, and a web message cannot replace your configured attribution. Model attribution is optional project metadata; it does not change account ownership.
