// Fetch helpers. Errors carry the server's `detail` message so views can show what went wrong.

async function failure(res) {
  const data = await res.json().catch(() => ({}));
  return new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? res.statusText));
}

export async function getJSON(url, params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ''));
  const res = await fetch(q.size ? `${url}?${q}` : url);
  if (!res.ok) throw await failure(res);
  return res.json();
}

export async function postJSON(url, body, {signal} = {}) {
  const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal});
  if (!res.ok) throw await failure(res);
  return res;
}

export async function readNdjson(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream: true});
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}
