<script setup lang="ts">
import { computed } from 'vue'

interface ScheduleEvent {
  id: string
  title: string
  checkpoint: string
  start: string
  end: string | null
  last_status?: string | null
  last_error?: string | null
}

const props = defineProps<{ year: number; month: number; events: ScheduleEvent[] }>()
const emit = defineEmits<{
  'select-day': [iso: string]
  'select-event': [id: string]
}>()

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'
]
const DOW = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']

const monthLabel = computed(() => `${MONTH_NAMES[props.month]} ${props.year}`)

function dayKey(date: Date): string {
  return date.toISOString().split('T')[0]
}

function localDayKey(d: Date): string {
  // Use local-day for visual placement on the calendar
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

const grid = computed(() => {
  const first = new Date(props.year, props.month, 1)
  const last = new Date(props.year, props.month + 1, 0)
  const firstDow = (first.getDay() + 6) % 7
  const totalDays = last.getDate()

  const cells: Array<{ key: string; date: Date | null }> = []
  for (let i = 0; i < firstDow; i++) cells.push({ key: `e-${i}`, date: null })
  for (let i = 1; i <= totalDays; i++) {
    const d = new Date(props.year, props.month, i)
    cells.push({ key: localDayKey(d), date: d })
  }
  while (cells.length % 7 !== 0) {
    cells.push({ key: `t-${cells.length}`, date: null })
  }
  return cells
})

const eventsByDay = computed(() => {
  const out: Record<string, ScheduleEvent[]> = {}
  // For open-ended events (end == null) we paint through the last day of the
  // currently-shown month. The user can navigate forward to see subsequent
  // months and the same event will continue to show — that matches the
  // intended "pinned forever" UX.
  const lastDayOfMonth = new Date(props.year, props.month + 1, 0)
  for (const e of props.events) {
    const start = new Date(e.start)
    const end = e.end ? new Date(e.end) : lastDayOfMonth
    if (isNaN(end.getTime())) continue
    const cursor = new Date(start.getFullYear(), start.getMonth(), start.getDate())
    while (cursor <= end) {
      const k = localDayKey(cursor)
      if (!out[k]) out[k] = []
      out[k].push(e)
      cursor.setDate(cursor.getDate() + 1)
    }
  }
  return out
})

function isToday(date: Date): boolean {
  const t = new Date()
  return (
    date.getFullYear() === t.getFullYear() &&
    date.getMonth() === t.getMonth() &&
    date.getDate() === t.getDate()
  )
}

function eventClass(e: ScheduleEvent): string {
  const base = e.end ? 'event' : 'event open'
  if (e.last_error) return base + ' err'
  if (e.last_status) return base + ' ok'
  return base
}

function eventLabel(e: ScheduleEvent): string {
  return e.end ? e.title : `${e.title} →`
}
</script>

<template>
  <div class="card month-calendar">
    <div class="month-head">
      <h2>{{ monthLabel }}</h2>
      <div class="row">
        <slot name="actions" />
      </div>
    </div>

    <div class="dow">
      <div v-for="d in DOW" :key="d">{{ d }}</div>
    </div>

    <div class="grid-cells">
      <div
        v-for="cell in grid"
        :key="cell.key"
        :class="['cell', cell.date ? '' : 'empty', cell.date && isToday(cell.date) ? 'today' : '']"
        @click="cell.date && emit('select-day', dayKey(cell.date))"
      >
        <template v-if="cell.date">
          <div class="day-num">{{ cell.date.getDate() }}</div>
          <div class="events">
            <button
              v-for="e in eventsByDay[cell.key] || []"
              :key="e.id"
              :class="eventClass(e)"
              :title="`${e.title}\n${new Date(e.start).toLocaleString()} → ${e.end ? new Date(e.end).toLocaleString() : 'open-ended'}`"
              @click.stop="emit('select-event', e.id)"
            >
              {{ eventLabel(e) }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.month-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}
.month-head h2 {
  font-family: 'VT323', monospace;
  font-size: 1.5rem;
  letter-spacing: 0.06em;
  margin: 0;
}
.dow {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  font-size: 0.65rem;
  letter-spacing: 0.1em;
  color: var(--muted);
  margin-bottom: 4px;
}
.dow > div {
  padding: 0.2rem 0.4rem;
}
.grid-cells {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
}
.cell {
  background: var(--surface);
  min-height: 96px;
  padding: 0.4rem 0.4rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
}
.cell.empty {
  background: var(--bg);
  cursor: default;
}
.cell.today .day-num {
  color: var(--accent);
}
.day-num {
  font-family: 'VT323', monospace;
  font-size: 1rem;
  letter-spacing: 0.05em;
  color: var(--muted);
  margin-bottom: 0.25rem;
}
.events {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}
.event {
  appearance: none;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text);
  font-family: 'Space Mono', monospace;
  font-size: 0.7rem;
  text-align: left;
  padding: 0.15rem 0.35rem;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.event:hover {
  border-color: var(--accent);
}
.event.ok {
  border-left: 3px solid var(--green);
}
.event.err {
  border-left: 3px solid var(--red);
}
.event.open {
  background: linear-gradient(
    90deg,
    var(--surface-2) 0%,
    var(--surface-2) 92%,
    color-mix(in srgb, var(--accent) 30%, var(--surface-2)) 100%
  );
}
</style>
