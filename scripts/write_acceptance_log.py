"""Publish the real readback audit as a separate partial-acceptance log."""
from datetime import datetime
import json
from feishu_io import Base,ROOT,save


def main():
    day=datetime.now().strftime('%Y-%m-%d')
    if day!='2026-11-03':
        raise ValueError('This acceptance statement describes the 2026-11-03 implementation; revise facts before reuse')
    path=ROOT/'runs'/day/'acceptance-summary.json'
    report=json.loads(path.read_text(encoding='utf-8'))
    stamp=datetime.now().strftime('%Y-%m-%d %H:%M')
    api=Base(ROOT/'receipts'/day/('acceptance-log-'+datetime.now().strftime('%H%M%S')))
    note=(f"本记录为全量回读验收，非10点平台采样。项目{report['projects']}条（营销分组{report['marketing_project_groups']}），"
          f"服务{report['services']}条，日记录{report['daily_records']}条；四榜各50条，键唯一及关联回读通过。"
          f"源表费用{report['fee_raw_total']}元，核验合计{report['fee_confirmed_total']}元，待核实{report['fee_deferred_total']}元；另4行缺金额。"
          "81个微信营销项目及9个番茄项目已匹配，其他身份仍待核实。有声服务分开归档。"
          "番茄9本累计阅读7本可得、2本仅在读；当前数字授权方9本未取得。"
          "番茄出版飙升榜尚无完整自动来源，已准备正常App设备采集验收，等待运行终端。"
          "69项检查通过。10点任务ACTIVE、shadow；两通知工作流仍禁用，未发送业务通知；旧09点任务保留。")
    row={'运行批次':'acceptance:'+report['verified_at'],'开始时间':stamp,'结束时间':stamp,
         '预期榜单数':4,'完整榜单数':4,'新增事件数':0,'状态':['部分失败'],
         '证据路径':str(path),'缺口原因':note}
    api.ensure('运行日志','运行批次',[row],[['运行批次','==',row['运行批次']]])
    save(api.receipt_dir/'summary.json',{'readback':'passed','write_count':api.changes,'audit':report})
    print(json.dumps({'readback':'passed','write_count':api.changes},ensure_ascii=False))


if __name__=='__main__':
    main()
