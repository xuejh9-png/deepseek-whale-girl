"""Issue #1 contract tests; all fixtures are synthetic and isolated."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

import usage_stats as U
import pet_sources as S
import pet_daemon as D
import claude_cost as C


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')


def counts(n, out=0, cached=0):
    return {'input_tokens': n, 'output_tokens': out, 'cached_input_tokens': cached,
            'total_tokens': n + out}


def event(ts, total, turn='t1', rid=None, delta=None):
    if rid:
        return {'type': 'token_usage_record', 'timestamp': ts, 'payload': {
            'thread_id': 's1', 'turn_id': turn, 'response_id': rid,
            'usage': counts(total if delta is None else delta), 'thread_token_usage': counts(total)}}
    return {'type': 'event_msg', 'timestamp': ts, 'payload': {
        'type': 'token_count', 'info': {'total_token_usage': counts(total)}}}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(U, 'PROJECTS_DIR', str(self.root/'wb')),
                        patch.object(U, 'CODEX_SESSIONS', str(self.root/'codex')),
                        patch.object(U, 'CODEX_INDEX', str(self.root/'index')),
                        patch.object(U, 'CLAUDE_PROJECTS', str(self.root/'claude'))]
        for p in self.patches: p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches: self.addCleanup(p.stop)

    def codex(self, rows, name='a'):
        head = [{'type':'session_meta','payload':{'id':'s1'}},
                {'type':'turn_context','payload':{'turn_id':'t1','model':'gpt-test'}}]
        write(self.root/'codex/2026/10/25'/f'{name}.jsonl',head + rows)

    def claude(self, entries, name='a'):
        write(self.root/'claude/project'/f'{name}.jsonl',entries)

    def test_codex_cumulative_dual_events_replay_and_reset(self):
        rows=[event('2026-10-25T10:00:00Z',10,rid='r1'),event('2026-10-25T10:00:01Z',10),
              event('2026-10-25T10:00:02Z',30,rid='r2',delta=20),event('2026-10-25T10:00:03Z',30),
              {'type':'turn_context','payload':{'turn_id':'t2'}},
              event('2026-10-25T10:00:04Z',5,'t2',rid='r3'),event('2026-10-25T10:00:05Z',5)]
        self.codex(rows);self.codex(rows,'replay')
        self.assertEqual(sum(r['total'] for r in U.iter_codex_calls()),35)

    def test_codex_snapshot_only_midnight_offset(self):
        self.codex([event('2026-10-25T23:59:59+08:00',100),event('2026-10-26T00:00:01+08:00',130)])
        today=U.today_usage(dt.datetime.fromisoformat('2026-10-26T12:00:00+08:00'))
        self.assertEqual(today['totalTokens'],30)
        self.assertEqual(today['dayLocal'],'2026-10-26')
        self.assertEqual(today['coverage']['claude'],'missing')
        self.assertFalse(today['complete'])

    def test_codex_no_cumulative_id_dedup_and_then_snapshot(self):
        r={'type':'token_usage_record','timestamp':'2026-10-25T10:00:00Z',
           'payload':{'response_id':'r1','thread_id':'s1','usage':counts(10,2,5)}}
        self.codex([r,r,event('2026-10-25T10:00:01Z',30)])
        # A different cumulative ledger must never override the response ledger.
        self.assertEqual(sum(x['total'] for x in U.iter_codex_calls()),12)

    def test_codex_distinct_ledger_offsets_never_readd_history(self):
        # Real-world failure: response cumulative=30,962,428, snapshot=30,557,726.
        # Interleaving these looked like repeated resets and inflated totals 200x.
        self.codex([event('2026-10-25T10:00:00Z',30962428,rid='r1',delta=100),
                    event('2026-10-25T10:00:01Z',30557726),
                    event('2026-10-25T10:00:02Z',31013528,rid='r2',delta=51100),
                    event('2026-10-25T10:00:03Z',30608826)])
        self.assertEqual(sum(r['total'] for r in U.iter_codex_calls()),51200)

    def test_codex_uncovered_turn_uses_only_snapshot_difference(self):
        self.codex([event('2026-10-25T10:00:00Z',110,rid='r1',delta=10),
                    event('2026-10-25T10:00:01Z',100),
                    {'type':'turn_context','payload':{'turn_id':'t2'}},
                    event('2026-10-25T10:00:02Z',130)])
        self.assertEqual(sum(r['total'] for r in U.iter_codex_calls()),40)

    def test_claude_stream_cache_and_synthetic_filter(self):
        base={'type':'assistant','timestamp':'2026-10-26T01:00:00Z','sessionId':'s',
              'message':{'id':'m','model':'claude-sonnet-4','usage':{
                  'input_tokens':10,'output_tokens':2,'cache_creation_input_tokens':20,
                  'cache_read_input_tokens':30,'total_tokens':62}}}
        later=copy.deepcopy(base);later['message']['usage']['output_tokens']=8
        other=copy.deepcopy(base);other['message']['model']='gpt-test'
        fake=copy.deepcopy(base);fake['message']['model']='<synthetic>'
        self.claude([base,later,other,fake]);self.claude([base],'replay')
        data=list(U.iter_claude_calls());self.assertEqual(len(data),1)
        self.assertEqual(data[0]['total'],68);self.assertEqual(data[0]['cached'],30)

    def test_claude_no_trustworthy_identity_is_not_zero(self):
        self.claude([{'type':'assistant','timestamp':'2026-10-26T01:00:00Z',
                      'message':{'model':'claude-sonnet-4','usage':{'input_tokens':10}}}])
        t=U.today_usage();self.assertEqual(t['coverage']['claude'],'no_trusted_usage')
        self.assertIsNone(t['bySource']['claude']['totalTokens'])

    def test_workbuddy_existing_dedup_all_models(self):
        ts=int(dt.datetime.now().timestamp()*1000)-1000
        a={'timestamp':ts,'providerData':{'requestModelName':'DeepSeek-test',
                                        'rawUsage':{'prompt_tokens':100,'completion_tokens':3,'total_tokens':103}}}
        b=copy.deepcopy(a);b['providerData']['requestModelName']='other-model'
        write(self.root/'wb/proj/a.jsonl',[a,a,b])
        self.assertEqual(U.today_usage()['bySource']['workbuddy']['totalTokens'],206)

    def test_all_three_sources_sum(self):
        ts='2026-10-26T01:00:00Z';now=dt.datetime.fromisoformat('2026-10-26T12:00:00+08:00')
        self.codex([event(ts,20)])
        write(self.root/'wb/p/a.jsonl',[{'timestamp':U._iso_ms(ts),'providerData':{
            'requestModelName':'deepseek-test','rawUsage':{'prompt_tokens':10}}}])
        self.claude([{'type':'assistant','timestamp':ts,'message':{'id':'m','model':'claude-opus-4',
                     'usage':{'input_tokens':5,'output_tokens':2,'cache_read_input_tokens':3}}}])
        today=U.today_usage(now);self.assertEqual(today['totalTokens'],40);self.assertTrue(today['complete'])

    def test_report_removed_heatmap_keeps_chart_and_safe_inline_json(self):
        record={'ts':U._iso_ms('2026-10-26T01:00:00Z'),'when':dt.datetime(2026,10,26),
                'total':10,'prompt':10,'completion':0,'cached':0,'model':'x','source':U.SRC_CLAUDE,
                'project':'</script><script>bad()</script>','session':'s'}
        text=U.build_html(U.aggregate([record]),'test',{},[record])
        self.assertNotIn('class="heat"',text);self.assertNotIn('.heat{',text)
        self.assertIn('Token 活动（按天）',text);self.assertIn('regenBtn',text)
        self.assertNotIn('</script><script>bad()',text)


class BillingTests(unittest.TestCase):
    def bill(self):
        rows=[{'requestId':str(i),'timestamp':'2026-10-26T01:00:00Z','model':'claude-sonnet-4',
               'currency':'USD','billedAmount':'0.10'} for i in range(5)]
        rows += [{'requestId':'other','timestamp':'2026-10-26T01:00:00Z','model':'gpt-test',
                  'currency':'USD','billedAmount':'5.00'}]
        rows += [copy.deepcopy(rows[0])]
        return {'schemaVersion':1,'coverageStart':'2026-01-01T00:00:00+08:00',
                'coverageEnd':'2026-10-26T10:00:00+08:00','exportedAt':'2026-10-26T10:01:00+08:00',
                'fullHistory':True,'billedTotals':{'USD':'5.50'},'entries':rows}

    def snap(self,b):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bill.json';p.write_text(json.dumps(b), encoding='utf-8');return C.snapshot(str(p))

    def test_claude_only_and_dedup(self):
        s=self.snap(self.bill());self.assertEqual(s['amount'],0.5);self.assertTrue(s['verified'])

    def test_code_spend_splits_cache_components_and_falls_back_by_family(self):
        rows=[{'model':'claude-opus-5-5','input':1000000,'completion':1000000,
               'cacheRead':1000000,'cacheCreation':1000000,'total':4000000,
               'when':dt.datetime(2026,10,26,9,0)},
              {'model':'claude-opus-9-9','input':0,'completion':0,'cacheRead':0,
               'cacheCreation':1000000,'total':1000000,'when':dt.datetime(2026,10,22,9,0)}]
        with patch.object(C,'iter_claude_calls',return_value=iter(rows)), \
             patch.object(C,'price_table',return_value=C.FALLBACK_PRICES), \
             patch.object(C,'provider_snapshot',return_value=None), \
             patch.object(C,'snapshot',return_value={'amount':None,'verified':False}):
            got=C.spend_snapshot(now=dt.datetime(2026,10,26,12,0))
        # 4 + 20 + 0.2 + 5（四条分量各 1M），外加未知小版本按 opus 家族兜底 5。
        self.assertAlmostEqual(got['amount'],34.2,places=4)
        self.assertAlmostEqual(float(got['today']['USD']),29.2,places=4)
        self.assertEqual((got['requests'],got['todayRequests']),(2,1))
        self.assertTrue(got['verified'])

    def test_missing_partial_and_invalid_totals_are_unknown(self):
        self.assertIsNone(C.snapshot('/not/a/ledger')['amount'])
        b=self.bill();b['fullHistory']=False;self.assertIsNone(self.snap(b)['amount'])
        b=self.bill();b['billedTotals']['USD']='100';self.assertIsNone(self.snap(b)['amount'])

    def test_multi_currency_not_added(self):
        b=self.bill();b['entries'].append({'requestId':'eur','timestamp':'2026-10-26T01:00:00Z',
            'model':'claude-haiku-4','currency':'EUR','billedAmount':'0.2'});b['billedTotals']['EUR']='0.2'
        s=self.snap(b);self.assertIsNone(s['amount']);self.assertEqual(s['byCurrency'],{'USD':'0.50','EUR':'0.2'})

    def test_adjustments_failed_charges_and_retries(self):
        b=self.bill();b['entries'][0]['status']='failed';b['entries'][-1]['status']='failed'
        b['entries'] += [{'entryId':'refund','kind':'adjustment','timestamp':'2026-10-26T01:00:00Z',
                         'model':'claude-sonnet-4','currency':'USD','billedAmount':'-0.05'}]
        b['billedTotals']['USD']='5.45';self.assertEqual(self.snap(b)['amount'],0.45)

    def test_conflicting_replay_rejected(self):
        b=self.bill();b['entries'][-1]['billedAmount']='0.20'
        self.assertIsNone(self.snap(b)['amount'])

    def test_credit_usage_does_not_become_usd(self):
        b=self.bill();del b['entries'][0]['billedAmount'];b['entries'][0]['credit_usage']=0.1
        self.assertIsNone(self.snap(b)['amount'])

    def test_import_permissions_secrets_removed_and_export_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=self.bill();b['apiKey']='secret';b['entries'][0]['body']='secret'
            src=Path(tmp)/'raw.json';dst=Path(tmp)/'private/ledger.json';out=Path(tmp)/'export.json'
            src.write_text(json.dumps(b), encoding='utf-8');C.import_ledger(str(src),str(dst));C.import_ledger(str(dst),str(out))
            # Windows 不保留 POSIX 权限位（st_mode 恒为 0o666 附近），这条断言只在 POSIX 上成立。
            # 真正要守住的是"文件在私有目录里、密钥被剥离"，权限位只是 POSIX 上的额外保证。
            if os.name != 'nt':
                self.assertEqual(dst.stat().st_mode & 0o777,0o600)
            self.assertNotIn('secret',dst.read_text(encoding='utf-8'));self.assertEqual(C.snapshot(str(out))['amount'],0.5)


class TaskTests(unittest.TestCase):
    def test_three_tasks_completion_expiration_missing_rename_and_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);now=1792970000000
            with patch.object(S,'PROJECTS_DIR',str(root/'p')),patch.object(S,'SESSIONS_DIR',str(root/'s')),patch.object(S,'now_ms',return_value=now):
                (root/'s').mkdir(parents=True,exist_ok=True)
                for sid,kind,age in [('a','function_call',1000),('b','reasoning',1000),('c','message',30000)]:
                    write(root/'p/demo'/f'{sid}.jsonl',[{'timestamp':now-35000,'type':'message','role':'user','content':'任务文字 '+sid},
                                                      {'timestamp':now-age,'type':kind,'role':'assistant'}])
                    (root/'s'/f'{sid}.json').write_text(json.dumps({'sessionId':sid,'lastHeartbeat':now-1000,'cwd':'/demo/'+sid}), encoding='utf-8')
                source=S.WorkBuddySource();self.assertEqual(source.snapshot()['state'],'working')
                tasks=source.tasks;self.assertEqual([t['state'] for t in tasks],['working','thinking','waiting'])
                self.assertEqual([t['task'] for t in tasks],['任务文字 a','任务文字 b','任务文字 c'])
                write(root/'p/demo/a.jsonl',[{'timestamp':now-1000,'type':'message','role':'assistant'}])
                self.assertEqual(source.snapshot()['state'],'thinking');self.assertEqual(len(source.tasks),3)
                (root/'p/demo/b.jsonl').rename(root/'p/demo/renamed.jsonl')
                self.assertEqual(len(source.tasks_snapshot()),2)
                (root/'p/demo/renamed.jsonl').rename(root/'p/demo/b.jsonl')
                self.assertEqual([t['id'] for t in source.tasks_snapshot()],[t['id'] for t in tasks])
                (root/'p/demo/a.jsonl').unlink();self.assertEqual(len(source.tasks_snapshot()),2)
                with patch.object(S,'now_ms',return_value=now+61000):self.assertEqual(source.snapshot()['state'],'offline')


class ApiTests(unittest.TestCase):
    def test_cache_midnight_and_no_workbuddy(self):
        D._usage_cache.update(ts=0,data=None,day=None)
        with patch.object(D,'compute_usage',return_value={'today':{'totalTokens':42}}) as calc:
            self.assertEqual(D.build_usage()['today']['totalTokens'],42);D.build_usage();self.assertEqual(calc.call_count,1)
            D._usage_cache['day']='1900-01-01';D.build_usage();self.assertEqual(calc.call_count,2)
        D._usage_cache.update(ts=0,data=None,day=None)

    def test_loopback_origin_and_static_boundaries(self):
        server=D.Server(('127.0.0.1',0),D.PetHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:%d'%server.server_address[1]
        try:
            with patch.object(D.COST,'spend_snapshot',return_value={'amount':None,'verified':False}):
                with urllib.request.urlopen(url+'/claude-cost') as r:
                    self.assertIsNone(json.load(r)['amount']);self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'))
                for headers in [{'Origin':'https://evil.test'},{'Origin':'null'},{'Host':'evil.test'}]:
                    with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(url+'/claude-cost',headers=headers))
                    self.assertEqual(e.exception.code,403)
                for path in ['/.git/config','/README.md','/web/../claude_cost.py']:
                    with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url+path)
                    self.assertEqual(e.exception.code,404)
        finally:server.shutdown();server.server_close();thread.join()

    def test_regenerate_uses_current_renderer_and_preserves_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=str(Path(tmp)/'report.html')
            with patch.object(D.importlib,'reload',return_value=U),patch.object(U,'OUT_HTML',out), \
                 patch.object(U.os.path,'isdir',return_value=True),patch.object(U,'iter_all_calls',return_value=iter([])), \
                 patch.object(U,'load_session_meta',return_value={}):
                result=D.regenerate_report()
                self.assertTrue(result['ok'])
                report=Path(out).read_text(encoding='utf-8')
                self.assertIn('Token 活动（按天）',report)
                self.assertNotIn('class="heat"',report)
                self.assertFalse(Path(out+'.tmp').exists())

    def test_old_agent_and_usage_fields_remain(self):
        with patch.object(D,'_source',S.NullSource()):
            state=D.build_state();self.assertEqual(state['agent']['state'],'idle');self.assertEqual(state['tasks'],[])
        with patch.object(U,'iter_calls',return_value=iter([])),patch.object(U,'today_usage',return_value={'totalTokens':1}):
            result=D.compute_usage();self.assertIn('inputTokens',result);self.assertIn('byModel',result);self.assertIn('today',result)


if __name__=='__main__': unittest.main()
