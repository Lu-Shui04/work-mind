<template>
  <!-- frontend/src/components/erp/ApprovalTimeline.vue
       审批链 + 完整对话明细。
       - 每个节点显示：状态、AI 结论（approve/reject/need_info）、结论理由、逐条检查项
       - 任一节点 need_info 时流程暂停，这里出现"补充材料"面板：可以回答问题、也可以直接改申请数据 -->
  <div class="approval-timeline">
    <!-- 左：审批链 -->
    <div class="steps-panel">
      <div class="steps-title">审批流程</div>

      <div v-if="erpStore.chainReason.length" class="chain-reason">
        <div class="cr-title">审批链依据</div>
        <div v-for="r in erpStore.chainReason" :key="r" class="cr-item">{{ r }}</div>
      </div>

      <div v-if="erpStore.approving && !erpStore.approvalSteps.length" class="steps-loading">
        <div class="spinner" />
        <span>正在安排审批流程...</span>
      </div>

      <div class="steps-list">
        <div v-for="(step, idx) in erpStore.approvalSteps" :key="step.stepId"
             class="step-item" :class="step.status">
          <div class="step-line" v-if="idx < erpStore.approvalSteps.length - 1" :class="{ active: step.status === 'approved' }" />
          <div class="step-avatar" :style="{ background: step.role.color + '22', color: step.role.color }">
            {{ step.role.name?.slice(0, 1) }}
          </div>
          <div class="step-info">
            <div class="step-name">{{ step.role.name }}</div>
            <div class="step-status" :class="step.status">{{ stepStatusText(step.status, step.decision) }}</div>
          </div>
        </div>
      </div>

      <div v-if="erpStore.finalResult" class="final-result" :class="erpStore.finalResult.status">
        <div class="final-text">
          {{ erpStore.finalResult.approved ? '审批通过' : '审批驳回' }}
        </div>
        <div v-if="!erpStore.finalResult.approved" class="final-sub">
          卡在：{{ erpStore.finalResult.rejectedBy }}
        </div>
      </div>
    </div>

    <!-- 右：对话与操作 -->
    <div class="conversation" ref="convEl">
      <div v-if="!erpStore.approvalMessages.length && !erpStore.approving" class="conv-empty">
        <div>审批开始后，各角色的判断与依据会显示在这里</div>
        <div class="conv-empty-sub">AI 只给结论和检查项；需要补充材料时流程会暂停等你处理</div>
      </div>

      <div v-if="erpStore.approving && !erpStore.approvalMessages.length" class="conv-thinking">
        <div class="spinner" />
        <span>审批人正在审核...</span>
      </div>

      <div v-for="msg in erpStore.approvalMessages" :key="msg.id" class="msg-bubble-wrap"
           :class="{ 'is-applicant': msg.from === 'applicant' }">
        <template v-if="msg.from !== 'applicant'">
          <div class="msg-avatar" :style="{ background: msg.role?.color + '22', color: msg.role?.color }">
            {{ msg.role?.name?.slice(0, 1) || '?' }}
          </div>
          <div class="msg-content left">
            <div class="msg-sender">
              {{ msg.role?.name }}
              <span class="msg-type-tag" :class="msg.type">{{ typeLabel(msg.type) }}</span>
            </div>
            <div class="msg-bubble" :class="[msg.type, decisionClass(msg)]">
              {{ msg.content }}
            </div>
            <!-- 结论的检查项：解释"凭什么" -->
            <div v-if="msg.checklist?.length" class="checklist">
              <div v-for="(c, i) in msg.checklist" :key="i" class="check-item" :class="c.result">
                <span class="ck-mark">{{ c.result === 'pass' ? '✓' : c.result === 'warn' ? '!' : '✕' }}</span>
                <span class="ck-item">{{ c.item }}</span>
                <span class="ck-note">{{ c.note }}</span>
              </div>
            </div>
          </div>
        </template>

        <template v-else>
          <div class="msg-content right">
            <div class="msg-sender right">申请人{{ msg.patched ? '（同时修改了申请数据）' : '' }}</div>
            <div class="msg-bubble applicant">{{ msg.content }}</div>
          </div>
          <div class="msg-avatar applicant">我</div>
        </template>
      </div>

      <!-- 补料面板：AI 提出问题时，人在这里回答 / 改数据 / 继续 -->
      <div v-if="erpStore.waitingInfo" class="wait-panel">
        <div class="wait-head">
          <span class="wait-icon">⏸</span>
          <span class="wait-title">
            流程已暂停：{{ waitingRoleName }} 需要你补充材料
          </span>
        </div>

        <div v-if="erpStore.waitingInfo.questions?.length" class="wait-questions">
          <div class="wq-label">需要你回答的问题</div>
          <div v-for="(q, i) in erpStore.waitingInfo.questions" :key="i" class="wq-item">{{ i + 1 }}. {{ q }}</div>
        </div>

        <textarea v-model="answer" class="wait-input" rows="3"
                  placeholder="在这里补充说明（例如：婚假按 1 天申请是笔误，实际需要 3 天；证明材料已在附件中）" />

        <div class="wait-form-toggle" @click="showForm = !showForm">
          <span>{{ showForm ? '▾' : '▸' }}</span> 需要直接修改申请数据？（{{ showForm ? '收起' : '展开' }}）
        </div>

        <!-- 可编辑申请数据：AI 解析错了 / 情况有变，直接改完再继续 -->
        <div v-if="showForm && erpStore.parsedForm" class="wait-form">
          <template v-if="erpStore.formType === 'expense'">
            <label class="wf-field"><span>费用类型</span>
              <select v-model="erpStore.parsedForm.type" class="wf-input">
                <option value="travel">差旅费</option><option value="meal">餐饮费</option>
                <option value="office">办公用品</option><option value="training">培训费</option>
                <option value="other">其他</option>
              </select>
            </label>
            <label class="wf-field"><span>报销事由</span>
              <input v-model="erpStore.parsedForm.reason" class="wf-input" />
            </label>
            <div class="wf-items">
              <div class="wf-items-head"><span>费用明细</span>
                <button class="wf-mini" @click="erpStore.addItem()">+ 添加一行</button>
              </div>
              <div v-for="(it, i) in erpStore.parsedForm.items" :key="i" class="wf-item-row">
                <input v-model="it.name" class="wf-input" placeholder="项目" />
                <input v-model.number="it.amount" type="number" class="wf-input num" @input="erpStore.recomputeExpense()" />
                <input v-model="it.date" type="date" class="wf-input" />
                <button class="wf-del" @click="erpStore.removeItem(i)">×</button>
              </div>
              <div class="wf-total">合计 ¥{{ erpStore.parsedForm.totalAmount }}</div>
            </div>
          </template>

          <template v-else>
            <label class="wf-field"><span>假期类型</span>
              <select v-model="erpStore.parsedForm.type" class="wf-input">
                <option value="annual">年假</option><option value="personal">事假</option>
                <option value="sick">病假</option><option value="compensatory">调休</option>
                <option value="marriage">婚假</option><option value="maternity">产假</option>
              </select>
            </label>
            <label class="wf-field"><span>开始日期</span>
              <input v-model="erpStore.parsedForm.startDate" type="date" class="wf-input" @change="erpStore.recomputeLeave()" />
            </label>
            <label class="wf-field"><span>结束日期</span>
              <input v-model="erpStore.parsedForm.endDate" type="date" class="wf-input" @change="erpStore.recomputeLeave()" />
            </label>
            <label class="wf-field"><span>自然日 / 工作日</span>
              <span class="wf-readonly">{{ erpStore.parsedForm.days }} 天 / {{ erpStore.parsedForm.workdays }} 天</span>
            </label>
            <label class="wf-field span2"><span>请假原因</span>
              <input v-model="erpStore.parsedForm.reason" class="wf-input" />
            </label>
          </template>
        </div>

        <div class="wait-actions">
          <button class="btn-ghost-sm" :disabled="erpStore.resuming" @click="submit(false)">
            仅提交说明继续
          </button>
          <button class="btn-primary-sm" :disabled="erpStore.resuming" @click="submit(true)">
            {{ erpStore.resuming ? '继续评审中...' : '提交修改并继续评审' }}
          </button>
        </div>
        <div class="wait-tip">
          提交后由当前审批人重新评审；同一节点最多追问 2 轮，之后必须给出通过与驳回的明确结论。
        </div>
      </div>

      <div v-if="erpStore.approving && erpStore.approvalMessages.length && !erpStore.waitingInfo" class="conv-thinking inline">
        <div class="typing-dots"><span /><span /><span /></div>
        <span>审批人正在思考...</span>
      </div>
      <div ref="bottomEl" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { useErpStore } from '@/stores/erp.js'

const erpStore = useErpStore()
const convEl   = ref(null)
const bottomEl = ref(null)
const answer   = ref('')
const showForm = ref(false)

const waitingRoleName = computed(() => {
  const w = erpStore.waitingInfo
  if (!w) return ''
  const step = erpStore.approvalSteps.find(s => s.stepId === w.stepId)
  return step?.role?.name || w.role?.name || '审批人'
})

function stepStatusText(status, decision) {
  if (status === 'approved') return '已通过'
  if (status === 'rejected') return '已驳回'
  if (status === 'waiting_info') return '待补充材料'
  if (status === 'reviewing') return '审核中'
  if (status === 'pending') return '待审核'
  return decision || status
}
function typeLabel(type) {
  return { question: '提出问题', answer: '申请人补充', decision: '审批结论' }[type] || ''
}
function decisionClass(msg) {
  if (msg.type !== 'decision') return ''
  return msg.decision === 'approve' ? 'ok' : msg.decision === 'reject' ? 'bad' : 'warn'
}

async function submit(patchForm) {
  await erpStore.resumeWithAnswer(answer.value, { patchForm })
  answer.value = ''
  showForm.value = false
}

watch(() => erpStore.approvalMessages.length, async () => {
  await nextTick()
  bottomEl.value?.scrollIntoView({ behavior: 'smooth' })
})
watch(() => erpStore.waitingInfo, async (v) => {
  if (v) { await nextTick(); bottomEl.value?.scrollIntoView({ behavior: 'smooth' }) }
})
</script>

<style scoped>
.approval-timeline { display: flex; height: 100%; gap: 0; overflow: hidden; }

/* 左侧审批链 */
.steps-panel {
  width: 220px; flex-shrink: 0; padding: var(--space-md);
  background: var(--color-bg); border-right: 1px solid var(--color-border);
  display: flex; flex-direction: column; gap: var(--space-md); overflow-y: auto;
}
.steps-title { font-size: 12.5px; font-weight: 600; color: var(--color-text); }
.chain-reason { background: var(--color-surface); border: 1px solid var(--color-border-light); border-radius: var(--radius-md); padding: 8px 10px; }
.cr-title { font-size: 10.5px; color: var(--color-text-muted); margin-bottom: 4px; }
.cr-item { font-size: 11px; color: var(--color-primary-dark); line-height: 1.5; }
.steps-loading { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--color-text-muted); }
.steps-list { display: flex; flex-direction: column; gap: 10px; }
.step-item { position: relative; display: flex; gap: 8px; align-items: center; }
.step-line { position: absolute; left: 13px; top: 30px; width: 2px; height: 14px; background: var(--color-border); }
.step-line.active { background: var(--color-success); }
.step-avatar { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; flex-shrink: 0; }
.step-info { display: flex; flex-direction: column; }
.step-name { font-size: 12px; color: var(--color-text); font-weight: 600; }
.step-status { font-size: 10.5px; color: var(--color-text-muted); }
.step-status.approved { color: var(--color-success); }
.step-status.rejected { color: var(--color-danger); }
.step-status.waiting_info { color: var(--color-warning); font-weight: 600; }
.step-status.reviewing { color: var(--color-primary); }
.final-result { margin-top: auto; padding: 8px 10px; border-radius: var(--radius-md); background: var(--color-surface); border: 1px solid var(--color-border-light); }
.final-result.approved { background: #dcfce7; border-color: #86efac; }
.final-result.rejected { background: #fee2e2; border-color: #fca5a5; }
.final-text { font-size: 12.5px; font-weight: 600; }
.final-result.approved .final-text { color: #15803d; }
.final-result.rejected .final-text { color: #b91c1c; }
.final-sub { font-size: 11px; color: var(--color-text-sub); margin-top: 2px; }

/* 右侧对话 */
.conversation { flex: 1; min-width: 0; padding: var(--space-md); overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
.conv-empty { color: var(--color-text-muted); font-size: 12.5px; display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center; height: 100%; }
.conv-empty-sub { font-size: 11.5px; color: var(--color-text-muted); }
.conv-thinking { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--color-text-muted); }
.msg-bubble-wrap { display: flex; gap: 8px; }
.msg-bubble-wrap.is-applicant { flex-direction: row-reverse; }
.msg-avatar { width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; flex-shrink: 0; }
.msg-avatar.applicant { background: var(--color-primary); color: #fff; font-size: 11px; }
.msg-content { max-width: 78%; display: flex; flex-direction: column; gap: 4px; }
.msg-content.right { align-items: flex-end; }
.msg-sender { font-size: 11px; color: var(--color-text-muted); display: flex; align-items: center; gap: 6px; }
.msg-type-tag { font-size: 10px; padding: 0 6px; border-radius: var(--radius-full); background: var(--color-border-light); color: var(--color-text-muted); }
.msg-type-tag.question { background: #fef3c7; color: #b45309; }
.msg-type-tag.decision { background: #e0e7ff; color: #4338ca; }
.msg-type-tag.answer { background: #dcfce7; color: #15803d; }
.msg-bubble { font-size: 12.5px; line-height: 1.65; padding: 8px 11px; border-radius: var(--radius-md); background: var(--color-bg); color: var(--color-text); white-space: pre-wrap; }
.msg-bubble.question { border-left: 3px solid var(--color-warning); }
.msg-bubble.decision.ok { border-left: 3px solid var(--color-success); }
.msg-bubble.decision.bad { border-left: 3px solid var(--color-danger); }
.msg-bubble.decision.warn { border-left: 3px solid var(--color-warning); }
.msg-bubble.applicant { background: var(--color-primary); color: #fff; }

.checklist { display: flex; flex-direction: column; gap: 3px; margin-top: 2px; }
.check-item { display: flex; gap: 6px; font-size: 11px; align-items: baseline; }
.ck-mark { width: 12px; text-align: center; font-weight: 700; }
.check-item.pass .ck-mark { color: var(--color-success); }
.check-item.warn .ck-mark { color: var(--color-warning); }
.check-item.fail .ck-mark { color: var(--color-danger); }
.ck-item { color: var(--color-text); min-width: 130px; }
.ck-note { color: var(--color-text-muted); }

/* 补料面板 */
.wait-panel { border: 1px solid var(--color-warning); background: #fffdf5; border-radius: var(--radius-lg); padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.wait-head { display: flex; align-items: center; gap: 8px; }
.wait-icon { font-size: 14px; }
.wait-title { font-size: 12.5px; font-weight: 600; color: #b45309; }
.wait-questions { display: flex; flex-direction: column; gap: 3px; }
.wq-label { font-size: 11px; color: var(--color-text-muted); }
.wq-item { font-size: 12px; color: var(--color-text); line-height: 1.6; }
.wait-input { border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 7px 9px; font-size: 12px; font-family: var(--font-sans); resize: vertical; background: var(--color-surface); color: var(--color-text); outline: none; }
.wait-input:focus { border-color: var(--color-primary); }
.wait-form-toggle { font-size: 11.5px; color: var(--color-primary); cursor: pointer; user-select: none; }
.wait-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; background: var(--color-surface); border: 1px solid var(--color-border-light); border-radius: var(--radius-md); padding: 10px; }
.wf-field { display: flex; flex-direction: column; gap: 3px; font-size: 11px; color: var(--color-text-sub); }
.wf-field.span2 { grid-column: span 2; }
.wf-input { border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 4px 7px; font-size: 12px; background: var(--color-surface); color: var(--color-text); outline: none; width: 100%; }
.wf-input.num { text-align: right; }
.wf-readonly { font-size: 12px; color: var(--color-text); }
.wf-items { grid-column: span 2; display: flex; flex-direction: column; gap: 5px; }
.wf-items-head { display: flex; align-items: center; justify-content: space-between; font-size: 11px; color: var(--color-text-sub); }
.wf-mini { border: 1px solid var(--color-border); background: var(--color-surface); border-radius: var(--radius-sm); font-size: 11px; padding: 2px 8px; cursor: pointer; color: var(--color-primary); }
.wf-item-row { display: grid; grid-template-columns: 1.4fr 0.8fr 1fr 24px; gap: 5px; align-items: center; }
.wf-del { border: none; background: none; color: var(--color-danger); cursor: pointer; font-size: 14px; }
.wf-total { font-size: 11.5px; font-weight: 600; color: var(--color-primary); text-align: right; }
.wait-actions { display: flex; gap: 8px; justify-content: flex-end; }
.btn-ghost-sm { border: 1px solid var(--color-border); background: var(--color-surface); color: var(--color-text-sub); border-radius: var(--radius-md); padding: 6px 12px; font-size: 12px; cursor: pointer; }
.btn-primary-sm { border: none; background: var(--color-primary); color: #fff; border-radius: var(--radius-md); padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; }
.btn-primary-sm:disabled, .btn-ghost-sm:disabled { opacity: .55; cursor: not-allowed; }
.wait-tip { font-size: 10.5px; color: var(--color-text-muted); line-height: 1.5; }

.spinner { width: 14px; height: 14px; border: 2px solid var(--color-border); border-top-color: var(--color-primary); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.typing-dots { display: inline-flex; gap: 3px; }
.typing-dots span { width: 5px; height: 5px; border-radius: 50%; background: var(--color-text-muted); animation: blink 1.2s infinite; }
.typing-dots span:nth-child(2) { animation-delay: .2s; }
.typing-dots span:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 100% { opacity: .25 } 50% { opacity: 1 } }
</style>
