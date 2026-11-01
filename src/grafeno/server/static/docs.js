function normalizeSearchText(value) {
  return String(value || "").toLocaleLowerCase();
}

function operationMatchesSearch(op, search) {
  const operation = op.get("operation");
  const fields = [
    op.get("path"),
    operation && operation.get("operationId"),
    operation && operation.get("summary")
  ];
  return fields.some((field) => normalizeSearchText(field).includes(search));
}

function filterOperations(taggedOps, phrase) {
  const search = normalizeSearchText(phrase);
  if (!search) {
    return taggedOps;
  }
  return taggedOps.map((tagObj, tag) => {
    if (normalizeSearchText(tag).includes(search)) {
      return tagObj;
    }
    const operations = tagObj.get("operations").filter(
      (op) => operationMatchesSearch(op, search)
    );
    return tagObj.set("operations", operations);
  }).filter((tagObj, tag) => (
    normalizeSearchText(tag).includes(search) || tagObj.get("operations").size > 0
  ));
}

const specUrl = document.getElementById("swagger-ui").dataset.specUrl;
window.ui = SwaggerUIBundle({
  url: specUrl,
  dom_id: "#swagger-ui",
  deepLinking: true,
  displayOperationId: true,
  displayRequestDuration: true,
  docExpansion: "none",
  filter: true,
  fn: { opsFilter: filterOperations },
  presets: [SwaggerUIBundle.presets.apis],
  layout: "BaseLayout"
});
