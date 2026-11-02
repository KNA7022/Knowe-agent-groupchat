"""Encrypted, group-scoped learning workflows. No model calls or school writes."""
from __future__ import annotations
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs
from backend.groups import GroupLifecycle, GroupNotFoundError
from backend.knowledge_api import KnowledgeHttpResponse
from backend.knowe_rag.crypto import MasterKey, field_aad


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result.astimezone(timezone.utc)


def stamp(value):
    return value.isoformat()


def title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError('invalid_text')
    return value.strip()


def bounded(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
        raise ValueError('invalid_number')
    return value


class SchoolGateway:
    """Read-only boundary: adapters may list courses/assessments, never submit."""
    def snapshot(self, *, include_ranking=False):
        return {'mode': 'mock', 'courses': [{'external_id': 'demo-math', 'title': '演示：线性代数'}],
                'assessments': [{'external_id': 'demo-exam', 'course_external_id': 'demo-math', 'title': '演示测验'}],
                'timetable': [{'course_external_id': 'demo-math', 'weekday': 1, 'start': '09:00', 'end': '10:00'}],
                'grades': [{'course_external_id': 'demo-math', 'grade': None, 'status': 'unknown'}],
                'ranking': {'position': None, 'status': 'unknown'} if include_ranking else None,
                'authentication': {'mode': 'mock', 'authenticated': False}, 'read_only': True}


class LearningService:
    def __init__(self, root, groups, *, master_key=None):
        self.groups = groups
        root = Path(root) / 'learning'
        root.mkdir(parents=True, exist_ok=True)
        self.key = master_key or MasterKey.load_or_create(root / 'master-key.dpapi')
        self.db = sqlite3.connect(root / 'learning.sqlite', check_same_thread=False)
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('CREATE TABLE IF NOT EXISTS learning_state (group_id TEXT PRIMARY KEY, sealed BLOB NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS learning_meta (name TEXT PRIMARY KEY, sealed BLOB NOT NULL)')
        row = self.db.execute("SELECT sealed FROM learning_meta WHERE name='key_verifier'").fetchone()
        if row:
            if self.key.open(row[0], aad=field_aad('learning', 'key_verifier')) != b'learning-key-v1':
                raise ValueError('learning_key_mismatch')
        else:
            # Existing WIP stores must be opened before binding the verifier.
            for gid, blob in self.db.execute('SELECT group_id, sealed FROM learning_state'):
                self.key.open(blob, aad=field_aad(gid, 'learning'))
            self.db.execute('INSERT INTO learning_meta VALUES (?,?)', ('key_verifier', self.key.seal(b'learning-key-v1', aad=field_aad('learning', 'key_verifier'))))
        self.db.commit()
        self.lock = threading.RLock()
        self.reconcile()

    def close(self):
        self.db.close()

    def reconcile(self):
        with self.lock:
            allowed = {g.group_id for g in self.groups.list_groups() if g.lifecycle in {GroupLifecycle.ACTIVE, GroupLifecycle.ARCHIVED}}
            for (gid,) in self.db.execute('SELECT group_id FROM learning_state').fetchall():
                if gid not in allowed:
                    self.db.execute('DELETE FROM learning_state WHERE group_id=?', (gid,))
            self.db.commit()

    def scope(self, gid, mutation=False):
        group = self.groups.get(gid)
        transaction = self.groups.delete_transaction(gid)
        if transaction and transaction.get('stage') != 'committed':
            raise PermissionError('group_deletion_in_progress')
        if group.lifecycle not in {GroupLifecycle.ACTIVE, GroupLifecycle.ARCHIVED} or (mutation and group.lifecycle != GroupLifecycle.ACTIVE):
            raise PermissionError('group_unavailable')

    def read(self, gid):
        self.scope(gid)
        row = self.db.execute('SELECT sealed FROM learning_state WHERE group_id=?', (gid,)).fetchone()
        return json.loads(self.key.open(row[0], aad=field_aad(gid, 'learning'))) if row else {
            'revision': 0, 'courses': [], 'assessments': [], 'tasks': [], 'availability': [],
            'mastery': {}, 'questions': [], 'attempts': [], 'candidates': [], 'conflicts': []}

    def save(self, gid, state):
        self.scope(gid, True)
        state['revision'] += 1
        blob = self.key.seal(json.dumps(state, ensure_ascii=False).encode(), aad=field_aad(gid, 'learning'))
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO learning_state VALUES (?,?)', (gid, blob))

    def schedule(self, state, now):
        """Earliest deadline first, priority tie-break; split work across real availability."""
        slots = [(max(instant(s['start']), now), instant(s['end'])) for s in state['availability'] if instant(s['end']) > now]
        slots.sort()
        state['conflicts'] = []
        occupied = []
        pending = sorted((t for t in state['tasks'] if t['status'] == 'pending'),
                         key=lambda t: (instant(t['deadline']), -t['priority'], t['id']))
        for task in pending:
            task['sessions'] = []
            remaining = task['minutes']
            earliest = max(now, instant(task.get('not_before', stamp(now))))
            deadline = instant(task['deadline'])
            for start, end in slots:
                cursor, stop = max(start, earliest), min(end, deadline)
                for busy_start, busy_end in sorted(occupied) + [(stop, stop)]:
                    if busy_end <= cursor:
                        continue
                    free_end = min(stop, busy_start)
                    available = max(0, int((free_end - cursor).total_seconds() // 60))
                    take = min(remaining, available, 60)
                    while take > 0:
                        finish = cursor + timedelta(minutes=take)
                        task['sessions'].append({'start': stamp(cursor), 'end': stamp(finish)})
                        occupied.append((cursor, finish))
                        remaining -= take
                        cursor = finish
                        available -= take
                        take = min(remaining, available, 60)
                    cursor = max(cursor, busy_end)
                    if cursor >= stop or remaining == 0:
                        break
                if remaining == 0:
                    break
            if remaining:
                state['conflicts'].append({'task_id': task['id'], 'unallocated_minutes': remaining, 'reason': 'insufficient_time_before_deadline'})

    def mutate(self, gid, payload, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock:
            self.scope(gid, True)
            s = self.read(gid)
            if payload.get('revision') != s['revision']:
                raise RuntimeError('stale_revision_reload')
            op = payload.get('action')
            def find(kind, key):
                return next(x for x in s[kind] if x['id'] == payload.get(key))
            def course():
                return find('courses', 'course_id')
            def task(name, cid, minutes, deadline, priority=2, **extra):
                row = {'id': uuid.uuid4().hex, 'title': name, 'course_id': cid, 'minutes': minutes,
                       'deadline': stamp(instant(deadline)), 'priority': priority, 'status': 'pending', 'sessions': [], **extra}
                s['tasks'].append(row)
                return row
            if op == 'course':
                s['courses'].append({'id': uuid.uuid4().hex, 'title': title(payload.get('title'))})
            elif op == 'reminder_settings':
                if type(payload.get('enabled')) is not bool:
                    raise ValueError('invalid_notification_setting')
                s['notifications_enabled'] = payload['enabled']
            elif op == 'availability':
                slots = payload.get('slots')
                if not isinstance(slots, list) or len(slots) > 366:
                    raise ValueError('invalid_availability')
                normalized = sorted([{'start': stamp(instant(x['start'])), 'end': stamp(instant(x['end']))} for x in slots], key=lambda x: x['start'])
                for i, slot in enumerate(normalized):
                    if instant(slot['start']) >= instant(slot['end']) or (i and instant(normalized[i-1]['end']) > instant(slot['start'])):
                        raise ValueError('overlapping_or_empty_availability')
                s['availability'] = normalized
            elif op in {'assessment', 'task'}:
                c = course()
                name = title(payload.get('title'))
                deadline = stamp(instant(payload['deadline']))
                minutes = int(bounded(payload.get('minutes', 60), 5, 10000))
                priority = int(bounded(payload.get('priority', 2), 1, 5))
                if op == 'assessment':
                    assessment = {'id': uuid.uuid4().hex, 'course_id': c['id'], 'title': name, 'deadline': deadline}
                    s['assessments'].append(assessment)
                    task('备考：' + name, c['id'], minutes, deadline, priority, assessment_id=assessment['id'])
                else:
                    task(name, c['id'], minutes, deadline, priority)
            elif op in {'postpone', 'skip', 'complete'}:
                t = find('tasks', 'task_id')
                if t['status'] != 'pending':
                    raise ValueError('task_not_pending')
                if op == 'postpone':
                    t['not_before'] = stamp(instant(payload['not_before']))
                else:
                    t['status'] = 'skipped' if op == 'skip' else 'completed'
                    t['sessions'] = []
            elif op == 'question':
                c = course()
                s['questions'].append({'id': uuid.uuid4().hex, 'course_id': c['id'], 'topic': title(payload.get('topic')),
                    'prompt': title(payload.get('prompt')), 'answer': title(payload.get('answer')), 'explanation': title(payload.get('explanation')),
                    'difficulty': int(bounded(payload.get('difficulty', 2), 1, 5))})
            elif op == 'practice':
                q = find('questions', 'question_id')
                answer = title(payload.get('answer'))
                correct = answer.strip().casefold() == q['answer'].strip().casefold()
                key = q['course_id'] + ':' + q['topic']
                m = s['mastery'].setdefault(key, {'course_id': q['course_id'], 'topic': q['topic'], 'score': 0.0, 'attempts': 0, 'streak': 0})
                m['attempts'] += 1
                m['score'] = round(m['score'] * .65 + (.35 if correct else 0), 4)
                m['streak'] = m['streak'] + 1 if correct else 0
                days = [1, 3, 7, 14, 30][min(max(m['streak'] - 1, 0), 4)] if correct else 1
                m['review_at'] = stamp(now + timedelta(days=days))
                s['attempts'].append({'id': uuid.uuid4().hex, 'question_id': q['id'], 'correct': correct, 'at': stamp(now), 'explanation': q['explanation']})
                for old in s['tasks']:
                    if old.get('review_key') == key and old['status'] == 'pending':
                        old['status'], old['sessions'] = 'superseded', []
                task('复习：' + q['topic'], q['course_id'], 15, now + timedelta(days=days+1), 3, not_before=m['review_at'], review_key=key)
            elif op == 'feedback':
                t = find('tasks', 'task_id')
                if t['status'] != 'completed':
                    raise ValueError('complete_before_feedback')
                rating = int(bounded(payload.get('rating'), 1, 4))
                t['feedback'] = {'rating': rating, 'note': title(payload.get('note')), 'at': stamp(now), 'origin': 'self_report'}
            elif op == 'edit_task':
                t = find('tasks', 'task_id')
                if t['status'] != 'pending':
                    raise ValueError('task_not_pending')
                t.update(title=title(payload.get('title')), deadline=stamp(instant(payload['deadline'])),
                    minutes=int(bounded(payload.get('minutes'), 5, 10000)), priority=int(bounded(payload.get('priority'), 1, 5)))
            elif op == 'candidate':
                kind = payload.get('kind')
                if kind not in {'prompt', 'routing', 'retrieval', 'learning_method', 'agent_template'}:
                    raise ValueError('unsupported_candidate_kind')
                s['candidates'].append({'id': uuid.uuid4().hex, 'kind': kind, 'text': title(payload.get('text')), 'status': 'pending', 'evaluation': None})
            elif op == 'evaluate_candidate':
                c = find('candidates', 'candidate_id')
                # Evidence is recorded as user-supplied, never fabricated into an executed evaluation.
                e = payload.get('evaluation', {})
                if not all(isinstance(e.get(k), bool) for k in ('golden_pass', 'permission_pass', 'shadow_pass')):
                    raise ValueError('evaluation_results_required')
                c['evaluation'] = {**{k: e[k] for k in ('golden_pass', 'permission_pass', 'shadow_pass')}, 'report': title(e.get('report')), 'origin': 'manual_review'}
                c['status'] = 'evaluated'
            elif op == 'approve_candidate':
                c = find('candidates', 'candidate_id')
                e = c.get('evaluation') or {}
                if payload.get('confirmed') is not True or not all(e.get(k) is True for k in ('golden_pass', 'permission_pass', 'shadow_pass')):
                    raise ValueError('evaluation_and_human_approval_required')
                c['status'] = 'approved_for_manual_application'
            elif op == 'delete_course':
                c = course()
                cid = c['id']
                removed_questions = {q['id'] for q in s['questions'] if q['course_id'] == cid}
                for collection in ('courses', 'assessments', 'tasks', 'questions'):
                    s[collection] = [x for x in s[collection] if (x['id'] if collection == 'courses' else x['course_id']) != cid]
                s['attempts'] = [x for x in s['attempts'] if x['question_id'] not in removed_questions]
                s['mastery'] = {k:v for k,v in s['mastery'].items() if v['course_id'] != cid}
            elif op != 'replan':
                raise ValueError('unknown_action')
            if any(len(s[k]) > 10000 for k in ('tasks', 'questions', 'attempts', 'courses', 'candidates')):
                raise ValueError('learning_capacity_reached')
            self.schedule(s, now)
            self.save(gid, s)
            return s

    def calendar(self, gid, state):
        def escape(value):
            return value.replace('\\', '\\\\').replace('\r', '').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//StudyCrew//Learning//ZH', 'CALSCALE:GREGORIAN']
        for t in state['tasks']:
            if t['status'] != 'pending':
                continue
            for i, session in enumerate(t['sessions']):
                fmt = lambda v: instant(v).strftime('%Y%m%dT%H%M%SZ')
                lines += ['BEGIN:VEVENT', f'UID:{gid}-{t["id"]}-{i}@studycrew', 'DTSTAMP:' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                          'SEQUENCE:' + str(state['revision']), 'DTSTART:' + fmt(session['start']), 'DTEND:' + fmt(session['end']), 'SUMMARY:StudyCrew 学习任务', 'END:VEVENT']
        lines.append('END:VCALENDAR')
        folded = []
        for line in lines:
            segment = ''
            for char in line:
                if len((segment + char).encode('utf8')) > 73:
                    folded.append(segment)
                    segment = ' '
                segment += char
            folded.append(segment)
        return '\r\n'.join(folded) + '\r\n'

    async def dispatch(self, method, path, query, body):
        try:
            gid = (parse_qs(query).get('group_id') or [''])[0]
            if not gid:
                raise ValueError('group_required')
            with self.lock:
                self.scope(gid, method == 'POST')
                if method == 'POST' and path == '/api/learning/action':
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ValueError('invalid_payload')
                    state = self.mutate(gid, payload)
                elif method == 'GET' and path in {'/api/learning/state', '/api/learning/ics'}:
                    state = self.read(gid)
                else:
                    return KnowledgeHttpResponse(404, {'ok': False, 'error': 'not_found'})
                result = {'ok': True, 'state': state, 'school': SchoolGateway().snapshot(include_ranking=(parse_qs(query).get('include_ranking') == ['true']))}
                # Practice answers remain server-side until an attempt is graded.
                result['state'] = {**state, 'questions': [{k:v for k,v in q.items() if k not in {'answer', 'explanation'}} for q in state['questions']]}
                if path.endswith('/ics'):
                    result['ics'] = self.calendar(gid, state)
                return KnowledgeHttpResponse(200, result)
        except (PermissionError, GroupNotFoundError):
            return KnowledgeHttpResponse(403, {'ok': False, 'error': 'group_unavailable'})
        except RuntimeError:
            return KnowledgeHttpResponse(409, {'ok': False, 'error': 'stale_revision_reload'})
        except (ValueError, TypeError, KeyError, StopIteration):
            return KnowledgeHttpResponse(400, {'ok': False, 'error': 'invalid_learning_request'})
