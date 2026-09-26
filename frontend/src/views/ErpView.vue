<template>
  <!-- frontend/src/views/ErpView.vue
       报销/请假：左侧智能填单（可编辑）+ 申请记录，右侧审批链与完整对话。
       审批人要求补充材料时，流程会在右侧暂停，等你回答/改数据后继续。 -->
  <div class="erp-view">
    <aside class="erp-sidebar">
      <div class="type-tabs">
        <button class="type-tab" :class="{ active: erpStore.formType === 'expense' }"
                @click="switchType('expense')" :disabled="erpStore.approving">报销申请</button>
        <button class="type-tab" :class="{ active: erpStore.formType === 'leave' }"
                @click="switchType('leave')" :disabled="erpStore.approving">请假申请</button>
      </div>
      <div class="sidebar-scroll">
        <SmartFormParser />
      </div>
      <div class="record-section">
        <div class="record-header" @click="showRecords = !showRecords">
          <span>申请记录 ({{ erpStore.applications.length }})</span>
          <span class="record-header-ops">
            <button v-if="erpStore.applications.length" class="mini danger"
                    @click.stop="clearAll" title="清空全部申请记录">清空</button>
            <span>{{ showRecords ? '▴' : '▾' }}</span>
          </span>
        </div>
        <div v-if="showRecords" class="record-list">
          <div v-if="!erpStore.applications.length" class="record-empty">暂无申请记录</div>
          <div v-for="app in erpStore.applications" :key="app.id" class="record-item"
               :class="{ active: app.id === erpStore.currentAppId }" @click="openApplication(app.id)">
            <div class="record-top">
              <span class="record-id">{{ app.id }}</span>
              <span class="record-status" :class="app.status">{{ statusLabel(app.status) }}</span>
            </div>
            <div class="record-desc">
              {{ app.reason || (app.formType === 'leave' ? '请假 ' + (app.days ?? '-') + ' 天' : '') }}
            </div>
            <div class="record-meta">
              {{ app.formType === 'expense' ? '¥' + (app.amount ?? '-') : (app.days ?? '-') + ' 天' }}
              · 进度 {{ app.stepDone }}/{{ app.stepTotal }}
              <span v-if="app.currentRole"> · 当前 {{ app.currentRole }}</span>
              · {{ formatTime(app.createdAt) }}
            </div>
            <div class="record-ops" @click.stop>
              <button v-if="app.status !== 'approved'" class="mini" @click="withdraw(app.id)">撤回</button>
              <button class="mini danger" @click="remove(app.id)" title="彻底删除这条记录">删除</button>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <main class="erp-main">
      <div v-if="!erpStore.approving && !erpStore.approvalMessages.length && !erpStore.finalResult" class="main-empty">
        <div class="empty-title">ERP 智能报销与请假</div>
        <div class="empty-desc">
          左侧用自然语言描述，AI 解析成结构化表单（每个字段都能改）→ 提交后进入 Multi-Agent 审批；
          审批人给出结论与检查项，需要补充材料时流程会暂停等你处理。
        </div>
        <div class="feature-list">
          <div class="feature-item">① 审批结论是结构化的：通过与驳回都有理由和逐条检查项</div>
          <div class="feature-item">② 审批人只管自己的职责范围，不会互相串味</div>
          <div class="feature-item">③ 提出问题时流程暂停，你可以补充说明、也可以直接改申请数据</div>
        </div>
      </div>
      <div v-else class="approval-area">
        <div class="app-summary">
          <span class="app-type">{{ erpStore.formType === 'expense' ? '报销申请' : '请假申请' }}</span>
          <span v-if="erpStore.parsedForm">
            {{ erpStore.parsedForm.reason }}
            · {{ erpStore.formType === 'expense'
                  ? '¥' + erpStore.parsedForm.totalAmount
                  : erpStore.parsedForm.workdays + ' 个工作日' }}
          </span>
          <span class="app-id">{{ erpStore.currentAppId }}</span>
          <span v-if="erpStore.waitingInfo" class="app-flag wait">⏸ 待补充材料</span>
          <span v-else-if="erpStore.approving" class="app-flag run">审批中</span>
          <span v-else-if="erpStore.finalResult" class="app-flag" :class="erpStore.finalResult.approved ? 'ok' : 'bad'">
            {{ erpStore.finalResult.approved ? '已通过' : '已驳回' }}
          </span>
        </div>
        <ApprovalTimeline class="timeline-area" />
        <div v-if="erpStore.finalResult" class="done-actions">
          <button class="btn btn-ghost" @click="erpStore.reset()">开始新申请</button>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useErpStore } from '@/stores/erp.js'
import SmartFormParser from '@/components/erp/SmartFormParser.vue'
import ApprovalTimeline from '@/components/erp/ApprovalTimeline.vue'

const erpStore = useErpStore()
const showRecords = ref(true)

const STATUS_TEXT = {
  in_progress: '审批中', waiting_info: '待补充材料', approved: '已通过',
  rejected: '已驳回', withdrawn: '已撤回',
}
const statusLabel = (s) => STATUS_TEXT[s] || s

function switchType(type) {
  erpStore.formType = type
  erpStore.reset()
}
async function openApplication(appId) {
  await erpStore.loadApplication(appId)
  showRecords.value = true
}
async function withdraw(appId) {
  if (!confirm('撤回该申请？记录会保留，状态变为"已撤回"。')) return
  await erpStore.withdraw(appId)
}
async function remove(appId) {
  if (!confirm('彻底删除这条申请记录？（测试清理用，不可恢复）')) return
  await erpStore.deleteApplication(appId)
}
async function clearAll() {
  if (!confirm('清空全部申请记录？不可恢复。')) return
  await erpStore.clearApplications()
}
function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

onMounted(() => erpStore.loadApplications())
</script>

<style scoped>
.erp-view { display: flex; gap: var(--space-lg); height: 100%; }
.erp-sidebar {
  width: 460px; flex-shrink: 0; display: flex; flex-direction: column; gap: var(--space-md);
  background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-lg); padding: var(--space-md); min-height: 0;
}
.type-tabs { display: flex; gap: 6px; background: var(--color-bg); border-radius: var(--radius-md); padding: 3px; }
.type-tab { flex: 1; border: none; background: none; cursor: pointer; font-size: 12.5px; padding: 6px; border-radius: var(--radius-sm); color: var(--color-text-sub); }
.type-tab.active { background: var(--color-surface); color: var(--color-primary); font-weight: 600; box-shadow: var(--shadow-sm); }
.type-tab:disabled { opacity: .6; cursor: not-allowed; }
.sidebar-scroll { overflow: auto; flex: 1; }

.record-section { border-top: 1px solid var(--color-border); padding-top: 10px; max-height: 230px; overflow: auto; }
.record-header { display: flex; align-items: center; justify-content: space-between; cursor: pointer; font-size: 12.5px; font-weight: 600; color: var(--color-text); }
.record-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.record-empty { font-size: 11.5px; color: var(--color-text-muted); }
.record-item { border: 1px solid var(--color-border-light); border-radius: var(--radius-md); padding: 7px 9px; cursor: pointer; transition: var(--transition); }
.record-item:hover { border-color: var(--color-primary); }
.record-item.active { border-color: var(--color-primary); background: var(--color-primary-bg); }
.record-top { display: flex; align-items: center; justify-content: space-between; }
.record-id { font-family: var(--font-mono); font-size: 10.5px; color: var(--color-text-muted); }
.record-status { font-size: 10px; padding: 1px 7px; border-radius: var(--radius-full); }
.record-status.in_progress { background: #e0e7ff; color: #4338ca; }
.record-status.waiting_info { background: #fef3c7; color: #b45309; }
.record-status.approved { background: #dcfce7; color: #15803d; }
.record-status.rejected { background: #fee2e2; color: #b91c1c; }
.record-status.withdrawn { background: var(--color-border-light); color: var(--color-text-muted); }
.record-desc { font-size: 11.5px; color: var(--color-text); margin-top: 4px; }
.record-meta { font-size: 10.5px; color: var(--color-text-muted); margin-top: 2px; }
.record-header-ops { display: flex; align-items: center; gap: 8px; }
.record-ops { margin-top: 5px; display: flex; gap: 10px; }
.mini { border: none; background: none; font-size: 11px; cursor: pointer; padding: 0; }
.mini.danger { color: var(--color-danger); }
.mini:hover { text-decoration: underline; }

.erp-main {
  flex: 1; min-width: 0; background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: var(--radius-lg); overflow: hidden; display: flex; flex-direction: column;
}
.main-empty { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; text-align: center; padding: var(--space-lg); }
.empty-title { font-size: 16px; font-weight: 600; color: var(--color-text); }
.empty-desc { font-size: 12.5px; color: var(--color-text-sub); max-width: 560px; line-height: 1.7; }
.feature-list { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; text-align: left; }
.feature-item { font-size: 12px; color: var(--color-text-muted); }

.approval-area { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.app-summary {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px var(--space-md); border-bottom: 1px solid var(--color-border);
  font-size: 12px; color: var(--color-text-sub);
}
.app-type { font-size: 13px; font-weight: 600; color: var(--color-text); }
.app-id { font-family: var(--font-mono); font-size: 11px; color: var(--color-text-muted); }
.app-flag { font-size: 10.5px; padding: 1px 8px; border-radius: var(--radius-full); background: var(--color-border-light); color: var(--color-text-sub); }
.app-flag.wait { background: #fef3c7; color: #b45309; font-weight: 600; }
.app-flag.run { background: #e0e7ff; color: #4338ca; }
.app-flag.ok { background: #dcfce7; color: #15803d; }
.app-flag.bad { background: #fee2e2; color: #b91c1c; }
.timeline-area { flex: 1; min-height: 0; }
.done-actions { padding: 10px var(--space-md); border-top: 1px solid var(--color-border); display: flex; justify-content: flex-end; }
.btn { border: 1px solid var(--color-border); background: var(--color-surface); color: var(--color-text-sub); border-radius: var(--radius-md); padding: 6px 12px; font-size: 12px; cursor: pointer; }
</style>
