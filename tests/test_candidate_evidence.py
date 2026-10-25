import json
from pathlib import Path
from types import SimpleNamespace

from fnv_relay.candidate_evidence import Observer, collect, digest


def setup_work(tmp_path):
    (tmp_path/'cache').mkdir()
    (tmp_path/'pack').mkdir()
    (tmp_path/'pack/pack.json').write_text('{"address":"0x401000"}')
    (tmp_path/'candidate.cpp').write_text('int f(){return 1;}')
    return tmp_path


def fake_checker(repair=False, fail_report=False):
    k=SimpleNamespace()
    k.calls=0
    k.objects=[]
    k.msvc_compile=lambda *a: (k.objects[-1], '', 0)
    k.compare=lambda *a: [{'compiled_extent':5001}]
    k.derive_bindings=lambda *a: ({'helper':4198400},{'derived':4202496},[1,5,9])
    k.Namer=lambda:None
    def aligned(entry,coff,sym,bindings,extent,namer,max_lines):
        assert extent==5001 and max_lines>70
        if fail_report:raise ValueError('fixture')
        return 'ORIG ret\nYOURS nop\n',1,['push ebp','ret'],['push ebp','nop']
    k.aligned_diff=aligned
    def once(entry,source):
        k.calls+=1
        coff=SimpleNamespace(data=('coff'+source).encode(),sections={1:{'data':b'\0'*20,'relocs':[(1,1,20),(5,2,6),(9,3,20)]}},symbols={1:{'name':'helper'},2:{'name':'derived'},3:{'name':'unknown'}})
        k.objects.append(coff)
        obj,_,_=k.msvc_compile('tu')
        sym={'name':'f','section':1,'value':0}
        k.derive_bindings(obj,sym,entry,b'')
        k.compare(obj,None,{})
        return {'status':'wip','candidate':source,'ndiff_insn':8}
    k._check_once=once
    def candidate(entry,source):
        first=k._check_once(entry,source)
        if repair:
            k._check_once(entry,source+' retry')
            return first  # Original returned, despite a later successful compilation.
        return first
    k.check_candidate=candidate
    return k


def test_capture_full_reports_with_no_recompile(tmp_path):
    work=setup_work(tmp_path);k=fake_checker();o=Observer(k,work);o.install()
    result=k.check_candidate({'size':6000},'source')
    assert result=={'status':'wip','candidate':'source','ndiff_insn':8}
    assert k.calls==1
    summary,files=collect(work,digest(work/'candidate.cpp'))
    assert summary['status']=='available' and summary['candidate_bytes']==5001
    assert summary['full_extent_ndiff_insn']==1 and summary['official_ndiff_insn']==8
    assert summary['additional_compiler_invocations']==0
    assert summary['unresolved_relocations']==1
    assert summary['first_divergence']['kind']=='replace'
    assert {Path(f['path']).name for f in files}=={'diagnostics.json','aligned-diff.txt','target-disassembly.txt','candidate-disassembly.txt','regions.json','relocations.json'}
    assert not any(Path(f['path']).suffix in ('.obj','.cpp') for f in files)


def test_retry_capture_belongs_to_returned_result(tmp_path):
    work=setup_work(tmp_path);k=fake_checker(repair=True);o=Observer(k,work);o.install()
    result=k.check_candidate({'size':6000},'source')
    assert k.calls==2 and result['candidate']=='source'
    assert (o.output/'candidate.obj').read_bytes()==b'coffsource'
    assert (o.output/'checked-candidate.cpp').read_text()=='source'


def test_diagnostic_failure_preserves_official_result(tmp_path):
    work=setup_work(tmp_path);k=fake_checker(fail_report=True);o=Observer(k,work);o.install()
    result=k.check_candidate({'size':6000},'source')
    assert result['status']=='wip' and k.calls==1
    summary,_=collect(work,digest(work/'candidate.cpp'))
    assert summary['reason_code']=='diagnostic_generation_failed'


def test_compile_failure_explicit_unavailable(tmp_path):
    work=setup_work(tmp_path);k=fake_checker();o=Observer(k,work)
    o.capture({'status':'compile_error'},{'coff':None})
    summary,_=collect(work,digest(work/'candidate.cpp'))
    assert summary['reason_code']=='no_compiled_object'


def test_historical_result_does_not_recompile(tmp_path):
    work=setup_work(tmp_path)
    summary,files=collect(work,digest(work/'candidate.cpp'))
    assert summary['reason_code']=='not_captured' and files==[]


def test_modified_object_or_source_rejects_evidence(tmp_path):
    work=setup_work(tmp_path);k=fake_checker();o=Observer(k,work);o.install();k.check_candidate({'size':6000},'source')
    assert collect(work,'0'*64)[0]['reason_code']=='invalid_saved_evidence'
    (o.output/'candidate.obj').write_bytes(b'tampered')
    summary,files=collect(work,digest(work/'candidate.cpp'))
    assert summary['reason_code']=='invalid_saved_evidence' and files==[]


def test_no_unique_symbol_report_is_unavailable(tmp_path):
    work=setup_work(tmp_path);o=Observer(fake_checker(),work)
    o.capture({}, {'coff':SimpleNamespace(data=b'object')})
    assert collect(work,digest(work/'candidate.cpp'))[0]['reason_code']=='no_comparable_target_symbol'


def test_same_section_relocation_is_resolved(tmp_path):
    work=setup_work(tmp_path);k=fake_checker();o=Observer(k,work);o.install()
    original=k.msvc_compile
    def compiled(*args,**kwargs):
        result=original(*args,**kwargs)
        result[0].symbols[3].update(section=1,value=12)
        return result
    k.msvc_compile=compiled
    k.check_candidate({'size':6000,'addr':0x401000},'source')
    summary,_=collect(work,digest(work/'candidate.cpp'))
    assert summary['status']=='available' and summary['unresolved_relocations']==0
    rows=json.loads((o.output/'relocations.json').read_text())['relocations']
    assert rows[2]['binding_origin']=='same_section'
    assert rows[2]['resolved_address']==0x40100c


def test_unwritable_diagnostics_do_not_replace_official_verdict(tmp_path,monkeypatch):
    work=setup_work(tmp_path);k=fake_checker();o=Observer(k,work);o.install()
    def denied(*args,**kwargs): raise PermissionError('fixture')
    monkeypatch.setattr(o,'capture',denied)
    monkeypatch.setattr(o,'unavailable',denied)
    assert k.check_candidate({'size':6000},'source')['status']=='wip'
    assert k.calls==1
