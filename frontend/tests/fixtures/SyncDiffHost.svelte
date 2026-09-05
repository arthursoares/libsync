<script lang="ts">
  import { untrack } from 'svelte';
  import SyncDiff from '../../src/lib/components/SyncDiff.svelte';

  type Album = { id: string | number; title: string; artist: string };
  let { initialItems, notify }: { initialItems: Album[]; notify: (ids: (string | number)[]) => void } = $props();
  let items = $state(untrack(() => initialItems));
  let selected = $state<Set<string | number>>(new Set());
  let notifications = $state(0);

  export function replaceItems(next: Album[]) { items = next; }

  function onSelection(next: Set<string | number>) {
    selected = next;
    // Parent reads must not become dependencies of the child's seed effect.
    notifications += 1;
    notify([...selected]);
  }
</script>

<SyncDiff label="Added" icon_color="var(--pop)" {items} selectable onchange={onSelection} />
<output data-selection>{[...selected].join(',')}</output>
<output data-notifications>{notifications}</output>
