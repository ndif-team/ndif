<script setup lang="ts">
// Styled autocomplete input — replacement for native <datalist>, which
// renders an OS-skinned dropdown that doesn't match the rest of the UI.
//
// Filters ``options`` against the current value (case-insensitive substring
// match) and shows up to MAX_VISIBLE matches in a styled popover. Keyboard
// nav: ↑ / ↓ to move, Enter to commit, Esc to close. Mouse: click to commit.

import { computed, nextTick, ref, watch } from 'vue'

const MAX_VISIBLE = 50

const props = withDefaults(
  defineProps<{
    modelValue: string | null
    options?: string[]
    placeholder?: string
    required?: boolean
    autofocus?: boolean
  }>(),
  { options: () => [], required: false, autofocus: false }
)

// Always emit string — never null. Callers that want null-on-empty can
// transform on submit (`form.actor_class || null`), same as the existing
// modals already do. Keeps the v-model contract symmetric for ``string``
// fields like ``checkpoint``.
const emit = defineEmits<{ 'update:modelValue': [v: string] }>()

const open = ref(false)
const highlightedIndex = ref(-1)
const inputEl = ref<HTMLInputElement | null>(null)

const value = computed({
  get: () => props.modelValue ?? '',
  set: (v: string) => emit('update:modelValue', v)
})

const filtered = computed(() => {
  const q = (value.value || '').toLowerCase()
  if (!q) return props.options.slice(0, MAX_VISIBLE)
  return props.options
    .filter((o) => o.toLowerCase().includes(q))
    .slice(0, MAX_VISIBLE)
})

// Reset highlight whenever the option set changes (typing narrows it).
watch(filtered, () => {
  highlightedIndex.value = filtered.value.length > 0 ? 0 : -1
})

function commit(option: string) {
  value.value = option
  open.value = false
  highlightedIndex.value = -1
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    open.value = true
    if (filtered.value.length === 0) return
    highlightedIndex.value = (highlightedIndex.value + 1) % filtered.value.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (filtered.value.length === 0) return
    highlightedIndex.value =
      highlightedIndex.value <= 0
        ? filtered.value.length - 1
        : highlightedIndex.value - 1
  } else if (e.key === 'Enter') {
    if (open.value && highlightedIndex.value >= 0) {
      e.preventDefault()
      commit(filtered.value[highlightedIndex.value])
    }
  } else if (e.key === 'Escape') {
    open.value = false
  }
}

function onFocus() {
  open.value = true
}

function onBlur(e: FocusEvent) {
  // Defer close so a click on a dropdown item lands before we hide it.
  // Without this, mousedown selection races against blur and never fires.
  const next = e.relatedTarget as HTMLElement | null
  const root = (e.currentTarget as HTMLElement).closest('.ac-wrap')
  if (!next || !root || !root.contains(next)) {
    setTimeout(() => {
      open.value = false
    }, 120)
  }
}

if (props.autofocus) {
  nextTick(() => inputEl.value?.focus())
}
</script>

<template>
  <div class="ac-wrap" @focusout="onBlur">
    <input
      ref="inputEl"
      v-model="value"
      type="text"
      :placeholder="placeholder"
      :required="required"
      autocomplete="off"
      @focus="onFocus"
      @keydown="onKeydown"
    />
    <ul
      v-if="open && filtered.length > 0"
      class="ac-list"
      role="listbox"
      tabindex="-1"
    >
      <li
        v-for="(opt, i) in filtered"
        :key="opt"
        :class="['ac-item', i === highlightedIndex ? 'active' : '']"
        role="option"
        :aria-selected="i === highlightedIndex"
        @mousedown.prevent="commit(opt)"
        @mouseenter="highlightedIndex = i"
      >
        {{ opt }}
      </li>
    </ul>
  </div>
</template>

<style scoped>
.ac-wrap {
  position: relative;
}
.ac-wrap input {
  width: 100%;
}
.ac-list {
  position: absolute;
  top: calc(100% + 2px);
  left: 0;
  right: 0;
  margin: 0;
  padding: 0;
  list-style: none;
  background: var(--surface);
  border: 1px solid var(--border);
  max-height: 220px;
  overflow-y: auto;
  z-index: 50;
  font-family: 'Space Mono', monospace;
  font-size: 0.78rem;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
}
.ac-item {
  padding: 0.4rem 0.6rem;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border-bottom: 1px solid var(--border);
}
.ac-item:last-child {
  border-bottom: none;
}
.ac-item:hover,
.ac-item.active {
  background: var(--surface-2);
  color: var(--accent);
}
</style>
