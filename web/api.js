let csrfToken = "";

export function setCsrfToken(value) {
  csrfToken = value || "";
}

export async function api(path, options = {}) {
  const { etag, onEtag, ...requestOptions } = options;
  options = requestOptions;
  const method = (options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) headers["X-CSRF-Token"] = csrfToken;
  if (etag) headers['If-None-Match'] = etag;
  const response = await fetch(path, { ...options, method, headers });
  if (response.status === 304) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    // FastAPI validation errors carry a list of {loc, msg} entries instead of a string.
    const detail = Array.isArray(payload?.detail)
      ? payload.detail.map((item) => `${item.loc?.slice(1).join(".") || "request"}: ${item.msg}`).join("; ")
      : payload?.detail;
    const error = new Error(detail || payload?.error || payload || `Request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  onEtag?.(response.headers.get('etag'));
  return payload;
}
