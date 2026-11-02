"""Run with pinned scratch SDK interpreter; synthetic callbacks, external I/O denied."""
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Probe-only reuse of already-installed native runtimes; production uses requirements/spec.
sys.path.append('E:/python/Lib/site-packages')

def deny(*args, **kwargs): raise AssertionError('network_denied')
socket.socket.connect = deny
socket.getaddrinfo = deny
from backend.knowe_memory.mem0_bridge import Mem0Projection, SemanticMemoryUnavailable

def embed(text, **kwargs): return [1.0, float('blue' in text), float('red' in text), .5]
def generate(messages):
    return json.dumps({'memory':[{'id':'0','text':'Synthetic orb is blue.','attributed_to':'user'},
                                {'id':'1','text':'Synthetic orb is blue.','attributed_to':'user'}]})

with Mem0Projection(embed=embed,generate=generate,model_identity='synthetic-fixture',dimensions=4) as candidate:
    assert candidate.sdk.__class__.__module__ == 'mem0.memory.main'
    assert candidate.extract('Synthetic evidence.') == ['Synthetic orb is blue.']
    assert candidate.extract('Synthetic evidence.') == []
    assert candidate.sdk.db.connection.execute('PRAGMA database_list').fetchall() == [(0,'main','')]
    record=SimpleNamespace(memory_id='authoritative-a',version=3,text='Synthetic orb is blue.')
    with Mem0Projection(embed=embed,generate=generate,model_identity='synthetic-fixture',dimensions=4) as approved:
        approved.rebuild([record])
        assert approved.search('blue',resolve=lambda mid,version: record if (mid,version)==('authoritative-a',3) else None)==[record]
        assert approved.search('blue',resolve=lambda mid,version: None)==[]
        assert approved.arena is not candidate.arena
    def failed_insert(*args,**kwargs):
        candidate.arena.fail()
        raise RuntimeError('synthetic_insert_failure')
    candidate.sdk.vector_store.insert=failed_insert
    # New different fact must execute an insert rather than existing SDK dedup.
    candidate.arena.generate=lambda m:json.dumps({'memory':[{'id':'0','text':'Synthetic orb is red.','attributed_to':'user'}]})
    try:
        candidate.extract('Different synthetic evidence.')
    except SemanticMemoryUnavailable:
        pass
    else:
        raise AssertionError('false_ADD_not_rejected')
from backend.knowe_memory.mem0_adapters import ARENAS
assert not ARENAS
from backend.knowe_memory.mem0_bridge import local_factory
factory=local_factory()
prompts=[]
def capture(messages):
    prompts.append(messages)
    return '{"memory":[]}'
for _ in range(2):
    with factory(gid='synthetic-group',cid='synthetic-course',sensitivity='personal',operation='extract',generate=capture) as p:
        assert p.extract('Synthetic orb is blue.')==[]
assert len(prompts)==2 and prompts[0]==prompts[1], 'fresh_projection_exact_prompt_changed'
with factory(gid='synthetic-group',cid='synthetic-course',sensitivity='public',operation='search') as p:
    chinese=SimpleNamespace(memory_id='math',version=1,text='矩阵乘法需要左边的列数等于右边的行数。')
    p.rebuild([chinese])
    assert p.search('矩阵相乘的条件',resolve=lambda mid,v:chinese if (mid,v)==('math',1) else None)==[chinese]
assert not ARENAS
print(json.dumps({'sdk':'2.0.20','actual_sdk':True,'extraction_dedup':True,'version_postfilter':True,'false_ADD_denied':True,'external_calls':0,'remaining_arenas':0}))
