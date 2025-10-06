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

  const updateVoiceControls = () => {
    if (!profileSelect) {
      return;
    }
    const value = profileSelect.value;
    const isStandard = !value || value === "__standard";
    const isFormula = value === "__formula";
    const isSavedProfile = Boolean(value && !isStandard && !isFormula);

    if (voiceField) {
      voiceField.hidden = false;
      voiceField.setAttribute("aria-hidden", "false");
    }
    if (voiceSelect) {
      voiceSelect.disabled = !isStandard;
      voiceSelect.dataset.state = isStandard ? "editable" : "locked";
    }

    let showFormula = isFormula || isSavedProfile;
    let presetFormula = "";
    if (isSavedProfile) {
      const option = profileSelect.selectedOptions[0];
      if (option) {
        presetFormula = option.dataset.formula || "";
        const profileLang = option.dataset.language || "";
        if (profileLang && languageSelect) {
          languageSelect.value = profileLang;
        }
      }
    }

    if (formulaField) {
      formulaField.hidden = !showFormula;
      formulaField.setAttribute("aria-hidden", showFormula ? "false" : "true");
    }
    if (formulaInput) {
      formulaInput.disabled = !showFormula;
      if (showFormula) {
        if (presetFormula) {
          formulaInput.value = presetFormula;
        }
      } else {
        formulaInput.value = formulaInput.value.trim();
      }
      formulaInput.dataset.state = isSavedProfile ? "locked" : "editable";
      formulaInput.readOnly = isSavedProfile;
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
    profileSelect.addEventListener("change", updateVoiceControls);
    updateVoiceControls();
  }

  hydrateDefaultVoice();

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
