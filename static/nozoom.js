// Kein Zoomen per Fingergeste (Pinch/Doppeltipp). iOS Safari ignoriert
// user-scalable=no im Viewport-Meta, deshalb zusätzlich touch-action + gesturestart.
(function () {
  document.documentElement.style.touchAction = 'pan-x pan-y';
  ['gesturestart', 'gesturechange'].forEach(function (t) {
    document.addEventListener(t, function (e) { e.preventDefault(); }, { passive: false });
  });
  document.addEventListener('touchmove', function (e) {
    if (e.touches.length > 1) e.preventDefault();
  }, { passive: false });
})();
