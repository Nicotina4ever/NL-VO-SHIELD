<script lang="ts">
  import { onMount } from "svelte";
  import { t, hostErrorMessage } from "../i18n.svelte";
  import { toast } from "../ui.svelte";
  import { engine, loadPresets, loadProfiles, refreshEngines } from "../engine.svelte";
  import { engineApi } from "../ipc";
  import { BUILTIN_PRESETS } from "../presets";
  import { formatDate } from "../format";
  import type { ChainStep, Preset, Profile } from "../types";
  import Icon from "../components/Icon.svelte";
  import Modal from "../components/Modal.svelte";
  import Badge from "../components/Badge.svelte";

  type Tab = "profiles" | "presets";
  let tab = $state<Tab>("profiles");

  let profileDraft = $state<Profile | null>(null);
  let savingProfile = $state(false);
  let profileError = $state<string | null>(null);
  let deletingProfile = $state<Profile | null>(null);

  interface StepDraft {
    engine: string;
    action: string;
    required: boolean;
    paramsText: string;
  }

  interface PresetDraft {
    id: string;
    name: string;
    description: string;
    steps: StepDraft[];
    isNew: boolean;
  }

  let presetDraft = $state<PresetDraft | null>(null);
  let savingPreset = $state(false);
  let presetError = $state<string | null>(null);

  function newProfile(): void {
    profileDraft = {
      id: `profile_${Date.now().toString(36)}`,
      name: "",
      org: "",
      contact: "",
      usage_terms: "",
      notes: "",
      default_preset: "",
    };
    profileError = null;
  }

  function editProfile(profile: Profile): void {
    profileDraft = { ...profile };
    profileError = null;
  }

  async function saveProfile(): Promise<void> {
    if (!profileDraft) return;
    if (!profileDraft.name.trim()) {
      profileError = t("common.required");
      return;
    }
    savingProfile = true;
    profileError = null;
    try {
      await engineApi.saveProfile({
        ...profileDraft,
        name: profileDraft.name.trim(),
      });
      await loadProfiles();
      toast("ok", t("profiles.saved"), profileDraft.name);
      profileDraft = null;
    } catch (err) {
      profileError = hostErrorMessage(err as { code?: string; message?: string });
    } finally {
      savingProfile = false;
    }
  }

  async function confirmDelete(): Promise<void> {
    if (!deletingProfile) return;
    try {
      await engineApi.deleteProfile(deletingProfile.id);
      await loadProfiles();
      toast("ok", t("common.delete"), deletingProfile.name);
    } catch (err) {
      toast("err", t("common.deleteFailed"), hostErrorMessage(err as { code?: string; message?: string }));
    } finally {
      deletingProfile = null;
    }
  }

  function openPreset(preset?: Preset): void {
    const chain = (Array.isArray(preset?.chain) ? preset?.chain : []) as ChainStep[];
    presetDraft = {
      id: preset?.id ?? `preset_${Date.now().toString(36)}`,
      name: preset?.name ?? "",
      description: (preset?.description as string) ?? "",
      isNew: !preset,
      steps:
        chain.length > 0
          ? chain.map((step) => ({
              engine: step.engine,
              action: step.action,
              required: step.required !== false,
              paramsText: JSON.stringify(step.params ?? {}, null, 0),
            }))
          : [{ engine: "spectral_cloak", action: "perturb", required: true, paramsText: '{"strength":"medium"}' }],
    };
    presetError = null;
  }

  function addStep(): void {
    if (!presetDraft) return;
    presetDraft.steps = [
      ...presetDraft.steps,
      { engine: "audioseal", action: "embed", required: true, paramsText: "{}" },
    ];
  }

  function removeStep(index: number): void {
    if (!presetDraft) return;
    presetDraft.steps = presetDraft.steps.filter((_, i) => i !== index);
  }

  async function savePreset(): Promise<void> {
    if (!presetDraft) return;
    if (!presetDraft.name.trim()) {
      presetError = t("common.required");
      return;
    }
    const steps: ChainStep[] = [];
    for (const [index, step] of presetDraft.steps.entries()) {
      let params: Record<string, unknown> = {};
      const text = step.paramsText.trim();
      if (text) {
        try {
          const parsed = JSON.parse(text);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            params = parsed as Record<string, unknown>;
          } else {
            presetError = `${t("profiles.invalidJson")} (#${index + 1})`;
            return;
          }
        } catch {
          presetError = `${t("profiles.invalidJson")} (#${index + 1})`;
          return;
        }
      }
      steps.push({
        engine: step.engine.trim(),
        action: step.action.trim(),
        required: step.required,
        params,
      });
    }
    savingPreset = true;
    presetError = null;
    try {
      await engineApi.savePreset({
        id: presetDraft.id,
        name: presetDraft.name.trim(),
        description: presetDraft.description.trim() || null,
        chain: steps,
      });
      await loadPresets();
      toast("ok", t("profiles.presetSaved"), presetDraft.name);
      presetDraft = null;
    } catch (err) {
      presetError = hostErrorMessage(err as { code?: string; message?: string });
    } finally {
      savingPreset = false;
    }
  }

  onMount(() => {
    void (async () => {
      await loadProfiles();
      await loadPresets();
      if (engine.engines.length === 0) void refreshEngines();
    })();
  });
</script>

<div class="page">
  <div class="page-head">
    <div class="page-head__text">
      <h1>{t("profiles.title")}</h1>
      <p>{t("profiles.subtitle")}</p>
    </div>
    <div class="page-head__actions">
      {#if tab === "profiles"}
        <button class="btn btn--primary btn--sm" onclick={newProfile}>
          <Icon name="plus" size={14} />
          {t("profiles.newProfile")}
        </button>
      {:else}
        <button class="btn btn--primary btn--sm" onclick={() => openPreset()}>
          <Icon name="plus" size={14} />
          {t("profiles.newPreset")}
        </button>
      {/if}
    </div>
  </div>

  <div class="card">
    <div class="tabs" role="tablist">
      <button
        class="tab {tab === 'profiles' ? 'active' : ''}"
        onclick={() => (tab = "profiles")}
        role="tab"
        aria-selected={tab === "profiles"}
      >
        {t("profiles.tabProfiles")}
      </button>
      <button
        class="tab {tab === 'presets' ? 'active' : ''}"
        onclick={() => (tab = "presets")}
        role="tab"
        aria-selected={tab === "presets"}
      >
        {t("profiles.tabPresets")}
      </button>
    </div>

    {#if tab === "profiles"}
      <div class="card__body card__body--flush">
        {#if engine.profiles.length === 0}
          <div class="empty">
            <div class="bold">{t("profiles.noProfiles")}</div>
            <div class="small" style="margin-top:4px">{t("profiles.noProfilesHint")}</div>
            <div style="margin-top:10px">
              <button class="btn btn--sm" onclick={newProfile}>{t("profiles.newProfile")}</button>
            </div>
          </div>
        {:else}
          <table class="table">
            <thead>
              <tr>
                <th>{t("profiles.name")}</th>
                <th>{t("profiles.org")}</th>
                <th>{t("profiles.usageTerms")}</th>
                <th>{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {#each engine.profiles as profile (profile.id)}
                <tr>
                  <td>
                    <div class="bold">{profile.name}</div>
                    <div class="small faint mono">{profile.id}</div>
                  </td>
                  <td class="small">{profile.org || "—"}</td>
                  <td class="small truncate" style="max-width:320px" title={profile.usage_terms ?? ""}>
                    {profile.usage_terms || "—"}
                  </td>
                  <td>
                    <div class="row gap-sm">
                      <button class="btn btn--ghost btn--sm" onclick={() => editProfile(profile)}>
                        {t("common.edit")}
                      </button>
                      <button class="btn btn--danger btn--sm" onclick={() => (deletingProfile = profile)}>
                        {t("common.delete")}
                      </button>
                    </div>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </div>
    {:else}
      <div class="card__body">
        <div class="hint-box small" style="margin-bottom:12px">{t("profiles.builtinNote")}</div>

        <div class="col" style="gap:8px">
          {#each BUILTIN_PRESETS as builtin (builtin.id)}
            <div class="list-row">
              <div class="list-row__main">
                <div class="row gap-sm">
                  <span class="bold">{t(builtin.nameKey)}</span>
                  <Badge label={t("profiles.builtin")} tone="muted" />
                </div>
                <div class="small muted">{t(builtin.descKey)}</div>
              </div>
              <span class="mono small faint">
                {builtin.antiClone ? "anti-clone" : ""}
                {builtin.watermark ? " · watermark" : ""}
                {builtin.c2pa ? " · c2pa" : ""}
              </span>
            </div>
          {/each}
        </div>

        <div class="row" style="margin:16px 0 8px">
          <span class="field__label">{t("profiles.tabPresets")}</span>
          <span class="spacer"></span>
          <span class="small faint">{engine.presets.length}</span>
        </div>

        {#if engine.presets.length === 0}
          <div class="empty">{t("profiles.noPresets")}</div>
        {:else}
          <div class="col" style="gap:8px">
            {#each engine.presets as preset (preset.id)}
              <div class="list-row">
                <div class="list-row__main">
                  <div class="bold">{preset.name}</div>
                  <div class="small muted">{preset.description ?? "—"}</div>
                  <div class="small mono faint">
                    {((preset.chain as ChainStep[] | null) ?? [])
                      .map((step) => `${step.engine}.${step.action}`)
                      .join(" → ") || "—"}
                  </div>
                </div>
                <span class="small faint mono">{formatDate(preset.updated_at as string)}</span>
                <button class="btn btn--ghost btn--sm" onclick={() => openPreset(preset)}>
                  {t("common.edit")}
                </button>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  </div>
</div>

{#if profileDraft}
  <Modal title={t("profiles.editProfile")} onclose={() => (profileDraft = null)}>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (profileDraft = null)}>{t("common.cancel")}</button>
      <button class="btn btn--primary" onclick={() => void saveProfile()} disabled={savingProfile}>
        {#if savingProfile}<span class="spinner"></span>{/if}
        {t("common.save")}
      </button>
    {/snippet}
    <div class="col" style="gap:12px">
      <div class="field">
        <label for="profile-name">{t("profiles.name")}</label>
        <input id="profile-name" class="input" bind:value={profileDraft.name} />
      </div>
      <div class="grid grid-2" style="gap:12px">
        <div class="field">
          <label for="profile-org">{t("profiles.org")}</label>
          <input id="profile-org" class="input" bind:value={profileDraft.org} />
        </div>
        <div class="field">
          <label for="profile-contact">{t("profiles.contact")}</label>
          <input id="profile-contact" class="input" bind:value={profileDraft.contact} />
        </div>
      </div>
      <div class="field">
        <label for="profile-terms">{t("profiles.usageTerms")}</label>
        <textarea id="profile-terms" class="textarea" rows="4" bind:value={profileDraft.usage_terms}></textarea>
        <span class="field__help">{t("profiles.usageTermsHint")}</span>
      </div>
      <div class="field">
        <label for="profile-notes">{t("profiles.notes")}</label>
        <textarea id="profile-notes" class="textarea" rows="3" bind:value={profileDraft.notes}></textarea>
      </div>
      {#if profileError}
        <div class="err-box">{profileError}</div>
      {/if}
    </div>
  </Modal>
{/if}

{#if deletingProfile}
  <Modal title={t("common.confirm")} onclose={() => (deletingProfile = null)}>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (deletingProfile = null)}>{t("common.cancel")}</button>
      <button class="btn btn--danger" onclick={() => void confirmDelete()}>{t("common.delete")}</button>
    {/snippet}
    <p>{t("profiles.deleteProfileConfirm")}</p>
  </Modal>
{/if}

{#if presetDraft}
  <Modal title={t("profiles.newPreset")} onclose={() => (presetDraft = null)} wide>
    {#snippet footer()}
      <button class="btn btn--ghost" onclick={() => (presetDraft = null)}>{t("common.cancel")}</button>
      <button class="btn btn--primary" onclick={() => void savePreset()} disabled={savingPreset}>
        {#if savingPreset}<span class="spinner"></span>{/if}
        {t("common.save")}
      </button>
    {/snippet}
    <div class="col" style="gap:12px">
      <div class="grid grid-2" style="gap:12px">
        <div class="field">
          <label for="preset-name">{t("profiles.presetName")}</label>
          <input id="preset-name" class="input" bind:value={presetDraft.name} />
        </div>
        <div class="field">
          <label for="preset-desc">{t("profiles.presetDescription")}</label>
          <input id="preset-desc" class="input" bind:value={presetDraft.description} />
        </div>
      </div>

      <div class="row">
        <span class="field__label">{t("profiles.chain")}</span>
        <span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" onclick={addStep}>
          <Icon name="plus" size={13} />
          {t("profiles.addStep")}
        </button>
      </div>

      <div class="col" style="gap:8px">
        {#each presetDraft.steps as step, index (index)}
          <div class="card" style="background:var(--nl-bg-2);box-shadow:none">
            <div class="card__body" style="padding:10px">
              <div class="grid" style="grid-template-columns:1fr 1fr auto auto;gap:8px;align-items:end">
                <div class="field">
                  <label for={`step-engine-${index}`}>{t("profiles.stepEngine")}</label>
                  <input
                    id={`step-engine-${index}`}
                    class="input mono"
                    list="nlvs-engine-ids"
                    bind:value={step.engine}
                  />
                </div>
                <div class="field">
                  <label for={`step-action-${index}`}>{t("profiles.stepAction")}</label>
                  <input id={`step-action-${index}`} class="input mono" bind:value={step.action} />
                </div>
                <label class="check" style="padding-bottom:6px">
                  <input type="checkbox" bind:checked={step.required} />
                  <span>{t("profiles.stepRequired")}</span>
                </label>
                <button
                  class="btn btn--ghost btn--sm"
                  onclick={() => removeStep(index)}
                  aria-label={t("profiles.removeStep")}
                  title={t("profiles.removeStep")}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
              <div class="field" style="margin-top:8px">
                <label for={`step-params-${index}`}>{t("profiles.stepParams")}</label>
                <input
                  id={`step-params-${index}`}
                  class="input mono"
                  bind:value={step.paramsText}
                  spellcheck="false"
                />
              </div>
            </div>
          </div>
        {/each}
      </div>

      {#if presetError}
        <div class="err-box">{presetError}</div>
      {/if}
    </div>
  </Modal>
{/if}

<datalist id="nlvs-engine-ids">
  {#each engine.engines as item (item.id)}
    <option value={item.id}></option>
  {/each}
</datalist>
