<script setup lang="ts">
import { ref, computed } from 'vue'

export interface Deployment {
  model_key: string
  repo_id?: string
  revision?: string | null
  deployment_level?: 'HOT' | 'WARM' | 'COLD' | string
  application_state?: string
  pinned?: boolean
  n_params?: number
  size_bytes?: number
  actor_class?: string | null
  schedule?: { start_time?: string; end_time?: string; title?: string } | null
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
}>()

const menuOpen = ref(false)

function close() {
  menuOpen.value = false
}

const levelClass = computed(() => 'level-' + (props.deployment.deployment_level || '').toLowerCase())
// COLD shows a standalone Deploy button (opens the deploy modal so the user
// can pick actor_class / pinned / etc.). WARM uses a kebab menu with
// Deploy + Evict — the Deploy goes through a fast-path that redeploys the
// existing model_key as-is (no modal, no canonicalize round-trip) since
// every option is already pinned by the existing deployment record.
const isCold = computed(() => props.deployment.deployment_level === 'COLD')
const isWarm = computed(() => props.deployment.deployment_level === 'WARM')
const isPending = computed(() => !!props.deployment.pending)

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

function onRestart() {
  close()
  if (!confirm(`Restart ${props.deployment.repo_id || props.deployment.model_key}?`)) return
  emit('restart', props.deployment)
}

function onEvict() {
  close()
  const name = props.deployment.repo_id || props.deployment.model_key
  // If this model is in an active schedule entry, the next reconcile tick
  // will re-deploy it. The admin should remove the entry from the Schedule
  // tab if they want it gone permanently.
  const what = props.deployment.pinned
    ? `Evict ${name}?\n\n(This is pinned. If a schedule entry covers it, the next reconcile will re-deploy it. Remove the entry from the Schedule tab to make it permanent.)`
    : `Evict ${name}?`
  if (!confirm(what)) return
  emit('evict', props.deployment)
}

function onDeploy() {
  close()
  emit('deploy', props.deployment)
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

        <!-- WARM: kebab → Deploy (fast-path redeploy via existing model_key) / Evict -->
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
            <button type="button" @click="onDeploy">Deploy</button>
            <button type="button" class="danger" @click="onEvict">Evict</button>
          </div>
        </div>

        <!-- HOT: kebab → Restart / Evict -->
        <div v-else class="menu-wrap" @focusout="onMenuBlur">
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
            <button type="button" @click="onRestart">Restart</button>
            <button type="button" class="danger" @click="onEvict">Evict</button>
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
