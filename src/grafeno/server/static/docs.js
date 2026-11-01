const specUrl = document.getElementById("swagger-ui").dataset.specUrl;
window.ui = SwaggerUIBundle({
  url: specUrl,
  dom_id: "#swagger-ui",
  deepLinking: true,
  displayOperationId: true,
  displayRequestDuration: true,
  docExpansion: "none",
  filter: true,
  presets: [SwaggerUIBundle.presets.apis],
  layout: "BaseLayout"
});
