from __future__ import annotations
import json, urllib.parse, urllib.request
from pathlib import Path
ROOT=Path(r'E:\ZJUBJ\数据001\评分对比\_对比工作')
OUT=Path(r'E:\codex work\RPF-Agent-12项目实时校准数据集.json')
BASE='http://100.100.30.150:18200/api'
RUNS={1:('PRJ-20261104-1X7E',191),2:('PRJ-20261106-KDK2',190),3:('PRJ-20261029-J0AH',192),4:('PRJ-20261030-1KJP',193),5:('PRJ-20261103-N7UE',194),6:('PRJ-20261104-B3KP',195),7:('PRJ-20261118-RGX5',196),8:('PRJ-20261111-YIE5',197),9:('PRJ-20261112-9QCT',198),10:('PRJ-20261116-Y3DS',199),11:('PRJ-20261117-10G5',200),12:('PRJ-20261118-42FO',201)}
CAT={'商务客观':'commercial_objective','商务主观':'commercial_subjective','技术客观':'technical_objective','技术主观':'technical_subjective','资格/符合性':'qualification','价格':'price'}
def norm(v): return ''.join(str(v or '').split()).replace('（','(').replace('）',')')
def get(url):
 with urllib.request.urlopen(url,timeout=60) as r: return json.load(r)
def manual_scores(items,names):
 wanted={norm(x) for x in names}; out={}
 for item in items or []:
  if norm(item.get('item')) in wanted:
   for b,v in (item.get('scores') or {}).items(): out[b]=out.get(b,0)+float(v)
 return out
rows=[]
for n,(pid,rid) in RUNS.items():
 data=json.loads((ROOT/f'测试{n}.json').read_text(encoding='utf-8'))
 summary=get(f'{BASE}/runs/{rid}/score-summary?{urllib.parse.urlencode({"project_id":pid,"max_reason_chars":200})}')
 ai={}
 for bidder in summary.get('bidders') or []:
  for bucket in bidder.get('buckets') or []:
   for item in bucket.get('items') or []:
    if item.get('final') is not None:
     ai[(str(item.get('code')),bidder['name'])]=item
 for align in data.get('alignment') or []:
  code=str(align.get('ai_code') or '')
  if not code: continue
  exp=manual_scores((data.get('manual') or {}).get('items'),align.get('manual_items') or [])
  for bidder,manual in exp.items():
   item=ai.get((code,bidder))
   if not item: continue
   max_score=float(item.get('max_score') or 0)
   got=float(item.get('final'))
   rows.append({'test_id':f'测试{n}','project_id':pid,'run_id':rid,'item_code':code,'item_name':item.get('criterion_name') or item.get('name'),'category':CAT.get(str(align.get('category') or ''),'other'),'max_score':max_score,'bidder':bidder,'ai_score':got,'manual_score':manual,'ai_ratio':got/max_score if max_score else None,'manual_ratio':manual/max_score if max_score else None})
OUT.write_text(json.dumps({'rows':rows,'count':len(rows)},ensure_ascii=False,indent=2),encoding='utf-8')
print(len(rows), OUT)
