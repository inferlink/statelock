// SPDX-License-Identifier: Apache-2.0
// Whether the replayed element (`this`) is still what a click at (x, y) hits.
function (x, y) {
  const hit = document.elementFromPoint(x, y);
  return !!hit && (hit === this || this.contains(hit));
}
