<template>
  <!-- frontend/src/components/erp/SmartFormParser.vue
       智能填单：自然语言 → AI 解析 → **逐字段可编辑** → 提交审批。
       AI 抽错的金额/日期/事由必须能在提交前改掉，否则错误会直接进入审批流。 -->
  <div class="smart-parser">
    <div class="input-section">
      <div class="input-header">
        <span class="input-label">用自然语言描述</span>
      </div>
      <div class="example-chips">
        <span v-for="eg in currentExamples" :key="eg" class="example-chip" @click="inputText = eg">
          {{ eg.slice(0, 16) }}...
        </span>
      </div>
      <textarea v-model="inputText" class="input nl-input" :placeholder="currentPlaceholder" rows="3"
                :disabled="erpStore.parsing" />
      <div class="parse-actions">
        <span class="char-hint">{{ inputText.length }} 字</span>
        <button class="btn btn-primary" :disabled="!inputText.trim() || erpStore.parsing" @click="doParse">
          <span v-if="erpStore.parsing" class="spinner" />
          {{ erpStore.parsing ? '解析中...' : 'AI 解析' }}
        </button>
      </div>
    </div>

    <!-- 报销表单（可编辑） -->
    <div v-if="form && erpStore.formType === 'expense'" class="parsed-form">
      <div class="form-title">
        <span>报销申请单</span>
        <span class="form-badge">可修改</span>
      </div>

      <div v-if="form.warnings?.length" class="warnings">
        <div v-for="w in form.warnings" :key="w" class="warning-item">⚠ {{ w }}</div>
      </div>

      <div class="form-grid">
        <label class="field"><span class="lb">费用类型</span>
          <select v-model="form.type" class="input">
            <option value="travel">差旅费</option><option value="meal">餐饮费</option>
            <option value="office">办公用品</option><option value="training">培训费</option>
            <option value="other">其他</option>
          </select>
        </label>
        <label class="field"><span class="lb">报销部门</span>
          <input v-model="form.dept" class="input" placeholder="如：技术研发部" />
        </label>
        <label class="field span2"><span class="lb">报销事由</span>
          <input v-model="form.reason" class="input" placeholder="20 字以内" />
        </label>
      </div>

      <div class="detail-title">
        费用明细
        <button class="mini" @click="erpStore.addItem()">+ 添加一行</button>
      </div>
      <div class="items">
        <div class="item-head"><span>项目</span><span class="num">金额</span><span>发生日期</span><span>备注</span><span /></div>
        <div v-for="(item, i) in form.items" :key="i" class="item-row">
          <input v-model="item.name" class="input" placeholder="如：高铁票" />
          <input v-model.number="item.amount" type="number" min="0" step="0.01" class="input num"
                 @input="erpStore.recomputeExpense()" />
          <input v-model="item.date" type="date" class="input" />
          <input v-model="item.note" class="input" placeholder="可选" />
          <button class="del" @click="erpStore.removeItem(i)" title="删除">×</button>
        </div>
        <div v-if="!form.items?.length" class="item-empty">还没有明细，点「添加一行」</div>
        <div class="item-total">合计 ¥{{ form.totalAmount }}（按明细自动汇总）</div>
      </div>
    </div>

    <!-- 请假表单（可编辑） -->
    <div v-if="form && erpStore.formType === 'leave'" class="parsed-form">
      <div class="form-title">
        <span>请假申请单</span>
        <span class="form-badge">可修改</span>
      </div>

      <div v-if="form.warnings?.length" class="warnings">
        <div v-for="w in form.warnings" :key="w" class="warning-item">⚠ {{ w }}</div>
      </div>

      <div class="form-grid">
        <label class="field"><span class="lb">假期类型</span>
          <select v-model="form.type" class="input">
            <option value="annual">年假</option><option value="personal">事假</option>
            <option value="sick">病假</option><option value="compensatory">调休</option>
            <option value="marriage">婚假</option><option value="maternity">产假</option>
          </select>
        </label>
        <label class="field"><span class="lb">紧急联系人</span>
          <input v-model="form.emergencyContact" class="input" placeholder="可选" />
        </label>
        <label class="field"><span class="lb">开始日期</span>
          <input v-model="form.startDate" type="date" class="input" @change="erpStore.recomputeLeave()" />
        </label>
        <label class="field"><span class="lb">结束日期</span>
          <input v-model="form.endDate" type="date" class="input" @change="erpStore.recomputeLeave()" />
        </label>
        <div class="field"><span class="lb">天数</span>
          <span class="readonly">{{ form.days }} 自然日 / {{ form.workdays }} 工作日（自动计算）</span>
        </div>
        <label class="field"><span class="lb">请假原因</span>
          <input v-model="form.reason" class="input" placeholder="30 字以内" />
        </label>
      </div>
    </div>

    <!-- 提交 -->
    <div v-if="form && !erpStore.approving && !erpStore.finalResult" class="submit-area">
      <div class="submit-hint">确认无误后提交；审批中若审批人要求补充材料，流程会暂停等你处理。</div>
      <div class="submit-row">
        <button class="btn btn-ghost" @click="erpStore.reset()">重新填写</button>
        <button class="btn btn-primary" @click="submit">提交审批</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useErpStore } from '@/stores/erp.js'

const erpStore  = useErpStore()
const inputText = ref('')

const examples = {
  expense: [
    '上周去上海出差，高铁票来回980元，住宿两晚共1100元，餐饮三天共420元，请帮我填报销单',
    '购买了两本技术书籍，共158元，用于学习新框架',
    '和客户吃工作餐，消费460元，请帮我报销',
  ],
  leave: [
    '我下周一到周三请年假，去外地旅游，请帮我走申请流程',
    '我明天需要请一天事假，去医院体检',
    '我想请婚假，结婚典礼在下个月5号',
  ],
}
const currentExamples = computed(() => examples[erpStore.formType] || [])
const currentPlaceholder = computed(() => erpStore.formType === 'expense'
  ? '如："上周去北京出差，高铁来回820元，住宿两晚1160元，帮我填报销单"'
  : '如："我下周一到周三请年假，去外地旅游，请帮我走请假申请"')

const form = computed(() => erpStore.parsedForm)

async function doParse() {
  await erpStore.parseForm(inputText.value)
}
async function submit() {
  await erpStore.submitApproval('申请人')
}
</script>

<style scoped>
.smart-parser { display: flex; flex-direction: column; gap: 12px; }
.input-section { display: flex; flex-direction: column; gap: 7px; }
.input-header { display: flex; align-items: center; justify-content: space-between; }
.input-label { font-size: 12.5px; font-weight: 600; color: var(--color-text); }
.example-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.example-chip { font-size: 11px; color: var(--color-text-sub); background: var(--color-bg); border: 1px solid var(--color-border-light); border-radius: var(--radius-full); padding: 2px 8px; cursor: pointer; }
.example-chip:hover { border-color: var(--color-primary); color: var(--color-primary); }
.input { border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 5px 8px; font-size: 12px; background: var(--color-surface); color: var(--color-text); outline: none; width: 100%; }
.input:focus { border-color: var(--color-primary); }
.nl-input { resize: vertical; font-family: var(--font-sans); }
.parse-actions { display: flex; align-items: center; justify-content: space-between; }
.char-hint { font-size: 11px; color: var(--color-text-muted); }
.btn { border: 1px solid var(--color-border); background: var(--color-surface); color: var(--color-text-sub); border-radius: var(--radius-md); padding: 6px 12px; font-size: 12px; cursor: pointer; }
.btn-primary { background: var(--color-primary); border-color: var(--color-primary); color: #fff; font-weight: 600; }
.btn-primary:disabled { opacity: .5; cursor: not-allowed; }
.btn-ghost { background: var(--color-surface); }

.parsed-form { border-top: 1px dashed var(--color-border); padding-top: 10px; display: flex; flex-direction: column; gap: 9px; }
.form-title { display: flex; align-items: center; gap: 8px; font-size: 12.5px; font-weight: 600; color: var(--color-text); }
.form-badge { font-size: 10.5px; font-weight: 400; color: var(--color-primary-dark); background: var(--color-primary-bg); border-radius: var(--radius-full); padding: 1px 8px; }
.warnings { display: flex; flex-direction: column; gap: 4px; }
.warning-item { font-size: 11.5px; color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: var(--radius-sm); padding: 4px 8px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.field { display: flex; flex-direction: column; gap: 3px; }
.field.span2 { grid-column: span 2; }
.lb { font-size: 11px; color: var(--color-text-sub); }
.readonly { font-size: 11.5px; color: var(--color-text); }
.detail-title { display: flex; align-items: center; justify-content: space-between; font-size: 11.5px; font-weight: 600; color: var(--color-text-sub); }
.mini { border: 1px solid var(--color-border); background: var(--color-surface); border-radius: var(--radius-sm); font-size: 11px; padding: 2px 8px; cursor: pointer; color: var(--color-primary); }
.items { display: flex; flex-direction: column; gap: 5px; }
.item-head, .item-row { display: grid; grid-template-columns: 1.3fr 0.8fr 1.1fr 1fr 22px; gap: 5px; align-items: center; }
.item-head { font-size: 10.5px; color: var(--color-text-muted); }
.item-head .num { text-align: right; }
.item-row .input.num { text-align: right; }
.del { border: none; background: none; color: var(--color-danger); cursor: pointer; font-size: 14px; }
.item-empty { font-size: 11.5px; color: var(--color-text-muted); text-align: center; padding: 6px 0; }
.item-total { font-size: 11.5px; font-weight: 600; color: var(--color-primary); text-align: right; }
.submit-area { display: flex; flex-direction: column; gap: 6px; }
.submit-hint { font-size: 11px; color: var(--color-text-muted); line-height: 1.5; }
.submit-row { display: flex; justify-content: flex-end; gap: 8px; }
.spinner { display: inline-block; width: 10px; height: 10px; margin-right: 5px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
