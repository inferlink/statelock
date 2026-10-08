// SPDX-License-Identifier: Apache-2.0
// Statelock page guard. Runs in an isolated world, invisible to page scripts.
// Placeholders replaced at install: __STATELOCK_PATTERNS__, __STATELOCK_BINDING__, __STATELOCK_EVENTS__,
// __STATELOCK_REPLAY_ELEMENT__; and by load_js, the shared frame depth limit (MAX_FRAME_DEPTH).
(() => {
  if (globalThis.__statelockGuardInstalled) return;
  globalThis.__statelockGuardInstalled = true;
  const patterns = __STATELOCK_PATTERNS__;
  // Absolute scopes are parsed by Python (core/urls.py); path-only scopes remain substrings.
  const matchesScope = (scope, where) => {
    const address = where.host
      ? `${where.protocol}//${where.host}${where.pathname}`
      : `${where.protocol}${where.pathname}`;
    if ('substring' in scope) return address.includes(scope.substring);
    if (!['http:', 'https:'].includes(where.protocol) || where.origin !== scope.origin) return false;
    if (!scope.path) return true;
    let index = where.pathname.indexOf(scope.path);
    while (index !== -1) {
      const end = index + scope.path.length;
      if (end === where.pathname.length || where.pathname[end] === '/') return true;
      index = where.pathname.indexOf(scope.path, index + 1);
    }
    return false;
  };
  // An ancestor frame Statelock can only see the origin of (cross-origin): governed when
  // the origin may match (a path-only scope, or an absolute scope on that origin).
  const originMayMatch = (origin) => patterns.some((scope) => 'substring' in scope || scope.origin === origin);
  // A frame is governed like the page it is in: about:srcdoc and about:blank frames, and
  // frames of other origins, inside a governed page are governed too.
  const governed = () => {
    if (patterns === null) return true;
    const origins = location.ancestorOrigins;
    let frame = window;
    for (let depth = 0; depth < __STATELOCK_MAX_FRAME_DEPTH__; depth += 1) {
      let where = null;
      try {
        where = frame.location;
        void where.href; // throws for a cross-origin frame
      } catch (error) {
        where = null;
      }
      if (where !== null) {
        if (patterns.some((scope) => matchesScope(scope, where))) return true;
      } else {
        // depth >= 1 here: ancestorOrigins[0] is the parent's origin.
        const origin = origins && depth >= 1 ? origins[depth - 1] : undefined;
        if (origin === undefined || originMayMatch(origin)) return true; // unknown: fail closed
      }
      if (frame === window.top) return false;
      frame = frame.parent;
    }
    return true;
  };
  const report = (payload) => {
    try {
      globalThis.__STATELOCK_BINDING__(JSON.stringify(payload));
    } catch (error) {}
  };
  const describe = (node) => {
    if (!node || node.nodeType !== 1) return {};
    return {
      tag_name: node.tagName,
      id: node.id || null,
      text: (node.innerText || node.textContent || '').trim().slice(0, 200),
      aria_label: node.getAttribute('aria-label'),
    };
  };
  const cancel = (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  // A synthetic plain left click in the top frame (element.click() from agent
  // code) may be replayed by Statelock as governed mouse input. Its element is
  // kept under an id for Statelock to fetch (it decides, scrolls and clicks);
  // nothing here touches the page. Otherwise null.
  const MAX_REPLAY_ELEMENTS = 32;
  const replayElements = new Map();
  let nextReplayId = 1;
  const replayId = (event) => {
    const el = event.target;
    if (event.type !== 'click' || window !== window.top || !el || el.nodeType !== 1 || !el.isConnected) return null;
    if (event.button !== 0 || event.ctrlKey || event.shiftKey || event.altKey || event.metaKey) return null;
    const id = nextReplayId++;
    replayElements.set(id, el);
    if (replayElements.size > MAX_REPLAY_ELEMENTS) replayElements.delete(replayElements.keys().next().value);
    return id;
  };
  globalThis.__STATELOCK_REPLAY_ELEMENT__ = (id) => {
    const el = replayElements.get(id) || null;
    replayElements.delete(id);
    return el;
  };

  // File inputs. Files chosen through a trusted path (the file chooser,
  // DOM.setFileInputFiles, a real drop) fire trusted input/change events; the
  // File objects are remembered here. Files set by code (input.files = ...,
  // Playwright set_input_files over a remote connection) are cleared before
  // the site can see or send them.
  const trustedFiles = new WeakMap();
  const isFileInput = (node) =>
    !!node && node.nodeType === 1 && node.tagName === 'INPUT' && String(node.type).toLowerCase() === 'file';
  const fileInputs = (root) =>
    root instanceof HTMLFormElement
      ? Array.from(root.elements).filter(isFileInput)
      : Array.from(root.querySelectorAll('input[type="file" i]'));
  const filesAreTrusted = (input) => {
    const files = Array.from(input.files || []);
    if (files.length === 0) return true;
    const known = trustedFiles.get(input);
    return !!known && known.length === files.length && files.every((file, index) => file === known[index]);
  };
  const rejectFiles = (input, via) => {
    const names = Array.from(input.files || [], (file) => file.name).slice(0, 20);
    try {
      input.value = '';
    } catch (error) {}
    trustedFiles.delete(input);
    report({ kind: 'untrusted_file_input', via, url: location.href, target: describe(input), files: names });
  };
  const rejectUntrustedFiles = (root, via) => {
    let rejected = 0;
    for (const input of fileInputs(root)) {
      if (!filesAreTrusted(input)) {
        rejectFiles(input, via);
        rejected += 1;
      }
    }
    return rejected;
  };

  // A trusted user gesture (real click, key or touch) in the current task. A submit event
  // is trusted also when code calls form.requestSubmit(); it is a real submission only
  // when it happens during such a gesture (a click on a submit button, Enter in a field).
  let gesture = false;
  const noteGesture = (event) => {
    if (!event.isTrusted || gesture) return;
    gesture = true;
    setTimeout(() => {
      gesture = false;
    }, 0);
  };
  for (const type of ['click', 'keydown', 'keypress', 'keyup', 'pointerdown', 'pointerup', 'mousedown', 'mouseup',
    'touchstart', 'touchend', 'drop']) {
    window.addEventListener(type, noteGesture, true);
  }

  const handler = (event) => {
    if (!governed()) return;
    if (event.isTrusted) {
      // Real input must not carry files that code put into a file input.
      if (rejectUntrustedFiles(document, event.type) > 0) {
        cancel(event);
        return;
      }
      if (event.type === 'submit' && gesture) report({ kind: 'trusted_submit', url: location.href });
      return;
    }
    // Synthetic event from page code: cancel before any page handler runs.
    cancel(event);
    report({
      kind: 'synthetic_event',
      event_type: event.type,
      url: location.href,
      target: describe(event.target),
      replay_id: replayId(event),
    });
  };
  for (const type of __STATELOCK_EVENTS__) {
    window.addEventListener(type, handler, true);
  }

  const fileHandler = (event) => {
    if (!governed()) return;
    const input = typeof event.composedPath === 'function' ? event.composedPath()[0] : event.target;
    if (!isFileInput(input)) return;
    if (event.isTrusted) {
      trustedFiles.set(input, Array.from(input.files || []));
      return;
    }
    if (filesAreTrusted(input)) return;
    cancel(event);
    rejectFiles(input, `event:${event.type}`);
  };
  window.addEventListener('input', fileHandler, true);
  window.addEventListener('change', fileHandler, true);

  // Form submission and new FormData(form) in site code.
  window.addEventListener(
    'formdata',
    (event) => {
      if (!governed() || !(event.target instanceof HTMLFormElement)) return;
      for (const input of fileInputs(event.target)) {
        if (filesAreTrusted(input)) continue;
        if (input.name) event.formData.delete(input.name);
        rejectFiles(input, 'formdata');
      }
    },
    true,
  );
})();
