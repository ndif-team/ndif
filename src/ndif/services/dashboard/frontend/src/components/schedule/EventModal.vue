<script setup lang="ts">
import { ref, watch } from 'vue'
import type { CacheValues } from '@/deploy'
import AutocompleteInput from '@/components/AutocompleteInput.vue'

export interface EventForm {
  id?: string
  title: string
  checkpoint: string
  revision: string | null
  actor_class: string | null
  envoy_class: string | null
  padding_factor: number | null
  execution_timeout_seconds: number | null
  start: string
  // null == open-ended ("forever"). Sent as end: null over the wire.
  end: string | null
}

const props = defineProps<{
  initial: EventForm
  mode: 'create' | 'edit'
  saving?: boolean
  error?: string | null
  cache?: CacheValues
}>()

const emit = defineEmits<{
  save: [data: EventForm]
  close: []
  delete: [id: string]
  duplicate: [data: EventForm]
}>()

const form = ref<EventForm>({ ...props.initial })

watch(
  () => props.initial,
  (v) => {
    form.value = { ...v }
  }
)

function toLocalInput(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function fromLocalInput(s: string): string {
  if (!s) return ''
  const d = new Date(s)
  return d.toISOString()
}

function submit() {
  emit('save', {
    ...form.value,
    start: form.value.start ? fromLocalInput(toLocalInput(form.value.start)) : form.value.start,
    end: form.value.end ? fromLocalInput(toLocalInput(form.value.end)) : null
  })
}

function onStartInput(e: Event) {
  const v = (e.target as HTMLInputElement).value
  form.value.start = fromLocalInput(v)
}
function onEndInput(e: Event) {
  const v = (e.target as HTMLInputElement).value
  form.value.end = v ? fromLocalInput(v) : null
}

// Toggle between bounded ("ends at <pick a time>") and open-ended (no end).
function toggleOpenEnded(e: Event) {
  const checked = (e.target as HTMLInputElement).checked
  if (checked) {
    form.value.end = null
  } else {
    // Default to start + 1 day if we don't have an end yet.
    const start = new Date(form.value.start || Date.now())
    const end = new Date(start)
    end.setDate(end.getDate() + 1)
    form.value.end = end.toISOString()
  }
}
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')">
    <div class="modal">
      <h3>{{ mode === 'create' ? 'New deployment' : 'Edit deployment' }}</h3>

      <form class="grid form-grid" @submit.prevent="submit">
        <label class="field full">
          Title
          <input v-model="form.title" required maxlength="120" />
        </label>

        <label class="field full">
          Checkpoint (HF repo id)
          <AutocompleteInput
            v-model="form.checkpoint"
            placeholder="meta-llama/Llama-3.1-8B"
            :options="cache?.repo_id ?? []"
            required
          />
        </label>

        <label class="field">
          Revision
          <input v-model="form.revision" placeholder="(default)" />
        </label>

        <label class="field">
          Actor class
          <AutocompleteInput
            v-model="form.actor_class"
            placeholder="ndif.services.ray.deployments.modeling.base.ModelActor"
            :options="cache?.actor_class ?? []"
          />
        </label>

        <label class="field full">
          Envoy class
          <AutocompleteInput
            v-model="form.envoy_class"
            placeholder="nnsight.modeling.language.LanguageModel"
            :options="cache?.envoy_class ?? []"
          />
        </label>

        <label class="field">
          Padding factor
          <input
            type="number"
            step="0.01"
            min="0"
            v-model.number="form.padding_factor"
            placeholder="(default)"
          />
        </label>

        <label class="field">
          Execution timeout (s)
          <input
            type="number"
            step="1"
            min="0"
            v-model.number="form.execution_timeout_seconds"
            placeholder="(default)"
          />
        </label>

        <label class="field">
          Start
          <input
            type="datetime-local"
            :value="toLocalInput(form.start)"
            @input="onStartInput"
            required
          />
        </label>

        <label class="field">
          End
          <input
            type="datetime-local"
            :value="toLocalInput(form.end)"
            :disabled="form.end == null"
            :required="form.end != null"
            @input="onEndInput"
          />
        </label>

        <label class="field full check-field">
          <input
            type="checkbox"
            :checked="form.end == null"
            @change="toggleOpenEnded"
          />
          <span>
            <strong>Open-ended</strong>
            <span class="muted">
              — pinned forever, no end date. Use this when you want a model
              pinned indefinitely.
            </span>
          </span>
        </label>

        <p v-if="error" class="error full">{{ error }}</p>

        <div class="actions full">
          <button type="button" class="btn" @click="emit('close')">Cancel</button>
          <button
            v-if="mode === 'edit' && form.id"
            type="button"
            class="btn"
            @click="emit('duplicate', form)"
          >
            Duplicate
          </button>
          <button
            v-if="mode === 'edit' && form.id"
            type="button"
            class="btn danger"
            @click="emit('delete', form.id!)"
          >
            Delete
          </button>
          <button type="submit" class="btn primary" :disabled="saving">
            {{ saving ? '...' : mode === 'create' ? 'Create' : 'Save' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.form-grid {
  grid-template-columns: 1fr 1fr;
  gap: 0.85rem;
}
.field.full {
  grid-column: 1 / -1;
}
.check-field {
  flex-direction: row;
  align-items: center;
  gap: 0.55rem;
  text-transform: none;
  letter-spacing: 0.02em;
}
.check-field input[type='checkbox'] {
  width: 1rem;
  height: 1rem;
  margin: 0;
}
.check-field strong { color: var(--text); }
.check-field .muted { color: var(--muted); font-size: 0.75rem; }
.actions.full {
  grid-column: 1 / -1;
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
}
.error {
  color: var(--red);
  font-size: 0.8rem;
}
</style>
