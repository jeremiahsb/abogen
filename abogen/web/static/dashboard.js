const initDashboard = () => {
  const profileSelect = document.querySelector('[data-role="voice-profile"]');
  const voiceField = document.querySelector('[data-role="voice-field"]');
  const voiceSelect = document.querySelector('[data-role="voice-select"]');
  const formulaField = document.querySelector('[data-role="formula-field"]');
  const formulaInput = document.querySelector('[data-role="voice-formula"]');
  const languageSelect = document.getElementById("language");

  const sourceText = document.querySelector('[data-role="source-text"]');
  const previewEl = document.querySelector('[data-role="text-preview"]');
  const previewBody = document.querySelector('[data-role="preview-body"]');
  const charCountEl = document.querySelector('[data-role="char-count"]');
  const wordCountEl = document.querySelector('[data-role="word-count"]');

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

    if (voiceField) {
      const showVoice = isStandard;
      voiceField.hidden = !showVoice;
      voiceField.setAttribute("aria-hidden", showVoice ? "false" : "true");
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

    if (formulaField) {
      const showFormula = isFormula;
      formulaField.hidden = !showFormula;
      formulaField.setAttribute("aria-hidden", showFormula ? "false" : "true");
    }
    if (formulaInput) {
      if (isFormula) {
        formulaInput.disabled = false;
        formulaInput.readOnly = false;
        formulaInput.dataset.state = "editable";
      } else {
        formulaInput.disabled = !isSavedProfile;
        formulaInput.readOnly = true;
        formulaInput.dataset.state = isSavedProfile ? "locked" : "editable";
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
