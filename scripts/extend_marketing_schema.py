"""Add source lineage fields to this task's new Base, preserving existing fields."""
import json
import subprocess
from feishu_io import resolve_cli
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = None  # Resolved only when this explicit maintenance command is run.


def main():
    global CLI
    CLI = resolve_cli()
    manifest_path = ROOT / 'config/base-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    directory = ROOT / 'receipts/marketing-schema'
    directory.mkdir(parents=True, exist_ok=True)
    definitions = {
        '营销服务明细': [
            {'name': name, 'type': 'text'} for name in
            ['原始书名', '服务平台', '源作品ID', '服务对象', '金额口径', '校验说明', '源行指纹']
        ] + [{'name':'单价（元）', 'type':'number', 'style':{'type':'plain','precision':2}},
             {'name':'源行号','type':'number','style':{'type':'plain','precision':0}}],
        '项目总表': [{'name':'作品形式','type':'text'}, {'name':'平台匹配状态','type':'text'}] + [
            {'name':name,'type':'lookup','from':'营销服务明细','select':field,'aggregate':'sum',
             'where':{'logic':'and','conditions':[['作品唯一键','==',{'type':'field_ref','field':'作品唯一键'}]]}}
            for name,field in [('源表服务费合计','原始行总金额'),('源表邀评人次','邀评人数')]
        ],
    }
    for name, fields in definitions.items():
        table = manifest['tables'][name]
        prefix = [CLI, 'base']
        common = ['--base-token',manifest['base']['base_token'],'--table-id',table['id'],'--as','user']
        def call(command, extras, label):
            proc = subprocess.run(prefix + [command] + common + extras, cwd=ROOT,
                                  capture_output=True, encoding='utf-8', timeout=120)
            (directory / (label+'.json')).write_text(proc.stdout,encoding='utf-8')
            if proc.returncode:
                raise RuntimeError(proc.stderr + proc.stdout)
            reply = json.loads(proc.stdout)
            if reply.get('ok') is False:
                raise RuntimeError(str(reply))
            return reply
        current = call('+field-list', [], name+'-before')['data']
        names = {f['name'] for f in current['fields']}
        missing = [f for f in fields if f['name'] not in names]
        if missing:
            request = directory / (name+'-request.json')
            request.write_text(json.dumps(missing,ensure_ascii=False,indent=2),encoding='utf-8')
            call('+field-create', ['--json','@'+str(request.relative_to(ROOT)), '--i-have-read-guide'], name+'-create')
        after = call('+field-list', [], name+'-after')['data']
        if not {f['name'] for f in fields} <= {f['name'] for f in after['fields']}:
            raise RuntimeError('Schema readback failed')
        table['field_readback'] = after
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        print(name, 'added', len(missing))


if __name__ == '__main__':
    main()
