document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector(".prepare-form");
  if (!form) return;

  const chapterRows = Array.from(form.querySelectorAll("[data-role=chapter-row]"));

  const updateRowState = (row) => {
    const enabled = row.querySelector('[data-role=chapter-enabled]');
    const inputs = Array.from(row.querySelectorAll("input[type=text], select, textarea"));
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

  const analyzeButton = form.querySelector('[data-role="analyze-button"]');
  const speakerModeSelect = form.querySelector("#speaker_mode");
  const updateAnalyzeVisibility = () => {
    if (!analyzeButton || !speakerModeSelect) return;
    const isMulti = speakerModeSelect.value === "multi";
    analyzeButton.hidden = !isMulti;
    analyzeButton.setAttribute("aria-hidden", isMulti ? "false" : "true");
    analyzeButton.disabled = !isMulti;
  };

  if (analyzeButton && speakerModeSelect) {
    speakerModeSelect.addEventListener("change", updateAnalyzeVisibility);
    updateAnalyzeVisibility();
  }

  const updatePreviewVoice = (select) => {
    const container = select.closest(".speaker-list__item");
    if (!container) return;
    const previewButton = container.querySelector('[data-role="speaker-preview"]');
    if (!previewButton) return;
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
      updatePreviewVoice(target);
    });
    updatePreviewVoice(select);
  });

  const randomizeToggles = Array.from(form.querySelectorAll('[data-role="randomize-toggle"]'));
  randomizeToggles.forEach((checkbox) => {
    handleRandomizeToggle(checkbox);
    checkbox.addEventListener("change", () => handleRandomizeToggle(checkbox));
  });

  form.addEventListener("click", (event) => {
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
    select.value = chip.dataset.voice || "";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
});
