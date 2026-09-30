import assert from "node:assert/strict";
import test from "node:test";

import { api, setCsrfToken } from "../api.js";


function response(payload, { ok = true, status = 200, contentType = "application/json" } = {}) {
  return {
    ok,
    status,
    headers: { get: () => contentType },
    json: async () => payload,
    text: async () => String(payload),
  };
}


test("api serializes JSON and protects every mutation", async () => {
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    return response({ ok: true });
  };
  setCsrfToken("csrf-value");

  assert.deepEqual(await api("/api/pools", { method: "post", body: { name: "Pool" } }), { ok: true });
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-value");
  assert.equal(calls[0].options.body, '{"name":"Pool"}');

  await api("/api/bootstrap");
  assert.equal(calls[1].options.method, "GET");
  assert.equal(calls[1].options.headers["X-CSRF-Token"], undefined);
});


test("api reports JSON and text failures", async () => {
  globalThis.fetch = async () => response({ detail: "invalid pool" }, { ok: false, status: 400 });
  await assert.rejects(() => api("/api/pools"), /invalid pool/);

  globalThis.fetch = async () => response("gateway unavailable", {
    ok: false,
    status: 502,
    contentType: "text/plain",
  });
  await assert.rejects(() => api("/api/pools"), /gateway unavailable/);
});


test('conditional responses avoid body parsing and retain status codes on errors', async () => {
  let options;
  globalThis.fetch = async (_path, input) => {
    options = input;
    return {status: 304, ok: false, json() { throw new Error('must not parse'); }};
  };
  assert.equal(await api('/dashboard', {etag: '"revision"'}), null);
  assert.equal(options.headers['If-None-Match'], '"revision"');
  globalThis.fetch = async () => response({detail: 'updated'}, {ok: false, status: 409});
  await assert.rejects(() => api('/swaps'), (error) => error.status === 409);
});


test("api reports FastAPI validation errors by field", async () => {
  const detail = [{ loc: ["body", "rpm_limit"], msg: "Input should be greater than or equal to 1" }];
  globalThis.fetch = async () => response({ detail }, { ok: false, status: 422 });
  await assert.rejects(() => api("/api/settings/envio"), { message: "rpm_limit: Input should be greater than or equal to 1", status: 422 });
});
