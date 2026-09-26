# scripts/smoke-workflow.py
# 冒烟测试的工作流部分：验证「开始执行有节点进度事件」+「确认后结果是流式输出」。
# 单独放一个文件而不是塞进 shell 的 heredoc：heredoc 里的 $ 和引号太容易和 shell 打架。
import json
import re
import urllib.request

API = 'http://localhost:3000'
H = {'Content-Type': 'application/json', 'X-Tenant-Id': 'tenant-demo',
     'X-User-Id': 'u-tech-01', 'X-User-Departments': 'tech', 'X-User-Clearance': 'internal'}
SEP = chr(10) + chr(10)
NL = chr(10)


def sse(path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), headers=H)
    events = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        buf = ''
        for raw in resp:
            buf += raw.decode('utf-8')
            while SEP in buf:
                block, buf = buf.split(SEP, 1)
                ev = (re.search(r'^event: (.+)$', block, re.M) or [None, '?'])[1]
                data = {}
                for one in block.splitlines():
                    if one.startswith('data: '):
                        try:
                            data = json.loads(one[6:])
                        except Exception:
                            pass
                events.append((ev, data))
    return events


def say(ok, text):
    print(('  ✅ ' if ok else '  ❌ ') + text)


ev1 = sse('/api/workflow/start/stream', {
    'workflowId': 'weekly_report',
    'input': {'points': '完成知识库重构；修复三个线上缺陷；评审两次需求', 'dept': '前端研发组'}})
node_starts = [d for e, d in ev1 if e == 'node_start']
tokens1 = [d for e, d in ev1 if e == 'token']
paused = [d for e, d in ev1 if e == 'paused']
say(len(node_starts) >= 2, f'开始执行有节点进度事件（{len(node_starts)} 个节点开始）')
say(len(tokens1) > 0, f'中间节点也有流式 token（{len(tokens1)} 个，贴在流程图卡片上）')
say(all(not d.get('isResult') for d in tokens1), '中间节点的 token 不会混进正文')

if not paused:
    say(False, '工作流没有按预期暂停在人工审核')
else:
    ev2 = sse('/api/workflow/resume/stream',
              {'threadId': paused[0]['threadId'], 'feedback': '风险部分写具体一点'})
    tokens2 = [d for e, d in ev2 if e == 'token']
    done = [d for e, d in ev2 if e == 'completed']
    say(len(tokens2) > 5, f'确认后正文是流式的（{len(tokens2)} 个 token）')
    say(bool(tokens2) and all(d.get('isResult') for d in tokens2), '正文 token 全部标记 isResult')
    say(bool(done) and len(done[0].get('result', '')) > 0, '最终结果非空')
