// SPDX-License-Identifier: Apache-2.0
// Statelock.fetch (proxy/requests.py): one HTTP request made with the page's fetch() in
// Statelock's isolated world of the page, where the site's own fetch() is untouched. The
// response body is read up to maxBytes and returned base64-encoded.
async ({ url, method, headers, body, maxBytes, timeoutMs, redirect }) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const init = { method, headers, credentials: 'include', redirect, signal: controller.signal };
    if (body !== null) init.body = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
    const response = await fetch(url, init);
    const reader = response.body ? response.body.getReader() : null;
    const chunks = [];
    let size = 0;
    while (reader) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > maxBytes) {
        controller.abort();
        return { error: 'response_too_large', size };
      }
      chunks.push(value);
    }
    let binary = '';
    for (const chunk of chunks) {
      for (let i = 0; i < chunk.length; i += 32768) binary += String.fromCharCode(...chunk.subarray(i, i + 32768));
    }
    return {
      status: response.status,
      statusText: response.statusText,
      url: response.url,
      redirected: response.redirected,
      headers: [...response.headers.entries()],
      body: btoa(binary),
    };
  } catch (error) {
    return { error: 'fetch_failed', message: String((error && error.message) || error) };
  } finally {
    clearTimeout(timer);
  }
}
