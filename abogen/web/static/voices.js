const setupVoiceMixer = () => {
  const data = window.ABOGEN_VOICE_MIXER_DATA || {};
  const languages = data.languages || {};
  const voiceCatalog = Array.isArray(data.voice_catalog) ? data.voice_catalog : [];
  const samples = data.sample_voice_texts || {};
  let profiles = data.voice_profiles_data || {};

  const app = document.getElementById("voice-mixer-app");
  if (!app) {
    return;
  }

  const profileListEl = app.querySelector('[data-role="profile-list"]');
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
  const headerActions = document.querySelector(".voice-mixer__header-actions");
  const availableListEl = app.querySelector('[data-role="available-voices"]');
  const selectedListEl = app.querySelector('[data-role="selected-voices"]');
  const dropzoneEl = app.querySelector('[data-role="dropzone"]');
  const emptyStateEl = app.querySelector('[data-role="mix-empty"]');

  if (!profileListEl || !availableListEl || !selectedListEl) {
    return;
  }

  const voiceLookup = new Map();
  voiceCatalog.forEach((voice) => {
    if (voice && voice.id) {
      voiceLookup.set(voice.id, voice);
    }
  });

  const availableCards = new Map();
  const selectedControls = new Map();

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

  let statusTimeout = null;

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
  const formatWeight = (value) => value.toFixed(2);

  const setSliderFill = (slider, weight) => {
    const percent = Math.round(clamp(weight, 0, 1) * 100);
    slider.style.background = `linear-gradient(90deg, var(--accent) 0%, var(--accent) ${percent}%, rgba(148, 163, 184, 0.25) ${percent}%, rgba(148, 163, 184, 0.25) 100%)`;
  };

  const voiceGenderIcon = (gender) => {
    if (!gender) return "•";
    const initial = gender[0].toLowerCase();
    if (initial === "f") return "♀";
    if (initial === "m") return "♂";
    return "•";
  };

  const voiceLanguageLabel = (code) => languages[code] || code?.toUpperCase() || "";

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

  const mixTotal = () => {
    let total = 0;
    state.draft.voices.forEach((weight) => {
      total += weight;
    });
    return total;
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

  const ensureEmptyState = () => {
    if (!emptyStateEl) return;
    emptyStateEl.hidden = state.draft.voices.size > 0;
  };

  const updateAvailableState = () => {
    availableCards.forEach(({ card, addButton }, voiceId) => {
      const isActive = state.draft.voices.has(voiceId);
      card.classList.toggle("is-active", isActive);
      if (addButton) {
        addButton.disabled = isActive;
        addButton.textContent = isActive ? "Added" : "Add";
      }
    });
  };

  const setSliderFocus = (voiceId) => {
    const control = selectedControls.get(voiceId);
    if (control?.slider) {
      control.slider.focus({ preventScroll: false });
    }
  };

  const renderSelectedVoices = () => {
    selectedControls.clear();
    selectedListEl.innerHTML = "";

    state.draft.voices.forEach((weight, voiceId) => {
      const meta = voiceLookup.get(voiceId) || {};
      const card = document.createElement("div");
      card.className = "mix-voice";
      card.dataset.voiceId = voiceId;

      const header = document.createElement("div");
      header.className = "mix-voice__header";

      const titleWrap = document.createElement("div");
      titleWrap.className = "mix-voice__info";

      const title = document.createElement("div");
      title.className = "mix-voice__title";
      title.textContent = meta.display_name || meta.name || voiceId;

      const metaLabel = document.createElement("div");
      metaLabel.className = "mix-voice__meta";
      const languageCode = meta.language || voiceId.charAt(0) || "a";
      metaLabel.textContent = `${voiceLanguageLabel(languageCode)} · ${voiceGenderIcon(meta.gender)}`;

      titleWrap.appendChild(title);
      titleWrap.appendChild(metaLabel);

      const weightLabel = document.createElement("span");
      weightLabel.className = "mix-voice__weight";
      weightLabel.textContent = formatWeight(weight);

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "mix-voice__remove";
      removeBtn.setAttribute("aria-label", `Remove ${title.textContent} from mix`);
      removeBtn.innerHTML = "&times;";
      removeBtn.addEventListener("click", () => {
        state.draft.voices.delete(voiceId);
        renderSelectedVoices();
        updateAvailableState();
        updateMixSummary();
        markDirty();
      });

      header.appendChild(titleWrap);
      header.appendChild(weightLabel);
      header.appendChild(removeBtn);

      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = "0";
      slider.max = "100";
      slider.step = "1";
      slider.className = "mix-slider";
      slider.value = String(Math.round(clamp(weight, 0, 1) * 100));
      setSliderFill(slider, weight);
      slider.addEventListener("input", () => {
        const value = clamp(Number(slider.value) / 100, 0, 1);
        state.draft.voices.set(voiceId, value);
        weightLabel.textContent = formatWeight(value);
        setSliderFill(slider, value);
        updateMixSummary();
        markDirty();
      });

      card.appendChild(header);
      card.appendChild(slider);
      selectedListEl.appendChild(card);

      selectedControls.set(voiceId, { slider, weightLabel });
    });

    ensureEmptyState();
  };

  const renderAvailableVoices = () => {
    availableCards.clear();
    availableListEl.innerHTML = "";

    const sortedVoices = voiceCatalog
      .slice()
      .sort((a, b) => (a.display_name || a.id).localeCompare(b.display_name || b.id));

    sortedVoices.forEach((voice) => {
      if (!voice?.id) {
        return;
      }
      const card = document.createElement("article");
      card.className = "voice-available__card";
      card.draggable = true;
      card.dataset.voiceId = voice.id;
      card.tabIndex = 0;

      card.addEventListener("dragstart", (event) => {
        card.classList.add("is-dragging");
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "copy";
          event.dataTransfer.setData("text/plain", voice.id);
        }
      });

      card.addEventListener("dragend", () => {
        card.classList.remove("is-dragging");
      });

      card.addEventListener("dblclick", () => {
        addVoiceToDraft(voice.id);
      });

      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          addVoiceToDraft(voice.id);
        }
      });

      const info = document.createElement("div");
      info.className = "voice-available__info";

      const name = document.createElement("div");
      name.className = "voice-available__name";
      name.textContent = voice.display_name || voice.id;

      const meta = document.createElement("div");
      meta.className = "voice-available__meta";
      const languageCode = voice.language || voice.id.charAt(0) || "a";
      meta.textContent = `${voiceLanguageLabel(languageCode)} · ${voiceGenderIcon(voice.gender)}`;

      info.appendChild(name);
      info.appendChild(meta);

      const addButton = document.createElement("button");
      addButton.type = "button";
      addButton.className = "voice-available__add";
      addButton.textContent = "Add";
      addButton.addEventListener("click", (event) => {
        event.stopPropagation();
        addVoiceToDraft(voice.id);
      });

      card.appendChild(info);
      card.appendChild(addButton);
      availableListEl.appendChild(card);

      availableCards.set(voice.id, { card, addButton });
    });

    updateAvailableState();
  };

  const addVoiceToDraft = (voiceId, weight = 0.6) => {
    if (!voiceLookup.has(voiceId)) {
      return;
    }
    if (state.draft.voices.has(voiceId)) {
      setSliderFocus(voiceId);
      return;
    }
    state.draft.voices.set(voiceId, clamp(weight, 0.05, 1));
    renderSelectedVoices();
    updateAvailableState();
    updateMixSummary();
    markDirty();
    setSliderFocus(voiceId);
  };

  const buildProfilePayload = () =>
    Array.from(state.draft.voices.entries()).map(([voiceId, weight]) => ({
      id: voiceId,
      weight,
      enabled: weight > 0,
    }));

  const updateActionButtons = () => {
    const hasSelection = Boolean(state.selectedProfile && profiles[state.selectedProfile]);
    if (duplicateBtn) {
      duplicateBtn.disabled = !hasSelection;
    }
    if (deleteBtn) {
      deleteBtn.disabled = !hasSelection;
    }
  };

  const applyDraftToControls = () => {
    if (nameInput) {
      nameInput.value = state.draft.name || "";
    }
    if (languageSelect) {
      languageSelect.value = state.draft.language || "a";
    }
    renderSelectedVoices();
    updateMixSummary();
    updateAvailableState();
    updateActionButtons();
    resetDirty();
  };

  const renderProfileList = () => {
    profileListEl.innerHTML = "";

    const header = document.createElement("div");
    header.className = "voice-list__header";
    const heading = document.createElement("h2");
    heading.textContent = "Saved profiles";
    header.appendChild(heading);
    profileListEl.appendChild(header);

    const names = Object.keys(profiles).sort((a, b) => a.localeCompare(b));
    if (!names.length) {
      const empty = document.createElement("p");
      empty.className = "tag";
      empty.textContent = "No profiles yet. Create one on the right.";
      profileListEl.appendChild(empty);
      return;
    }

    const list = document.createElement("ul");
    list.className = "voice-list";

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
          const value = clamp(parseFloat(weight), 0, 1);
          if (!Number.isNaN(value) && value > 0) {
            state.draft.voices.set(String(voiceId), value);
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

  const refreshProfiles = (nextProfiles, selectedName = null) => {
    profiles = nextProfiles || {};
    renderProfileList();
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

  const loadSampleText = () => {
    if (!previewTextEl || !languageSelect) return;
    const lang = languageSelect.value || "a";
    previewTextEl.value = samples[lang] || samples.a || "This is a sample of the selected voice.";
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
      updateMixSummary();
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

  if (dropzoneEl) {
    const setHover = (hovered) => {
      dropzoneEl.classList.toggle("is-hovered", hovered);
    };
    [dropzoneEl, selectedListEl].forEach((target) => {
      target.addEventListener("dragover", (event) => {
        event.preventDefault();
        setHover(true);
      });
      target.addEventListener("dragenter", (event) => {
        event.preventDefault();
        setHover(true);
      });
      target.addEventListener("dragleave", (event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          setHover(false);
        }
      });
      target.addEventListener("drop", (event) => {
        event.preventDefault();
        const voiceId = event.dataTransfer?.getData("text/plain");
        if (voiceId) {
          addVoiceToDraft(voiceId);
        }
        setHover(false);
      });
    });

    dropzoneEl.addEventListener("click", () => {
      const firstInactive = Array.from(availableCards.entries()).find(
        ([voiceId]) => !state.draft.voices.has(voiceId),
      );
      if (firstInactive) {
        addVoiceToDraft(firstInactive[0]);
      }
    });
  }

  renderAvailableVoices();
  renderProfileList();
  createNewProfile();

  if (Object.keys(profiles).length) {
    const first = Object.keys(profiles).sort((a, b) => a.localeCompare(b))[0];
    selectProfile(first);
  }

  loadSampleText();
  updateActionButtons();
  app.dataset.state = "ready";

  window.addEventListener("beforeunload", () => {
    if (state.previewUrl) {
      URL.revokeObjectURL(state.previewUrl);
    }
  });
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", setupVoiceMixer, { once: true });
} else {
  setupVoiceMixer();
}
