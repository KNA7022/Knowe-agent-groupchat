"""Opt-in synthetic production memory probe; importing never sends requests.

Caller owns the credential and shared-budget AsyncClient factory. The only model
request is the approved extraction (no retries, at most 2048 output tokens).
Mem0, local E5 and USearch are the actual pinned implementations, never mocks.
"""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from scripts.live_protocol_probe import ROOT  # source-package bootstrap
from backend.commercial_gateway import ProviderGatewayClient
from backend.groups import GroupRepository
from backend.learning import LearningService
from backend.memory_api import MemoryService as GovernedMemoryService
from backend.knowe_memory.mem0_bridge import local_factory, PIN
from backend.knowe_memory.local_embedding import LocalE5Embedding
from backend.knowe_rag.crypto import MasterKey
from backend.task_runtime.service import TaskRuntimeService


async def run_memory_probe(data_root, binding, credential, client_factory):
    """Approve extraction, confirm one fact, recall semantically, delete, recall.

    This is service/API integration evidence, not a desktop UI test. All material
    is synthetic. Credentials remain in the closure and are never logged/saved.
    The factory must enforce the caller's shared paid budget on each actual send.
    """
    if (not isinstance(binding, dict)
            or not all(binding.get(k) for k in ('provider', 'model', 'base_url'))
            or not credential or not callable(client_factory)):
        raise ValueError('explicit binding, credential and budget client factory required')
    root = Path(data_root) / ('memory-probe-' + uuid.uuid4().hex)
    report = {'pass': False, 'scope': 'synthetic real governed memory API, not desktop UI',
              'maximum_model_requests': 1, 'output_cap': 2048, 'stages': [],
              'data_root': str(root), 'sdk_required_version': PIN}
    memory = learning = runtime = None
    gateways = []
    try:
        key = MasterKey.load_or_create(root / 'master-key.dpapi')
        groups = GroupRepository(root)
        group = groups.create_group('Synthetic memory integration probe')
        learning = LearningService(root, groups, master_key=key)
        state = learning.mutate(group.group_id, {'revision': 0, 'action': 'course',
                                                 'title': 'Synthetic study course'})
        cid = state['courses'][0]['id']
        runtime = TaskRuntimeService(root, groups, card_master_key=key)
        memory = GovernedMemoryService(root, groups, key, learning=learning,
                                       semantic_factory=local_factory(), runtime=runtime)

        def factory(authority, _gid):
            gateway = ProviderGatewayClient(base_url=binding['base_url'], model=binding['model'],
                provider=binding['provider'], transport=binding.get('transport', 'openai_chat'),
                api_key='', credential_provider=lambda: credential,
                authority_provider=lambda: authority, max_retries=0,
                client_factory=client_factory)
            gateways.append(gateway)
            return gateway

        memory.extractions.gateway_factory = factory
        query = f'group_id={group.group_id}&course_id={cid}&sensitivity=personal'

        async def act(payload):
            response = await memory.dispatch('POST', '/api/memory/action', query,
                                            json.dumps(payload).encode('utf-8'))
            report['stages'].append({'action': payload['action'], 'status': response.status})
            if response.status != 200:
                raise RuntimeError('memory_probe_stage_failed')
            return response.body

        def attempts():
            return sum(int(gateway._transport.last_attempts) for gateway in gateways)

        initial = await act({'action': 'extract',
            'text': 'For synthetic study sessions, I prefer worked examples before practice questions.',
            'source': {'kind': 'manual', 'version': 1}})
        pending = initial['pending_operations']
        if len(pending) != 1 or initial['candidates'] or initial['records'] or attempts() != 0:
            raise RuntimeError('memory_probe_approval_boundary_failed')
        op = pending[0]
        approval = runtime.store.get_approval(op['approval_id'])
        if approval is None or approval.status != 'pending':
            raise RuntimeError('memory_probe_real_approval_missing')
        report['approval_before_egress'] = True
        runtime.resolve_approval(group.group_id, op['approval_id'], approved=True,
            capabilities=('provider_egress',), request_key='synthetic-memory-approve:' + op['operation_id'])
        extracted = await act({'action': 'extract', 'operation_id': op['operation_id']})
        if len(extracted['candidates']) != 1 or extracted['records'] or attempts() != 1:
            raise RuntimeError('memory_probe_single_candidate_boundary_failed')
        candidate = extracted['candidates'][0]
        if candidate['status'] != 'candidate' or candidate['sensitivity'] != 'personal':
            raise RuntimeError('memory_probe_candidate_scope_failed')
        report['extraction_run_status'] = runtime.store.get_run(op['run_id']).status.value
        report['candidate_requires_confirmation'] = True
        confirmed = await act({'action': 'approve', 'memory_id': candidate['id'],
                               'version': candidate['version'], 'confirmed': True})
        if confirmed['candidates'] or len(confirmed['records']) != 1:
            raise RuntimeError('memory_probe_confirmation_failed')
        approved = confirmed['records'][0]
        if approved['version'] <= candidate['version']:
            raise RuntimeError('memory_probe_confirmation_version_failed')
        found = await act({'action': 'search', 'query': 'What study method do I prefer?'})
        if (found['mode'] != 'mem0_semantic' or found['semantic_recall'] is not True
                or [row['id'] for row in found['records']] != [approved['id']]):
            raise RuntimeError('memory_probe_actual_semantic_recall_failed')
        report['semantic_recall_verified'] = True
        await act({'action': 'delete', 'memory_id': approved['id'], 'version': approved['version']})
        deleted = await act({'action': 'search', 'query': 'What study method do I prefer?'})
        if deleted['records'] or deleted['candidates'] or not deleted['semantic_recall']:
            raise RuntimeError('memory_probe_deleted_fact_recalled')
        # Check the authoritative encrypted repository, not only the view result.
        if memory.repository.record(memory_id=approved['id']) is not None:
            raise RuntimeError('memory_probe_authoritative_delete_failed')
        from backend.knowe_memory.mem0_adapters import ARENAS
        if ARENAS:
            raise RuntimeError('memory_probe_projection_not_disposed')
        report.update(pass_=True, model_requests=attempts(), deletion_no_recall=True,
                      actual_sdk=True, actual_local_embedding=True, actual_usearch=True,
                      local_embedding_identity=LocalE5Embedding().model_identity,
                      remaining_sdk_arenas=0)
        report['pass'] = report.pop('pass_') and attempts() == 1
    except Exception as exc:
        # Raw SDK/provider exceptions can contain content; report only the class.
        report['error'] = type(exc).__name__
    finally:
        if memory is not None:
            memory.close()
        if learning is not None:
            learning.close()
        if runtime is not None:
            runtime.close()
    return report
