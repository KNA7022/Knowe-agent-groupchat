"""Pinned public E5 CPU pack. Model installation is explicit; inference is offline."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import threading
import urllib.request

REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
MODEL_ID = f'intfloat/multilingual-e5-small@{REVISION}:int8:mean-l2:384'
ARTIFACTS = {
    'onnx/model_qint8_avx512_vnni.onnx': (118346824, 'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88'),
    'tokenizer.json': (17082730, '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'),
    'config.json': (655, '69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959'),
    'tokenizer_config.json': (443, 'a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b'),
    'special_tokens_map.json': (167, 'd05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7'),
    'README.md': (497538, '0038de97aee16258cecbad7ffda4b4febd6953e747a00e0ddbc8e6ed241e9c1c'),
}


def bundled_root():
    return Path(__file__).resolve().parents[1] / 'model_assets' / 'e5-small'


def verify(root):
    root = Path(root)
    for name, (size, digest) in ARTIFACTS.items():
        path = root / name
        if not path.is_file() or path.stat().st_size != size:
            raise RuntimeError('local_embedding_pack_missing')
        value = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): value.update(block)
        if value.hexdigest() != digest:
            raise RuntimeError('local_embedding_pack_hash_mismatch')


def install(root):
    """Developer packaging acquisition, fixed manifest, no source/user bytes transmitted."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in ARTIFACTS.items():
        target = root / name
        if target.is_file() and target.stat().st_size == size and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + '.part')
        url = f'https://huggingface.co/intfloat/multilingual-e5-small/resolve/{REVISION}/{name}'
        count, hasher = 0, hashlib.sha256()
        # Public acquisition uses no model gateway keys/cookies/credentials.
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'StudyCrew-model-pack/1'}), timeout=120) as response, part.open('wb') as stream:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                count += len(block)
                if count > size: raise RuntimeError('model_artifact_oversized')
                stream.write(block); hasher.update(block)
            stream.flush(); os.fsync(stream.fileno())
        if count != size or hasher.hexdigest() != digest:
            raise RuntimeError('model_artifact_verification_failed')
        os.replace(part,target)
    verify(root)
    (root/'manifest.json').write_text(json.dumps({'model_id':MODEL_ID,'revision':REVISION,'license':'MIT','publisher':'intfloat','artifacts':ARTIFACTS},ensure_ascii=False,indent=2),encoding='utf-8')


class LocalE5Embedding:
    dimensions = 384
    model_identity = MODEL_ID
    def __init__(self, root=None):
        self.root = Path(root or bundled_root())
        self.lock = threading.RLock()
        self.session = self.tokenizer = None
    def load(self):
        with self.lock:
            if self.session is not None: return
            verify(self.root)
            import onnxruntime as ort
            from tokenizers import Tokenizer
            ort.disable_telemetry_events()
            options=ort.SessionOptions()
            options.intra_op_num_threads=2; options.inter_op_num_threads=1
            options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
            options.enable_profiling=False
            session=ort.InferenceSession(str(self.root/'onnx/model_qint8_avx512_vnni.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
            session.disable_fallback()
            if session.get_providers()!=['CPUExecutionProvider']: raise RuntimeError('non_cpu_embedding_provider_denied')
            tokenizer=Tokenizer.from_file(str(self.root/'tokenizer.json'))
            tokenizer.enable_truncation(max_length=512)
            tokenizer.enable_padding(direction='right',pad_id=1,pad_token='<pad>')
            self.session,self.tokenizer=session,tokenizer
    def embed(self,text, *, query=False):
        if not isinstance(text,str) or not text.strip() or len(text)>16000: raise ValueError('invalid_embedding_text')
        self.load()
        import numpy as np
        with self.lock:
            encoded=self.tokenizer.encode(('query: ' if query else 'passage: ')+text)
            ids=np.asarray([encoded.ids],dtype=np.int64)
            mask=np.asarray([encoded.attention_mask],dtype=np.int64)
            values={'input_ids':ids,'attention_mask':mask,'token_type_ids':np.asarray([encoded.type_ids],dtype=np.int64)}
            inputs={item.name:values[item.name] for item in self.session.get_inputs()}
            tokens=self.session.run(None,inputs)[0]
            vector=(tokens*mask[:,:,None]).sum(axis=1)/mask.sum(axis=1,keepdims=True)
            vector=vector[0]
            norm=np.linalg.norm(vector)
            if vector.shape!=(384,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm<=0: raise RuntimeError('invalid_embedding_output')
            return (vector/norm).tolist()


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--install',action='store_true');parser.add_argument('--root')
    args=parser.parse_args()
    if args.install:
        install(args.root or bundled_root())
        print(json.dumps({'installed':True,'model_id':MODEL_ID,'bytes':sum(x[0] for x in ARTIFACTS.values())}))
