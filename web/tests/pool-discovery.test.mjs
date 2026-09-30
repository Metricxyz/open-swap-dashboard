import assert from "node:assert/strict";
import test from "node:test";

import { bindAutomaticPoolDiscovery } from "../pool-discovery.js";


test("a complete pool address triggers discovery immediately", () => {
  const address = new EventTarget();
  const chain = new EventTarget();
  address.value = "";
  let resets = 0;
  let discoveries = 0;
  bindAutomaticPoolDiscovery(address, chain, () => resets++, () => discoveries++);

  address.value = "0xA6929e903c42a79394F09365f59e916Cb0aCcfd9";
  address.dispatchEvent(new Event("input"));

  assert.equal(resets, 1);
  assert.equal(discoveries, 1);
});


test("incomplete addresses reset the form without discovery", () => {
  const address = new EventTarget();
  const chain = new EventTarget();
  address.value = "0xA692";
  let discoveries = 0;
  bindAutomaticPoolDiscovery(address, chain, () => {}, () => discoveries++);

  address.dispatchEvent(new Event("input"));
  assert.equal(discoveries, 0);

  address.value = "  0xA6929e903c42a79394F09365f59e916Cb0aCcfd9  ";
  chain.dispatchEvent(new Event("change"));
  assert.equal(discoveries, 1);
});
