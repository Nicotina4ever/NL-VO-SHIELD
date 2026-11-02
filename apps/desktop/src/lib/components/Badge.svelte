<script lang="ts">
  let {
    label,
    tone = "muted",
    title = "",
  }: { label: string; tone?: string; title?: string } = $props();
</script>

<span class="badge badge--{tone}" {title}>{label}</span>
