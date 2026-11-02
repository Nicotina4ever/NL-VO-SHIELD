<script lang="ts">
  import type { StepState } from "../run.svelte";
  import { t } from "../i18n.svelte";
  import { formatPct } from "../format";

  let { steps = [] }: { steps?: StepState[] } = $props();

  function phaseClass(phase: string): string {
    if (phase === "running") return "step--running";
    if (phase === "done") return "step--done";
    if (phase === "failed") return "step--failed";
    return "";
  }
</script>

<div class="steps">
  {#each steps as step (step.index)}
    <div class="step {phaseClass(step.phase)}">
      <div class="step__dot"></div>
      <div class="stack" style="gap:2px">
        <span class="step__name">
          {step.engine} · {step.action}
          {#if !step.required}<span class="faint small">({t("common.optional")})</span>{/if}
        </span>
        {#if step.message}
          <span class="step__msg">{step.message}</span>
        {:else if step.phase === "pending"}
          <span class="step__msg">{t("batch.waiting")}</span>
        {/if}
      </div>
      <span class="mono small faint">
        {step.phase === "done"
          ? "✓"
          : step.phase === "failed"
            ? "✕"
            : step.phase === "skipped"
              ? "–"
              : formatPct(step.pct)}
      </span>
    </div>
  {/each}
</div>
