<script setup lang="ts">
import { ref, computed } from 'vue'

// One replica record inside a Deployment's ``replicas`` array. The
// dashboard's /api/status aggregates per-replica entries from the
// controller into the parent Deployment, so each model gets one card and
// each card carries N of these.
export interface ReplicaInfo {
  replica_id: string
  deployment_level?: 'HOT' | 'WARM' | string
  application_state?: string
  pinned?: boolean
}

export interface Deployment {
  model_key: string
  repo_id?: string
  revision?: string | null
  // Card-level "best" across replicas (HOT > WARM > COLD).
  deployment_level?: 'HOT' | 'WARM' | 'COLD' | string
  // Card-level "best" across replicas (RUNNING > DEPLOYING > NOT_STARTED >
  // UNHEALTHY).
  application_state?: string
  // OR across replicas; also OR'd with the active schedule.
  pinned?: boolean
  n_params?: number
  size_bytes?: number
  actor_class?: string | null
  schedule?: { start_time?: string; end_time?: string; title?: string } | null
  replicas?: ReplicaInfo[]
  // pending = optimistic placeholder for an in-flight deploy. The card
  // shows a pulse + no badges/menu until the server-side state catches up.
  pending?: boolean
}

const props = defineProps<{
  deployment: Deployment
  busy?: boolean
}>()

const emit = defineEmits<{
  restart: [d: Deployment]
  evict: [d: Deployment]
  deploy: [d: Deployment]
  addReplica: [d: Deployment]
  restartReplica: [d: Deployment, replicaId: string]
  evictReplica: [d: Deployment, replicaId: string]
  deployReplica: [d: Deployment, replicaId: string]
}>()

const menuOpen = ref(false)

function close() {
  menuOpen.value = false
}

const levelClass = computed(() => 'level-' + (props.deployment.deployment_level || '').toLowerCase())

// COLD shows a standalone Deploy button (opens the deploy modal so the user
// can pick actor_class / pinned / etc.). WARM and HOT use a kebab menu
// with "Add Replica" + actions appropriate to the card-level state.
const isCold = computed(() => props.deployment.deployment_level === 'COLD')
const isWarm = computed(() => props.deployment.deployment_level === 'WARM')
const isHot = computed(() => props.deployment.deployment_level === 'HOT')
const isPending = computed(() => !!props.deployment.pending)

const replicas = computed<ReplicaInfo[]>(() => props.deployment.replicas || [])

// Counts for the small "M/N ready" badge under the state pill.
const replicaCounts = computed(() => {
  let ready = 0
  let total = 0
  for (const r of replicas.value) {
    if (r.deployment_level === 'HOT') {
      total++
      if (r.application_state === 'RUNNING') ready++
    }
  }
  return { ready, total }
})

// Last segment of a dotted import path. ``foo.bar.Baz`` → ``Baz``.
function basename(path: string | null | undefined): string | null {
  if (!path) return null
  const i = path.lastIndexOf('.')
  return i === -1 ? path : path.slice(i + 1)
}

// Envoy class is the prefix before ``:`` in the model_key
// (e.g. ``nnsight.modeling.vlm.VisionLanguageModel:{...}``).
const envoyClass = computed(() => {
  const k = props.deployment.model_key || ''
  const i = k.indexOf(':')
  return basename(i === -1 ? null : k.slice(0, i))
})
const actorClass = computed(() => basename(props.deployment.actor_class))

const params = computed(() => {
  const n = props.deployment.n_params
  if (!n) return null
  return n / 1e9 < 1 ? (n / 1e9).toFixed(1) + 'B' : Math.round(n / 1e9) + 'B'
})
const sizeGB = computed(() => {
  const b = props.deployment.size_bytes
  if (!b) return null
  return (b / 1024 ** 3).toFixed(1) + ' GB'
})

function fmtRemaining(end: string | undefined): string | null {
  if (!end) return null
  const diff = new Date(end).getTime() - Date.now()
  if (diff < 0) return 'ended'
  const s = Math.floor(diff / 1000)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 10) return '>10d left'
  if (d > 0) return `${d}d ${h}h left`
  if (h > 0) return `${h}h ${m}m left`
  return `${m}m left`
}

const scheduleLabel = computed(() => {
  const s = props.deployment.schedule
  if (!s || !s.start_time) return null
  if (!s.end_time) return 'pinned'
  const now = new Date().getTime()
  const start = new Date(s.start_time).getTime()
  if (now < start) {
    return (
      'starts ' +
      new Date(s.start_time).toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: 'numeric'
      })
    )
  }
  return fmtRemaining(s.end_time)
})

// ---- replica dot styling ---------------------------------------------------

function replicaClass(r: ReplicaInfo): string {
  const lvl = r.deployment_level
  const state = r.application_state
  const cls = ['replica-dot']
  if (r.pinned) cls.push('pinned')
  if (lvl === 'WARM') cls.push('warm')
  else if (lvl === 'HOT') {
    cls.push('hot')
    if (state === 'RUNNING') cls.push('running')
    else if (state === 'DEPLOYING' || state === 'NOT_STARTED')
      cls.push('deploying')
    else if (state === 'UNHEALTHY') cls.push('unhealthy')
  }
  return cls.join(' ')
}

function replicaTooltip(r: ReplicaInfo): string {
  const bits: string[] = [r.replica_id]
  if (r.deployment_level) bits.push(r.deployment_level.toLowerCase())
  if (r.application_state) bits.push(r.application_state.toLowerCase())
  if (r.pinned) bits.push('pinned')
  return bits.join(' · ')
}

// ---- handlers --------------------------------------------------------------

function onRestart() {
  close()
  const name = props.deployment.repo_id || props.deployment.model_key
  const n = replicas.value.filter((r) => r.deployment_level === 'HOT').length
  if (!confirm(`Restart ${n} replica(s) of ${name}?`)) return
  emit('restart', props.deployment)
}

function onEvict() {
  close()
  const name = props.deployment.repo_id || props.deployment.model_key
  const n = replicas.value.length
  // If this model is in an active schedule entry, the next reconcile tick
  // will re-deploy it. The admin should remove the entry from the Schedule
  // tab if they want it gone permanently.
  const noun = n === 1 ? 'replica' : 'replicas'
  const what = props.deployment.pinned
    ? `Evict all ${n} ${noun} of ${name}?\n\n(This is pinned. If a schedule entry covers it, the next reconcile will re-deploy it. Remove the entry from the Schedule tab to make it permanent.)`
    : `Evict all ${n} ${noun} of ${name}?`
  if (!confirm(what)) return
  emit('evict', props.deployment)
}

function onDeploy() {
  close()
  emit('deploy', props.deployment)
}

function onAddReplica() {
  close()
  emit('addReplica', props.deployment)
}

function onReplicaRestart(r: ReplicaInfo) {
  emit('restartReplica', props.deployment, r.replica_id)
}

function onReplicaEvict(r: ReplicaInfo) {
  emit('evictReplica', props.deployment, r.replica_id)
}

function onReplicaDeploy(r: ReplicaInfo) {
  emit('deployReplica', props.deployment, r.replica_id)
}

function onMenuBlur(e: FocusEvent) {
  const next = e.relatedTarget as HTMLElement | null
  const root = (e.currentTarget as HTMLElement).closest('.menu-wrap')
  if (!next || !root || !root.contains(next)) close()
}
</script>

<template>
  <div :class="['nn-deployment', levelClass, busy ? 'busy' : '', isPending ? 'pending' : '']">
    <div class="nn-card-body">
      <div class="card-head">
        <div class="repo-id" :title="deployment.repo_id || deployment.model_key">
          {{ deployment.repo_id || deployment.model_key }}
        </div>

        <!-- Pending placeholder: just a pulse where the menu would be -->
        <span v-if="isPending" class="pulse-dot" aria-label="deploying"></span>

        <!-- COLD: standalone Deploy button — opens the deploy modal so the
             user can pick actor_class / pinned / etc. -->
        <button
          v-else-if="isCold"
          type="button"
          class="cold-deploy-btn"
          :disabled="busy"
          @click.stop="onDeploy"
        >
          Deploy
        </button>

        <!-- WARM (no HOT replicas): Add Replica + Evict All -->
        <div v-else-if="isWarm" class="menu-wrap" @focusout="onMenuBlur">
          <button
            class="kebab"
            type="button"
            :disabled="busy"
            aria-label="Actions"
            @click.stop="menuOpen = !menuOpen"
            @blur="onMenuBlur"
          >
            ⋯
          </button>
          <div v-if="menuOpen" class="menu" role="menu">
            <button type="button" @click="onAddReplica">Add Replica</button>
            <button type="button" class="danger" @click="onEvict">Evict All</button>
          </div>
        </div>

        <!-- HOT: Restart All + Add Replica + Evict All -->
        <div v-else-if="isHot" class="menu-wrap" @focusout="onMenuBlur">
          <button
            class="kebab"
            type="button"
            :disabled="busy"
            aria-label="Actions"
            @click.stop="menuOpen = !menuOpen"
            @blur="onMenuBlur"
          >
            ⋯
          </button>
          <div v-if="menuOpen" class="menu" role="menu">
            <button type="button" @click="onRestart">Restart All</button>
            <button type="button" @click="onAddReplica">Add Replica</button>
            <button type="button" class="danger" @click="onEvict">Evict All</button>
          </div>
        </div>
      </div>

      <!-- Pending: deliberately empty body apart from a "deploying..." line -->
      <div v-if="isPending" class="pending-line">deploying<span class="dots">…</span></div>

      <template v-else>
        <div class="badges">
          <span :class="['pill', 'level-pill', levelClass]">
            {{ deployment.deployment_level || '?' }}
          </span>
          <span
            v-if="deployment.application_state"
            :class="[
              'pill',
              deployment.application_state === 'RUNNING'
                ? 'ok'
                : deployment.application_state === 'DEPLOYING' ||
                  deployment.application_state === 'NOT_STARTED'
                ? 'warn'
                : 'bad'
            ]"
          >
            {{ deployment.application_state.toLowerCase() }}
          </span>
          <span
            v-if="deployment.pinned"
            class="pill pinned"
            :title="deployment.schedule ? 'Scheduled deployment' : 'Pinned'"
          >
            {{ scheduleLabel ? 'pinned · ' + scheduleLabel : 'pinned' }}
          </span>
          <span
            v-if="replicaCounts.total > 1"
            class="pill replica-count"
            :title="'HOT replicas ready / total'"
          >
            {{ replicaCounts.ready }}/{{ replicaCounts.total }} ready
          </span>
        </div>

        <!-- Replica dots — one per replica, hover shows per-replica actions. -->
        <div v-if="replicas.length" class="replica-row">
          <div
            v-for="r in replicas"
            :key="r.replica_id"
            class="replica-wrap"
          >
            <span
              :class="replicaClass(r)"
              :title="replicaTooltip(r)"
              :aria-label="replicaTooltip(r)"
              tabindex="0"
            ></span>
            <div class="replica-menu" role="menu">
              <div class="replica-menu-label">{{ r.replica_id }}</div>
              <button
                v-if="r.deployment_level === 'HOT'"
                type="button"
                @click.stop="onReplicaRestart(r)"
              >
                Restart
              </button>
              <button
                v-else-if="r.deployment_level === 'WARM'"
                type="button"
                @click.stop="onReplicaDeploy(r)"
              >
                Deploy
              </button>
              <button type="button" class="danger" @click.stop="onReplicaEvict(r)">
                Evict
              </button>
            </div>
          </div>
        </div>

        <div class="meta">
          <span v-if="deployment.revision" :title="'revision: ' + deployment.revision">
            ⌖ {{ deployment.revision }}
          </span>
          <span v-if="params" :title="'parameters'">⊟ {{ params }}</span>
          <span v-if="sizeGB" :title="'GPU memory footprint'">⊞ {{ sizeGB }}</span>
          <span
            v-if="envoyClass"
            :title="'envoy class: ' + (deployment.model_key || '').split(':')[0]"
          >
            ◉ {{ envoyClass }}
          </span>
          <span
            v-if="actorClass"
            :title="'actor class: ' + deployment.actor_class"
          >
            ⏵ {{ actorClass }}
          </span>
        </div>
      </template>
    </div>

    <a
      v-if="deployment.repo_id && !isPending"
      :href="`https://huggingface.co/${deployment.repo_id}`"
      target="_blank"
      rel="noopener"
      class="hf-link"
      :tabindex="busy ? -1 : 0"
      @click.stop
      >HF</a
    >

    <div v-if="busy && !isPending" class="busy-overlay">
      <div class="spinner"></div>
    </div>
  </div>
</template>

<style scoped>
.nn-deployment {
  position: relative;
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 3px solid var(--muted);
  display: flex;
  flex-direction: column;
  min-height: 110px;
  transition: border-color 0.15s, transform 0.1s;
}
.nn-deployment:hover {
  border-color: var(--accent);
}
.nn-deployment.level-hot {
  border-left-color: var(--green);
}
.nn-deployment.level-warm {
  border-left-color: var(--amber);
}
.nn-deployment.level-cold {
  border-left-color: var(--blue);
}
.nn-deployment.pending {
  border-left-color: var(--accent);
  animation: pending-pulse 1.4s ease-in-out infinite;
}

@keyframes pending-pulse {
  0%, 100% { box-shadow: inset 3px 0 0 var(--accent), 0 0 0 0 transparent; }
  50% { box-shadow: inset 3px 0 0 var(--accent), 0 0 0 4px var(--green-soft); }
}

.nn-card-body {
  padding: 0.85rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  flex: 1;
}

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.5rem;
}
.repo-id {
  font-family: 'Space Mono', monospace;
  font-size: 0.85rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}

.menu-wrap {
  position: relative;
  flex-shrink: 0;
}
.kebab {
  appearance: none;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text);
  width: 1.6rem;
  height: 1.6rem;
  font-size: 1rem;
  line-height: 1;
  cursor: pointer;
  padding: 0;
  font-family: inherit;
}
.kebab:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.kebab:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.cold-deploy-btn {
  appearance: none;
  background: transparent;
  border: 1px solid var(--green);
  color: var(--green);
  font-family: 'Space Mono', monospace;
  font-size: 0.7rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 0.3rem 0.6rem;
  cursor: pointer;
  flex-shrink: 0;
}
.cold-deploy-btn:hover {
  background: var(--green-soft);
}
.cold-deploy-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.pulse-dot {
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 50%;
  background: var(--accent);
  flex-shrink: 0;
  margin-top: 0.4rem;
  animation: dot-pulse 1.2s ease-in-out infinite;
}
@keyframes dot-pulse {
  0%, 100% { transform: scale(0.7); opacity: 0.4; }
  50% { transform: scale(1.2); opacity: 1; }
}

.pending-line {
  font-family: 'VT323', monospace;
  letter-spacing: 0.08em;
  font-size: 1rem;
  color: var(--accent);
  text-transform: uppercase;
}
.pending-line .dots {
  display: inline-block;
  animation: ellipsis-blink 1s steps(3) infinite;
  letter-spacing: -0.05em;
  margin-left: 0.15em;
}
@keyframes ellipsis-blink {
  0%   { opacity: 0; }
  33%  { opacity: 0.4; }
  66%  { opacity: 1; }
  100% { opacity: 0; }
}

.menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  background: var(--surface);
  border: 1px solid var(--border);
  z-index: 5;
  min-width: 120px;
  display: flex;
  flex-direction: column;
}
.menu button {
  appearance: none;
  background: transparent;
  border: none;
  color: var(--text);
  text-align: left;
  padding: 0.5rem 0.75rem;
  font-family: 'Space Mono', monospace;
  font-size: 0.78rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  cursor: pointer;
}
.menu button:hover {
  background: var(--surface-2);
}
.menu button.danger {
  color: var(--red);
}

.badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}
.level-pill {
  letter-spacing: 0.1em;
}
.level-pill.level-hot {
  color: var(--green);
  border-color: var(--green);
}
.level-pill.level-warm {
  color: var(--amber);
  border-color: var(--amber);
}
.level-pill.level-cold {
  color: var(--blue);
  border-color: var(--blue);
}
.pill.pinned {
  color: var(--accent);
  border-color: var(--accent);
  text-transform: none;
  letter-spacing: 0.04em;
  font-size: 0.65rem;
}
.pill.replica-count {
  color: var(--muted);
  border-color: var(--border);
  text-transform: none;
  letter-spacing: 0.04em;
  font-size: 0.65rem;
}

/* ----- Replica dots ----- */
.replica-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  align-items: center;
  padding: 0.1rem 0;
}
.replica-wrap {
  position: relative;
}
.replica-dot {
  display: inline-block;
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 50%;
  background: var(--muted);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.15s;
  outline: none;
}
.replica-dot:hover,
.replica-dot:focus {
  transform: scale(1.25);
}
.replica-dot.hot.running {
  background: var(--green);
  border-color: var(--green);
}
.replica-dot.hot.deploying {
  background: var(--amber);
  border-color: var(--amber);
  animation: deploy-flash 1s ease-in-out infinite;
}
.replica-dot.hot.unhealthy {
  background: var(--red);
  border-color: var(--red);
}
.replica-dot.warm {
  background: var(--amber);
  border-color: var(--amber);
}
.replica-dot.pinned {
  box-shadow: 0 0 0 2px var(--accent);
}
@keyframes deploy-flash {
  0%, 100% {
    background: var(--amber);
    border-color: var(--amber);
  }
  50% {
    background: var(--green);
    border-color: var(--green);
  }
}

/* Hover popover menu for a single replica. Sticks slightly to the dot via
   the small padding band so the cursor can travel from dot to menu without
   re-triggering the hover boundary. */
.replica-menu {
  display: none;
  position: absolute;
  top: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--surface);
  border: 1px solid var(--border);
  flex-direction: column;
  min-width: 100px;
  z-index: 6;
  padding-top: 0.05rem;
}
.replica-wrap:hover .replica-menu,
.replica-wrap:focus-within .replica-menu {
  display: flex;
}
.replica-menu-label {
  font-family: 'Space Mono', monospace;
  font-size: 0.65rem;
  color: var(--muted);
  padding: 0.35rem 0.6rem 0.15rem;
  letter-spacing: 0.06em;
  border-bottom: 1px solid var(--border);
}
.replica-menu button {
  appearance: none;
  background: transparent;
  border: none;
  color: var(--text);
  text-align: left;
  padding: 0.4rem 0.6rem;
  font-family: 'Space Mono', monospace;
  font-size: 0.72rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  cursor: pointer;
}
.replica-menu button:hover {
  background: var(--surface-2);
}
.replica-menu button.danger {
  color: var(--red);
}

.meta {
  display: flex;
  gap: 0.85rem;
  font-size: 0.72rem;
  color: var(--muted);
  font-family: 'Space Mono', monospace;
  margin-top: auto;
}

.hf-link {
  position: absolute;
  bottom: 0.45rem;
  right: 0.6rem;
  font-size: 0.6rem;
  letter-spacing: 0.1em;
  color: var(--muted);
  text-decoration: none;
  border: 1px solid var(--border);
  padding: 0.05rem 0.35rem;
  font-family: 'Space Mono', monospace;
}
.hf-link:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.busy {
  pointer-events: none;
}
.busy-overlay {
  position: absolute;
  inset: 0;
  background: color-mix(in srgb, var(--surface) 70%, transparent);
  display: grid;
  place-items: center;
  z-index: 4;
}
.spinner {
  width: 22px;
  height: 22px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
