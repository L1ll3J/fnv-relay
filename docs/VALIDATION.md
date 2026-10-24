# Validation and preview boundaries

The package is a standalone Python CLI. Its polling, claim guards, compilation, logging, and expiry guardians make no model API calls. The operator's ChatGPT web conversation supplies candidates through Notion; its subscription and tool limits remain applicable.

Validated on Ubuntu/Linux under WSL2 on 2026-10-24:

- Offline transport, storage, CLI, and controller tests, including a complete mocked registration → claim → candidate batch → independent plain MATCH → owner-account submission → next assignment loop.
- A real retained known-match source compiled with the original installed Microsoft compiler and official checker in a new dedicated Wine prefix: `IMAGE OK` for all 16 image probes, a standalone `MATCH`, and exit status 0. Original game, checker, compiler, pack, and source hashes remained unchanged.
- That real check used distinct user/network/PID namespaces and Landlock ABI 7. An isolation test also verifies an unrelated private file is denied.
- Bootstrap extraction from a retained Microsoft ISO: all 2,705 extracted files matched the working compiler tree. System apt installation and a completely fresh end-to-end bootstrap were not run.
- Original target bytes and checker-rendered disassembly exported successfully from the same retained pack.

The new standalone Notion internal-token client has mocked transport tests. It has not been tested through a live newcomer account from initial login to a real server-verified contribution. Existing live private Pro lanes use a separate transport; their operation does not establish that this new package's complete live loop has passed. Treat this as a preview and start with one small lane.

Current boundaries:

- Linux/WSL2 only; no native Windows support claim.
- Compiler checks run sequentially. Each claim has an independent ordinary-code expiry guardian.
- Selection orders available size bands; it does not have a global ranking of every open function. A size-filter miss releases the claim and pauses admissions for that unchanged lane policy rather than repeatedly claiming unsuitable functions.
- Official diagnostic output is preserved. This checker version can limit its instruction-diff window to 4 KiB even for larger functions; local MATCH validation still uses the official full comparison. This preview does not yet add the private relay's supplementary full-span diagnostic instrumentation. Keep that distinction in mind when judging large-function near misses.
- Defect proposals and findings are retained for review. They do not automatically file project flags. No claim is flagged merely because a web agent calls it defective.
- Ambiguous external writes and interrupted compiler reservations are retained and are not automatically repeated. `reconcile` lists pending operations; the preview deliberately provides no blind retry override. A setup interrupted after a successful claim can require manual recovery; its independent guardian remains responsible for a known lease deadline.
- Draining stops new assignments; it does not make unsolved claims disappear. Check official account status before shutting down the machine. An offline or powered-off machine cannot release claims.

No game/compiler binaries, original project sources, candidate examples, private state, credentials, or live Notion links are included in the release. Test sources and services are synthetic.

Run the offline checks with `python -m pip install '.[test]'` followed by `python -m pytest -q`. These tests make no model API requests.
