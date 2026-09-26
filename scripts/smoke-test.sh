#!/usr/bin/env bash
# scripts/smoke-test.sh
# 端到端冒烟测试：对着**正在运行的服务**跑一遍关键链路，验证真实行为而不是函数返回值。
#
# 为什么需要它：单元测试覆盖不到「路由里引用了一个不存在的变量」「前端某个状态被覆盖」这类错误 ——
# 实测踩过两次：对话结束时残留旧代码 history.append(...) 每轮抛 NameError（回答照样出来，但那轮没进记忆）；
# 工作流 startWorkflow 先置 running=true 又 reset() 把它置回 false（点开始执行后界面停在输入表单上）。
# 单测全绿，用户一用就发现。
#
# 用法：bash scripts/smoke-test.sh   （需要后端已在 localhost:3000 运行）
set -uo pipefail

API="${API_BASE:-http://localhost:3000}"
HDR=(-H "Content-Type: application/json" -H "X-Tenant-Id: tenant-demo" \
     -H "X-User-Id: u-tech-01" -H "X-User-Departments: tech" -H "X-User-Clearance: internal")

pass=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }
check(){ if printf %s "$2" | grep -q "$3"; then ok "$1"; else bad "$1（未匹配：$3）"; fi; }
nocheck(){ if printf %s "$2" | grep -q "$3"; then bad "$1（不该出现：$3）"; else ok "$1"; fi; }

_collect() {  # stdin 是 SSE 流，输出拼接后的 token 文本
  python3 -c "
import json,sys
t=sys.stdin.read(); out=''
for b in t.split(chr(10)+chr(10)):
    if b.startswith('event: token'):
        try: out+=json.loads(b.split(chr(10))[1][6:])['token']
        except Exception: pass
print(out)"
}

chat() {  # $1=消息 $2=会话id $3=可选 useKnowledge(true/false)
  local extra=""
  [ -n "${3:-}" ] && extra=",\"useKnowledge\":$3"
  curl -s -N --max-time 120 "$API/api/chat/stream" "${HDR[@]}" \
    -d "{\"message\":\"$1\",\"sessionId\":\"$2\",\"userId\":\"u-tech-01\"$extra}" | _collect
}

agent() {  # $1=任务 $2=会话id
  curl -s -N --max-time 300 "$API/api/agent/run" "${HDR[@]}" \
    -d "{\"task\":\"$1\",\"sessionId\":\"$2\"}" | _collect
}

echo "接口：$API"

echo '[1/5] 对话：多轮记忆'
S="smoke-mem-$$"
chat "我叫李雷，在技术部做前端开发" "$S" > /dev/null
r=$(chat "我叫什么名字？在哪个部门？" "$S")
check "记得上一轮的自我介绍" "$r" "李雷"
check "记得部门" "$r" "技术部"

echo '[2/5] 对话：超过 10 轮触发摘要后仍记得早期信息'
S2="smoke-sum-$$"
chat "请记住：我的工号是 WM-7788" "$S2" > /dev/null
for i in 1 2 3 4 5 6 7 8 9 10 11; do chat "好的，收到$i" "$S2" > /dev/null; done
sleep 4   # 等异步摘要跑完
sum=$(curl -s "$API/api/chat/sessions" | python3 -c "import json,sys;print(any(s['id']=='$S2' and s['hasSummary'] for s in json.load(sys.stdin)['sessions']))")
if [ "$sum" = "True" ]; then ok "超过 10 轮已生成摘要"; else bad "未生成摘要（hasSummary=$sum）"; fi
r=$(chat "我的工号是多少？" "$S2")
check "摘要里仍保留早期信息" "$r" "WM-7788"

echo '[3/5] 知识库三态语义'
Q="帮我分析一下《三体》这部小说的人物关系"
r=$(chat "$Q" "smoke-auto-$$")
check "自动：库中没有时先说明未命中" "$r" "知识库未命中"
if [ "$(printf %s "$r" | wc -c)" -gt 150 ]; then ok "自动：说明之后仍然给出回答"; else bad "自动：只回了说明、没有正文"; fi
r=$(chat "$Q" "smoke-force-$$" true)
check "强制：查不到就说查不到" "$r" "知识库中未找到相关内容"
nocheck "强制：不出现通用知识回答的提示" "$r" "知识库未命中"
r=$(chat "$Q" "smoke-off-$$" false)
nocheck "关闭：不出现任何未命中话术" "$r" "知识库中未找到"
nocheck "关闭：也不出现通用知识提示" "$r" "知识库未命中"

echo '[4/5] Agent：跨任务记忆'
SA="smoke-agent-$$"
agent "我出差3天，酒店每晚580元，机票往返1200元，餐费每天150元，帮我算一下总报销金额" "$SA" > /dev/null
r=$(agent "刚才那次出差，如果酒店换成每晚700元，总报销金额变成多少？" "$SA")
check "记得上一个任务的差旅明细（700*3+1200+150*3=3750）" "$r" "3,\?750"

echo '[5/5] 工作流：执行有节点进度、确认后结果流式输出'
wf_out=$(python3 scripts/smoke-workflow.py 2>&1)
printf %s "$wf_out"
echo
pass=$((pass + $(printf %s "$wf_out" | grep -c "✅" || true)))
fail=$((fail + $(printf %s "$wf_out" | grep -c "❌" || true)))

echo
echo "冒烟测试结果：通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ] || exit 1
