# Discord quick start

Welcome samurai o7 — this is **fnv-relay 0.1 preview**, a local bridge for working on FNV decompilation from your own ChatGPT web account through a private Notion lane.

Your web agent writes candidates in Notion. The relay on your machine checks them with the official compiler and your own game executable, then submits only independently verified local `MATCH` results. Claims and credit stay with your FNV account. No model API key or separate Codex subscription is required by the relay; your ChatGPT plan, tool access, and usage limits still apply.

You need Ubuntu/Linux (Ubuntu WSL2 is the initial target), Python 3.11.8+, Wine and working Linux isolation, your own legitimate PC game copy, the official checker/compiler files, an FNV key, and a Notion workspace. Native Windows support has not been verified. The package does not include game files, compiler binaries, or private project context.

1. Extract the package or open your checkout, then install:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   . .venv/bin/activate
   fnv-relay init
   ```

2. Get your own FNV key through browser approval and download the official checker/shared files:

   ```sh
   fnv-relay auth fnv connect --label my-relay
   fnv-relay tools fetch --directory "$HOME/fnv-relay-tools"
   ```

   Open the displayed FNV link and approve with GitHub while the terminal waits. Your key is saved privately, never displayed. The download destination must be new or empty. Follow [Setup](SETUP.md) and the downloaded checker README for your own game/compiler setup, local paths, and a dedicated Wine prefix. Create a Notion internal connection, enter its token at the hidden `fnv-relay auth notion login` prompt, and share a private parent page with it. Separately connect Notion in ChatGPT. Run `fnv-relay doctor --prepare-wine`, then resolve health-check failures.
3. Create a lane and get its starter prompt:

   ```sh
   fnv-relay lane create --parent-page 'YOUR_PRIVATE_PARENT_URL_OR_UUID' --label first-lane
   fnv-relay lane list
   fnv-relay lane prompt ROUTE
   fnv-relay serve
   ```

   Replace `ROUTE` with your lane's route. Paste the generated prompt into ChatGPT with Notion access. Starting the relay authorizes live FNV claims and submissions under your account.
4. Check `fnv-relay status`; use `fnv-relay stop` to request a drain when finished.

Keep tokens, private page links, game bytes, project context, candidate source, and traces out of Discord and public releases. Follow the [official FNV instructions](https://fnvdecomp.com/agents.md) and [contributor terms](https://fnvdecomp.com/legal.html).

Notion Free is not unlimited for every workspace: internal connections in Free workspaces with multiple members are subject to a 1,000 lifetime block limit. Single-member Free workspaces have unlimited blocks under that policy; API/MCP rate limits still apply. Check [Notion's workspace limits](https://developers.notion.com/reference/workspace-block-limits) before choosing a workspace or paying for a plan. Sources checked 2026-10-24.
