# Ubuntu / WSL2 setup

The bootstrap supports **Ubuntu 24.04 on x86_64**, either native Linux or WSL2. It installs Wine, the exact Microsoft compiler required by fnvcheck, a private Python environment, and the official checker/shared files. Bring your own legitimate `FalloutNV.exe`; the script never downloads or distributes the game.

From an unpacked checkout:

```bash
bash scripts/bootstrap-ubuntu.sh --dry-run --game '/path/to/FalloutNV.exe'
bash scripts/bootstrap-ubuntu.sh --game '/path/to/FalloutNV.exe'
```

Run as your normal user. The script asks before installing; `sudo` requests your password for Ubuntu packages. `--yes` skips the script and apt confirmations, while GitHub device approval and any sudo password remain interactive. The Microsoft ISO is about 749 MiB; allow several GiB of temporary disk space and a working internet connection.

The script:

1. Enables Ubuntu's i386 packages and installs Wine/wine32, Python/venv, 7-Zip, cabextract, msitools, curl and util-linux.
2. Downloads [Microsoft Visual C++ 2008 Express SP1](https://download.microsoft.com/download/E/8/E/E8EEB394-7F42-4963-A2D8-29559B738298/VS2008ExpressWithSP1ENUX1504728.iso) directly over HTTPS. It extracts the original `VC` and `SDK` trees using the [official project's](https://fnvdecomp.com/agents.md) fnvcheck README recipe.
3. Records the ISO URL, observed SHA256, extractor versions, and every installed compiler file's SHA256 in `install-manifest.json`. This is a provenance record, not a claim of a Microsoft-published checksum.
4. Installs this checkout and Capstone into its own venv. It initializes local relay state and configures your game, compiler, Python and dedicated Wine prefix.
5. Starts GitHub device approval for the FNV account, fetches the official checker/shared files, and configures their paths. You approve the device flow yourself; no pre-existing FNV API key is required.
6. Prepares the dedicated Wine prefix, runs doctor, verifies compiler version **15.00.30729.01**, and validates the game with the official `--check-image` command. Missing Notion authorization is expected here; other failed doctor checks stop setup.

Defaults are `~/.local/share/fnv-relay-tools` for installed tools and `~/.local/share/fnv-relay` for private CLI state. The tools directory must be new, including after a partial failure. Nothing existing is overwritten or removed. Change destinations with `--tools-dir` and `--state-dir`; temporary extraction files are cleaned only inside the directory created by this invocation. Your retained ISO and game stay untouched.

This repository ships code only. The Microsoft compiler/ISO, checker downloads and game remain outside the repository and must not be bundled into release archives.

## Authorize Notion

Create a Notion internal connection with permission to read, insert and update content, then share your chosen parent page with that connection. Store its token through the CLI's hidden terminal prompt:

```bash
CLI="$HOME/.local/share/fnv-relay-tools/venv/bin/fnv-relay"
"$CLI" auth notion login
"$CLI" doctor
```

If you selected a custom state directory, add `--state-dir '/your/state'` before the subcommand on every CLI command. Keep tokens out of shell arguments, page content and chat messages.

**ChatGPT needs its own Notion connection and access to the relay pages.** The CLI's internal connection does not authorize ChatGPT automatically. Connect Notion in ChatGPT and approve the pages separately before giving a worker its lane prompt. See the main README for lane creation and serving.

## Existing prerequisites or retained ISO

`--skip-apt` avoids all sudo/apt calls when the required Ubuntu packages already exist. `--iso '/path/to/VS2008ExpressWithSP1ENUX1504728.iso'` uses a retained ISO and still records its SHA256. `--extract-only --iso ... --tools-dir /new/path` performs only the original compiler extraction and provenance recording: no Wine, pip, account access or relay configuration.

The extraction recipe was verified against a retained ISO with SHA256 `6d3b5eedfa461969c5d645871b53002d162a2e238486900d2fdf71774d53fb91`; all 2,705 extracted files matched the working compiler tree. This is an observed checksum, not a publisher-authenticated checksum. Full system package installation was not run during that verification.

`--no-connect` installs the compiler and Python environment and configures those local paths, then stops before account access. Continue manually:

```bash
TOOLS="$HOME/.local/share/fnv-relay-tools"
CLI="$TOOLS/venv/bin/fnv-relay"
"$CLI" auth fnv connect
"$CLI" tools fetch --directory "$TOOLS/official"
"$CLI" config --checker-dir "$TOOLS/official/fnvcheck" \
  --function-index "$TOOLS/official/function_index.json" \
  --symbols "$TOOLS/official/fnvdecomp_symbols.json" \
  --compiler-tips "$TOOLS/official/compiler_tips.md"
"$CLI" doctor --prepare-wine
```

The last command reports missing Notion auth until you complete the preceding Notion steps. Compile checks require Landlock ABI 3+ and working user/network/PID namespaces. The bootstrap does not weaken kernel policies or install an unsandboxed fallback. On WSL2, use a current WSL kernel; unsupported host capabilities require host configuration before serving.

macOS, Windows outside WSL2, ARM, and other Ubuntu releases are not supported by this bootstrap.
