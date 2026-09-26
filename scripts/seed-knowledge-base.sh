#!/usr/bin/env bash
# scripts/seed-knowledge-base.sh
# 把 docs/company 下的公司制度文档灌进知识库。
#
# 为什么需要它：
#   Agent / 智能对话的示例任务依赖公司制度文档（差旅报销标准、年假政策…）。
#   数据库重建后（换机器、删掉 workmind-pgdata 卷、docker compose down -v）知识库是空的，
#   示例任务就会"查不到公司信息"，看起来像 Agent 坏了。
#
# 可重复执行：后端按文件 sha256 幂等，重复上传只复用、不重复解析也不重复计费；
#             元数据变了（部门/密级/版本）会自动按新元数据重新入库。
#
# 用法：
#   bash scripts/seed-knowledge-base.sh
#   API_BASE=http://localhost:3000 bash scripts/seed-knowledge-base.sh
set -euo pipefail

API="${API_BASE:-http://localhost:3000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/docs/company"
MANIFEST="$DIR/manifest.txt"

[ -f "$MANIFEST" ] || { echo "找不到清单文件：$MANIFEST" >&2; exit 1; }

# 用「管理层」身份上传：文档可见性由文档自身的部门/密级决定、与上传者无关，
# 但用最高权限身份可以保证机密文档也顺利入库。
HDR=(
  -H "X-Tenant-Id: tenant-demo"
  -H "X-User-Id: u-dir-01"
  -H "X-User-Name: %E5%BC%A0%E6%80%BB"
  -H "X-User-Departments: hr,tech,finance,legal,product,sales,general"
  -H "X-User-Clearance: confidential"
)

echo "接口：$API"
echo "文档目录：$DIR"
echo

ok=0; dup=0; fail=0
while IFS='|' read -r file title dept dtype sec ver eff owner tags; do
  file="$(printf '%s' "$file" | tr -d '\r')"
  case "$file" in ''|'#'*) continue ;; esac
  title="$(printf '%s' "$title" | tr -d '\r')"
  path="$DIR/$file"

  if [ ! -f "$path" ]; then
    echo "✗ $title —— 找不到文件 $file"
    fail=$((fail + 1))
    continue
  fi

  resp="$(curl -sS --max-time 300 -X POST "$API/api/knowledge/documents" "${HDR[@]}" \
    -F "file=@$path" \
    -F "document_title=$title" \
    -F "department=$dept" \
    -F "doc_type=$dtype" \
    -F "security_level=$sec" \
    -F "version=$ver" \
    -F "effective_date=$eff" \
    -F "owner=$owner" \
    -F "tags=$tags" 2>&1 || true)"

  if printf '%s' "$resp" | grep -q '"duplicated": *true'; then
    echo "= $title（$dept/$sec）—— 已在库中，复用"
    dup=$((dup + 1))
  elif printf '%s' "$resp" | grep -q '"ingest_status": *"indexed"'; then
    chunks="$(printf '%s' "$resp" | grep -o '"chunk_count": *[0-9]*' | head -1 | tr -dc '0-9')"
    echo "✓ $title（$dept/$sec）—— 入库成功，切片 ${chunks:-?} 个"
    ok=$((ok + 1))
  else
    msg="$(printf '%s' "$resp" | grep -o '"message": *"[^"]*"' | head -1 | sed 's/.*"message": *"//; s/"$//')"
    echo "✗ $title —— 失败：${msg:-${resp:0:200}}"
    fail=$((fail + 1))
  fi
done < "$MANIFEST"

echo
echo "完成：新入库 $ok 篇 / 复用 $dup 篇 / 失败 $fail 篇"
[ "$fail" -eq 0 ] || exit 1

cat <<'TIP'

验证方式：
  1) 知识库页应当能看到这些文档（按当前身份过滤：general 全员可见，其余按部门/密级）；
  2) Agent 页点「知识查询」示例任务，应当能查到年假政策并带出引用来源；
  3) 命令行快速验证：
     curl -s -X POST http://localhost:3000/api/knowledge/search \
       -H 'Content-Type: application/json' \
       -H 'X-User-Id: u-tech-01' -H 'X-User-Departments: tech' -H 'X-User-Clearance: internal' \
       -d '{"query":"出差住宿费报销标准","k":3}'
TIP
