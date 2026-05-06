<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { api, ApiError } from '@/api'
import MonthCalendar from '@/components/schedule/MonthCalendar.vue'
import EventModal, {
  type EventForm
} from '@/components/schedule/EventModal.vue'
import { DEFAULT_ENVOY_CLASS } from '@/deploy'
import { useCache } from '@/composables/useCache'

interface ScheduleEvent {
  id: string
  title: string
  checkpoint: string
  revision: string | null
  actor_class: string | null
  envoy_class: string | null
  padding_factor: number | null
  execution_timeout_seconds: number | null
  start: string
  end: string | null
  created_at: string
  updated_at: string
  last_status: string | null
  last_error: string | null
}

const events = ref<ScheduleEvent[]>([])
const loading = ref(true)
const fetchError = ref<string | null>(null)

const today = new Date()
const year = ref(today.getFullYear())
const month = ref(today.getMonth())

const modalOpen = ref(false)
const modalMode = ref<'create' | 'edit'>('create')
const modalInitial = ref<EventForm>(emptyForm())
const saving = ref(false)
const saveError = ref<string | null>(null)
const { cache, refresh: loadCache } = useCache()

function emptyForm(start?: Date): EventForm {
  const s = start ?? new Date()
  s.setMinutes(0, 0, 0)
  const e = new Date(s)
  e.setHours(e.getHours() + 1)
  return {
    title: '',
    checkpoint: '',
    revision: null,
    actor_class: null,
    envoy_class: DEFAULT_ENVOY_CLASS,
    padding_factor: null,
    execution_timeout_seconds: null,
    start: s.toISOString(),
    end: e.toISOString()
  }
}

async function load() {
  try {
    events.value = await api.get<ScheduleEvent[]>('/api/schedule')
    fetchError.value = null
  } catch (e) {
    fetchError.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

function prevMonth() {
  if (month.value === 0) {
    month.value = 11
    year.value--
  } else {
    month.value--
  }
}
function nextMonth() {
  if (month.value === 11) {
    month.value = 0
    year.value++
  } else {
    month.value++
  }
}
function goToday() {
  const t = new Date()
  year.value = t.getFullYear()
  month.value = t.getMonth()
}

function openCreate(day?: string) {
  modalMode.value = 'create'
  const d = day ? new Date(day + 'T09:00:00') : undefined
  modalInitial.value = emptyForm(d)
  saveError.value = null
  modalOpen.value = true
}

function openEdit(eventId: string) {
  const e = events.value.find((x) => x.id === eventId)
  if (!e) return
  modalMode.value = 'edit'
  modalInitial.value = {
    id: e.id,
    title: e.title,
    checkpoint: e.checkpoint,
    revision: e.revision,
    actor_class: e.actor_class,
    envoy_class: e.envoy_class ?? DEFAULT_ENVOY_CLASS,
    padding_factor: e.padding_factor,
    execution_timeout_seconds: e.execution_timeout_seconds,
    start: e.start,
    end: e.end
  }
  saveError.value = null
  modalOpen.value = true
}

function close() {
  modalOpen.value = false
}

async function save(data: EventForm) {
  saveError.value = null
  saving.value = true
  try {
    const payload = {
      title: data.title,
      checkpoint: data.checkpoint,
      revision: data.revision || null,
      actor_class: data.actor_class || null,
      envoy_class: data.envoy_class || null,
      padding_factor: data.padding_factor ?? null,
      execution_timeout_seconds: data.execution_timeout_seconds ?? null,
      start: data.start,
      end: data.end
    }
    if (modalMode.value === 'create') {
      await api.post('/api/schedule', payload)
    } else if (data.id) {
      await api.put(`/api/schedule/${data.id}`, payload)
    }
    await load()
    // The reconcile background task may have just deployed (and bumped the
    // cache); refresh so the next modal sees the new entries.
    loadCache()
    modalOpen.value = false
  } catch (e) {
    saveError.value =
      e instanceof ApiError && typeof e.detail === 'string'
        ? e.detail
        : (e as Error).message
  } finally {
    saving.value = false
  }
}

async function remove(id: string) {
  if (!confirm('Delete this scheduled deployment?')) return
  saving.value = true
  try {
    await api.del(`/api/schedule/${id}`)
    await load()
    modalOpen.value = false
  } catch (e) {
    saveError.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

function duplicate(data: EventForm) {
  modalMode.value = 'create'
  modalInitial.value = { ...data, id: undefined, title: data.title + ' (copy)' }
  saveError.value = null
}

const upcoming = computed(() => {
  const now = Date.now()
  return [...events.value]
    .filter((e) => e.end == null || new Date(e.end).getTime() >= now)
    .sort((a, b) => new Date(a.start).getTime() - new Date(b.start).getTime())
    .slice(0, 8)
})
</script>

<template>
  <section class="schedule">
    <div class="row schedule-head">
      <h1 class="page-title">Schedule</h1>
      <div class="row">
        <button class="btn" @click="prevMonth">←</button>
        <button class="btn" @click="goToday">Today</button>
        <button class="btn" @click="nextMonth">→</button>
        <button class="btn primary" @click="openCreate()">+ New</button>
      </div>
    </div>

    <div v-if="fetchError" class="card error">{{ fetchError }}</div>

    <MonthCalendar
      :year="year"
      :month="month"
      :events="events"
      @select-day="openCreate"
      @select-event="openEdit"
    />

    <div class="card">
      <h3>Upcoming</h3>
      <table class="upcoming-table" v-if="upcoming.length">
        <thead>
          <tr>
            <th>Title</th>
            <th>Checkpoint</th>
            <th>Start</th>
            <th>End</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="e in upcoming"
            :key="e.id"
            class="row-click"
            @click="openEdit(e.id)"
          >
            <td>{{ e.title }}</td>
            <td class="mono">{{ e.checkpoint }}</td>
            <td>{{ new Date(e.start).toLocaleString() }}</td>
            <td>{{ e.end ? new Date(e.end).toLocaleString() : '— open-ended' }}</td>
            <td>
              <span v-if="e.last_error" class="pill bad">err</span>
              <span v-else-if="e.last_status" class="pill ok">{{ e.last_status }}</span>
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">No upcoming deployments</p>
    </div>

    <EventModal
      v-if="modalOpen"
      :initial="modalInitial"
      :mode="modalMode"
      :saving="saving"
      :error="saveError"
      :cache="cache"
      @save="save"
      @close="close"
      @delete="remove"
      @duplicate="duplicate"
    />
  </section>
</template>

<style scoped>
.schedule {
  display: grid;
  gap: 1rem;
}
.schedule-head {
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
.page-title {
  font-family: 'VT323', monospace;
  font-weight: normal;
  font-size: 1.6rem;
  letter-spacing: 0.06em;
}
.error {
  color: var(--red);
}
.upcoming-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.upcoming-table th {
  text-align: left;
  font-size: 0.65rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid var(--border);
}
.upcoming-table td {
  padding: 0.45rem 0.5rem;
  border-bottom: 1px solid var(--border);
}
.row-click {
  cursor: pointer;
}
.row-click:hover {
  background: var(--surface-2);
}
.mono {
  font-family: 'Space Mono', monospace;
}
</style>
