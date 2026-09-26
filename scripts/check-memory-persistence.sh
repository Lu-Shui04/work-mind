#!/usr/bin/env bash
# scripts/check-memory-persistence.sh
# 验收：会话记忆是否**跨后端重启**保留。
#
# 为什么单独有这个脚本：以前会话/记忆/画像都在进程内存里，
# docker compose restart / up --build 之后全部清空，用户上一句刚说过的信息下一句就不记得了。
# 单测（tests/test_memory.py）跑在没连数据库的容器里，只能覆盖「内存兜底」分支；
# 真正「重启还在不在」只能这样打真实服务 + 真重启来验。
#
# 用法：bash scripts/check-memory-persistence.sh
set -uo pipefail

API="${API_BASE:-http://localhost:3000}"
CONTAINER="${CONTAINER:-workmind-server}"
HDR=(-H "Content-Type: application/json" -H "X-Tenant-Id: tenant-demo" \
     -H "X-User-Id: u-tech-01" -H "X-User-Departments: tech" -H "X-User-Clearance: internal")

pass=0; fail=0
ok()  { echo "  ✅ $1"; pass=$((pass+1)); }
bad() { echo "  ❌ $1"; fail=$((fail+1)); }

_answer() {  # stdin 是 SSE，输出拼接后的回答
  python3 -c "
import json,sys
t=sys.stdin.read(); out=''
for b in t.split(chr(10)+chr(10)):
    if b.startswith('event: token'):
        try: out+=json.loads(b.split(chr(10))[1][6:])['token']
        except Exception: pass
print(out)"
}

ask() {  # $1=消息 $2=会话id
  curl -s -N --max-time 120 "$API/api/chat/stream" "${HDR[@]}" \
    -d "{\"message\":\"$1\",\"sessionId\":\"$2\",\"userId\":\"u-tech-01\"}" | _answer
}

S="persist-check-$$"
FACT="WM-9527"
echo "会话 id：$S"
echo "要记住的事实：工牌号 $FACT"
echo

echo '[1/3] 写入记忆'
r=$(ask "记住：我的工牌号是 $FACT，我在上海办公" "$S")
if printf %s "$r" | grep -q "$FACT"; then ok "模型确认记住了"; else bad "模型没有确认记住（回答：${r:0:80}）"; fi
rows=$(docker exec workmind-db psql -U workmind -d workmind -t -A -c "select count(*) from chat_messages where session_id='$S'" 2>/dev/null | tr -d '[:space:]')
if [ "${rows:-0}" -ge 2 ] 2>/dev/null; then ok "会话已落库（$rows 条消息）"; else bad "数据库里没有这次会话（rows=${rows:-?}）"; fi

echo '[2/3] 重启后端容器'
docker restart "$CONTAINER" >/dev/null 2>&1 || { bad "重启失败"; exit 1; }
code=''
for i in $(seq 1 30); do
  sleep 1
  code=$(curl -s -o /dev/null -w "%{http_code}" "$API/health/" || true)
  [ "$code" = "200" ] && break
done
if [ "$code" = "200" ]; then ok "容器已重启并恢复健康"; else bad "等待健康检查超时"; fi

echo '[3/3] 重启后回忆'
r=$(ask "我的工牌号是多少？" "$S")
if printf %s "$r" | grep -q "$FACT"; then ok "重启后仍然记得（跨重启保留 ✔）"; else bad "重启后忘记了：${r:0:120}"; fi
sessions=$(curl -s "$API/api/chat/sessions" "${HDR[@]}")
if printf %s "$sessions" | grep -q "$S"; then ok "会话列表里也还在"; else bad "会话列表里查不到这个会话"; fi

curl -s -X DELETE "$API/api/chat/sessions/$S" "${HDR[@]}" >/dev/null 2>&1 || true
echo
echo "结果：通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ] || exit 1
