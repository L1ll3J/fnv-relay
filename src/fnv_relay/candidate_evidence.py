"""Read-only observation of official checks; never compiles or changes a verdict."""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import runpy
import sys


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


class Observer:
    """Bind the returned result to its own COFF, including official repair retries."""

    def __init__(self, checker, work):
        self.checker = checker
        self.work = Path(work)
        self.output = self.work / 'cache' / 'candidate-evidence'
        self.output.mkdir(mode=0o700)
        self.frames = []
        self.results = {}

    def install(self):
        k = self.checker
        compile_original = k.msvc_compile
        compare_original = k.compare
        derive_original = k.derive_bindings
        once_original = k._check_once
        candidate_original = k.check_candidate

        def compile_observed(*args, **kwargs):
            result = compile_original(*args, **kwargs)
            if self.frames:
                self.frames[-1]['coff'] = result[0]
            return result

        def compare_observed(*args, **kwargs):
            result = compare_original(*args, **kwargs)
            if self.frames:
                self.frames[-1]['comparison'] = result
            return result

        def derive_observed(*args, **kwargs):
            result = derive_original(*args, **kwargs)
            if self.frames:
                self.frames[-1].update(sym=args[1], bindings=result)
            return result

        def once_observed(entry, *args, **kwargs):
            frame = {'entry': entry}
            self.frames.append(frame)
            try:
                result = once_original(entry, *args, **kwargs)
                # Retain the result itself: id reuse cannot select another attempt.
                self.results[id(result)] = (result, frame)
                return result
            finally:
                self.frames.pop()

        def candidate_observed(*args, **kwargs):
            result = candidate_original(*args, **kwargs)
            try:
                selected = self.results.get(id(result))
                if selected is None or selected[0] is not result:
                    self.unavailable('returned_result_not_observed')
                else:
                    self.capture(result, selected[1])
            except Exception as error:
                # Diagnostic failure must leave the official result and stdout intact.
                try:
                    self.unavailable('diagnostic_generation_failed', type(error).__name__)
                except OSError:
                    pass  # Parent reports missing evidence; official check still completes.
            return result

        k.msvc_compile = compile_observed
        k.compare = compare_observed
        k.derive_bindings = derive_observed
        k._check_once = once_observed
        k.check_candidate = candidate_observed

    def identity(self):
        return {'source_sha256': digest(self.work / 'candidate.cpp'),
                'pack_manifest_sha256': digest(self.work / 'pack' / 'pack.json')}

    def unavailable(self, reason, error_type=None):
        value = {'schema': 'fnv-candidate-evidence/1', 'status': 'unavailable',
                 'reason_code': reason, **self.identity(), 'additional_compiler_invocations': 0}
        if error_type:
            value['error_type'] = error_type
        write_json(self.output / 'diagnostics.json', value)

    def capture(self, result, frame):
        coff = frame.get('coff')
        if coff is None:
            self.unavailable('no_compiled_object')
            return
        (self.output / 'candidate.obj').write_bytes(coff.data)
        if 'sym' not in frame or 'bindings' not in frame or not frame.get('comparison'):
            self.unavailable('no_comparable_target_symbol')
            return
        k = self.checker
        entry, sym = frame['entry'], frame['sym']
        comparisons = frame['comparison']
        if len(comparisons) != 1:
            self.unavailable('ambiguous_comparison')
            return
        extent = comparisons[0].get('compiled_extent')
        if type(extent) is not int or extent <= 0 or max(extent, entry['size']) > 262144:
            self.unavailable('diagnostic_extent_limit')
            return
        canonical, derived, _ = frame['bindings']
        bindings = {key: value for key, value in list(canonical.items()) + list(derived.items())
                    if not key.startswith('!')}
        text, differences, original, candidate = k.aligned_diff(
            entry, coff, sym, bindings, extent, k.Namer(), max_lines=1000000)
        if 'diff truncated' in text:
            self.unavailable('diagnostic_output_limit')
            return
        (self.output / 'target-disassembly.txt').write_text('\n'.join(original) + '\n')
        (self.output / 'candidate-disassembly.txt').write_text('\n'.join(candidate) + '\n')
        (self.output / 'aligned-diff.txt').write_text(text + '\n')
        section = coff.sections[sym['section']]
        relocations = []
        for offset, symbol_index, kind in section['relocs']:
            relative = offset - sym['value']
            if not 0 <= relative < extent:
                continue
            relocation_symbol = coff.symbols[symbol_index]
            name = relocation_symbol['name']
            local = name not in bindings and relocation_symbol.get('section') == sym['section']
            resolved = bindings.get(name)
            if local:
                resolved = entry['addr'] + relocation_symbol['value'] - sym['value']
            relocations.append({'offset': relative, 'coff_type': kind, 'symbol': name,
                                'binding_origin': 'canonical' if name in canonical else 'derived' if name in bindings else 'same_section' if local else 'unresolved',
                                'resolved_address': resolved,
                                'normalization_applied': kind in (6, 11, 20) and relative <= extent - 4,
                                'raw_addend_hex': section['data'][offset:offset+4].hex()})
        write_json(self.output / 'relocations.json', {'relocations': relocations,
                   'canonical_bindings': canonical, 'derived_bindings': derived,
                   'interpretation': 'Official checker bindings; derived bindings depend on its positional assumptions and are not independent semantic proof.'})
        opcodes = difflib.SequenceMatcher(None, original, candidate, autojunk=False).get_opcodes()
        regions = [{'kind': kind, 'target_instruction_range': [i1, i2],
                    'candidate_instruction_range': [j1, j2]}
                   for kind, i1, i2, j1, j2 in opcodes]
        write_json(self.output / 'regions.json', {'index_basis': 'zero-based half-open instruction ranges', 'regions': regions})
        compiled_source = result.get('candidate')
        if isinstance(compiled_source, str):
            (self.output / 'checked-candidate.cpp').write_text(compiled_source, errors='surrogateescape')
        checker_file = getattr(k, '__file__', None)
        components = {p.name: digest(p) for p in Path(checker_file).parent.glob('*.py')} if checker_file else {}
        summary = {'schema': 'fnv-candidate-evidence/1', 'status': 'available', **self.identity(),
                   'object_sha256': digest(self.output / 'candidate.obj'),
                   'checker_component_sha256': components,
                   'compiler_flags': json.loads((self.work / 'pack' / 'pack.json').read_text()).get('flags'),
                   'target_address': entry.get('addr'),
                   'target_symbol': sym['name'], 'target_bytes': entry['size'], 'candidate_bytes': extent,
                   'full_extent_ndiff_insn': differences,
                   'official_ndiff_insn': result.get('ndiff_insn'),
                   'first_divergence': next((r for r in regions if r['kind'] != 'equal'), None),
                   'matching_regions': sum(r['kind'] == 'equal' for r in regions),
                   'different_regions': sum(r['kind'] != 'equal' for r in regions),
                   'unresolved_relocations': sum(r['binding_origin'] == 'unresolved' for r in relocations),
                   'additional_compiler_invocations': 0,
                   'normalization': 'Official linked_bytes/aligned_diff, same selected object and relocation bindings. Full indexed target and full candidate extent; diagnostic only, not a MATCH verdict.',
                   'compiled_source_sha256': digest(self.output / 'checked-candidate.cpp') if isinstance(compiled_source, str) else None,
                   'artifacts': {p.name: digest(p) for p in sorted(self.output.iterdir()) if p.is_file()}}
        write_json(self.output / 'diagnostics.json', summary)


def run(checker_dir, work, argv):
    """Run the unmodified official CLI in-process, preserving its exit/stdout."""
    sys.path.insert(0, str(checker_dir))
    observer = Observer(None, work)
    try:
        import fc_check
        observer.checker = fc_check
        observer.install()
    except (ImportError, AttributeError):
        # Unsupported observation API leaves the official CLI in charge.
        observer.unavailable('observer_api_unavailable')
    sys.argv = argv
    runpy.run_path(str(Path(checker_dir) / 'fnvcheck.py'), run_name='__main__')


def collect(work, source_sha256):
    """Validate observer output before exposing artifact paths to the controller."""
    work = Path(work).resolve()
    directory = work / 'cache' / 'candidate-evidence'
    manifest = directory / 'diagnostics.json'
    if not manifest.is_file():
        return {'status': 'unavailable', 'reason_code': 'not_captured', 'additional_compiler_invocations': 0}, []
    try:
        row = json.loads(manifest.read_text())
        if row['source_sha256'] != source_sha256 or row['pack_manifest_sha256'] != digest(work / 'pack' / 'pack.json'):
            raise ValueError('Evidence input binding differs')
        allowed = {'candidate.obj', 'target-disassembly.txt', 'candidate-disassembly.txt', 'aligned-diff.txt',
                   'relocations.json', 'regions.json', 'checked-candidate.cpp'}
        artifacts = row.get('artifacts', {})
        if not isinstance(artifacts, dict) or set(artifacts) - allowed:
            raise ValueError('Unknown diagnostic artifact')
        files = []
        for name, expected in artifacts.items():
            path = directory / name
            if path.is_symlink() or digest(path) != expected:
                raise ValueError('Diagnostic artifact binding differs')
            if path.suffix in ('.txt', '.json'):
                files.append({'path': str(path), 'sha256': expected})
        if row.get('status') == 'available' and row['object_sha256'] != artifacts['candidate.obj']:
            raise ValueError('Object binding differs')
        files.append({'path': str(manifest), 'sha256': digest(manifest)})
        return {key: value for key, value in row.items() if key != 'artifacts'}, files
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'unavailable', 'reason_code': 'invalid_saved_evidence', 'additional_compiler_invocations': 0}, []
