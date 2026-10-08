// SPDX-License-Identifier: Apache-2.0
// The element an action targets, described in Statelock's state world of the element's
// own frame. Called with Runtime.callFunctionOn on the element (`this`, or a text node in
// it) as (source, unresolved): source is "pointer" or "focus"; unresolved marks a frame
// owner whose content Statelock cannot read (an out-of-process or cross-origin frame).
function (source, unresolved) {
  let element = this && this.nodeType === 3 ? this.parentElement : this;
  if (!element || element.nodeType !== 1) return null;
  if (!unresolved) {
    // The nearest interactive ancestor, also across shadow-root boundaries (a label
    // inside a custom element's shadow root activates the link or button around it).
    const interactive = 'button, a, input, summary, [role="button"], [role="link"], [role="menuitem"]';
    for (let node = element; node; ) {
      if (node.nodeType === 1 && node.matches(interactive)) {
        element = node;
        break;
      }
      const parent = node.parentNode;
      node = parent && parent.nodeType === 11 && parent.host ? parent.host : node.parentElement;
    }
  }
  const rect = element.getBoundingClientRect();
  const isButtonInput = element.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(element.type);
  const label = element.innerText || element.textContent || (isButtonInput ? element.value : '') || element.getAttribute('alt') || '';
  const described = {
    source,
    tag_name: element.tagName,
    id: element.id || null,
    class_name: typeof element.className === 'string' ? element.className || null : null,
    text: String(label).trim().slice(0, 500),
    aria_label: element.getAttribute('aria-label'),
    role: element.getAttribute('role'),
    href: element.getAttribute('href'),
    input_type: element.tagName === 'INPUT' ? String(element.type || 'text').toLowerCase() : null,
    autocomplete: element.getAttribute('autocomplete'),
    x: rect.x,
    y: rect.y,
    width: rect.width,
    height: rect.height,
  };
  if (unresolved) {
    described.unresolved = true;
    described.frame_url = element.getAttribute('src') ? element.src || null : null;
  }
  return described;
}
