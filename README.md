# fnv-relay

A local bridge between your own ChatGPT web conversation, a private Notion workspace, and [fnvdecomp](https://fnvdecomp.com/agents.md).

**0.1 preview:** initial target is Ubuntu/Linux, including Ubuntu on WSL2. Native Windows support has not been verified. Complete the setup and health checks before starting work; preview tests are not evidence of a successful live contribution.

The web agent reads a lane in Notion and writes C++ candidates there. Your local relay retrieves them, runs the official checker with your own game executable and compiler, and submits only independently checked `MATCH` results under your FNV account. The relay has no model API dependency and uses no third-party coordination server. You normally do not need to forward source files by hand.

You need Python 3.11.8+, Wine, a supported Linux sandbox environment, a legitimately purchased PC copy of Fallout: New Vegas, the official checker/compiler and shared files, an FNV account/key, and your own Notion workspace. Your ChatGPT account needs access to the Notion tools used in the web conversation. Account features and usage limits still apply; this does not promise unlimited agent use or require a separate Codex subscription or model API key.

## Start here

On Ubuntu 24.04 x86_64 (including WSL2), the [bootstrap script](docs/BOOTSTRAP.md) installs Wine, extracts Microsoft's original compiler, and sets up the relay. Supply your own game:

```sh
bash scripts/bootstrap-ubuntu.sh --game '/path/to/FalloutNV.exe'
```

You approve FNV's browser login and authorize Notion separately. For manual installation:

From an extracted release or local checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/fnv-relay --help
```

Follow [Setup](docs/SETUP.md) for browser approval with `auth fnv connect`, official downloads with `tools fetch`, the game/toolchain paths, Notion authentication, health checks, and your first lane. [Discord quick start](docs/DISCORD.md) is a short introduction you can share. [Validation and preview boundaries](docs/VALIDATION.md) distinguishes tested behavior from remaining limitations.

## Data and ownership

Claims and project credit belong to your FNV account. Credentials and local work stay in your local state directory; lane context is written to the Notion pages you authorize. Keep that parent page private in your own workspace. Notion is an external service, so content written there is subject to its policies.

Do not publish state directories, private page links, tokens, context packs, candidate source, game files, compiler binaries, or debug traces as part of this package. Follow the [FNV contributor terms](https://fnvdecomp.com/legal.html) and official agent rules. This package's MIT license covers its own code only; it does not license the game, Microsoft compiler, checker, or project context.

Official setup references were checked on 2026-10-24. Follow current upstream instructions when they change.
