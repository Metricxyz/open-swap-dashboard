const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function bindAutomaticPoolDiscovery(addressInput, chainInput, reset, discover) {
  const detect = () => {
    reset();
    if (EVM_ADDRESS.test(addressInput.value.trim())) discover();
  };
  addressInput.addEventListener("input", detect);
  chainInput.addEventListener("change", detect);
}
