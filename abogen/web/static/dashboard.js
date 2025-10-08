const initDashboard = () => {
  const uploadModal = document.querySelector('[data-role="upload-modal"]');
  const openModalButtons = document.querySelectorAll('[data-role="open-upload-modal"]');
  const scope = uploadModal || document;

  const profileSelect = scope.querySelector('[data-role="voice-profile"]');
  const voiceField = scope.querySelector('[data-role="voice-field"]');
  const voiceSelect = scope.querySelector('[data-role="voice-select"]');
  const formulaField = scope.querySelector('[data-role="formula-field"]');
  const formulaInput = scope.querySelector('[data-role="voice-formula"]');
  const languageSelect = uploadModal?.querySelector("#language") || document.getElementById("language");

  const sourceText = scope.querySelector('[data-role="source-text"]');
  const previewEl = scope.querySelector('[data-role="text-preview"]');
  const previewBody = scope.querySelector('[data-role="preview-body"]');
  const charCountEl = scope.querySelector('[data-role="char-count"]');
  const wordCountEl = scope.querySelector('[data-role="word-count"]');

  let lastTrigger = null;

  const openUploadModal = (trigger) => {
    if (!uploadModal) return;
    lastTrigger = trigger || null;
    uploadModal.hidden = false;
    uploadModal.dataset.open = "true";
    document.body.classList.add("modal-open");
    const focusTarget = uploadModal.querySelector("#source_file") || uploadModal.querySelector("#source_text") || uploadModal;
    if (focusTarget instanceof HTMLElement) {
      focusTarget.focus({ preventScroll: true });
    }
  };

  const closeUploadModal = () => {
    if (!uploadModal || uploadModal.hidden) {
      return;
    }
    uploadModal.hidden = true;
    delete uploadModal.dataset.open;
    document.body.classList.remove("modal-open");
    if (lastTrigger && lastTrigger instanceof HTMLElement) {
      lastTrigger.focus({ preventScroll: true });
    }
  };

  openModalButtons.forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      openUploadModal(button);
    });
  });

  if (uploadModal) {
    uploadModal.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof Element && target.closest('[data-role="upload-modal-close"]')) {
        event.preventDefault();
        closeUploadModal();
      }
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && uploadModal && !uploadModal.hidden) {
      closeUploadModal();
    }
  });

  const hydrateDefaultVoice = () => {
    if (!voiceSelect) return;
    const defaultVoice = voiceSelect.dataset.default;
    if (!defaultVoice) return;
    const option = voiceSelect.querySelector(`option[value="${defaultVoice}"]`);
    if (option) {
      voiceSelect.value = defaultVoice;
    }
  };

  const selectFirstProfileIfAvailable = () => {
    if (!profileSelect) return false;
    const saved = Array.from(profileSelect.options).filter(
      (option) => option.value && option.value !== "__standard" && option.value !== "__formula",
    );
    if (!saved.length) {
      profileSelect.value = "__standard";
      return false;
    }
    saved[0].selected = true;
    return true;
  };

  const applySavedProfile = (option) => {
    if (!option) return;
    const presetFormula = option.dataset.formula || "";
    const profileLang = option.dataset.language || "";
    if (formulaInput) {
      formulaInput.value = presetFormula;
      formulaInput.readOnly = true;
      formulaInput.dataset.state = "locked";
    }
    if (profileLang && languageSelect) {
      languageSelect.value = profileLang;
    }
  };

  const updateVoiceControls = () => {
    if (!profileSelect) {
      return;
    }
    const value = profileSelect.value || "__standard";
    const isStandard = value === "__standard";
    const isFormula = value === "__formula";
    const isSavedProfile = !isStandard && !isFormula;

    const showVoiceField = isStandard;
    if (voiceField) {
      voiceField.hidden = !showVoiceField;
      voiceField.setAttribute("aria-hidden", showVoiceField ? "false" : "true");
      voiceField.dataset.state = showVoiceField ? "visible" : "hidden";
    }
    if (voiceSelect) {
      voiceSelect.disabled = !isStandard;
      voiceSelect.dataset.state = isStandard ? "editable" : "locked";
      if (isStandard) {
        hydrateDefaultVoice();
      }
    }

    if (isSavedProfile) {
      applySavedProfile(profileSelect.selectedOptions[0] || null);
    } else if (!isFormula && formulaInput) {
      formulaInput.value = "";
    }

    const showFormulaField = isFormula;
    if (formulaField) {
      const shouldShow = showFormulaField;
      formulaField.hidden = !shouldShow;
      formulaField.setAttribute("aria-hidden", shouldShow ? "false" : "true");
      formulaField.dataset.state = shouldShow ? "visible" : "hidden";
    }
    if (formulaInput) {
      if (isFormula) {
        formulaInput.disabled = false;
        formulaInput.readOnly = false;
        formulaInput.dataset.state = "editable";
      } else if (isSavedProfile) {
        formulaInput.disabled = false;
        formulaInput.readOnly = true;
        formulaInput.dataset.state = "locked";
      } else {
        formulaInput.disabled = true;
        formulaInput.readOnly = true;
        formulaInput.value = "";
        formulaInput.dataset.state = "editable";
      }
    }
  };

  const updatePreview = () => {
    if (!sourceText || !previewBody || !charCountEl || !wordCountEl) {
      return;
    }
    const raw = sourceText.value || "";
    const trimmed = raw.trim();
    const charCount = raw.length;
    const wordCount = trimmed ? trimmed.split(/\s+/).length : 0;

    const charLabel = `${charCount.toLocaleString()} ${charCount === 1 ? "character" : "characters"}`;
    const wordLabel = `${wordCount.toLocaleString()} ${wordCount === 1 ? "word" : "words"}`;

    charCountEl.textContent = charLabel;
    wordCountEl.textContent = wordLabel;

    if (!trimmed) {
      previewBody.textContent = "Paste text to see a live preview and character count.";
      if (previewEl) {
        previewEl.setAttribute("data-state", "empty");
      }
      return;
    }

    const snippet = trimmed.length > 1200 ? `${trimmed.slice(0, 1200)}…` : trimmed;
    previewBody.textContent = snippet;
    if (previewEl) {
      previewEl.setAttribute("data-state", "ready");
    }
  };

  if (profileSelect) {
    const hasSaved = selectFirstProfileIfAvailable();
    profileSelect.addEventListener("change", updateVoiceControls);
    updateVoiceControls();
    if (!hasSaved) {
      hydrateDefaultVoice();
    }
  } else {
    hydrateDefaultVoice();
  }

  if (sourceText) {
    sourceText.addEventListener("input", updatePreview);
    updatePreview();
  }
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initDashboard, { once: true });
} else {
  initDashboard();
}
