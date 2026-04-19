<script lang="ts">
  interface Candidate {
    album_id: number;
    source: string;
    artist: string;
    title: string;
    score: number;
    reason: string;
  }

  interface ReviewEntry {
    folder: string;
    local_bit_depth: number | null;
    local_sample_rate: number | null;
    candidates: Candidate[];
  }

  interface ScanResult {
    status: 'running' | 'complete' | 'error';
    scanned?: number;
    total?: number;
    sentinel_skipped?: number;
    auto_matched?: { album_id: number; folder: string; reason: string }[];
    review?: ReviewEntry[];
    unmatched?: string[];
    error?: string;
  }

  let { result, onConfirm, onClose } = $props<{
    result: ScanResult;
    onConfirm: (albumId: number, folder: string) => Promise<void>;
    onClose: () => void;
  }>();

  let expanded = $state<{ auto: boolean; review: boolean; unmatched: boolean }>({
    auto: false, review: true, unmatched: false,
  });
  let processing = $state<Set<number>>(new Set());

  async function confirm(entry: ReviewEntry, candidate: Candidate) {
    processing = new Set([...processing, candidate.album_id]);
    try {
      await onConfirm(candidate.album_id, entry.folder);
      // Remove the entry from the list.
      if (result.review) {
        result.review = result.review.filter(e => e !== entry);
      }
    } finally {
      processing.delete(candidate.album_id);
      processing = new Set(processing);
    }
  }

  function copyUnmatched() {
    if (!result.unmatched) return;
    navigator.clipboard.writeText(result.unmatched.join('\n'));
  }
</script>

<div class="overlay" role="dialog" aria-labelledby="scan-review-title">
  <button class="backdrop" aria-label="Close" onclick={onClose}></button>
  <aside class="panel">
    <header>
      <h2 id="scan-review-title">Scan Results</h2>
      <button class="close" onclick={onClose} aria-label="Close">×</button>
    </header>

    {#if result.status === 'running'}
      <p>Scanning… {result.scanned ?? 0} / {result.total ?? '?'}</p>
    {:else if result.status === 'error'}
      <p class="error">Scan failed: {result.error}</p>
    {:else}
      <p class="summary">
        Scanned {result.scanned} folders ·
        {result.auto_matched?.length ?? 0} auto-matched ·
        {result.review?.length ?? 0} need review ·
        {result.unmatched?.length ?? 0} unmatched
        {#if result.sentinel_skipped}· {result.sentinel_skipped} already sentineled{/if}
      </p>

      <!-- Auto-matched -->
      <section>
        <button class="section-header"
                onclick={() => expanded.auto = !expanded.auto}>
          {expanded.auto ? '▾' : '▸'} Auto-matched ({result.auto_matched?.length ?? 0})
        </button>
        {#if expanded.auto && result.auto_matched}
          <ul class="list">
            {#each result.auto_matched as m}
              <li><code>{m.folder}</code> → album #{m.album_id}</li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- Review -->
      <section>
        <button class="section-header"
                onclick={() => expanded.review = !expanded.review}>
          {expanded.review ? '▾' : '▸'} Needs review ({result.review?.length ?? 0})
        </button>
        {#if expanded.review && result.review}
          <ul class="list">
            {#each result.review as entry}
              <li class="review-entry">
                <div class="folder"><code>{entry.folder}</code>
                  {#if entry.local_bit_depth}· local {entry.local_bit_depth}-bit{/if}
                </div>
                {#each entry.candidates as cand}
                  <div class="candidate">
                    <div>
                      <strong>{cand.artist}</strong> — {cand.title}
                      <span class="source">({cand.source})</span>
                      <div class="reason">{cand.reason}</div>
                    </div>
                    <button class="btn btn-secondary btn-sm"
                            disabled={processing.has(cand.album_id)}
                            onclick={() => confirm(entry, cand)}>
                      {processing.has(cand.album_id) ? '…' : 'Confirm'}
                    </button>
                  </div>
                {/each}
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- Unmatched -->
      <section>
        <button class="section-header"
                onclick={() => expanded.unmatched = !expanded.unmatched}>
          {expanded.unmatched ? '▾' : '▸'} Unmatched folders ({result.unmatched?.length ?? 0})
        </button>
        {#if expanded.unmatched && result.unmatched}
          <div class="unmatched-actions">
            <button class="btn btn-secondary btn-sm" onclick={copyUnmatched}>
              Copy paths to clipboard
            </button>
          </div>
          <ul class="list">
            {#each result.unmatched as path}
              <li><code>{path}</code></li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}
  </aside>
</div>

<style>
  .overlay { position: fixed; inset: 0; z-index: 1000; }
  .backdrop {
    position: absolute; inset: 0; background: rgba(0,0,0,0.4);
    border: none; cursor: pointer;
  }
  .panel {
    position: absolute; top: 0; right: 0; bottom: 0; width: min(640px, 90vw);
    background: var(--surface); padding: var(--space-4);
    overflow-y: auto; box-shadow: -4px 0 0 var(--shadow-color, #000);
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: var(--space-3);
  }
  .close {
    background: none; border: none; font-size: 1.5rem; cursor: pointer;
    color: var(--fg);
  }
  .summary { color: var(--muted); font-size: var(--text-sm); }
  .section-header {
    background: none; border: none; font-family: inherit;
    font-size: var(--text-base); padding: var(--space-2) 0;
    cursor: pointer; text-align: left; width: 100%;
    border-top: 1px solid var(--border); color: var(--fg);
  }
  .list { list-style: none; padding: 0; margin: 0 0 var(--space-3) 0; }
  .list li { padding: var(--space-2) 0; border-bottom: 1px solid var(--border); }
  .review-entry .folder { margin-bottom: var(--space-1); }
  .candidate {
    display: flex; justify-content: space-between; gap: var(--space-2);
    padding: var(--space-1) 0;
  }
  .candidate .reason { font-size: var(--text-xs); color: var(--muted); }
  .candidate .source { font-size: var(--text-xs); color: var(--muted); }
  .unmatched-actions { margin-bottom: var(--space-2); }
  .error { color: var(--destructive); }
  code { font-family: var(--font-mono); font-size: var(--text-xs); }
</style>
