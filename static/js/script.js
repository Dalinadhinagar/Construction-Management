document.addEventListener("DOMContentLoaded", () => {
  const menuToggle = document.querySelector("[data-menu-toggle]");
  const sidebar = document.getElementById("sidebar");
  if (menuToggle && sidebar) {
    menuToggle.addEventListener("click", () => sidebar.classList.toggle("sidebar-open"));
  }

  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll(".request-table tbody tr").forEach((row) => {
    const checkbox = row.querySelector(".item-check");
    const quantity = row.querySelector(".quantity-input");
    const materialId = row.querySelector(".material-id");
    if (!checkbox || !quantity || !materialId) return;

    const updateOffer = () => {
      const option = materialId.selectedOptions[0];
      const price = row.querySelector(".offer-price");
      const stock = row.querySelector(".offer-stock");
      if (!option || option.disabled) {
        if (price) price.textContent = "Unavailable";
        if (stock) stock.textContent = "—";
        quantity.max = "0";
        return;
      }
      const stockCount = Number(option.dataset.stock);
      if (price) price.textContent = `$${option.dataset.price} / ${option.dataset.unit}`;
      if (stock) stock.textContent = `${new Intl.NumberFormat().format(stockCount)} ${option.dataset.unit}`;
      quantity.max = String(stockCount);
      if (Number(quantity.value) > stockCount) quantity.value = String(stockCount);
    };

    updateOffer();
    materialId.addEventListener("change", updateOffer);
    checkbox.addEventListener("change", () => {
      quantity.disabled = !checkbox.checked;
      materialId.disabled = !checkbox.checked;
      if (checkbox.checked) {
        quantity.name = "quantity";
        materialId.name = "material_id";
        row.classList.add("row-selected");
      } else {
        quantity.removeAttribute("name");
        materialId.removeAttribute("name");
        row.classList.remove("row-selected");
      }
    });
  });

  document.querySelectorAll(".supplier-choice").forEach((select) => {
    const total = select.closest("form")?.querySelector("[data-assignment-total]");
    const updateTotal = () => {
      const option = select.selectedOptions[0];
      if (total && option) total.textContent = `$${option.dataset.total}`;
    };
    select.addEventListener("change", updateTotal);
    updateTotal();
  });

  document.querySelectorAll(".reassign-form select[name='supplier_id']").forEach((select) => {
    const total = select.closest("form")?.querySelector("[data-assignment-total]");
    select.addEventListener("change", () => {
      const option = select.selectedOptions[0];
      if (total && option?.dataset.total) {
        total.textContent = `New order total: $${option.dataset.total}`;
      }
    });
  });

  document.querySelectorAll(".request-layout").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!form.querySelector(".item-check:checked")) {
        event.preventDefault();
        window.alert("Select at least one material before submitting.");
      }
    });
  });
});
