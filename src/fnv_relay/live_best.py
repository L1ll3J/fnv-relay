"""Display-only best comparisons; saved and current scores stay scoped to pack and metric."""
import json
from pathlib import Path


def metrics(result):
    checker=result.get('checker',{})
    if not isinstance(checker,dict):return {}
    values=checker.get('metrics',checker)
    if not isinstance(values,dict):return {}
    return {key:values[key] for key in ('full_extent_ndiff_insn','ndiff_insn') if type(values.get(key)) is int and values[key]>=0}


class BestTracker:
    def __init__(self,store):
        self.store=store;self.best={}
        for path in (store.root/'claims').glob('*/checked-best.json'):
            try:
                saved=json.loads(path.read_text())
                if saved.get('actually_checked') is not True:continue
                pack=saved.get('pack_sha256') or saved.get('pack_archive_sha256')
                if not isinstance(pack,str):continue
                record=store.get('check:'+path.parent.name+':'+saved['source_sha256']) or {}
                result=record.get('result',{}) if record.get('state')=='complete' else {}
                values=metrics(result) if result.get('pack_sha256')==pack else {}
                # Imported, explicitly checked historical records retain named metrics.
                historical=saved.get('historical_metrics',{})
                if isinstance(historical,dict):
                    for original,key in (('full_extent_ndiff_insn','full_extent_ndiff_insn'),('official_ndiff_insn','ndiff_insn'),('official_window_difference','ndiff_insn')):
                        value=historical.get(original)
                        if type(value) is int and value>=0:values.setdefault(key,value)
                for metric,value in values.items():self.seed(pack,metric,value)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                continue

    def seed(self,pack,metric,value):
        key=(pack,metric);self.best[key]=min(value,self.best.get(key,value))

    def observe(self,claim_id,result):
        pack=result.get('pack_sha256')
        if not isinstance(pack,str):
            pack=next(((lane.get('lease')or{}).get('pack_sha256') for lane in self.store.lanes() if (lane.get('lease')or{}).get('claim_id')==claim_id),None)
        values=metrics(result)
        if not isinstance(pack,str) or not values:return None
        primary='full_extent_ndiff_insn' if 'full_extent_ndiff_insn' in values else 'ndiff_insn'
        old=self.best.get((pack,primary));new=values[primary]
        for metric,value in values.items():self.seed(pack,metric,value)
        if old is not None and new<old:return old,new,primary
        return None
