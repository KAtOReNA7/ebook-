"""Feishu shortcut transport; receipts, complete reads, no blind mutation retries."""
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_CLI_FALLBACK = str(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'pnpm/lark-cli.CMD')


def resolve_cli():
    """Resolve one executable path; never interpret configuration as a shell command."""
    explicit = os.environ.get('EBOOK_LARK_CLI')
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not explicit or not path.is_absolute() or not path.is_file():
            raise ValueError('EBOOK_LARK_CLI must be an existing absolute executable path, without arguments')
        return str(path.resolve())
    discovered = shutil.which('lark-cli')
    if discovered:
        return discovered
    if os.name == 'nt' and Path(WINDOWS_CLI_FALLBACK).is_file():
        return WINDOWS_CLI_FALLBACK
    raise FileNotFoundError('lark-cli not found; install it on PATH or set EBOOK_LARK_CLI to its absolute path')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class Base:
    def __init__(self, receipt_dir, presentation_enabled=True):
        self.manifest = json.loads((ROOT / 'config/base-manifest.json').read_text(encoding='utf-8'))
        self.token = self.manifest['base']['base_token']
        self.receipt_dir = Path(receipt_dir).resolve()
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        self.changes = 0
        self.presentation_enabled = presentation_enabled
        self._record_cache = {}
        self._project_titles = {}

    def _fields(self, table):
        return {f['name']: f for f in getattr(self, 'manifest', {}).get('tables', {}).get(table, {}).get('field_readback', {}).get('fields', [])}

    def _present(self, table, row, context=None):
        if not getattr(self, 'presentation_enabled', False): return dict(row)
        try:
            from .presentation import to_remote
        except ImportError:
            from presentation import to_remote
        row=dict(row)
        fields=self._fields(table)
        if table in ('每日数据','营销服务明细') and '书名' in fields and '书名' not in row:
            key=row.get('作品唯一键')
            title=self._project_titles.get(key)
            if title: row['书名']=title
        return to_remote(table,row,fields,context)

    def _scope_check(self,table,row,context=None,creating=False):
        if table == '通知事件' and (creating or '收件人' in row):
            try:
                from .notification_policy import enforce_recipients
            except ImportError:
                from notification_policy import enforce_recipients
            enforce_recipients(row, ROOT)
        try:
            from .service_scope import enforce_write
        except ImportError:
            from service_scope import enforce_write
        enforce_write(table,row,context,creating=creating)

    def _ensure_display_options(self, table, rows):
        if not getattr(self, 'presentation_enabled', False): return
        fields=self._fields(table)
        for name in ('版权方','一级分类','二级分类','监测范围'):
            if name not in fields: continue
            wanted={x for r in rows for x in (r.get(name) or [])}
            if wanted <= {x['name'] for x in fields[name].get('options',[])}: continue
            current=self.complete_field(table,self.call('+field-get',table,field_id=fields[name]['id'])['data']['field'])
            missing=wanted-{x['name'] for x in current.get('options',[])}
            if missing:
                payload={k:v for k,v in current.items() if k not in
                         {'id','remaining_options_count','truncated','truncated_options_count'}}
                payload['options'] += [{'name':x,'hue':'Blue','lightness':'Lighter'} for x in sorted(missing)]
                self.call('+field-update',table,payload,field_id=current['id'],yes=True)
            verified=self.complete_field(table,self.call('+field-get',table,field_id=current['id'])['data']['field'])
            if not wanted <= {x['name'] for x in verified['options']}: raise RuntimeError('Display option readback differs')
            fs=self.manifest['tables'][table]['field_readback']['fields']
            fs[:]=[verified if x['id']==verified['id'] else x for x in fs]
            self.manifest['tables'][table].setdefault('fields',{})[name]=verified
            save(ROOT/'config/base-manifest.json',self.manifest)

    def complete_field(self, table, field):
        # field-list/get currently returns only the first 50 static options.
        if field.get('type')!='select' or len(field.get('options',[]))<50: return field
        options=[]; expected_total=None
        for _ in range(100):
            data=self.call('+field-search-options',table,field_id=field['id'],limit=200,offset=len(options))['data']
            page=data['options']; total=data['total']
            if type(total) is not int or total<0: raise RuntimeError('Invalid select option total')
            if expected_total is None: expected_total=total
            if total!=expected_total: raise RuntimeError('Select option total changed during pagination')
            if not page and len(options)<total: raise RuntimeError('Incomplete select options')
            options.extend(page)
            if len(options)==total:
                if len({x['name'] for x in options})!=total: raise RuntimeError('Duplicate select options')
                return {**field,'options':options}
            if len(options)>total: raise RuntimeError('Select option count mismatch')
        raise RuntimeError('Select option pagination exceeded limit')

    def call(self, command, table=None, payload=None, **flags):
        try:
            from .runner_policy import READ_COMMANDS, enforce_writer
        except ImportError:
            from runner_policy import READ_COMMANDS, enforce_writer
        if command not in READ_COMMANDS:
            enforce_writer(ROOT)
        if payload is not None and isinstance(payload,dict) and command in ('+record-batch-create','+record-batch-update'):
            payload=dict(payload)
            if 'create_records' in payload:
                for r in payload['create_records']: self._scope_check(table,r,creating=True)
                payload['create_records']=[self._present(table,r) for r in payload['create_records']]
                self._ensure_display_options(table,payload['create_records'])
            if 'update_records' in payload:
                cache=getattr(self,'_record_cache',{}).get(table,{})
                for rid,r in payload['update_records'].items(): self._scope_check(table,r,cache.get(rid))
                payload['update_records']={rid:self._present(table,r,cache.get(rid)) for rid,r in payload['update_records'].items()}
                self._ensure_display_options(table,list(payload['update_records'].values()))
        args = [resolve_cli(), 'base', command, '--base-token', self.token, '--as', 'user']
        if table:
            args += ['--table-id', self.manifest['tables'][table]['id']]
        stamp = uuid.uuid4().hex
        if payload is not None:
            request = self.receipt_dir / (stamp + '-request.json')
            save(request, payload)
            args += ['--json', '@' + str(request.relative_to(ROOT))]
        for key, value in flags.items():
            args.append('--' + key.replace('_', '-'))
            if value is not True:
                args.append(str(value))
        pending = self.receipt_dir / (stamp + '-PENDING.json')
        if payload is not None:
            save(pending, {'command': command, 'table': table, 'request': str(request), 'state': 'unknown_until_ack_and_readback'})
        result = subprocess.run(args, cwd=ROOT, capture_output=True, encoding='utf-8', timeout=180,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        (self.receipt_dir / (stamp + '-stdout.json')).write_text(result.stdout, encoding='utf-8')
        (self.receipt_dir / (stamp + '-stderr.txt')).write_text(result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'{command}: stopped; inspect receipt {stamp}; no retry. {result.stderr[:500]} {result.stdout[:500]}')
        reply = json.loads(result.stdout)
        if reply.get('ok') is False or reply.get('data', {}).get('ignored_fields'):
            raise RuntimeError(f'Unconfirmed write: {stamp}')
        if payload is not None:
            pending.rename(pending.with_name(stamp + '-ACK.json'))
            self.changes += len(payload.get('create_records', payload.get('update_records', {}))) if isinstance(payload,dict) else 0
        return reply

    def read(self, table, conditions=None):
        flags = {'minimal_stdout': True, 'limit': 2000}
        if conditions:
            filt = self.receipt_dir / (uuid.uuid4().hex + '.filter.json')
            save(filt, {'logic': 'and', 'conditions': conditions})
            flags['filter_json'] = '@' + str(filt.relative_to(ROOT))
        rows, seen = [], set()
        # The CLI exposes offset pagination. Every page has its own receipt;
        # missing continuation metadata must never turn a partial read into a full one.
        for _ in range(100):
            path = self.receipt_dir / (uuid.uuid4().hex + '.ndjson')
            reply = self.call('+record-list', table, output=str(path.relative_to(ROOT)),
                              offset=len(rows), **flags)
            page = [json.loads(s) for s in path.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
            if type(reply.get('has_more')) is not bool or type(reply.get('records_count')) is not int:
                raise RuntimeError(f'{table}: incomplete pagination metadata; no full-read assertion')
            if reply['records_count'] != len(page):
                raise RuntimeError(f'{table}: page count differs from archived records')
            for row in page:
                record_id = row.get('record_id')
                if not isinstance(record_id, str) or not record_id or record_id in seen:
                    raise RuntimeError(f'{table}: missing or repeated record ID during pagination; read again before mutation')
                seen.add(record_id)
            rows.extend(page)
            if not reply['has_more']:
                if getattr(self,'presentation_enabled',False):
                    try:
                        from .presentation import from_remote
                    except ImportError:
                        from presentation import from_remote
                    rows=[from_remote(table,r,self._fields(table)) for r in rows]
                    self._record_cache[table]={r['record_id']:r for r in rows}
                    if table=='项目总表':
                        self._project_titles={r.get('作品唯一键'):r.get('书名') for r in rows if r.get('作品唯一键')}
                return rows
            if not page:
                raise RuntimeError(f'{table}: pagination did not advance; no full-read assertion')
        raise RuntimeError(f'{table}: pagination exceeded 100 pages; narrow predicate before continuing')

    def ensure(self, table, key, rows, conditions=None, update=False):
        current = self.read(table, conditions)
        context_index={r.get(key):r for r in current}
        rows=[self._present(table,r,context_index.get(r.get(key))) for r in rows]
        self._ensure_display_options(table,rows)
        fields = {f['name']: f for f in self.manifest['tables'][table]['field_readback']['fields']}
        for row in rows:
            for name, value in row.items():
                if name not in fields:
                    raise ValueError(f'{table}: unknown field {name}')
                field = fields[name]
                if field['type'] in ('lookup','formula','created_at','updated_at','auto_number'):
                    raise ValueError(f'{table}: readonly field {name}')
                if field['type'] == 'select' and value is not None:
                    options = {o['name'] for o in field['options']}
                    if not isinstance(value,list) or not set(value) <= options:
                        raise ValueError(f'{table}: unknown option {name}={value}')
        index = {}
        for r in current:
            k = r.get(key)
            if table == '项目总表' and key == '作品唯一键' and not k:
                continue  # Unclassified manual rows remain untouched, not duplicates.
            if k in index:
                raise RuntimeError(f'{table}: duplicate key {k}')
            index[k] = r
        incoming = [r[key] for r in rows]
        if len(incoming) != len(set(incoming)):
            raise RuntimeError(f'{table}: duplicate input keys')
        new = [r for r in rows if r[key] not in index]
        updates = {index[r[key]]['record_id']: r for r in rows if r[key] in index and update
                   and any(not same(v, self._present(table,index[r[key]]).get(k)) for k, v in r.items())}
        for i in range(0, len(new), 200):
            self.call('+record-batch-create', table, {'create_records': new[i:i+200]})
        items = list(updates.items())
        for i in range(0, len(items), 200):
            self.call('+record-batch-update', table, {'update_records': dict(items[i:i+200])})
        if new or updates:
            # Acknowledged creates can briefly be absent from filtered lists.
            # Re-read only: never replay a mutation or accept ACK as readback.
            for attempt in range(3):
                verified = self.read(table, conditions)
                keyed = [r for r in verified if not (table == '项目总表' and key == '作品唯一键' and not r.get(key))]
                index = {r[key]: r for r in keyed}
                if len(index) != len(keyed):
                    raise RuntimeError(f'{table}: duplicate keys on readback')
                problem = None
                for r in rows:
                    if r[key] not in index:
                        problem = f'{table}: missing readback {r[key]}'
                        break
                    if (r in new or update) and any(not same(v, self._present(table,index[r[key]]).get(k)) for k,v in r.items()):
                        problem = f'{table}: readback differs for {r[key]}'
                        break
                if problem is None:
                    break
                if attempt == 2:
                    raise RuntimeError(problem)
                time.sleep(attempt + 1)
        return index


def same(a, b):
    if a == b:
        return True
    if type(a) is float and type(b) in (int, float) and math.isclose(a, b, rel_tol=0, abs_tol=1e-9):
        return True
    if isinstance(a, str) and isinstance(b, str) and len(a) == 16 and a[4] == '-' and a[10] == ' ':
        return a.replace(' ', 'T') == b[:16]
    if isinstance(a,str) and isinstance(b,str) and a.startswith(('https://','http://')):
        match = re.fullmatch(r'\[[^\]]*\]\((https?://[^\s]+)\)', b)
        if match:
            return a == match.group(1)
        normalized = re.sub(r'\[[^\]\n]*\]\((https?://[^\s]+)\)', r'\1', b)
        if a == normalized:
            return True
    if isinstance(a, list) and isinstance(b, list):
        def norm(v):
            return sorted(str(x.get('id')) if isinstance(x,dict) else str(x) for x in v)
        return norm(a) == norm(b)
    return False
