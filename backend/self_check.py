"""Offline installed-runtime check; uses synthetic data and opens no network."""
import importlib.metadata
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace


def main(report_path=None):
    def denied(*args, **kwargs):
        raise RuntimeError('self_check_network_denied')
    socket.socket.connect = denied
    socket.getaddrinfo = denied
    result = {'ok':False,'scope':'offline_installed_runtime','external_calls':0}
    try:
        from .knowe_memory.local_embedding import LocalE5Embedding
        from .knowe_memory.mem0_bridge import local_factory
        import numpy as np
        embedding = LocalE5Embedding()
        vector = embedding.embed('合成数学课程：矩阵乘法。')
        if len(vector)!=384 or not np.isclose(np.linalg.norm(vector),1):
            raise RuntimeError('embedding_output_invalid')
        row=SimpleNamespace(memory_id='selfcheck',version=1,text='矩阵乘法要求左矩阵列数等于右矩阵行数。')
        with local_factory()(gid='selfcheck',cid='synthetic',sensitivity='public',operation='search') as projection:
            projection.rebuild([row])
            found=projection.search('矩阵相乘的要求',resolve=lambda mid,version:row if (mid,version)==('selfcheck',1) else None)
            if found != [row]:
                raise RuntimeError('semantic_retrieval_failed')
        result.update(ok=True,dimensions=len(vector),semantic_retrieval=True,
                      versions={name:importlib.metadata.version(name) for name in ('mem0ai','numpy','onnxruntime','tokenizers','usearch')})
    except Exception as exc:
        result['error_type']=type(exc).__name__
        # No user content or credentials ever enter this check.
        result['error']=str(exc)
    encoded=json.dumps(result,ensure_ascii=False)
    if report_path:
        Path(report_path).write_text(encoded+'\n',encoding='utf-8')
    if sys.stdout is not None:
        print(encoded)
    return 0 if result['ok'] else 1
