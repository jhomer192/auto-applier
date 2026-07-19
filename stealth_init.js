// Minimal, surgical anti-detection init script for the auto-applier browser.
// DELIBERATELY does NOT override navigator.webdriver, navigator.plugins, the
// prototype chain, or anything React reads during hydration — that class of
// override froze Greenhouse react-select hydration on 2026-06-21 and got the old
// stealth layer deleted. Headed-via-xvfb already fixes webdriver/plugins/chrome
// for free; this only patches the two datacenter tells that headed can't:
//   1. WebGL UNMASKED_RENDERER = "SwiftShader" (no GPU on the VPS)
//   2. navigator.deviceMemory = undefined (real Chrome reports 8)
(() => {
  // 1. WebGL renderer/vendor → a common real Intel integrated GPU.
  const FAKE_VENDOR = "Google Inc. (Intel)";
  const FAKE_RENDERER =
    "ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00005917) Direct3D11 vs_5_0 ps_5_0, D3D11)";
  for (const proto of [
    self.WebGLRenderingContext && WebGLRenderingContext.prototype,
    self.WebGL2RenderingContext && WebGL2RenderingContext.prototype,
  ]) {
    if (!proto) continue;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      // 37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL
      if (p === 37445) return FAKE_VENDOR;
      if (p === 37446) return FAKE_RENDERER;
      return orig.call(this, p);
    };
  }

  // 2. deviceMemory → 8 (only if the engine exposes the property at all).
  try {
    if (navigator.deviceMemory === undefined) {
      Object.defineProperty(navigator, "deviceMemory", { get: () => 8, configurable: true });
    }
  } catch {}
})();
