const setupVoiceMixer = () => {
  const data = window.ABOGEN_VOICE_MIXER_DATA || {};
  const languages = data.languages || {};
  const voiceCatalog = data.voice_catalog || [];
  const samples = data.sample_voice_texts || {};
  let profiles = data.voice_profiles_data || {};

  const app = document.getElementById("voice-mixer-app");
  const profileListEl = app.querySelector('[data-role="profile-list"]');
  const voiceGridEl = app.querySelector('[data-role="voice-grid"]');
  const statusEl = app.querySelector('[data-role="status"]');
  const saveBtn = app.querySelector('[data-role="save-profile"]');
  const duplicateBtn = app.querySelector('[data-role="duplicate-profile"]');
  const deleteBtn = app.querySelector('[data-role="delete-profile"]');
  const previewBtn = app.querySelector('[data-role="preview-button"]');
  const loadSampleBtn = app.querySelector('[data-role="load-sample"]');
  const previewTextEl = app.querySelector('[data-role="preview-text"]');
  const previewAudio = app.querySelector('[data-role="preview-audio"]');
  const profileSummaryEl = app.querySelector('[data-role="profile-summary"]');
  const mixTotalEl = app.querySelector('[data-role="mix-total"]');
  const nameInput = document.getElementById("profile-name");
  const languageSelect = document.getElementById("profile-language");
  const speedInput = document.getElementById("preview-speed");
  const importInput = document.getElementById("voice-import-input");
  const headerActions = document.querySelector('.voice-mixer__header-actions');

  if (!app) {
    return;
  }

  if (!voiceCatalog.length) {
    if (profileListEl) {
      profileListEl.innerHTML = "<p class=\"tag\">No voices available.</p>";
    }
    return;
  }

  const state = {
    selectedProfile: null,
    originalName: null,
    dirty: false,
    previewUrl: null,
    draft: {
      name: "",
      language: "a",
      voices: new Map(),
    },
  };

  const voiceControls = new Map();
  let statusTimeout = null;

  const voiceGenderIcon = (gender) => (gender === "Female" ? "♀" : gender === "Male" ? "♂" : "•");
  const voiceLanguageLabel = (code) => languages[code] || code.toUpperCase();

  const clearStatus = () => {
    if (statusTimeout) {
      clearTimeout(statusTimeout);
      statusTimeout = null;
    }
    if (statusEl) {
      statusEl.textContent = "";
      statusEl.className = "voice-status";
    }
  };

  const setStatus = (message, tone = "info", timeout = 4000) => {
    if (!statusEl) return;
    clearStatus();
    statusEl.textContent = message;
    statusEl.className = `voice-status voice-status--${tone}`;
    if (timeout > 0) {
      statusTimeout = window.setTimeout(() => {
        clearStatus();
      }, timeout);
    }
  };

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

  const formatWeight = (value) => value.toFixed(2);

  const mixTotal = () => {
    let total = 0;
    state.draft.voices.forEach((weight) => {
      total += weight;
    });
    return total;
  };

  const updateActionButtons = () => {
    const hasSelection = Boolean(state.selectedProfile && profiles[state.selectedProfile]);
    if (duplicateBtn) {
      duplicateBtn.disabled = !hasSelection;
    }
    if (deleteBtn) {
      deleteBtn.disabled = !hasSelection;
    }
  };

  const updateMixSummary = () => {
    if (mixTotalEl) {
      mixTotalEl.textContent = `Total weight: ${formatWeight(mixTotal())}`;
    }
    if (profileSummaryEl) {
      const voiceCount = state.draft.voices.size;
      if (!state.draft.name && !voiceCount) {
        profileSummaryEl.textContent = "Select or create a profile to begin.";
      } else {
        const profileLabel = state.draft.name ? `Editing: ${state.draft.name}` : "Unsaved profile";
        profileSummaryEl.textContent = `${profileLabel} · ${voiceCount} voice${voiceCount === 1 ? "" : "s"}`;
      }
    }
  };

  const markDirty = () => {
    state.dirty = true;
    if (saveBtn) {
      saveBtn.disabled = false;
    }
  };

  const resetDirty = () => {
    state.dirty = false;
    if (saveBtn) {
      saveBtn.disabled = true;
    }
  };

  const applyDraftToControls = () => {
    if (nameInput) {
      nameInput.value = state.draft.name || "";
    }
    if (languageSelect) {
      languageSelect.value = state.draft.language || "a";
    }

    voiceControls.forEach((control, voiceId) => {
      const weight = state.draft.voices.get(voiceId) || 0;
      const enabled = weight > 0;
      control.checkbox.checked = enabled;
      control.slider.disabled = !enabled;
      control.number.disabled = !enabled;
      control.slider.value = String(Math.round(weight * 100));
      control.number.value = formatWeight(enabled ? weight : 0);
      control.weightLabel.textContent = `${formatWeight(weight)}`;
    });

    updateMixSummary();
    resetDirty();
    updateActionButtons();
  };

  const setVoiceWeight = (voiceId, weight, enabled) => {
    const normalized = enabled ? clamp(weight, 0, 1) : 0;
    if (normalized > 0) {
      state.draft.voices.set(voiceId, normalized);
    } else {
      state.draft.voices.delete(voiceId);
    }
    updateMixSummary();
    markDirty();
  };

  const buildVoiceCard = (voice) => {
    const card = document.createElement("div");
    card.className = "voice-card";
    card.dataset.voiceId = voice.id;

    const header = document.createElement("div");
    header.className = "voice-card__header";

    const toggleLabel = document.createElement("label");
    toggleLabel.className = "voice-card__toggle";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "voice-card__checkbox";
    toggleLabel.appendChild(checkbox);

    const nameSpan = document.createElement("span");
    nameSpan.className = "voice-card__name";
    nameSpan.textContent = voice.display_name || voice.id;
    toggleLabel.appendChild(nameSpan);

    header.appendChild(toggleLabel);

    const meta = document.createElement("span");
    meta.className = "voice-card__meta";
    meta.textContent = `${voiceLanguageLabel(voice.language)} · ${voiceGenderIcon(voice.gender)}`;
    header.appendChild(meta);

    const weightLabel = document.createElement("span");
    weightLabel.className = "voice-card__value";
    weightLabel.textContent = "0.00";
    header.appendChild(weightLabel);

    const body = document.createElement("div");
    body.className = "voice-card__body";

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = "100";
    slider.step = "1";
    slider.value = "0";
    slider.disabled = true;
    slider.className = "voice-card__slider";

    const number = document.createElement("input");
    number.type = "number";
    number.min = "0";
    number.max = "1";
    number.step = "0.01";
    number.value = "0.00";
    number.disabled = true;
    number.className = "voice-card__number";

    body.appendChild(slider);
    body.appendChild(number);

    card.appendChild(header);
    card.appendChild(body);

    checkbox.addEventListener("change", () => {
      const enabled = checkbox.checked;
      slider.disabled = !enabled;
      number.disabled = !enabled;
      if (!enabled) {
        slider.value = "0";
        number.value = "0.00";
      }
      const weight = enabled ? parseFloat(number.value || "0") : 0;
      weightLabel.textContent = formatWeight(enabled ? weight : 0);
      setVoiceWeight(voice.id, weight, enabled);
    });

    slider.addEventListener("input", () => {
      const weight = clamp(parseInt(slider.value, 10) / 100, 0, 1);
      number.value = formatWeight(weight);
      weightLabel.textContent = formatWeight(weight);
      if (!checkbox.checked && weight > 0) {
        checkbox.checked = true;
        slider.disabled = false;
        number.disabled = false;
      }
      setVoiceWeight(voice.id, weight, true);
    });

    number.addEventListener("change", () => {
      const weight = clamp(parseFloat(number.value || "0"), 0, 1);
      number.value = formatWeight(weight);
      slider.value = String(Math.round(weight * 100));
      weightLabel.textContent = formatWeight(weight);
      if (!checkbox.checked && weight > 0) {
        checkbox.checked = true;
        slider.disabled = false;
        number.disabled = false;
      }
      setVoiceWeight(voice.id, weight, checkbox.checked);
    });

    voiceControls.set(voice.id, { checkbox, slider, number, weightLabel });
    return card;
  };

  const buildVoiceGrid = () => {
    if (!voiceGridEl) return;
    voiceGridEl.innerHTML = "";
    voiceCatalog.forEach((voice) => {
      voiceGridEl.appendChild(buildVoiceCard(voice));
    });
  };

  const loadSampleText = () => {
    if (!previewTextEl || !languageSelect) return;
    const lang = languageSelect.value || "a";
    previewTextEl.value = samples[lang] || samples.a || "This is a sample of the selected voice.";
  };

  const selectProfile = (name) => {
    state.selectedProfile = name;
    state.originalName = name;
    const profile = profiles[name];
    state.draft = {
      name,
      language: profile?.language || "a",
      voices: new Map(),
    };
    if (Array.isArray(profile?.voices)) {
      profile.voices.forEach((entry) => {
        if (Array.isArray(entry) && entry.length >= 2) {
          const [voiceId, weight] = entry;
          const value = parseFloat(weight);
          if (!Number.isNaN(value) && value > 0) {
            state.draft.voices.set(String(voiceId), clamp(value, 0, 1));
          }
        }
      });
    }
    applyDraftToControls();
    renderProfileList();
    loadSampleText();
    setStatus(`Loaded profile “${name}”.`, "info", 2500);
  };

  const createNewProfile = () => {
    state.selectedProfile = null;
    state.originalName = null;
    state.draft = {
      name: "",
      language: languageSelect ? languageSelect.value || "a" : "a",
      voices: new Map(),
    };
    applyDraftToControls();
    renderProfileList();
    loadSampleText();
  };

  const buildProfilePayload = () => {
    const payload = [];
    voiceControls.forEach((control, voiceId) => {
      const enabled = control.checkbox.checked;
      const weight = enabled ? clamp(parseFloat(control.number.value || "0"), 0, 1) : 0;
      payload.push({ id: voiceId, weight, enabled });
    });
    return payload;
  };

  const renderProfileList = () => {
    if (!profileListEl) return;
    profileListEl.innerHTML = "";

    const header = document.createElement("div");
    header.className = "voice-list__header";
    const title = document.createElement("h2");
    title.textContent = "Saved profiles";
    header.appendChild(title);
    profileListEl.appendChild(header);

    const list = document.createElement("ul");
    list.className = "voice-list";

    const names = Object.keys(profiles).sort((a, b) => a.localeCompare(b));
    if (!names.length) {
      const empty = document.createElement("p");
      empty.className = "tag";
      empty.textContent = "No profiles yet. Create one on the right.";
      profileListEl.appendChild(empty);
      return;
    }

    names.forEach((name) => {
      const li = document.createElement("li");
      li.className = "voice-list__item";
      if (state.selectedProfile === name) {
        li.classList.add("is-selected");
      }

      const selectBtn = document.createElement("button");
      selectBtn.type = "button";
      selectBtn.className = "voice-list__select";
      selectBtn.dataset.name = name;
      const profile = profiles[name] || {};
      selectBtn.innerHTML = `
        <span class="voice-list__name">${name}</span>
        <span class="voice-list__meta">${voiceLanguageLabel(profile.language || "a")}</span>
      `;
      selectBtn.addEventListener("click", () => selectProfile(name));

      const actions = document.createElement("div");
      actions.className = "voice-list__actions";

      const duplicateAction = document.createElement("button");
      duplicateAction.type = "button";
      duplicateAction.className = "voice-list__action";
      duplicateAction.textContent = "Duplicate";
      duplicateAction.addEventListener("click", (event) => {
        event.stopPropagation();
        runDuplicate(name);
      });

      const deleteAction = document.createElement("button");
      deleteAction.type = "button";
      deleteAction.className = "voice-list__action voice-list__action--danger";
      deleteAction.textContent = "Delete";
      deleteAction.addEventListener("click", (event) => {
        event.stopPropagation();
        runDelete(name);
      });

      actions.appendChild(duplicateAction);
      actions.appendChild(deleteAction);

      li.appendChild(selectBtn);
      li.appendChild(actions);
      list.appendChild(li);
    });

    profileListEl.appendChild(list);
  };

  const refreshProfiles = (nextProfiles, selectedName = null) => {
    profiles = nextProfiles || {};
    if (selectedName && profiles[selectedName]) {
      selectProfile(selectedName);
    } else if (state.selectedProfile && profiles[state.selectedProfile]) {
      selectProfile(state.selectedProfile);
    } else {
      const names = Object.keys(profiles);
      if (names.length) {
        selectProfile(names[0]);
      } else {
        createNewProfile();
      }
    }
    updateActionButtons();
  };

  const withJson = async (response) => {
    if (response.ok) {
      return response.json();
    }
    let message = "Unexpected error";
    try {
      const data = await response.json();
      message = data.error || data.message || message;
    } catch (err) {
      message = await response.text();
    }
    throw new Error(message);
  };

  const runSave = async () => {
    if (!nameInput) return;
    const name = nameInput.value.trim();
    if (!name) {
      setStatus("Give your profile a name first.", "warning");
      return;
    }
    const payload = {
      name,
      originalName: state.originalName,
      language: languageSelect ? languageSelect.value : "a",
      voices: buildProfilePayload(),
    };
    try {
      const response = await fetch("/api/voice-profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await withJson(response);
      refreshProfiles(result.profiles, result.profile);
      resetDirty();
      setStatus(`Saved profile “${result.profile}”.`, "success");
    } catch (error) {
      setStatus(error.message || "Failed to save profile", "danger", 7000);
    }
  };

  const runDelete = async (targetName = null) => {
    const name = targetName || state.selectedProfile;
    if (!name) {
      setStatus("Select a profile to delete.", "warning");
      return;
    }
    const confirmed = window.confirm(`Delete profile “${name}”?`);
    if (!confirmed) return;
    try {
      const response = await fetch(`/api/voice-profiles/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      const result = await withJson(response);
      refreshProfiles(result.profiles);
      setStatus(`Deleted profile “${name}”.`, "info");
    } catch (error) {
      setStatus(error.message || "Failed to delete profile", "danger", 7000);
    }
  };

  const runDuplicate = async (targetName = null) => {
    const name = targetName || state.selectedProfile;
    if (!name) {
      setStatus("Select a profile to duplicate.", "warning");
      return;
    }
    const newName = window.prompt("Duplicate profile as…", `${name} copy`);
    if (!newName) return;
    try {
      const response = await fetch(`/api/voice-profiles/${encodeURIComponent(name)}/duplicate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
      const result = await withJson(response);
      refreshProfiles(result.profiles, result.profile);
      setStatus(`Duplicated to “${result.profile}”.`, "success");
    } catch (error) {
      setStatus(error.message || "Failed to duplicate profile", "danger", 7000);
    }
  };

  const runImport = async (file) => {
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const replace = window.confirm("Replace existing profiles if duplicates are found?");
      const response = await fetch("/api/voice-profiles/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: parsed, replace_existing: replace }),
      });
      const result = await withJson(response);
      refreshProfiles(result.profiles);
      setStatus(`Imported ${result.imported.length} profile${result.imported.length === 1 ? "" : "s"}.`, "success");
    } catch (error) {
      setStatus(error.message || "Import failed", "danger", 7000);
    } finally {
      importInput.value = "";
    }
  };

  const runExport = async () => {
    const name = state.selectedProfile;
    const query = name ? `?names=${encodeURIComponent(name)}` : "";
    try {
      const response = await fetch(`/api/voice-profiles/export${query}`);
      if (!response.ok) {
        throw new Error("Export failed");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name ? `${name}.json` : "voice_profiles.json";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      setStatus("Export complete.", "success");
    } catch (error) {
      setStatus(error.message || "Export failed", "danger", 7000);
    }
  };

  const runPreview = async () => {
    if (!previewBtn) return;
    const payload = {
      language: languageSelect ? languageSelect.value : "a",
      voices: buildProfilePayload(),
      text: previewTextEl ? previewTextEl.value : "",
      speed: speedInput ? parseFloat(speedInput.value || "1") : 1,
    };
    const enabledVoices = payload.voices.filter((entry) => entry.enabled && entry.weight > 0);
    if (!enabledVoices.length) {
      setStatus("Enable at least one voice to preview.", "warning");
      return;
    }
    previewBtn.disabled = true;
    previewBtn.dataset.loading = "true";
    setStatus("Generating preview…", "info", 0);
    try {
      const response = await fetch("/api/voice-profiles/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const blob = await response.blob();
      if (state.previewUrl) {
        URL.revokeObjectURL(state.previewUrl);
      }
      state.previewUrl = URL.createObjectURL(blob);
      if (previewAudio) {
        previewAudio.src = state.previewUrl;
        previewAudio.play().catch(() => {});
      }
      setStatus("Preview ready.", "success");
    } catch (error) {
      setStatus(error.message || "Preview failed", "danger", 7000);
    } finally {
      previewBtn.disabled = false;
      previewBtn.dataset.loading = "false";
    }
  };

  if (saveBtn) {
    const form = saveBtn.closest("form");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        runSave();
      });
    }
  }

  if (duplicateBtn) {
    duplicateBtn.addEventListener("click", () => runDuplicate());
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", () => runDelete());
  }

  if (previewBtn) {
    previewBtn.addEventListener("click", () => runPreview());
  }

  if (loadSampleBtn) {
    loadSampleBtn.addEventListener("click", loadSampleText);
  }

  if (languageSelect) {
    languageSelect.addEventListener("change", () => {
      state.draft.language = languageSelect.value;
      markDirty();
      loadSampleText();
    });
  }

  if (nameInput) {
    nameInput.addEventListener("input", () => {
      state.draft.name = nameInput.value;
      markDirty();
    });
  }

  if (importInput) {
    importInput.addEventListener("change", () => {
      const [file] = importInput.files || [];
      if (file) {
        runImport(file);
      }
    });
  }

  if (headerActions) {
    headerActions.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const action = target.dataset.action;
      if (!action) return;
      if (action === "new-profile") {
        createNewProfile();
        setStatus("New profile ready.", "info");
      } else if (action === "import-profiles") {
        importInput?.click();
      } else if (action === "export-profiles") {
        runExport();
      }
    });
  }

  buildVoiceGrid();
  renderProfileList();
  createNewProfile();
  if (Object.keys(profiles).length) {
    const first = Object.keys(profiles).sort((a, b) => a.localeCompare(b))[0];
    selectProfile(first);
  }
  loadSampleText();
  app.dataset.state = "ready";
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", setupVoiceMixer, { once: true });
} else {
  setupVoiceMixer();
}
