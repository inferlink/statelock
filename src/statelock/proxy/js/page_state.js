// SPDX-License-Identifier: Apache-2.0
// Page state captured before and after each governed action, in Statelock's isolated world.
// Called as (point, useFocus, fieldSpecs) where point is {x, y} or null and
// fieldSpecs lists policy fields read by CSS selector:
// [{name, selector, attribute, frame, visible, read, url_contains}] (statelock/policy/fields.py).
(point, useFocus, fieldSpecs) => {
  const describe = (element, source) => {
    if (!element) return null;
    element = element.closest('button, a, input, summary, [role="button"], [role="link"], [role="menuitem"]') || element;
    const rect = element.getBoundingClientRect();
    return {
      source,
      tag_name: element.tagName,
      id: element.id || null,
      class_name: typeof element.className === 'string' ? element.className || null : null,
      text: (element.innerText || element.textContent || (element.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(element.type) ? element.value : '') || element.getAttribute('alt') || '').trim().slice(0, 500),
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
  };

  let target = null;
  if (point) {
    // Pointer actions: the element under the pointer.
    target = describe(document.elementFromPoint(point.x, point.y), 'pointer');
  } else if (useFocus) {
    // Keyboard actions: the focused element. body/html count as no target,
    // so page-wide text is never matched.
    const active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement) {
      target = describe(active, 'focus');
    }
  }

  const isPassword = (element) => element.tagName === 'INPUT' && String(element.type).toLowerCase() === 'password';
  const rawValue = (element, attribute) => {
    if (attribute) return element.getAttribute(attribute) || '';
    return (
      element.getAttribute('data-statelock-value') ||
      element.value ||
      element.innerText ||
      element.textContent ||
      ''
    ).trim();
  };
  const readValue = (element, attribute) => (isPassword(element) ? '' : rawValue(element, attribute));

  const fields = {};
  for (const element of document.querySelectorAll('[data-statelock-key]')) {
    const key = element.getAttribute('data-statelock-key');
    if (key && !isPassword(element)) fields[key] = rawValue(element, null);
  }

  const shown = (element) => {
    if (element.closest('[hidden]')) return false;
    const style = element.ownerDocument.defaultView.getComputedStyle(element);
    return style.visibility !== 'hidden' && element.getClientRects().length > 0;
  };
  // The document to search: the page, or a same-origin iframe in it (null when absent or cross-origin).
  const root = (frameSelector) => {
    if (!frameSelector) return document;
    try {
      const frame = document.querySelector(frameSelector);
      return (frame && frame.contentDocument) || null;
    } catch (error) {
      return null;
    }
  };
  // url_contains matches scheme://host[:port]/path (statelock/core/urls.py): never the query,
  // fragment or user info, which whoever builds the link chooses.
  const address = `${location.protocol}//${location.host}${location.pathname}`;
  const fieldValues = {};
  for (const spec of fieldSpecs || []) {
    if (spec.url_contains && !address.includes(spec.url_contains)) continue;
    const scope = root(spec.frame);
    let elements = [];
    try {
      elements = scope ? Array.from(scope.querySelectorAll(spec.selector)) : [];
    } catch (error) {}
    if (spec.visible) elements = elements.filter(shown);
    if (spec.read === 'count') {
      fieldValues[spec.name] = String(elements.length);
      continue;
    }
    const element = elements[0];
    if (!element) continue;
    // A password field's length only, never its value.
    if (spec.read === 'length') fieldValues[spec.name] = String(rawValue(element, spec.attribute).length);
    else fieldValues[spec.name] = readValue(element, spec.attribute);
  }

  return {
    url: window.location.href,
    title: document.title,
    page_text: ((document.body && document.body.innerText) || '').trim().slice(0, 1000000),
    page_text_truncated: ((document.body && document.body.innerText) || '').trim().length > 1000000,
    extracted_fields: fields,
    field_values: fieldValues,
    target_element: target,
  };
}
