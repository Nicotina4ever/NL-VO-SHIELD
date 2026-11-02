<script lang="ts">
  /** Icone SVG inline (stroke), coerenti con lo stile sobrio del tema. */
  let { name, size = 17 }: { name: string; size?: number } = $props();

  const icons: Record<string, string> = {
    dashboard:
      '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    shield: '<path d="M12 3l7 3v6c0 4.4-3 7.4-7 9-4-1.6-7-4.6-7-9V6l7-3z"/>',
    batch: '<path d="M4 6h16M4 12h16M4 18h10"/>',
    verify: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.2-4.2"/><path d="M8.6 11l1.8 1.8 3-3.4"/>',
    engines:
      '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3"/>',
    profiles: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6"/>',
    settings:
      '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5.2 5.2l1.9 1.9M16.9 16.9l1.9 1.9M18.8 5.2l-1.9 1.9M7.1 16.9l-1.9 1.9"/>',
    diagnostics: '<path d="M3 12h3.5l2.5-7 4 14 2.5-7H21"/>',
    about: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.6v.6"/>',
    folder: '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2.5h8.5A1.5 1.5 0 0 1 21 10v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18V7.5z"/>',
    file: '<path d="M14 3v5h5"/><path d="M6 3h8l5 5v13H6V3z"/>',
    download: '<path d="M12 4v10M8 10l4 4 4-4"/><path d="M5 19h14"/>',
    trash: '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.5-5.8"/><path d="M20 4v4.5h-4.5"/>',
    check: '<path d="M4.5 12.5l4.5 4.5L19.5 6.5"/>',
    play: '<path d="M8 5l11 7-11 7V5z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5H5V6h5"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a1 1 0 0 1 1-1h9"/>',
    alert: '<path d="M12 4l9 16H3L12 4z"/><path d="M12 10v4M12 17v.5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.6v.6"/>',
    search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.2-4.2"/>',
    up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
    eye: '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5.5l3.5 2"/>',
  };
</script>

<svg
  class="nav-item__icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.7"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {@html icons[name] ?? icons.info}
</svg>
