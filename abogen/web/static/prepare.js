document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector(".prepare-form");
  if (!form) return;

  const parseJSONScript = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
      const content = el.textContent || "";
      return content ? JSON.parse(content) : null;
    } catch (error) {
      console.warn(`Failed to parse JSON script for ${id}`, error);
      return null;
    }
  };

  const voiceCatalog = parseJSONScript("voice-catalog-data") || [];
  const languageMap = parseJSONScript("voice-language-map") || {};
  const voiceCatalogMap = new Map(voiceCatalog.map((voice) => [voice.id, voice]));

  const chapterRows = Array.from(form.querySelectorAll("[data-role=chapter-row]"));

  const updateRowState = (row) => {
    const enabled = row.querySelector('[data-role=chapter-enabled]');
    const inputs = Array.from(row.querySelectorAll("input[type=text], select, textarea"));
    const warning = row.querySelector('[data-role=chapter-warning]');
    const isChecked = enabled ? enabled.checked : true;
    row.dataset.disabled = isChecked ? "false" : "true";

    inputs.forEach((input) => {
      if (input === enabled) return;
      input.disabled = !isChecked;
      if (!isChecked) {
        if (input.tagName === "SELECT") {
          input.dataset.prevValue = input.value;
          input.value = "__default";
        }
        if (input.dataset.role === "formula-input") {
          input.value = "";
          input.hidden = true;
          input.setAttribute("aria-hidden", "true");
        }
      } else if (input.tagName === "SELECT" && input.dataset.prevValue) {
        input.value = input.dataset.prevValue;
      }
    });

    const select = row.querySelector("select[data-role=voice-select]");
    toggleFormula(select);

    if (warning) {
      warning.hidden = isChecked;
      warning.setAttribute("aria-hidden", isChecked ? "true" : "false");
    }
  };

  const toggleFormula = (select) => {
    if (!select) return;
    const container = select.closest("[data-role=chapter-row]");
    const formulaInput = container.querySelector('[data-role=formula-input]');
    const isFormula = select.value === "formula";
    formulaInput.hidden = !isFormula;
    formulaInput.setAttribute("aria-hidden", isFormula ? "false" : "true");
    if (!isFormula) {
      formulaInput.value = "";
    }
    if (isFormula) {
      formulaInput.required = true;
    } else {
      formulaInput.required = false;
    }
  };

  chapterRows.forEach((row) => {
    const enabled = row.querySelector('[data-role=chapter-enabled]');
    if (enabled) {
      enabled.addEventListener("change", () => updateRowState(row));
      updateRowState(row);
    }
    const select = row.querySelector("select[data-role=voice-select]");
    if (select) {
      select.addEventListener("change", () => toggleFormula(select));
      toggleFormula(select);
    }
  });

  const updatePreviewVoice = (select) => {
    const container = select.closest(".speaker-list__item");
    if (!container) return;
    const previewButton = container.querySelector('[data-role="speaker-preview"]');
    if (!previewButton) return;
    const formulaInput = container.querySelector('[data-role="speaker-formula"]');
    const mixContainer = container.querySelector('[data-role="speaker-mix"]');
    const mixLabel = container.querySelector('[data-role="speaker-mix-label"]');
    const formulaValue = formulaInput?.value?.trim();
    if (formulaValue) {
      previewButton.dataset.voice = formulaValue;
      if (mixContainer) {
        mixContainer.hidden = false;
      }
      if (mixLabel) {
        mixLabel.textContent = formulaValue;
      }
      return;
    }
    if (mixContainer) {
      mixContainer.hidden = true;
    }
    const defaultVoice = select.dataset.defaultVoice || previewButton.dataset.voice || "";
    const currentVoice = select.disabled ? defaultVoice : (select.value || defaultVoice);
    previewButton.dataset.voice = currentVoice || defaultVoice;
  };

  const handleRandomizeToggle = (checkbox) => {
    const container = checkbox.closest(".speaker-list__item");
    if (!container) return;
    const select = container.querySelector('[data-role="speaker-voice"]');
    if (!select) return;
    if (checkbox.checked) {
      if (!select.dataset.prevManual) {
        select.dataset.prevManual = select.value;
      }
      const formulaInput = container.querySelector('[data-role="speaker-formula"]');
      const mixContainer = container.querySelector('[data-role="speaker-mix"]');
      if (formulaInput) {
        formulaInput.value = "";
      }
      if (mixContainer) {
        mixContainer.hidden = true;
      }
      select.dataset.suppressRandomize = "1";
      select.disabled = true;
      select.value = "";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      delete select.dataset.suppressRandomize;
    } else {
      const previous = select.dataset.prevManual || "";
      select.disabled = false;
      select.dataset.suppressRandomize = "1";
      select.value = previous;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      delete select.dataset.suppressRandomize;
    }
  };

  const voiceSelects = Array.from(form.querySelectorAll('[data-role="speaker-voice"]'));
  voiceSelects.forEach((select) => {
    select.addEventListener("change", (event) => {
      const target = event.target;
      const container = target.closest(".speaker-list__item");
      if (container && !target.dataset.suppressRandomize) {
        const randomToggle = container.querySelector('[data-role="randomize-toggle"]');
        if (randomToggle && randomToggle.checked && target.value) {
          randomToggle.checked = false;
          handleRandomizeToggle(randomToggle);
        }
      }
      if (container && !target.dataset.suppressFormulaClear) {
        const formulaInput = container.querySelector('[data-role="speaker-formula"]');
        const mixContainer = container.querySelector('[data-role="speaker-mix"]');
        if (formulaInput) {
          formulaInput.value = "";
        }
        if (mixContainer) {
          mixContainer.hidden = true;
        }
      }
      if (!target.dataset.suppressRandomize) {
        target.dataset.prevManual = target.value || "";
      }
      updatePreviewVoice(target);
      delete target.dataset.suppressFormulaClear;
    });
    updatePreviewVoice(select);
  });

  const randomizeToggles = Array.from(form.querySelectorAll('[data-role="randomize-toggle"]'));
  randomizeToggles.forEach((checkbox) => {
    handleRandomizeToggle(checkbox);
    checkbox.addEventListener("change", () => handleRandomizeToggle(checkbox));
  });

  const finalizeAction = form.getAttribute("action");
  const analyzeUrl = form.dataset.analyzeUrl || "";
  const activeStepInput = form.querySelector('[data-role="active-step-input"]');
  const wizard = document.querySelector('[data-role="prepare-wizard"]');
  if (wizard) {
    const stepOrder = ["chapters", "speakers"];
    const indicatorOrder = ["setup", ...stepOrder];
    const indicator = wizard.querySelector('[data-role="wizard-indicator"]');
    const indicatorSteps = indicator ? Array.from(indicator.querySelectorAll('[data-role="wizard-step"]')) : [];
    const navButtons = Array.from(wizard.querySelectorAll('[data-role="prepare-step-nav"] [data-step-target]'));
    const nextButtons = Array.from(wizard.querySelectorAll('[data-role="step-next"]'));
    const prevButtons = Array.from(wizard.querySelectorAll('[data-role="step-prev"]'));
    const panels = new Map();
    const initialStep = wizard.dataset.initialStep || "chapters";
    stepOrder.forEach((step) => {
      const panel = wizard.querySelector(`[data-step-panel="${step}"]`);
      if (panel) {
        panels.set(step, panel);
        const isInitial = step === initialStep;
        panel.hidden = !isInitial;
        panel.setAttribute("aria-hidden", isInitial ? "false" : "true");
      }
    });

    const unlockedSteps = new Set(["chapters"]);
    if (initialStep === "speakers") {
      unlockedSteps.add("speakers");
    }
    let currentStep = initialStep;

    const updateIndicator = (activeStep) => {
      const activeIndex = indicatorOrder.indexOf(activeStep);
      indicatorSteps.forEach((item) => {
        const key = item.dataset.stepKey;
        if (!key) return;
        const index = indicatorOrder.indexOf(key);
        item.classList.remove("is-active", "is-complete");
        if (index < activeIndex) {
          item.classList.add("is-complete");
        } else if (index === activeIndex) {
          item.classList.add("is-active");
        }
      });
    };

    const unlockStep = (step) => {
      if (unlockedSteps.has(step)) {
        return;
      }
      unlockedSteps.add(step);
      navButtons.forEach((button) => {
        if (button.dataset.stepTarget === step) {
          button.disabled = false;
          button.removeAttribute("aria-disabled");
          button.dataset.state = "unlocked";
        }
      });
    };

    const setStep = (step) => {
      if (!panels.has(step)) {
        return;
      }
      currentStep = step;
      wizard.dataset.activeStep = step;
      if (activeStepInput) {
        activeStepInput.value = step;
      }
      panels.forEach((panel, key) => {
        const isActive = key === step;
        panel.hidden = !isActive;
        panel.setAttribute("aria-hidden", isActive ? "false" : "true");
      });
      navButtons.forEach((button) => {
        const target = button.dataset.stepTarget;
        if (!target) return;
        const isActive = target === step;
        button.classList.toggle("is-active", isActive);
        if (button.dataset.state === "locked" && !unlockedSteps.has(target)) {
          button.disabled = true;
          button.setAttribute("aria-disabled", "true");
        } else {
          button.disabled = false;
          button.removeAttribute("aria-disabled");
        }
      });
      updateIndicator(step);
    };

    const submitForAnalysis = () => {
      if (!analyzeUrl) {
        unlockStep("speakers");
        setStep("speakers");
        return;
      }
      if (!form.reportValidity()) {
        return;
      }
      if (activeStepInput) {
        activeStepInput.value = "speakers";
      }
      if (finalizeAction) {
        form.setAttribute("data-finalize-action", finalizeAction);
      }
      form.action = analyzeUrl;
      form.submit();
      if (finalizeAction) {
        window.setTimeout(() => {
          form.action = finalizeAction;
        }, 0);
      }
    };

    navButtons.forEach((button) => {
      button.addEventListener("click", () => {
        if (button.disabled) {
          return;
        }
        const target = button.dataset.stepTarget;
        if (!target) return;
        unlockStep(target);
        setStep(target);
      });
    });

    nextButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const target = button.dataset.stepTarget || "speakers";
        if (target === "speakers") {
          submitForAnalysis();
          return;
        }
        unlockStep(target);
        setStep(target);
      });
    });

    prevButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const target = button.dataset.stepTarget || "chapters";
        setStep(target);
      });
    });

    setStep(currentStep);
  }

  const voiceModal = document.querySelector('[data-role="voice-modal"]');
  let activeGenderFilter = "";

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

  const parseFormula = (formula) => {
    const mix = new Map();
    if (!formula) return mix;
    const parts = formula.split("+");
    parts.forEach((part) => {
      const segment = part.trim();
      if (!segment) return;
      const pieces = segment.split("*");
      const voiceId = pieces[0].trim();
      if (!voiceId) return;
      let weight = 1;
      if (pieces[1]) {
        const parsed = Number.parseFloat(pieces[1].trim());
        if (!Number.isNaN(parsed) && parsed > 0) {
          weight = parsed;
        }
      }
      mix.set(voiceId, clamp(weight, 0.05, 1));
    });
    return mix;
  };

  const normaliseMix = (mix) => {
    const entries = Array.from(mix.entries());
    const total = entries.reduce((sum, [, weight]) => sum + weight, 0);
    if (!total) return mix;
    entries.forEach(([voiceId, weight]) => {
      mix.set(voiceId, weight / total);
    });
    return mix;
  };

  const formatMix = (mix) => {
    const entries = Array.from(mix.entries());
    if (!entries.length) return "";
    let total = entries.reduce((sum, [, weight]) => sum + weight, 0);
    if (total < 0.5) {
      const scale = 0.5 / total;
      entries.forEach(([voiceId, weight]) => {
        mix.set(voiceId, clamp(weight * scale, 0.05, 1));
      });
      total = entries.reduce((sum, [, weight]) => sum + weight, 0);
    }
    return entries
      .map(([voiceId, weight]) => `${voiceId}*${(weight / total).toFixed(2)}`)
      .join("+");
  };

  const genderLabel = (value) => {
    switch ((value || "unknown").toLowerCase()) {
      case "male":
        return "Male";
      case "female":
        return "Female";
      case "either":
        return "Either";
      default:
        return "Unknown";
    }
  };

  const buildRandomMix = (gender, countOverride) => {
    const genderCode = (gender || "unknown").toLowerCase();
    const pool = voiceCatalog.filter((voice) => {
      const code = (voice.gender_code || "").toLowerCase();
      if (genderCode === "female") return code === "f";
      if (genderCode === "male") return code === "m";
      if (genderCode === "either") return code === "f" || code === "m";
      return true;
    });
    if (!pool.length) {
      return null;
    }
    const voices = [...pool];
    for (let i = voices.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [voices[i], voices[j]] = [voices[j], voices[i]];
    }
    const count = clamp(countOverride || Math.floor(Math.random() * 4) + 1, 1, 4);
    const selected = voices.slice(0, count);
    const mix = new Map();
    const rawWeights = selected.map(() => Math.random() + 0.2);
    const total = rawWeights.reduce((sum, weight) => sum + weight, 0);
    selected.forEach((voice, index) => {
      mix.set(voice.id, rawWeights[index] / total);
    });
    return mix;
  };

  const applyFormulaToSpeaker = (speakerItem, formula) => {
    if (!speakerItem) return;
    const select = speakerItem.querySelector('[data-role="speaker-voice"]');
    const formulaInput = speakerItem.querySelector('[data-role="speaker-formula"]');
    const mixLabel = speakerItem.querySelector('[data-role="speaker-mix-label"]');
    const mixContainer = speakerItem.querySelector('[data-role="speaker-mix"]');
    const previewButton = speakerItem.querySelector('[data-role="speaker-preview"]');
    const randomToggle = speakerItem.querySelector('[data-role="randomize-toggle"]');
    if (randomToggle && randomToggle.checked) {
      randomToggle.checked = false;
      handleRandomizeToggle(randomToggle);
    }
    if (formulaInput) {
      formulaInput.value = formula || "";
    }
    if (mixLabel) {
      mixLabel.textContent = formula || "";
    }
    if (mixContainer) {
      mixContainer.hidden = !formula;
    }
    if (select) {
      select.disabled = false;
      select.dataset.suppressRandomize = "1";
      select.dataset.suppressFormulaClear = "1";
      if (formula) {
        select.value = "";
      } else if (!select.value && select.dataset.prevManual) {
        select.value = select.dataset.prevManual;
      }
      select.dispatchEvent(new Event("change", { bubbles: true }));
      delete select.dataset.suppressRandomize;
    }
    if (previewButton) {
      if (formula) {
        previewButton.dataset.voice = formula;
      } else {
        const defaultVoice = select?.dataset.defaultVoice || previewButton.dataset.voice || "";
        previewButton.dataset.voice = select?.value || defaultVoice;
      }
    }
  };

  const hideGenderMenus = () => {
    form.querySelectorAll('[data-role="gender-menu"]').forEach((menu) => {
      menu.hidden = true;
      menu.setAttribute("aria-hidden", "true");
    });
    form.querySelectorAll('[data-role="gender-pill"]').forEach((pill) => {
      pill.classList.remove("is-open");
    });
  };

  const setGenderForSpeaker = (genderContainer, value) => {
    if (!genderContainer) return;
    const normalized = value || "unknown";
    const input = genderContainer.querySelector('[data-role="gender-input"]');
    if (input) {
      input.value = normalized;
    }
    const pill = genderContainer.querySelector('[data-role="gender-pill"]');
    if (pill) {
      pill.dataset.current = normalized;
      pill.textContent = `${genderLabel(normalized)} voice`;
    }
    const options = genderContainer.querySelectorAll('[data-role="gender-option"]');
    options.forEach((option) => {
      if ((option.dataset.value || "unknown") === normalized) {
        option.dataset.state = "active";
      } else {
        option.removeAttribute("data-state");
      }
    });
  };

  Array.from(form.querySelectorAll('[data-role="speaker-gender"]')).forEach((container) => {
    const input = container.querySelector('[data-role="gender-input"]');
    setGenderForSpeaker(container, input?.value || "unknown");
  });

  const modalState = {
    speakerItem: null,
    samples: [],
    recommended: new Set(),
    mix: new Map(),
    highlighted: "",
    defaultVoice: "",
    previewSettings: { language: "a", speed: "1", useGpu: "true" },
  };

  const resetModalState = () => {
    modalState.speakerItem = null;
    modalState.samples = [];
    modalState.recommended = new Set();
    modalState.mix = new Map();
    modalState.highlighted = "";
    modalState.defaultVoice = "";
    modalState.previewSettings = { language: "a", speed: "1", useGpu: "true" };
  };

  const getMixFormula = () => formatMix(normaliseMix(new Map(modalState.mix)));

  const renderVoiceList = (elements) => {
    if (!elements) return;
    const { list, searchInput, languageSelect } = elements;
    if (!list) return;
    list.innerHTML = "";
    const term = (searchInput?.value || "").trim().toLowerCase();
    const languageFilter = languageSelect?.value || "";
    const filtered = voiceCatalog
      .filter((voice) => {
        if (languageFilter && voice.language !== languageFilter) return false;
        if (activeGenderFilter && voice.gender_code !== activeGenderFilter) return false;
        if (term) {
          const haystacks = [voice.display_name, voice.id, voice.language_label, languageMap[voice.language]]
            .filter(Boolean)
            .map((value) => value.toLowerCase());
          if (!haystacks.some((value) => value.includes(term))) {
            return false;
          }
        }
        return true;
      })
      .sort((a, b) => {
        const aRecommended = modalState.recommended.has(a.id) ? 0 : 1;
        const bRecommended = modalState.recommended.has(b.id) ? 0 : 1;
        if (aRecommended !== bRecommended) {
          return aRecommended - bRecommended;
        }
        return a.display_name.localeCompare(b.display_name);
      });

    if (!filtered.length) {
      const emptyItem = document.createElement("li");
      emptyItem.className = "voice-browser__empty";
      emptyItem.textContent = "No voices matched your filters.";
      list.appendChild(emptyItem);
      return;
    }

    filtered.forEach((voice) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "voice-browser__entry";
      button.dataset.role = "voice-modal-item";
      button.dataset.voiceId = voice.id;
      if (modalState.mix.has(voice.id)) {
        button.dataset.inMix = "true";
      }
      if (modalState.highlighted === voice.id) {
        button.setAttribute("aria-current", "true");
      }
      if (modalState.recommended.has(voice.id)) {
        button.dataset.recommended = "true";
      }
      const nameSpan = document.createElement("span");
      nameSpan.className = "voice-browser__entry-name";
      nameSpan.textContent = voice.display_name;
      const metaSpan = document.createElement("span");
      metaSpan.className = "voice-browser__entry-meta";
      metaSpan.textContent = `${voice.language_label} · ${voice.gender}`;
      button.appendChild(nameSpan);
      button.appendChild(metaSpan);
      item.appendChild(button);
      list.appendChild(item);
    });
  };

  const renderMix = (elements) => {
    const { mixList, mixTotal } = elements;
    if (!mixList) return;
    mixList.innerHTML = "";
    const entries = Array.from(normaliseMix(new Map(modalState.mix)).entries());
    const total = entries.reduce((sum, [, weight]) => sum + weight, 0);
    if (mixTotal) {
      mixTotal.textContent = `Total weight: ${total.toFixed(2)}`;
    }
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "voice-browser__empty";
      empty.textContent = "Add voices from the list to build a blend.";
      mixList.appendChild(empty);
      return;
    }
    entries.forEach(([voiceId, weight]) => {
      const wrapper = document.createElement("div");
      wrapper.className = "voice-browser__mix-item";
      wrapper.dataset.voiceId = voiceId;

      const header = document.createElement("div");
      header.className = "voice-browser__mix-header";
      const voiceMeta = voiceCatalogMap.get(voiceId) || {};
      const title = document.createElement("span");
      title.className = "voice-browser__mix-name";
      title.textContent = voiceMeta.display_name || voiceId;
      const weightLabel = document.createElement("span");
      weightLabel.className = "voice-browser__mix-weight";
      weightLabel.textContent = weight.toFixed(2);
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "voice-browser__mix-remove";
      removeBtn.setAttribute("aria-label", `Remove ${title.textContent} from blend`);
      removeBtn.textContent = "✕";
      removeBtn.addEventListener("click", () => {
        modalState.mix.delete(voiceId);
        if (modalState.highlighted === voiceId) {
          modalState.highlighted = "";
        }
        renderMix(elements);
        renderVoiceList(elements);
        updateModalMeta(elements);
        updateApplyState(elements);
      });
      header.appendChild(title);
      header.appendChild(weightLabel);
      header.appendChild(removeBtn);

      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = "5";
      slider.max = "100";
      slider.step = "1";
      slider.value = String(Math.round(weight * 100));
      slider.addEventListener("input", () => {
        const value = clamp(Number.parseInt(slider.value, 10) / 100, 0.05, 1);
        modalState.mix.set(voiceId, value);
        modalState.highlighted = voiceId;
        renderMix(elements);
        updateModalMeta(elements);
        updateApplyState(elements);
      });

      wrapper.appendChild(header);
      wrapper.appendChild(slider);
      mixList.appendChild(wrapper);
    });
  };

  const renderSamples = (elements) => {
    if (!elements) return;
    const { samplesContainer } = elements;
    if (!samplesContainer) return;
    samplesContainer.innerHTML = "";

    if (!modalState.samples.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "No sample paragraphs available yet.";
      samplesContainer.appendChild(empty);
      return;
    }

    const formula = getMixFormula();
    modalState.samples.forEach((text, index) => {
      const sample = document.createElement("article");
      sample.className = "voice-browser__sample";
      sample.dataset.sampleIndex = String(index);
      if (index === 0) {
        sample.dataset.active = "true";
      }

      const paragraph = document.createElement("p");
      paragraph.textContent = text;
      const actions = document.createElement("div");
      actions.className = "voice-browser__sample-actions";

      const previewButton = document.createElement("button");
      previewButton.type = "button";
      previewButton.className = "button button--ghost button--small";
      previewButton.dataset.role = "speaker-preview";
      previewButton.dataset.previewText = text;
      previewButton.dataset.language = modalState.previewSettings.language;
      previewButton.dataset.speed = modalState.previewSettings.speed;
      previewButton.dataset.useGpu = modalState.previewSettings.useGpu;
      previewButton.dataset.voice = formula || modalState.defaultVoice || "";
      previewButton.textContent = "Preview sample";

      actions.appendChild(previewButton);
      sample.appendChild(paragraph);
      sample.appendChild(actions);
      samplesContainer.appendChild(sample);
    });
  };

  const updateModalMeta = (elements) => {
    if (!elements) return;
    const { nameLabel, metaLabel } = elements;
    if (!nameLabel || !metaLabel) return;
    if (!modalState.mix.size) {
      nameLabel.textContent = "Select voices to build a blend";
      metaLabel.textContent = "";
      return;
    }
    const highlight = modalState.highlighted && modalState.mix.has(modalState.highlighted)
      ? modalState.highlighted
      : Array.from(modalState.mix.keys())[0];
    modalState.highlighted = highlight;
    const voice = voiceCatalogMap.get(highlight);
    if (!voice) {
      nameLabel.textContent = highlight;
      metaLabel.textContent = "";
      return;
    }
    nameLabel.textContent = voice.display_name;
    metaLabel.textContent = `${voice.language_label} · ${voice.gender}`;
  };

  const updateApplyState = (elements) => {
    const { applyButton } = elements || {};
    if (!applyButton) return;
    const formula = getMixFormula();
    applyButton.disabled = !formula;
  };

  const refreshModal = (elements) => {
    renderVoiceList(elements);
    renderMix(elements);
    renderSamples(elements);
    updateModalMeta(elements);
    updateApplyState(elements);
  };

  const openVoiceBrowser = (speakerItem, sampleIndex = 0) => {
    if (!voiceModal) return;
    modalState.speakerItem = speakerItem;
    const select = speakerItem.querySelector('[data-role="speaker-voice"]');
    const previewTrigger = speakerItem.querySelector('[data-role="speaker-preview"]');
    const formulaInput = speakerItem.querySelector('[data-role="speaker-formula"]');
    modalState.defaultVoice = select?.dataset.defaultVoice || previewTrigger?.dataset.voice || "";
    modalState.mix = formulaInput?.value ? parseFormula(formulaInput.value) : new Map();
    if (!modalState.mix.size && select && select.value) {
      modalState.mix.set(select.value, 1);
    }
    modalState.mix = normaliseMix(modalState.mix);
    modalState.previewSettings = {
      language: previewTrigger?.dataset.language || "a",
      speed: previewTrigger?.dataset.speed || "1",
      useGpu: previewTrigger?.dataset.useGpu || "true",
    };

    const samplesTemplate = speakerItem.querySelector('template[data-role="speaker-samples"]');
    let samples = [];
    if (samplesTemplate) {
      try {
        const raw = samplesTemplate.innerHTML || "[]";
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          samples = parsed
            .map((entry) => (typeof entry === "string" ? entry : entry?.excerpt))
            .filter((value) => typeof value === "string" && value.trim().length);
        }
      } catch (error) {
        console.warn("Unable to parse speaker samples", error);
      }
    }
    if (previewTrigger?.dataset.previewText) {
      samples.unshift(previewTrigger.dataset.previewText);
    }
    const uniqueSamples = Array.from(new Set(samples));
    if (sampleIndex > 0 && sampleIndex < uniqueSamples.length) {
      const [picked] = uniqueSamples.splice(sampleIndex, 1);
      uniqueSamples.unshift(picked);
    }
    modalState.samples = uniqueSamples;
    modalState.recommended = new Set(
      Array.from(speakerItem.querySelectorAll('[data-role="recommended-voice"]')).map((btn) => btn.dataset.voice).filter(Boolean)
    );
    activeGenderFilter = "";

    const elements = {
      list: voiceModal.querySelector('[data-role="voice-modal-list"]'),
      searchInput: voiceModal.querySelector('[data-role="voice-modal-search"]'),
      languageSelect: voiceModal.querySelector('[data-role="voice-modal-language"]'),
      genderButtons: Array.from(voiceModal.querySelectorAll('[data-role="voice-modal-gender"]')),
      mixList: voiceModal.querySelector('[data-role="voice-modal-mix-list"]'),
      mixTotal: voiceModal.querySelector('[data-role="voice-modal-mix-total"]'),
      samplesContainer: voiceModal.querySelector('[data-role="voice-modal-samples"]'),
      applyButton: voiceModal.querySelector('[data-role="voice-modal-apply"]'),
      nameLabel: voiceModal.querySelector('[data-role="voice-modal-selected-name"]'),
      metaLabel: voiceModal.querySelector('[data-role="voice-modal-selected-meta"]'),
    };

    if (elements.searchInput) elements.searchInput.value = "";
    if (elements.languageSelect) elements.languageSelect.value = "";
    elements.genderButtons.forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.value === "" ? "true" : "false");
    });

    refreshModal(elements);

    voiceModal.hidden = false;
    voiceModal.dataset.open = "true";
    document.body.classList.add("modal-open");
    if (elements.searchInput) {
      setTimeout(() => elements.searchInput.focus({ preventScroll: true }), 0);
    }
  };

  const closeVoiceBrowser = () => {
    if (!voiceModal || voiceModal.hidden) return;
    voiceModal.hidden = true;
    voiceModal.removeAttribute("data-open");
    document.body.classList.remove("modal-open");
    resetModalState();
  };

  if (voiceModal) {
    const elements = {
      list: voiceModal.querySelector('[data-role="voice-modal-list"]'),
      searchInput: voiceModal.querySelector('[data-role="voice-modal-search"]'),
      languageSelect: voiceModal.querySelector('[data-role="voice-modal-language"]'),
      genderButtons: Array.from(voiceModal.querySelectorAll('[data-role="voice-modal-gender"]')),
      mixList: voiceModal.querySelector('[data-role="voice-modal-mix-list"]'),
      mixTotal: voiceModal.querySelector('[data-role="voice-modal-mix-total"]'),
      samplesContainer: voiceModal.querySelector('[data-role="voice-modal-samples"]'),
      applyButton: voiceModal.querySelector('[data-role="voice-modal-apply"]'),
      nameLabel: voiceModal.querySelector('[data-role="voice-modal-selected-name"]'),
      metaLabel: voiceModal.querySelector('[data-role="voice-modal-selected-meta"]'),
      randomButton: voiceModal.querySelector('[data-role="voice-modal-random"]'),
      clearButton: voiceModal.querySelector('[data-role="voice-modal-clear"]'),
    };

    if (elements.searchInput) {
      elements.searchInput.addEventListener("input", () => renderVoiceList(elements));
    }
    if (elements.languageSelect) {
      elements.languageSelect.addEventListener("change", () => renderVoiceList(elements));
    }
    elements.genderButtons.forEach((button) => {
      button.addEventListener("click", () => {
        activeGenderFilter = button.dataset.value || "";
        elements.genderButtons.forEach((btn) => btn.setAttribute("aria-pressed", btn === button ? "true" : "false"));
        renderVoiceList(elements);
      });
    });
    if (elements.list) {
      elements.list.addEventListener("click", (event) => {
        const target = event.target.closest('[data-role="voice-modal-item"]');
        if (!target) return;
        event.preventDefault();
        const voiceId = target.dataset.voiceId;
        if (!voiceId) return;
        if (!modalState.mix.has(voiceId)) {
          modalState.mix.set(voiceId, 0.5);
        }
        modalState.highlighted = voiceId;
        renderMix(elements);
        renderVoiceList(elements);
        updateModalMeta(elements);
        updateApplyState(elements);
      });
    }
    if (elements.randomButton) {
      elements.randomButton.addEventListener("click", () => {
        const genderInput = modalState.speakerItem?.querySelector('[data-role="gender-input"]');
        const gender = genderInput?.value || "unknown";
        const mix = buildRandomMix(gender);
        if (mix) {
          modalState.mix = mix;
          modalState.highlighted = Array.from(mix.keys())[0];
          refreshModal(elements);
        }
      });
    }
    if (elements.clearButton) {
      elements.clearButton.addEventListener("click", () => {
        modalState.mix.clear();
        modalState.highlighted = "";
        refreshModal(elements);
      });
    }
    if (elements.applyButton) {
      elements.applyButton.addEventListener("click", (event) => {
        event.preventDefault();
        if (!modalState.speakerItem) return;
        const formula = getMixFormula();
        if (!formula) return;
        applyFormulaToSpeaker(modalState.speakerItem, formula);
        closeVoiceBrowser();
      });
    }
    voiceModal.addEventListener("click", (event) => {
      if (event.target.closest('[data-role="voice-modal-close"]')) {
        event.preventDefault();
        closeVoiceBrowser();
      }
    });
    if (elements.samplesContainer) {
      elements.samplesContainer.addEventListener("click", (event) => {
        const sample = event.target.closest(".voice-browser__sample");
        if (!sample) return;
        elements.samplesContainer
          .querySelectorAll(".voice-browser__sample")
          .forEach((node) => node.removeAttribute("data-active"));
        sample.dataset.active = "true";
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !voiceModal.hidden) {
        closeVoiceBrowser();
      }
    });

    renderVoiceList(elements);
  }

  form.addEventListener("click", (event) => {
    const genderMenu = event.target.closest('[data-role="gender-menu"]');
    const genderPill = event.target.closest('[data-role="gender-pill"]');
    if (!genderMenu && !genderPill) {
      hideGenderMenus();
    }

    if (genderPill) {
      event.preventDefault();
      const menu = genderPill.parentElement?.querySelector('[data-role="gender-menu"]');
      const isOpen = menu && !menu.hidden;
      hideGenderMenus();
      if (menu && !isOpen) {
        menu.hidden = false;
        menu.setAttribute("aria-hidden", "false");
        genderPill.classList.add("is-open");
      }
      return;
    }

    const genderOption = event.target.closest('[data-role="gender-option"]');
    if (genderOption) {
      event.preventDefault();
      const container = genderOption.closest('[data-role="speaker-gender"]');
      setGenderForSpeaker(container, genderOption.dataset.value);
      hideGenderMenus();
      return;
    }

    const clearMixButton = event.target.closest('[data-role="clear-mix"]');
    if (clearMixButton) {
      event.preventDefault();
      const container = clearMixButton.closest(".speaker-list__item");
      applyFormulaToSpeaker(container, "");
      return;
    }

    const generateButton = event.target.closest('[data-role="generate-voice"]');
    if (generateButton) {
      event.preventDefault();
      const container = generateButton.closest(".speaker-list__item");
      if (!container) return;
      const genderInput = container.querySelector('[data-role="gender-input"]');
      const genderValue = genderInput?.value || "unknown";
      const mix = buildRandomMix(genderValue);
      if (!mix) {
        console.warn("No voices available to generate a mix for", genderValue);
        return;
      }
      const formula = formatMix(normaliseMix(mix));
      applyFormulaToSpeaker(container, formula);
      return;
    }

    const modalTrigger = event.target.closest('[data-role="open-voice-browser"]');
    if (modalTrigger) {
      event.preventDefault();
      const container = modalTrigger.closest(".speaker-list__item");
      if (!container) return;
      const sampleIndex = Number.parseInt(modalTrigger.dataset.sampleIndex || "0", 10);
      openVoiceBrowser(container, Number.isNaN(sampleIndex) ? 0 : sampleIndex);
      return;
    }

    const chip = event.target.closest('[data-role="recommended-voice"]');
    if (!chip) return;
    event.preventDefault();
    const container = chip.closest(".speaker-list__item");
    if (!container) return;
    const select = container.querySelector('[data-role="speaker-voice"]');
    if (!select) return;
    const randomToggle = container.querySelector('[data-role="randomize-toggle"]');
    if (randomToggle && randomToggle.checked) {
      randomToggle.checked = false;
      handleRandomizeToggle(randomToggle);
    }
    select.disabled = false;
    select.dataset.suppressRandomize = "1";
    select.value = chip.dataset.voice || "";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    delete select.dataset.suppressRandomize;
    select.dataset.prevManual = select.value || "";
  });

  document.addEventListener("click", (event) => {
    if (!form.contains(event.target)) {
      hideGenderMenus();
    }
  });
});
