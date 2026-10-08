// SPDX-License-Identifier: Apache-2.0
// Page state captured before and after each governed action, in Statelock's isolated world.
// Called as (fieldSpecs): the policy fields read by CSS selector,
// [{name, selector, attribute, frame, visible, read, url_contains}] (statelock/policy/fields.py).
// The element an action targets is resolved over CDP (inspector.py, describe_element.js).
(fieldSpecs) => {
  const MAX_TEXT = 1000000;
  const MAX_DEPTH = __STATELOCK_MAX_FRAME_DEPTH__; // nested frames and shadow roots read (proxy/js MAX_FRAME_DEPTH)
  const text = (value) => (value === null || value === undefined ? '' : String(value));
  const isPassword = (element) => element.tagName === 'INPUT' && String(element.type).toLowerCase() === 'password';
  // A form control's value (<input>, <select>, <progress>, <meter>, <li value>...). Numeric
  // values count only when the markup sets them (an <li> without value= reads 0).
  const ownValue = (element) => {
    const value = element.value;
    if (typeof value === 'string') return value || null;
    if (typeof value === 'number' && Number.isFinite(value) && element.hasAttribute('value')) return String(value);
    return null;
  };
  // data-statelock-value is the page's own value for a data-statelock-key field only; a
  // policy's selector field reads what the page shows.
  const rawValue = (element, attribute, marked) => {
    if (attribute) return text(element.getAttribute(attribute));
    const markup = marked ? element.getAttribute('data-statelock-value') : null;
    return text(markup || ownValue(element) || element.innerText || element.textContent).trim();
  };

  // Shown: rendered (checkVisibility: display, visibility, opacity 0, content-visibility,
  // here or in an ancestor), with a box larger than a pixel that is not left of or above
  // the page. Below the fold counts as shown. In a frame, the frame must be shown too.
  const boxShown = (element) => {
    if (!element.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true })) {
      return false;
    }
    const view = element.ownerDocument.defaultView;
    const left = view ? view.scrollX : 0;
    const top = view ? view.scrollY : 0;
    return Array.from(element.getClientRects()).some(
      (rect) => rect.width > 1 && rect.height > 1 && rect.right + left > 0 && rect.bottom + top > 0,
    );
  };
  const shown = (element) => {
    for (let node = element, depth = 0; node && depth < MAX_DEPTH; depth += 1) {
      if (!boxShown(node)) return false;
      if (node.ownerDocument === document) return true;
      node = node.ownerDocument.defaultView ? node.ownerDocument.defaultView.frameElement : null;
    }
    return false;
  };

  // Rendered text of the page, its open shadow roots and its same-origin frames. A shown
  // frame Statelock cannot read from here (cross-origin) is listed in page_text_unread.
  const parts = [];
  const unread = [];
  let length = 0;
  const add = (value) => {
    const part = text(value).trim();
    if (part) {
      parts.push(part);
      length += part.length + 1;
    }
  };
  const visit = (root, depth) => {
    if (length > MAX_TEXT || depth > MAX_DEPTH) return;
    if (root.nodeType === 9) {
      add(root.body ? root.body.innerText : root.documentElement ? root.documentElement.innerText : '');
    } else if (root.host.checkVisibility()) {
      // innerText of an element that is not rendered is its textContent: skip those.
      for (const child of root.childNodes) {
        if (child.nodeType === 1 && child.checkVisibility()) add(child.innerText);
        else if (child.nodeType === 3) add(child.textContent);
      }
    }
    for (const element of root.querySelectorAll('*')) {
      if (length > MAX_TEXT) return;
      if (element.shadowRoot) visit(element.shadowRoot, depth + 1);
      if (element.tagName !== 'IFRAME' && element.tagName !== 'FRAME') continue;
      let frameDocument = null;
      try {
        frameDocument = element.contentDocument;
      } catch (error) {}
      if (frameDocument) {
        if (element.checkVisibility()) visit(frameDocument, depth + 1);
      } else if (boxShown(element)) {
        unread.push(element.src || element.tagName.toLowerCase());
      }
    }
  };
  visit(document, 0);
  const pageText = parts.join('\n');

  const fields = {};
  for (const element of document.querySelectorAll('[data-statelock-key]')) {
    try {
      const key = element.getAttribute('data-statelock-key');
      if (key && !isPassword(element)) fields[key] = rawValue(element, null, true);
    } catch (error) {}
  }

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
    // A field that cannot be read is left out, so the rules that use it block.
    try {
      if (spec.url_contains && !address.includes(spec.url_contains)) continue;
      const scope = root(spec.frame);
      let elements = scope ? Array.from(scope.querySelectorAll(spec.selector)) : [];
      if (spec.visible) elements = elements.filter(shown);
      if (spec.read === 'count') {
        fieldValues[spec.name] = String(elements.length);
        continue;
      }
      const element = elements[0];
      if (!element) continue;
      // A password field's length only, never its value.
      if (spec.read === 'length') fieldValues[spec.name] = String(rawValue(element, spec.attribute, false).length);
      else fieldValues[spec.name] = isPassword(element) ? '' : rawValue(element, spec.attribute, false);
    } catch (error) {}
  }

  return {
    url: window.location.href,
    title: document.title,
    page_text: pageText.slice(0, MAX_TEXT),
    page_text_truncated: pageText.length > MAX_TEXT,
    page_text_unread: unread,
    extracted_fields: fields,
    field_values: fieldValues,
    scroll: { x: window.scrollX, y: window.scrollY },
  };
}
