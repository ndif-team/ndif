<script setup lang="ts">
import { computed, ref } from 'vue'

interface ConnectedEntry {
  timestamp: string
  status: string
}

const props = defineProps<{ entries: ConnectedEntry[] }>()

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
const DAY_LABELS = ['Mo','Tu','We','Th','Fr','Sa','Su']
const FULL_DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday']

function last30(): string[] {
  const out: string[] = []
  const now = new Date()
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(d.getDate() - i)
    out.push(d.toISOString().split('T')[0])
  }
  return out
}

const dayStatus = computed(() => {
  const map: Record<string, { total: number; failed: number; entries: ConnectedEntry[] }> = {}
  for (const e of props.entries) {
    const day = e.timestamp.split('T')[0]
    if (!map[day]) map[day] = { total: 0, failed: 0, entries: [] }
    map[day].total++
    if (e.status !== 'ok') map[day].failed++
    map[day].entries.push(e)
  }
  return map
})

const days = computed(() => last30())
const firstDow = computed(() => {
  const d = new Date(days.value[0] + 'T12:00:00')
  return (d.getDay() + 6) % 7
})

const monthLabel = computed(() => {
  const f = new Date(days.value[0] + 'T12:00:00')
  const l = new Date(days.value[days.value.length - 1] + 'T12:00:00')
  if (f.getMonth() === l.getMonth()) {
    return `${MONTHS[f.getMonth()]} ${f.getFullYear()}`
  }
  return `${MONTHS[f.getMonth()]} ${f.getFullYear()} – ${MONTHS[l.getMonth()]} ${l.getFullYear()}`
})

const selected = ref<string | null>(null)
const selectedDay = computed(() => {
  if (selected.value) return selected.value
  const today = new Date().toISOString().split('T')[0]
  return dayStatus.value[today] ? today : days.value[days.value.length - 1]
})

function dayClass(day: string): string {
  const st = dayStatus.value[day]
  let c = 'cal-day'
  if (!st) c += ' no-data'
  else c += st.failed > 0 ? ' fail' : ' ok'
  if (day === selectedDay.value) c += ' selected'
  return c
}

const timeline = computed(() => {
  const day = selectedDay.value
  const st = dayStatus.value[day]
  if (!st) return null
  const slots: (string | null)[] = new Array(144).fill(null)
  for (const e of st.entries) {
    const d = new Date(e.timestamp)
    const slot = d.getUTCHours() * 6 + Math.floor(d.getUTCMinutes() / 10)
    slots[slot] = e.status
  }
  const okCount = st.total - st.failed
  const detailDate = new Date(day + 'T12:00:00')
  return {
    label: `${FULL_DAYS[detailDate.getDay()]}, ${MONTHS[detailDate.getMonth()]} ${detailDate.getDate()}`,
    summary: `${okCount}/${st.total} ok — ${((okCount / st.total) * 100).toFixed(1)}%`,
    slots
  }
})
</script>

<template>
  <div class="card">
    <h3>Connected · 30 days</h3>
    <div class="cal-month">{{ monthLabel }}</div>

    <div class="cal-header">
      <div v-for="n in DAY_LABELS" :key="n" class="cal-label">{{ n }}</div>
    </div>

    <div class="cal-grid">
      <div v-for="i in firstDow" :key="`empty-${i}`" class="cal-day empty"></div>
      <div
        v-for="day in days"
        :key="day"
        :class="dayClass(day)"
        :title="day"
        @click="selected = day"
      ></div>
    </div>

    <div class="day-detail" v-if="timeline">
      <div class="day-detail-header">
        <span>{{ timeline.label }}</span>
        <span class="muted">{{ timeline.summary }}</span>
      </div>
      <div class="timeline">
        <div
          v-for="(s, idx) in timeline.slots"
          :key="idx"
          :class="['tl-slot', s === null ? 'gap' : s === 'ok' ? 'ok' : 'fail']"
          :title="`${String(Math.floor(idx / 6)).padStart(2, '0')}:${String((idx % 6) * 10).padStart(2, '0')} UTC — ${s ?? 'no data'}`"
        ></div>
      </div>
    </div>
    <p v-else class="muted day-detail-empty">No checks recorded</p>
  </div>
</template>

<style scoped>
.cal-month {
  font-family: 'VT323', monospace;
  letter-spacing: 0.05em;
  color: var(--muted);
  font-size: 1rem;
  margin-bottom: 0.5rem;
}
.cal-header,
.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
}
.cal-header {
  margin-bottom: 4px;
}
.cal-label {
  font-size: 0.6rem;
  color: var(--muted);
  text-align: center;
  letter-spacing: 0.1em;
}
.cal-day {
  aspect-ratio: 1 / 1;
  border: 1px solid var(--border);
  cursor: pointer;
  transition: transform 0.1s, border-color 0.15s;
}
.cal-day.empty {
  border-color: transparent;
  cursor: default;
}
.cal-day.no-data {
  background: transparent;
}
.cal-day.ok {
  background: var(--green-soft);
  border-color: var(--green);
}
.cal-day.fail {
  background: var(--red-soft);
  border-color: var(--red);
}
.cal-day.selected {
  outline: 1px solid var(--selected-ring);
  outline-offset: 1px;
}
.day-detail {
  margin-top: 1rem;
}
.day-detail-header {
  display: flex;
  justify-content: space-between;
  font-size: 0.85rem;
  margin-bottom: 0.4rem;
}
.day-detail-empty {
  margin-top: 0.75rem;
  font-size: 0.8rem;
}
.timeline {
  display: grid;
  grid-template-columns: repeat(144, 1fr);
  gap: 1px;
  height: 18px;
}
.tl-slot {
  height: 100%;
}
.tl-slot.ok {
  background: var(--green);
}
.tl-slot.fail {
  background: var(--red);
}
.tl-slot.gap {
  background: var(--border);
  opacity: 0.5;
}
</style>
