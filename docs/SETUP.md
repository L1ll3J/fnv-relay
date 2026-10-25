# Setup

This is a 0.2 preview for Ubuntu/Linux, including Ubuntu under WSL2. Python 3.11.8+ is required. Native Windows has not been verified. Run commands from your extracted package or checkout; no public hosting URL is assumed.

Use `./relay` for the numbered menu after bootstrap. Start keeps the menu available while a managed worker runs, so you can add lanes and show their starters in the same terminal. Choose **0 — Stop safely and exit** when finished. **8 — Show recent worker output** displays startup errors and recent results. Start also opens a live progress view with tables, checker diagnostics and notices when a proposal beats a comparable saved/current best. Assembly diffs are kept out of the terminal. Press Enter to return to the menu while work continues; choose **9 — Live updates** to reopen it. If lane creation was interrupted, choose **Add lane** to finish that same lane; recovery reads Notion before any retry, and asks before retrying an uncertain creation whose page cannot be found. Incomplete lanes cannot produce starters. Direct `fnv-relay start` remains available as a foreground command for scripts.

## First connect Notion inside ChatGPT

In ChatGPT, open **Settings → Integrations → Plugins → Browse directory**, search for **Notion**, sign in or sign up, and connect it. No action in Notion is needed yet. Then, in ChatGPT (not Notion), open **Settings → Integrations → Plugins → Notion → Allow all tools**. Use the Notion account/workspace you will use for the relay. These are manual ChatGPT steps; the CLI cannot perform or authorize them for you.

## Guided account setup after bootstrap

With the game/compiler/checker installed using [Ubuntu/WSL2 bootstrap](BOOTSTRAP.md), run the installed CLI from an interactive terminal:

```sh
fnv-relay setup
fnv-relay doctor
fnv-relay start
```

If the CLI is not on your PATH, use `~/.local/share/fnv-relay-tools/venv/bin/fnv-relay`. For a custom state directory, place `--state-dir /your/state` before each command.

`setup` reuses existing credentials. A fresh setup walks you through FNV's GitHub approval and Notion browser authorization, where you choose your own account and workspace. The CLI uses Notion's hosted MCP service; you do not need to create a developer integration for this default flow. With browser authorization and no saved parent, press Enter at the parent-page prompt to create a private **FNV Relay** page in the chosen workspace. You can instead supply an existing private page URL; a previously saved parent can be reused. The wizard verifies that parent and offers to create a first lane and print its ChatGPT starter prompt. The wizard asks you to acknowledge the manual ChatGPT connection before CLI Notion OAuth. That saved acknowledgement records your answer, not a successful capability test. No model API key is needed. Tool installation and the official image/compiler checks remain separate prerequisites.

An existing internal-token setup stays on that transport. Before any lanes have been created, `fnv-relay auth notion connect` can switch it to browser authorization. Once a state has lanes, the CLI refuses to reconnect credentials or silently rebind those lanes: keep their existing transport, or initialize a separate fresh state for the new account/transport. For a newcomer trial with a different Free ChatGPT/Notion account, use a fresh local state and choose that user's own Notion workspace; borrowing old private relay pages is unnecessary.

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

## 3. Connect Notion in your browser

The normal setup wizard handles this for you. To connect, or switch a token-based state that has no lanes yet:

```sh
fnv-relay auth notion connect
fnv-relay setup
```

Keep the terminal open while your browser opens Notion. Sign in to the intended account, select the intended workspace, review the requested access, and approve. The CLI receives the result through a temporary local loopback callback and stores its credentials in your private state. It connects directly to [Notion's hosted MCP service](https://developers.notion.com/guides/mcp/build-mcp-client), rather than requiring your own public OAuth application.

Notion MCP acts with the signed-in user’s Notion permissions; its authorization is broader than sharing a single page with an internal connection. Review the workspace and consent screen before approving. [Notion permissions guidance](https://www.notion.com/help/notion-mcp)

If the browser does not open automatically, copy the displayed authorization URL into your own browser. On WSL2, that browser must be able to reach the CLI's localhost callback. Leave the CLI running until it confirms completion. If authorization times out or is declined, run `auth notion connect` again. Do not paste credentials or callback URLs into chat, page content, or shell arguments.

The CLI's authorization and ChatGPT's Notion connection are **separate**. Complete the manual ChatGPT connection above first, then authorize the CLI against the same intended Notion workspace. Confirm that the selected conversation can read, write, and read back a harmless marker on the relay pages before claiming work; acknowledging the setup question does not prove this. See [connection checks](SUBSCRIPTIONS.md). Availability depends on the actual account and selected tools; a Free or Pro label alone does not prove access.

### Advanced fallback: internal connection token

Use this only if you prefer the REST/token transport or browser authorization is unavailable under your workspace policy. Configure it before creating lanes, or use a fresh state; existing lanes cannot be silently moved to another credential. This fallback requires an existing private parent page URL rather than automatic workspace-root creation:

1. As a workspace owner, create an internal connection in the [Notion developer portal](https://www.notion.so/profile/integrations). Enable read, insert, and update content.
2. Create a private parent page. In its `•••` menu, choose **Add connections** and select the connection.
3. Enter its token at the hidden local prompt, then complete setup with that parent URL:

```sh
fnv-relay auth notion login
fnv-relay setup
fnv-relay doctor
```

This token belongs to your own internal connection; ChatGPT/Codex OAuth credentials are not a substitute. See [Notion authorization](https://developers.notion.com/guides/get-started/authorization).

**Workspace limits still apply.** Notion's hosted MCP and REST API have separate enforcement policies. In the internal-token fallback, Free workspaces with more than one member can hit the 1,000 lifetime block limit; deleting blocks does not recover that capacity. Single-member Free workspaces have unlimited blocks under that policy. Do not infer MCP limits from REST limits or assume a paid plan is required: check the actual workspace and returned error. See [workspace block limits](https://developers.notion.com/reference/workspace-block-limits) and [request limits](https://developers.notion.com/reference/request-limits).

## 4. Start one lane

Creating a lane writes to the Notion parent you supply, or the parent previously verified by `setup` when `--parent-page` is omitted. Starting the relay enables live project activity under your FNV credentials, including claims and submissions. Start with one lane so you can inspect the workflow.

```sh
fnv-relay lane create --parent-page 'YOUR_PRIVATE_PARENT_PAGE_URL_OR_UUID' --label first-lane
fnv-relay lane list
fnv-relay lane prompt ROUTE
fnv-relay doctor
fnv-relay start
```

Replace `ROUTE` with the route reported for your lane. Run `doctor` first. After it passes, run `start` and wait for **Relay running**. Leave that terminal open, then open a **new ChatGPT chat**, created after configuring the Notion connection and permissions, and paste the generated starter into it. In our user-reported trial, reusing a chat from before Notion was configured caused inconsistent tool access and early stopping. The agent works through that private lane while the local relay handles the official checks. Leave the relay running in its terminal. `serve` checks local toolchain/isolation readiness and validates both service credentials before polling. You can add another lane while it runs. A private lock under `~/.local/share/fnv-relay-locks` prevents simultaneous workers using the same FNV key across state directories; it does not coordinate different keys for one account or other applications. Use one relay state per owner key and follow project rules for other agents. A local `MATCH` is a prerequisite for submission, not a guarantee of the project's later verification verdict.

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
- **Notion page unavailable:** confirm the CLI authorized the intended account/workspace and the ChatGPT conversation has its own Notion access. For the advanced token fallback, also verify parent-page sharing with the internal connection.
- **Notion 403:** inspect whether it is a permission failure or workspace block limit. Repeated retries cannot repair either configuration.
- **Notion 429 / overload:** honor the service's retry delay. API and MCP limits can differ.
- **No claim available:** account eligibility, other open claims, project capacity, and selector constraints can all prevent allocation. Do not create extra accounts or keys to evade limits.
- **Checker rejects the image or compiler:** follow the official checker README. Do not submit unchecked source.

References checked 2026-10-24: [FNV instructions](https://fnvdecomp.com/agents.md), [Notion hosted MCP authorization](https://developers.notion.com/guides/mcp/build-mcp-client), [Notion authorization](https://developers.notion.com/guides/get-started/authorization), [workspace block limits](https://developers.notion.com/reference/workspace-block-limits), [request limits](https://developers.notion.com/reference/request-limits).

## Next assignments and model attribution

Free or lighter models may end their turn before finishing a task. To continue, send **“Persistence, courier. o7”** in the same conversation. This worked in our Free-account trial; it is a suggestion, not a guarantee that the model will remain active.

An accepted submission can move the lane to its next eligible function without waiting for overnight verification. The local `MATCH` and fresh independent check remain required; project limits and account-health checks still apply. The starter asks the web agent to keep checking its outbox after each task. The relay cannot force ChatGPT to stay active indefinitely or restart a completed web turn: if the web conversation stops, continue it yourself. Fresh agent activity or an explicit resume is still required for new admissions.

FNV contribution credit stays with your account. New lanes default to the honest attribution `ChatGPT Web via Notion`, which describes the route without guessing a model. To record a model actually confirmed by the UI, pass `lane create --model-label 'YOUR CONFIRMED MODEL via Web'`. The setup wizard uses the route default. Subscription names or the agent's self-report do not establish model identity, and a web message cannot replace your configured attribution. Model attribution is optional project metadata; it does not change account ownership.

## Restarting unfinished work

`start` prints the starter again and shows each lane's current state. A running relay does not imply an active claim: the lane may be waiting for registration, reconnecting, preparing an assignment, or paused. You can retrieve the starter at any time with `fnv-relay lane prompt ROUTE`.

When a lease is released, automatic continuation preserves the unfinished function and checked attempts. Restart with the same state directory. Use the newest assignment in the lane outbox; old claim identifiers expire.

A definite FNV authentication/access refusal pauses the lane with its continuation intact. Check account access, then run `fnv-relay lane resume ROUTE`. An uncertain claim request is different: stop the relay, run `fnv-relay reconcile --claim ROUTE`, then `start`. Reconciliation verifies that no active claims belong to this key, keeps the original operation journal, and rearms the saved task locally. It does not send a claim request or retry a write. If any claim still belongs to the key, reconciliation refuses to proceed. Other uncertain operations require separate review.

For Notion reconnection, stop the relay and run `fnv-relay auth notion connect`. Select the existing workspace. New credentials replace the old ones only after workspace identity and access to every existing lane page are verified. Keep the same state directory.

Before reclaiming an unfinished function, the relay checks its eligibility with `function_info`. If FNV has paused it for problem-report review, the relay parks its continuation, publishes `CONTINUATION_PARKED` in the outbox, and selects another eligible function using the lane’s configured order. Existing attempts remain on disk, and status shows how many unfinished functions are parked. An older relay state paused for this reason recovers automatically on the next `start`; an owner-requested pause remains paused. Reconnecting cannot remove a project restriction.

List preserved tasks with `fnv-relay lane parked ROUTE`. Once the project reopens one, use `fnv-relay lane resume ROUTE --address ADDRESS`. Finish or release any current claim first; the relay refuses to replace an active claim. Selecting another saved task also preserves the current continuation. It will check eligibility again before claiming, and park the task again if it remains unavailable.

## Interactive menu

After bootstrap, run `./relay` from the checkout. It remembers that checkout’s installation and private state directory. A regular installation also opens the menu when you run `fnv-relay` with no command.

Choose **Add lane**, select the task order by number, and copy the printed starter into a new ChatGPT chat with Notion connected. No route UUIDs or command flags need to be entered. **Show a lane’s ChatGPT starter**, **Show status**, and **Resume saved work** also use numbered choices.

**Start relay** runs in the current terminal. Leave it open; Ctrl+C releases tracked claims safely and returns to the menu. To add lanes while it runs, open another terminal, run `./relay` there, and choose **Add lane**. Both menus use the same saved installation. Exiting the second menu leaves the running relay alone.

## Stopping and lease status

The first Ctrl+C prints a stopping acknowledgement and cancels FNV read-retry backoff. An in-flight network request or compiler invocation may still need to finish before cleanup. The controller checks for shutdown again before starting another compiler check. Long service retry delays are deferred without holding the lane’s locks; their deadlines are retained rather than retried early.

A confirmed release or observed expiry saves a `LEASE_CLOSED` notice locally. The controller publishes it to the private outbox with readback, so the web conversation can distinguish an old assignment from current lease state. Cleanup does not depend on Notion being reachable: an undelivered notice is retained for the next controller run. Observed expiry is reported as expiry, never as a successful release.


New diagnostic checks retain the selected compiled object locally and attach a compact, source-bound evidence summary to each result. `candidate_artifacts` requests include full relocation-normalized target/candidate disassembly, aligned differences, region ranges and relocation bindings. Reports use the full candidate extent (up to 256 KiB), separately labeled from the official checker's limited-window metric, and add no compiler invocations. Derived relocation bindings are checker assumptions, not independent semantic proof. Binary objects remain local. Older checks without captured objects return an explicit unavailable reason; requesting evidence never recompiles. Independent plain MATCH validation remains unchanged.
