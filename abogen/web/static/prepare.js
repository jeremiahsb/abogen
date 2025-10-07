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
});
