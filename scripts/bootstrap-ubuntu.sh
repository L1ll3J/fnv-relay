#!/usr/bin/env bash
# Owner-invoked Ubuntu/WSL2 setup; never run this script with sudo.
set -euo pipefail
umask 077
ISO_URL='https://download.microsoft.com/download/E/8/E/E8EEB394-7F42-4963-A2D8-29559B738298/VS2008ExpressWithSP1ENUX1504728.iso'
ISO_NAME='VS2008ExpressWithSP1ENUX1504728.iso'
TOOLS_DIR="${HOME}/.local/share/fnv-relay-tools"
STATE_DIR="${HOME}/.local/share/fnv-relay"
GAME=''; ISO=''; DRY_RUN=0; ASSUME_YES=0; SKIP_APT=0; EXTRACT_ONLY=0; NO_CONNECT=0
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
usage() {
    cat <<'EOF'
Usage: bash scripts/bootstrap-ubuntu.sh --game /path/to/FalloutNV.exe [options]
  --dry-run            Describe actions; no writes, downloads, sudo or auth
  --yes                Skip the install confirmation (sudo may ask a password)
  --tools-dir PATH     NEW tools directory (default ~/.local/share/fnv-relay-tools)
  --state-dir PATH     CLI state directory (default ~/.local/share/fnv-relay)
  --iso PATH           Use a retained Microsoft ISO instead of downloading 749 MiB
  --skip-apt           Require existing system prerequisites; do not run sudo/apt
  --no-connect         Install local tools; print account setup commands for later
  --extract-only       Unpack compiler only; requires --iso, no apt/auth/Wine/pip
  --help               Show this help
Supported: x86_64 Ubuntu 24.04, native or WSL2. Requires Landlock ABI3+
and user/network/PID namespaces for actual compilation. Never run as root.
EOF
}
fail() { printf '%s\n' "bootstrap: $*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --game|--tools-dir|--state-dir|--iso)
            (($# >= 2)) || fail "Missing value for $1"
            case "$1" in --game) GAME="$2";; --tools-dir) TOOLS_DIR="$2";; --state-dir) STATE_DIR="$2";; --iso) ISO="$2";; esac
            shift 2;;
        --dry-run) DRY_RUN=1; shift;; --yes) ASSUME_YES=1; shift;;
        --skip-apt) SKIP_APT=1; shift;; --extract-only) EXTRACT_ONLY=1; SKIP_APT=1; shift;;
        --no-connect) NO_CONNECT=1; shift;; --help|-h) usage; exit 0;;
        *) fail "Unknown option: $1";;
    esac
done
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail 'Only x86_64 Linux Ubuntu is supported'
# /etc/os-release is supplied by Ubuntu, never by the downloaded archive.
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || fail 'Ubuntu 24.04 is required'
(( EUID != 0 )) || fail 'Run as your normal user; the script invokes sudo only for apt'
if ((EXTRACT_ONLY)); then
    [[ -n "$ISO" && -f "$ISO" ]] || fail '--extract-only needs --iso pointing to a retained Microsoft ISO'
else
    [[ -n "$GAME" && -f "$GAME" ]] || fail '--game must point to your own FalloutNV.exe'
fi
[[ -z "$ISO" || -f "$ISO" ]] || fail '--iso file does not exist'
# Canonical paths keep all cleanup and publication inside our own directory.
TOOLS_DIR="$(realpath -m -- "$TOOLS_DIR")"
STATE_DIR="$(realpath -m -- "$STATE_DIR")"
[[ -z "$GAME" ]] || GAME="$(realpath -e -- "$GAME")"
[[ -z "$ISO" ]] || ISO="$(realpath -e -- "$ISO")"
[[ "$TOOLS_DIR" != / && "$TOOLS_DIR" != "$HOME" ]] || fail 'Unsafe tools directory'
[[ ! -e "$TOOLS_DIR" && ! -L "$TOOLS_DIR" ]] || fail 'Tools directory already exists; choose a NEW directory (nothing will be overwritten)'
if ((DRY_RUN)); then
    printf 'Platform: Ubuntu %s x86_64\nTools: %s\nState: %s\n' "$VERSION_ID" "$TOOLS_DIR" "$STATE_DIR"
    ((SKIP_APT)) || printf '%s\n' 'sudo dpkg --add-architecture i386' 'sudo apt-get update' 'sudo apt-get install python3 python3-venv python3-pip wine wine32:i386 p7zip-full cabextract msitools curl ca-certificates util-linux'
    printf 'ISO source: %s\n' "${ISO:-$ISO_URL}"
    printf '%s\n' 'Extract original VC2008 SP1 + SDK; record ISO SHA256 and compiler component hashes.'
    if ((!EXTRACT_ONLY)); then
        printf '%s\n' 'Create isolated Python venv; pip install this checkout and capstone.'
        printf 'Configure game %s, compiler, venv and dedicated Wine prefix.\n' "$GAME"
        ((NO_CONNECT)) || printf '%s\n' 'Interactive GitHub device approval; fetch official checker/shared files; doctor --prepare-wine; verify compiler version 15.00.30729.01.'
    fi
    exit 0
fi
if ((!ASSUME_YES)); then
    printf 'Install private FNV tools into %s? Downloads about 749 MiB without --iso. [y/N] ' "$TOOLS_DIR"
    read -r answer
    [[ "$answer" == y || "$answer" == Y ]] || fail 'Cancelled'
fi
if ((!SKIP_APT)); then
    command -v sudo >/dev/null || fail 'sudo is required; install prerequisites manually and use --skip-apt'
    sudo dpkg --add-architecture i386
    sudo apt-get update
    APT_FLAGS=(); ((ASSUME_YES)) && APT_FLAGS+=(-y)
    sudo apt-get install "${APT_FLAGS[@]}" python3 python3-venv python3-pip wine wine32:i386 p7zip-full cabextract msitools curl ca-certificates util-linux
fi
for tool in 7z cabextract msiextract python3; do command -v "$tool" >/dev/null || fail "Missing prerequisite: $tool"; done
if ((!EXTRACT_ONLY)); then
    for tool in wine wineboot wineserver unshare; do command -v "$tool" >/dev/null || fail "Missing prerequisite: $tool"; done
fi
mkdir -p -- "$(dirname -- "$TOOLS_DIR")"
mkdir -- "$TOOLS_DIR" # Atomic refusal if another installer acquired the path.
STAGING="$(mktemp -d -- "$TOOLS_DIR/.bootstrap.XXXXXXXX")"
cleanup() { [[ -z "${STAGING:-}" || ! -d "$STAGING" ]] || rm -rf -- "$STAGING"; }
trap cleanup EXIT
if [[ -z "$ISO" ]]; then
    command -v curl >/dev/null || fail 'curl is required to download the Microsoft ISO'
    ISO="$STAGING/$ISO_NAME"
    # Stay on HTTPS even across redirects; failure leaves no published compiler.
    curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 2 --output "$ISO" "$ISO_URL"
fi
ISO_SHA="$(sha256sum -- "$ISO")"; ISO_SHA="${ISO_SHA%% *}"
printf 'Microsoft ISO SHA256: %s\n' "$ISO_SHA"
TC="$STAGING/extract"
mkdir -- "$TC"
7z x -y "-o$TC" "$ISO" VCExpress >"$STAGING/7z.log"
[[ -f "$TC/VCExpress/Ixpvc.exe" && -f "$TC/VCExpress/WCU/WinSDK/WinSDK_Build.exe" ]] || fail 'Unexpected Microsoft ISO layout'
mkdir -- "$TC/ixp" "$TC/vcx" "$TC/sdkx" "$TC/sdko"
cabextract -q -d "$TC/ixp" "$TC/VCExpress/Ixpvc.exe"
(cd -- "$TC/ixp" && msiextract -C ../vcx vs_setup.msi >"$STAGING/vc-msi.log")
VC="$TC/vcx/Program Files/Microsoft Visual Studio 9.0/VC:Vc7"
[[ -d "$VC/include" && -d "$VC/lib" && -f "$VC/bin/cl.exe" ]] || fail 'Unexpected Visual C++ MSI layout'
COMPILER="$STAGING/msvc9"
mkdir -- "$COMPILER"
cp -a -- "$VC" "$COMPILER/VC"
for dll in mspdb80.dll msobj80.dll mspdbcore.dll mspdbsrv.exe; do
    mapfile -d '' matches < <(find "$TC/vcx/Program Files/Microsoft Visual Studio 9.0/Common7" -type f -iname "$dll" -print0)
    ((${#matches[@]} == 1)) || fail "Expected exactly one compiler dependency: $dll"
    cp -- "${matches[0]}" "$COMPILER/VC/bin/$dll"
done
cabextract -q -d "$TC/sdkx" "$TC/VCExpress/WCU/WinSDK/WinSDK_Build.exe"
(cd -- "$TC/sdkx" && msiextract -C ../sdko VistaClientHeadersLibs-x86.msi >"$STAGING/sdk-msi.log")
SDK="$TC/sdko/SOURCEDIR/Program Files/Microsoft SDKs:./Windows:./v6.0A:."
[[ -d "$SDK/Include" && -d "$SDK/Lib" ]] || fail 'Unexpected Windows SDK MSI layout'
mkdir -- "$COMPILER/SDK"
cp -a -- "$SDK/Include" "$COMPILER/SDK/Include"
cp -a -- "$SDK/Lib" "$COMPILER/SDK/Lib"
mv -- "$COMPILER" "$TOOLS_DIR/msvc9"
export FNV_BOOTSTRAP_DIR="$TOOLS_DIR" FNV_BOOTSTRAP_ISO_SHA="$ISO_SHA" FNV_BOOTSTRAP_ISO_URL="$ISO_URL"
python3 - <<'PY'
import hashlib,json,os,platform,subprocess
from pathlib import Path
root=Path(os.environ['FNV_BOOTSTRAP_DIR'])
files={str(p.relative_to(root/'msvc9')):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'msvc9').rglob('*')) if p.is_file()}
versions={}
for argv in (['7z'], ['cabextract','--version'], ['msiextract','--version'], ['python3','--version']):
 r=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=10)
 versions[argv[0]]=r.stdout.decode(errors='replace').strip().splitlines()[:3]
(root/'install-manifest.json').write_text(json.dumps({'iso_url':os.environ['FNV_BOOTSTRAP_ISO_URL'],'iso_sha256':os.environ['FNV_BOOTSTRAP_ISO_SHA'],'iso_hash_note':'Recorded observed SHA256; no publisher-provided checksum is asserted.','expected_compiler_version':'15.00.30729.01','compiler_version_verified':False,'platform':platform.platform(),'extractor_versions':versions,'compiler_files_sha256':files},indent=2)+'\n')
PY
if ((EXTRACT_ONLY)); then
    printf 'Compiler extraction complete: %s/msvc9\nManifest: %s/install-manifest.json\n' "$TOOLS_DIR" "$TOOLS_DIR"
    exit 0
fi
python3 -m venv "$TOOLS_DIR/venv"
PYTHON="$TOOLS_DIR/venv/bin/python"
"$PYTHON" -m pip install "$REPO_DIR" capstone
RELAY=("$TOOLS_DIR/venv/bin/fnv-relay" --state-dir "$STATE_DIR")
"${RELAY[@]}" init
"${RELAY[@]}" config --game-exe "$GAME" --compiler-dir "$TOOLS_DIR/msvc9" --python "$PYTHON" --wine-prefix "$TOOLS_DIR/wine-prefix"
if ((NO_CONNECT)); then
    printf 'Local installation complete. Continue with the account setup in docs/BOOTSTRAP.md.\nCLI: %s/venv/bin/fnv-relay\n' "$TOOLS_DIR"
    exit 0
fi
"${RELAY[@]}" auth fnv connect
"${RELAY[@]}" tools fetch --directory "$TOOLS_DIR/official"
"${RELAY[@]}" config --checker-dir "$TOOLS_DIR/official/fnvcheck" --function-index "$TOOLS_DIR/official/function_index.json" --symbols "$TOOLS_DIR/official/fnvdecomp_symbols.json" --compiler-tips "$TOOLS_DIR/official/compiler_tips.md"
# Missing Notion authorization is expected at installation time; host failures are not.
DOCTOR="$TOOLS_DIR/doctor.json"
"${RELAY[@]}" doctor --prepare-wine >"$DOCTOR" || true
"$PYTHON" - "$DOCTOR" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); checks=value['checks']
failed=[key for key,ok in checks.items() if key!='notion_auth' and not ok]
if failed: raise SystemExit('Host setup needs attention; failed doctor checks: '+', '.join(failed))
PY
"$PYTHON" - "$TOOLS_DIR" "$STATE_DIR" <<'PY'
import json,sys
from pathlib import Path
from fnv_relay.compiler import _env,_run
root,state=map(Path,sys.argv[1:]);cfg=json.loads((state/'config.json').read_text())['paths']
work=root/'wine-prefix/.relay-host'
code,out,err,timeout=_run(['/usr/bin/wine',str(root/'msvc9/VC/bin/cl.exe')],_env(cfg,work),30,work)
(root/'compiler-version.stdout.log').write_bytes(out);(root/'compiler-version.stderr.log').write_bytes(err)
if timeout or b'15.00.30729.01' not in out+err: raise SystemExit('Compiler version check failed; inspect compiler-version logs')
manifest=json.loads((root/'install-manifest.json').read_text());manifest['compiler_version_verified']=True
image_code,image_out,image_err,image_timeout=_run([cfg['python'],'-I','-B',str(Path(cfg['checker_dir'])/'fnvcheck.py'),'--check-image'],_env(cfg,work),60,work)
(root/'image-check.stdout.log').write_bytes(image_out);(root/'image-check.stderr.log').write_bytes(image_err)
if image_timeout or image_code or not image_out.startswith(b'IMAGE OK:'): raise SystemExit('Game image check failed; inspect image-check logs')
manifest['game_image_verified']=True
(root/'install-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
PY
printf '\nInstallation complete. CLI: %s/venv/bin/fnv-relay\n' "$TOOLS_DIR"
printf 'Next: authorize a Notion internal connection, share its parent page, then run:\n  %q --state-dir %q auth notion login\n  %q --state-dir %q doctor\n' "$TOOLS_DIR/venv/bin/fnv-relay" "$STATE_DIR" "$TOOLS_DIR/venv/bin/fnv-relay" "$STATE_DIR"
printf 'ChatGPT must separately connect to Notion and authorize those pages. See docs/BOOTSTRAP.md.\n'
