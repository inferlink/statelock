// SPDX-License-Identifier: Apache-2.0
// Where a real click on a replayed element (`this`) lands: its center, once the layout has
// stopped moving (a scroll or a viewport resize can still be settling), when the element
// itself is there (not covered, not zero-sized); else null. Runs in the guard world.
async function () {
  const FRAME_WAIT_MS = 100; // one animation frame, or this long when frames are throttled
  const MAX_SETTLE_FRAMES = 10; // frames waited for the center to stop moving
  const frame = () =>
    new Promise((resolve) => {
      requestAnimationFrame(() => resolve());
      setTimeout(resolve, FRAME_WAIT_MS);
    });
  const center = () => {
    if (!this.isConnected) return null;
    const rect = this.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  };
  let point = center();
  for (let i = 0; point && i < MAX_SETTLE_FRAMES; i++) {
    await frame();
    const next = center();
    if (next && next.x === point.x && next.y === point.y) break;
    point = next;
  }
  if (!point) return null;
  const hit = document.elementFromPoint(point.x, point.y);
  return hit && (hit === this || this.contains(hit)) ? point : null;
}
