// frontend/src/stores/erp.js
// ERP 模块状态：智能填单（可编辑）→ Multi-Agent 审批（结构化裁决）
// 关键交互：审批中任一节点返回 need_info 时，流程会**暂停**，由真人补充说明/修改申请后继续。
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { fetchStream } from '@/utils/http.js'
import http from '@/utils/http.js'
import { useAppStore } from './app.js'

export const useErpStore = defineStore('erp', () => {
  const appStore = useAppStore()

  // ── 智能填单 ──────────────────────────────────────────────────
  const formType   = ref('expense')      // expense | leave
  const parsedForm = ref(null)           // 可编辑的结构化表单
  const parsing    = ref(false)
  const localWarnings = ref([])

  // ── 审批流状态 ────────────────────────────────────────────────
  const approvalMessages = ref([])       // 全部气泡（提问 / 补充说明 / 结论）
  const approvalSteps    = ref([])       // 审批链节点
  const approving        = ref(false)
  const finalResult      = ref(null)
  const currentAppId     = ref('')
  const chainReason      = ref([])       // 审批链依据（为什么需要财务/总监）
  const waitingInfo      = ref(null)     // 非空 = 流程暂停，等待人工补料
  const resuming         = ref(false)

  const applications = ref([])

  const currentStatus = computed(() => {
    if (waitingInfo.value) return 'waiting_info'
    if (finalResult.value) return finalResult.value.approved ? 'approved' : 'rejected'
    if (approving.value) return 'in_progress'
    return 'idle'
  })
  const statusLabel = computed(() => ({
    in_progress: '审批中', waiting_info: '待补充材料', approved: '已通过',
    rejected: '已驳回', withdrawn: '已撤回', idle: '',
  }[currentStatus.value] || ''))

  function errMsg(err, fallback) {
    return err?.response?.data?.error?.message || err?.message || fallback
  }

  // ── 表单编辑（AI 抽错的地方必须能改）──────────────────────────
  function addItem() {
    parsedForm.value?.items?.push({ name: '', amount: 0, date: '', note: '' })
    recomputeExpense()
  }
  function removeItem(idx) {
    parsedForm.value?.items?.splice(idx, 1)
    recomputeExpense()
  }
  function recomputeExpense() {
    const f = parsedForm.value
    if (!f?.items) return
    f.totalAmount = Number(f.items.reduce((s, it) => s + (Number(it.amount) || 0), 0).toFixed(2))
  }
  function recomputeLeave() {
    const f = parsedForm.value
    if (!f?.startDate || !f?.endDate) return
    const start = new Date(f.startDate); const end = new Date(f.endDate)
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end < start) return
    let days = 0, workdays = 0
    const d = new Date(start)
    while (d <= end) {
      days += 1
      if (d.getDay() !== 0 && d.getDay() !== 6) workdays += 1
      d.setDate(d.getDate() + 1)
    }
    f.days = days
    f.workdays = workdays
  }

  function normalizeForm(form, type) {
    const f = { ...form }
    if (type === 'expense') {
      f.items = (f.items || []).map(it => ({
        name: it.name || '', amount: Number(it.amount) || 0,
        date: it.date || '', note: it.note || '',
      }))
      parsedForm.value = f
      recomputeExpense()
    } else {
      f.days = Number(f.days) || 0
      f.workdays = Number(f.workdays) || 0
      parsedForm.value = f
    }
    return f
  }

  async function parseForm(text) {
    if (!text.trim() || parsing.value) return
    parsing.value = true
    try {
      const data = await http.post('/erp/parse', { text, formType: formType.value })
      localWarnings.value = []
      return normalizeForm(data.form, formType.value)
    } catch (err) {
      appStore.toast.error(errMsg(err, '解析失败，请重新描述'))
    } finally {
      parsing.value = false
    }
  }

  // ── 统一的 SSE 事件处理（提交与补料复用）──────────────────────
  function handleEvent(event, data) {
    if (event === 'start') {
      currentAppId.value = data.appId
      chainReason.value = data.chainReason || []
      approvalMessages.value = []
      approvalSteps.value = []
      finalResult.value = null
      waitingInfo.value = null
    }
    if (event === 'plan') {
      approvalSteps.value = data.approvers.map(a => ({
        stepId: a.stepId, order: a.order, roleId: a.roleId, role: a.role,
        status: 'pending', decision: null, reason: '', checklist: [],
      }))
      if (data.chainReason?.length) chainReason.value = data.chainReason
    }
    if (event === 'approver_start') {
      const s = approvalSteps.value.find(x => x.roleId === data.roleId)
      if (s) s.status = 'reviewing'
    }
    if (event === 'message') {
      approvalMessages.value.push({ ...data, id: `m_${Date.now()}_${Math.random()}` })
    }
    if (event === 'approver_done') {
      const s = approvalSteps.value.find(x => x.roleId === data.roleId)
      if (s) {
        s.decision = data.decision
        s.reason = data.reason
        s.checklist = data.checklist || []
        s.status = data.decision === 'approve' ? 'approved' : 'rejected'
      }
    }
    if (event === 'need_info') {
      waitingInfo.value = data
      approving.value = false
      resuming.value = false
      const s = approvalSteps.value.find(x => x.stepId === data.stepId)
      if (s) { s.status = 'waiting_info'; s.questions = data.questions; s.reason = data.reason }
      // 直接在补料区修改申请数据
      if (data.formData) normalizeForm(data.formData, formType.value)
    }
    if (event === 'resumed') {
      waitingInfo.value = null
      if (data.formData) normalizeForm(data.formData, formType.value)
      const s = approvalSteps.value.find(x => x.stepId === data.stepId)
      if (s) s.status = 'reviewing'
    }
    if (event === 'paused') {
      approving.value = false
      resuming.value = false
    }
    if (event === 'final') {
      finalResult.value = data
      approving.value = false
      resuming.value = false
      waitingInfo.value = null
      loadApplications()
    }
    if (event === 'done') {
      approving.value = false
      resuming.value = false
    }
  }

  function handleError(err) {
    approving.value = false
    resuming.value = false
    appStore.toast.error(err.message || '审批流程出错')
  }

  // ── 提交审批 ──────────────────────────────────────────────────
  async function submitApproval(applicantName = '申请人') {
    if (!parsedForm.value || approving.value) return
    approving.value = true
    finalResult.value    = null
    waitingInfo.value    = null

    await fetchStream(
      '/api/erp/submit/stream',
      { formData: parsedForm.value, formType: formType.value, applicantName },
      { onEvent: handleEvent, onError: handleError, onDone: () => { approving.value = false } },
    )
    approving.value = false
  }

  // ── 补充材料并继续（关键：提出问题之后人是可以改的）─────────────
  async function resumeWithAnswer(answer, { patchForm = false } = {}) {
    if (!currentAppId.value || resuming.value) return
    resuming.value = true

    await fetchStream(
      `/api/erp/applications/${currentAppId.value}/resume/stream`,
      {
        answer: answer || '',
        // 补料区里改过的表单一起提交（服务端只接受白名单字段，并重算合计/工作日）
        formPatch: patchForm ? { ...parsedForm.value } : null,
      },
      { onEvent: handleEvent, onError: handleError, onDone: () => { resuming.value = false } },
    )
    resuming.value = false
  }

  async function loadApplications() {
    try {
      const data = await http.get('/erp/applications')
      applications.value = Array.isArray(data?.applications) ? data.applications : []
    } catch { /* 列表失败不打断主流程 */ }
  }

  async function loadApplication(appId) {
    try {
      const data = await http.get(`/erp/applications/${appId}`)
      const app = data.application
      currentAppId.value = app.id
      formType.value     = app.formType
      chainReason.value  = app.chainReason || []
      normalizeForm(app.formData, app.formType)
      approvalSteps.value = app.chain.map(s => ({
        stepId: s.stepId, order: s.order, roleId: s.roleId, role: s.role, status: s.status,
        decision: s.decision, reason: s.reason, checklist: s.checklist || [],
      }))
      approvalMessages.value = (app.messages || []).map((m, i) => ({ ...m, id: `h_${i}` }))
      finalResult.value = app.result || null
      waitingInfo.value = app.status === 'waiting_info'
        ? { appId: app.id, stepId: app.pendingStepId, questions: [], reason: '' }
        : null
      return app
    } catch (err) {
      appStore.toast.error(errMsg(err, '加载申请详情失败'))
    }
  }

  async function withdraw(appId) {
    try {
      await http.post(`/erp/applications/${appId}/withdraw`, {})
      if (currentAppId.value === appId) await loadApplication(appId)
      await loadApplications()
      appStore.toast.success('申请已撤回（记录保留）')
    } catch (err) {
      appStore.toast.error(errMsg(err, '撤回失败'))
    }
  }

  // 彻底删除一条申请记录（测试清理用）
  async function deleteApplication(appId) {
    try {
      await http.delete(`/erp/applications/${appId}`)
      if (currentAppId.value === appId) reset()
      await loadApplications()
      appStore.toast.success('申请记录已删除')
    } catch (err) {
      appStore.toast.error(errMsg(err, '删除失败'))
    }
  }

  // 清空全部申请记录
  async function clearApplications() {
    try {
      const data = await http.delete('/erp/applications')
      reset()
      await loadApplications()
      appStore.toast.success(`已清空 ${data.cleared ?? 0} 条申请记录`)
    } catch (err) {
      appStore.toast.error(errMsg(err, '清空失败'))
    }
  }

  function reset() {
    parsedForm.value       = null
    approvalMessages.value = []
    approvalSteps.value    = []
    finalResult.value      = null
    waitingInfo.value      = null
    currentAppId.value     = ''
    chainReason.value      = []
    localWarnings.value    = []
  }

  return {
    formType, parsedForm, parsing, localWarnings,
    approvalMessages, approvalSteps, approving, finalResult, currentAppId,
    chainReason, waitingInfo, resuming, applications, statusLabel,
    parseForm, addItem, removeItem, recomputeExpense, recomputeLeave,
    submitApproval, resumeWithAnswer,
    loadApplications, loadApplication, withdraw, deleteApplication, clearApplications, reset,
  }
})
