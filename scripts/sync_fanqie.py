"""Persist verified Fanqie observations for existing registered projects only."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from feishu_io import Base, ROOT, save
from work_copyright import apply_work_copyright
from presentation import short_copyright


def minute(value):
    return value[:16].replace('T',' ')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    args=parser.parse_args()
    source=Path(args.source).resolve()
    data=json.loads(source.read_text(encoding='utf-8'))
    if data.get('schema_version')!='fanqie.public.v1' or not data.get('finished_at'):
        raise ValueError('Not a finished Fanqie collector snapshot')
    day=data['collection_date']
    api=Base(ROOT/'receipts'/day/('fanqie-'+datetime.now().strftime('%H%M%S-%f')))
    projects=api.read('项目总表')
    by_id={}
    for project in projects:
        if ('番茄小说' in project.get('平台',[]) and project.get('平台作品ID')
                and project.get('作品唯一键') == 'fanqie:' + str(project['平台作品ID'])):
            bid=str(project['平台作品ID'])
            if bid in by_id:
                raise ValueError('Duplicate Fanqie platform ID in project table')
            by_id[bid]=project
    updates=[]
    daily=[]
    skipped=[]
    batch='fanqie:'+data['started_at']
    for book in data['books']:
        bid=str(book['book_id'])
        project=by_id.get(bid)
        if not project:
            skipped.append({'book_id':bid,'reason':'no_registered_project'})
            continue
        book=apply_work_copyright(book,'营销项目' in project.get('监测范围',[]))
        if book.get('identity_status')!='book_id_and_title_verified':
            raise ValueError('Unverified Fanqie identity: '+bid)
        key=project['作品唯一键']
        metric=book.get('cumulative_readers_metric',{})
        observed=book['detail_source']['observed_at']
        status='部分缺失' if book.get('missing_fields') else '成功'
        update={'作品唯一键':key,'作者':book.get('author'),'字数':book.get('word_count'),
                        '版权方原文':book.get('platform_copyright_provider',book.get('copyright_provider')),
                        '版权方':[short_copyright(book['copyright_provider'])] if book.get('copyright_provider') else [],
                        '作品分类':book.get('category'),
                        '最新点评人数':book.get('reviewer_count'),'最新书评数量':book.get('book_review_count'),
                        '最新累计阅读人数':book.get('cumulative_readers'),'最新采集时间':minute(observed),
                        '采集状态':[status], '指标精度说明':metric.get('raw','')+'；精度见每日原文，约数以一个显示单位缓冲判定。'}
        if not book.get('category'):
            update.pop('作品分类')  # Retain evidenced work-level fallback classification.
        if '基础资料来源' in api._fields('项目总表'):
            update['基础资料来源']=('作者、字数：番茄官方网页；分类：'
                + ('番茄官方网页' if book.get('category') else '沿用已核实作品资料，番茄来源本次未返回分类'))
        previous_observed=project.get('最新采集时间')
        if not previous_observed or minute(observed)>=minute(previous_observed):
            updates.append(update)
        daily.append({
            '日数据唯一键':day+':'+key,'作品唯一键':key,'关联项目':[{'id':project['record_id']}],
            '平台':['番茄小说'],'数据日期':day+' 00:00','采集时间':minute(observed),
            '点评人数':book.get('reviewer_count'),'书评数量':book.get('book_review_count'),
            '累计阅读人数':book.get('cumulative_readers'),'字数':book.get('word_count'),
            '版权方原文':book.get('platform_copyright_provider',book.get('copyright_provider')),
            '版权方':[short_copyright(book['copyright_provider'])] if book.get('copyright_provider') else [],
            '作品分类':book.get('category'),
            '采集状态':[status], '精度':[{'exact':'精确','approximate':'区间或约数'}.get(metric.get('precision'),'不可得')],
            '采集批次':batch,'数据来源':book.get('detail_url'),'原始证据定位':book['detail_source']['archive_path'],
            '缺失字段与原因':'；'.join({'copyright_provider':'版权方：当前采集来源未返回，尚待核实',
                'author':'作者：平台未提供','word_count':'字数：平台未提供',
                'reviewer_count':'点评人数：未取得有效值','book_review_count':'书评数量：未取得有效值',
                'cumulative_readers':'累计阅读人数：未取得标明人读过的有效值'}.get(k,k)
                for k in book.get('missing_fields',[])) or None,
            '指标原文':json.dumps({'cumulative_readers_metric':metric,'reviewer_count':book.get('reviewer_count'),
                                 'copyright_basis':book.get('copyright_basis'),
                                 'platform_copyright_provider':book.get('platform_copyright_provider',book.get('copyright_provider')),
                                 'book_review_count':book.get('book_review_count'),'mapping':data['field_mapping']},ensure_ascii=False),
        })
    api.ensure('项目总表','作品唯一键',updates,update=True)
    from daily_observation import select_daily_observations
    daily_filter=[['数据日期','==',f'ExactDate({day})']]
    daily=select_daily_observations(daily,api.read('每日数据',daily_filter))
    api.ensure('每日数据','日数据唯一键',daily,daily_filter,update=True)
    log={'运行批次':batch,'计划时间':day+' 10:00','开始时间':minute(data['started_at']),
         '结束时间':minute(data['finished_at']),'预期作品数':len(data['books']),
         '成功作品数':sum(r['采集状态']==['成功'] for r in daily),
         '失败作品数':len(data.get('errors',[])),'新增事件数':0,
         '状态':['部分失败' if skipped or data.get('errors') or any(b.get('missing_fields') for b in data['books']) else '成功'],
         '证据路径':str(source),'缺口原因':'本日志仅记录番茄作品指标同步；出版飙升榜状态单独核验。'+json.dumps(
             {'skipped':skipped,'missing':{b['book_id']:b.get('missing_fields') for b in data['books'] if b.get('missing_fields')}},ensure_ascii=False)}
    api.ensure('运行日志','运行批次',[log],[['运行批次','==',batch]],update=True)
    summary={'source':str(source),'daily_records':len(daily),'skipped':skipped,'write_count':api.changes,
             'readback':'passed','business_notifications_sent':0,'receipt_dir':str(api.receipt_dir)}
    save(api.receipt_dir/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
