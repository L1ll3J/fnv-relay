# Discord quick start

Welcome courier o7 — this is **fnv-relay 0.2 preview**, a local bridge for working on FNV decompilation from your own ChatGPT web account through a private Notion lane.

Your web agent writes candidates in Notion. The relay on your machine checks them with the official compiler and your own game executable, then submits only independently verified local `MATCH` results. Claims and credit stay with your FNV account. No model API key or separate Codex subscription is required by the relay; your ChatGPT plan, tool access, and usage limits still apply.

You need Ubuntu/Linux (Ubuntu WSL2 is the initial target), Python 3.11.8+, Wine and working Linux isolation, your own legitimate PC game copy, the official checker/compiler files, an FNV key, and a Notion workspace. Native Windows support has not been verified. The package does not include game files, compiler binaries, or private project context.

First connect Notion manually in ChatGPT: **Settings → Integrations → Plugins → Browse directory → search Notion → sign in/sign up → connect**. No action in Notion is needed yet. Back in ChatGPT, open **Settings → Integrations → Plugins → Notion → Allow all tools**. Use the workspace you intend to use for the relay.

1. Extract the package or open your checkout, then install:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   . .venv/bin/activate
   fnv-relay init
   ```

2. After the [Ubuntu/WSL2 bootstrap](BOOTSTRAP.md), run `fnv-relay setup`. It connects FNV and Notion through browser approval, lets you choose your own Notion workspace, creates a private parent by default, and can create a first lane with a starter prompt. You do not need a developer integration or API token for the default Notion flow. The wizard acknowledges your manual ChatGPT connection before its separate CLI OAuth flow; that acknowledgement does not prove tool access. Verify a harmless read/write/readback before any claim. If you prefer manual setup, get your own FNV key and shared tools:

   ```sh
   fnv-relay auth fnv connect --label my-relay
   fnv-relay tools fetch --directory "$HOME/fnv-relay-tools"
   ```

   Open the displayed FNV link and approve with GitHub while the terminal waits. Your key is saved privately, never displayed. The download destination must be new or empty. Follow [Setup](SETUP.md) and the downloaded checker README for your own game/compiler setup, local paths, and a dedicated Wine prefix. Run `fnv-relay auth notion connect` for browser authorization, then `fnv-relay setup`. Use the ChatGPT connection completed first and verify actual page read/write access. Advanced internal-token login remains available in [Setup](SETUP.md). Run `fnv-relay doctor --prepare-wine`, then resolve health-check failures.
3. Create a lane and get its starter prompt:

   ```sh
   fnv-relay lane create --parent-page 'YOUR_PRIVATE_PARENT_URL_OR_UUID' --label first-lane
   fnv-relay lane list
   fnv-relay lane prompt ROUTE
   fnv-relay start
   ```

   Replace `ROUTE` with your lane's route. Paste the generated prompt into ChatGPT with Notion access. Starting the relay authorizes live FNV claims and submissions under your account.
4. Check `fnv-relay status`; use `fnv-relay stop` to release tracked claims and exit safely. Use `fnv-relay drain` to finish current work first. Ctrl+C also requests safe release. Pending/uncertain claims remain reserved for review, so read the shutdown summary. Restarting continues saved work on unpaused lanes; use `lane resume "My lane"` for a manually paused lane. Continuation stays on its unfinished function.

Keep tokens, private page links, game bytes, project context, candidate source, and traces out of Discord and public releases. Follow the [official FNV instructions](https://fnvdecomp.com/agents.md) and [contributor terms](https://fnvdecomp.com/legal.html).

Notion MCP and REST limits differ. The advanced internal-token fallback can hit a 1,000 lifetime block limit in a Free workspace with multiple members; single-member Free workspaces have unlimited blocks under that policy. Check the actual workspace rather than inferring browser-MCP limits from REST limits. Check [Notion's workspace limits](https://developers.notion.com/reference/workspace-block-limits) before choosing a workspace or paying for a plan. Sources checked 2026-10-24.
