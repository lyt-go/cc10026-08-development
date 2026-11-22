<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="meta.key === 'data_merge'" class="ledger-panel" data-testid="merge-ledger">
      <header class="ledger-head">
        <h3>正式归并批次与撤回回执</h3>
        <button class="btn ghost" data-testid="refresh-ledger" type="button" @click="reloadLedger">
          刷新台账
        </button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次号</th><th>规则</th><th>主来源</th><th>从来源</th>
            <th>批次状态</th><th>关联原归并</th><th>撤回回执</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in mergeBatches" :key="batch.batch_no" :data-testid="`batch-${batch.batch_no}`">
            <td>{{ batch.batch_no }}</td>
            <td>{{ batch.rule_no }}</td>
            <td>{{ batch.primary_api }}</td>
            <td>{{ batch.secondary_api }}</td>
            <td>{{ batch.status }}</td>
            <td>{{ batch.corrected_from || '—' }}</td>
            <td>{{ batch.receipt_no || '—' }}</td>
          </tr>
          <tr v-if="!mergeBatches.length">
            <td colspan="7" class="empty-state">暂无正式批次，先在归并规则上发布正式批次</td>
          </tr>
        </tbody>
      </table>
      <div v-for="receipt in receipts" :key="receipt.receipt_no" class="receipt-card" :data-testid="`receipt-${receipt.receipt_no}`">
        <strong>{{ receipt.receipt_no }}</strong>
        <span>撤回批次 {{ receipt.batch_no }}：{{ receipt.reason }}</span>
        <ul>
          <li v-for="item in receipt.affected" :key="String(item.registration_id)">
            登记 {{ item.task_no }}：{{ item.state_before }} → {{ item.state_after }}
            （{{ item.next_action }}）
          </li>
        </ul>
      </div>
    </section>

    <section v-if="meta.key === 'export_task'" class="ledger-panel" data-testid="export-registrations">
      <header class="ledger-head">
        <h3>交付登记与撤回回执</h3>
        <button class="btn ghost" data-testid="refresh-registrations" type="button" @click="reloadRegistrations">
          刷新登记
        </button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>登记</th><th>登记状态</th><th>当前批次</th><th>原归并</th>
            <th>交付编号</th><th>交付文件</th><th>撤回回执</th><th>交付历史</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in registrations" :key="String(item.registration_id)" :data-testid="`registration-${item.registration_id}`">
            <td>{{ item.task_no }}</td>
            <td>{{ item.state }}</td>
            <td>{{ item.batch_no || '—' }}</td>
            <td>{{ item.original_batch_no || '—' }}</td>
            <td>{{ item.delivery_no || '—' }}</td>
            <td>{{ item.delivery_file || '—' }}</td>
            <td>{{ item.receipt_no || '—' }}</td>
            <td>
              <span v-for="delivery in item.deliveries" :key="delivery.delivery_id" class="delivery-chip">
                {{ delivery.delivery_id }}（{{ delivery.status }}）
              </span>
            </td>
          </tr>
          <tr v-if="!registrations.length">
            <td colspan="8" class="empty-state">暂无关联归并批次的导出登记</td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import { fetchJson } from '@/api/client'
import type { EntryRow } from '@/data/types'

type MergeBatch = {
  batch_no: string
  rule_no: string
  primary_api: string
  secondary_api: string
  status: string
  corrected_from: string
  receipt_no: string
}

type WithdrawReceipt = {
  receipt_no: string
  batch_no: string
  reason: string
  affected: {
    registration_id: number
    task_no: string
    state_before: string
    state_after: string
    next_action: string
  }[]
}

type RegistrationState = {
  registration_id: number
  task_no: string
  state: string
  batch_no: string
  original_batch_no: string
  delivery_no: string
  delivery_file: string
  receipt_no: string
  deliveries: { delivery_id: string; status: string; batch_no: string }[]
}

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const mergeBatches = ref<MergeBatch[]>([])
const receipts = ref<WithdrawReceipt[]>([])
const registrations = ref<RegistrationState[]>([])
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

function actionRemark(action: string): string | undefined {
  if (action === '撤回正式批次') {
    const reason = window.prompt('请填写撤回原因（会写入撤回回执）', '归并版引用了错误来源')
    if (reason === null) return undefined
    return reason.trim()
  }
  if (action === '登记更正来源') {
    return window.prompt('请说明更正来源补齐情况', '更正来源已补齐并核验')?.trim()
  }
  return undefined
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  const remark = actionRemark(action)
  if ((action === '撤回正式批次' || action === '登记更正来源') && remark === undefined) return
  try {
    const result = await applyAction(meta.key, Number(row.id), action, remark)
    if (!result.ok) throw new Error(result.message)
    await reload()
    await reloadLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reloadLedger() {
  try {
    const [batchPayload, receiptPayload] = await Promise.all([
      fetchJson<{ items: MergeBatch[] }>('/api/data_merge/batches'),
      fetchJson<{ items: WithdrawReceipt[] }>('/api/data_merge/receipts'),
    ])
    mergeBatches.value = batchPayload.items
    receipts.value = receiptPayload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归并台账读取失败'
  }
}

async function reloadRegistrations() {
  try {
    const payload = await fetchJson<{ items: RegistrationState[] }>(
      '/api/export_task/registrations',
    )
    registrations.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付登记读取失败'
  }
}

async function reloadLedgerPanels() {
  if (meta.key === 'data_merge') await reloadLedger()
  if (meta.key === 'export_task') await reloadRegistrations()
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    await reloadLedgerPanels()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
