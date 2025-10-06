const initDashboard = () => {
  const profileSelect = document.querySelector('[data-role="voice-profile"]');
  const voiceField = document.querySelector('[data-role="voice-field"]');
  const voiceSelect = document.querySelector('[data-role="voice-select"]');
  const formulaField = document.querySelector('[data-role="formula-field"]');
  const formulaInput = document.querySelector('[data-role="voice-formula"]');

  const sourceText = document.querySelector('[data-role="source-text"]');
  const previewEl = document.querySelector('[data-role="text-preview"]');
  const previewBody = document.querySelector('[data-role="preview-body"]');
  const charCountEl = document.querySelector('[data-role="char-count"]');
  const wordCountEl = document.querySelector('[data-role="word-count"]');

  const updateVoiceControls = () => {
    if (!profileSelect) {
      return;
    }
    const value = profileSelect.value;
    const showVoice = !value || value === "__standard";
    const showFormula = value === "__formula";

    if (voiceField) {
      voiceField.hidden = !showVoice;
      voiceField.setAttribute("aria-hidden", showVoice ? "false" : "true");
    }
    if (voiceSelect) {
      voiceSelect.disabled = !showVoice;
    }

    if (formulaField) {
      formulaField.hidden = !showFormula;
      formulaField.setAttribute("aria-hidden", showFormula ? "false" : "true");
    }
    if (formulaInput) {
      formulaInput.disabled = !showFormula;
      if (!showFormula) {
        formulaInput.value = formulaInput.value.trim();
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
    profileSelect.addEventListener("change", updateVoiceControls);
    updateVoiceControls();
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
