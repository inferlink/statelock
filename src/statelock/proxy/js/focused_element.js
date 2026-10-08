// SPDX-License-Identifier: Apache-2.0
// The focused element of a document or shadow root (`this`), followed into open shadow
// roots, in Statelock's state world; null when nothing is focused (the body or the root
// element has focus). Statelock follows closed shadow roots and frames over CDP.
function () {
  let element = this.activeElement;
  while (element && element.shadowRoot && element.shadowRoot.activeElement) {
    element = element.shadowRoot.activeElement;
  }
  const document = this.nodeType === 9 ? this : null;
  if (!element || (document && (element === document.body || element === document.documentElement))) return null;
  return element;
}
