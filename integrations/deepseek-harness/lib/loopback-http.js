/** Direct authenticated loopback transport; never consult proxy dispatchers.
 * Copyright (c) yangshaopeng.
 */
import http from 'node:http';

const LIMIT = 128 * 1024 * 1024;
const wireError = code => Object.assign(new Error('Direct loopback request failed'), { code });

export function loopbackFetch(input, options = {}) {
  return new Promise((resolve, reject) => {
    let url;
    try { url = new URL(input); } catch { reject(wireError('INVALID_LOOPBACK_URL')); return; }
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password || url.hash || !['GET', 'POST'].includes(options.method ?? 'GET')) { reject(wireError('INVALID_LOOPBACK_URL')); return; }
    if (options.signal?.aborted) { reject(wireError('ABORT_ERR')); return; }
    const body = options.body === undefined ? undefined : Buffer.from(options.body);
    if (body && body.length > 32 * 1024 * 1024) { reject(wireError('REQUEST_BUDGET_EXCEEDED')); return; }
    const limit = options.maxResponseBytes ?? LIMIT;
    if (!Number.isSafeInteger(limit) || limit < 0 || limit > LIMIT) { reject(wireError('INVALID_RESPONSE_BUDGET')); return; }
    let settled = false;
    let request;
    const finish = (error, response) => {
      if (settled) return; settled = true;
      options.signal?.removeEventListener('abort', abort);
      if (error) reject(error); else resolve(response);
    };
    const abort = () => { request?.destroy(wireError('ABORT_ERR')); finish(wireError('ABORT_ERR')); };
    try {
      // agent:false avoids a replaced global dispatcher/agent as well as all
      // HTTP_PROXY / HTTPS_PROXY / NODE_USE_ENV_PROXY handling. No DNS lookup.
      request = http.request({
        protocol: 'http:', hostname: '127.0.0.1', port: Number(url.port), path: url.pathname + url.search,
        method: options.method ?? 'GET', headers: options.headers ?? {}, agent: false,
      }, incoming => {
        const length = incoming.headers['content-length'];
        if ((incoming.statusCode >= 300 && incoming.statusCode < 400) || length !== undefined && (!/^\d+$/.test(length) || Number(length) > limit)) {
          const error = wireError(incoming.statusCode >= 300 && incoming.statusCode < 400 ? 'REDIRECT_REFUSED' : 'RESPONSE_BUDGET_EXCEEDED');
          finish(error); incoming.destroy(); request.destroy(); return;
        }
        const chunks = []; let total = 0;
        incoming.on('data', chunk => {
          total += chunk.length;
          if (total > limit) { finish(wireError('RESPONSE_BUDGET_EXCEEDED')); incoming.destroy(); request.destroy(); return; }
          chunks.push(chunk);
        });
        incoming.once('error', () => finish(wireError('RESPONSE_INCOMPLETE')));
        incoming.once('aborted', () => finish(wireError('RESPONSE_INCOMPLETE')));
        incoming.once('end', () => {
          if (!incoming.complete) { finish(wireError('RESPONSE_INCOMPLETE')); return; }
          const bytes = Buffer.concat(chunks, total);
          const headers = new Headers();
          for (const [key, value] of Object.entries(incoming.headers)) {
            if (Array.isArray(value)) for (const item of value) headers.append(key, item);
            else if (value !== undefined) headers.set(key, value);
          }
          finish(null, {
            ok: incoming.statusCode >= 200 && incoming.statusCode < 300, status: incoming.statusCode, headers,
            async json() { return JSON.parse(bytes.toString('utf8')); },
            async arrayBuffer() { return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); },
            async text() { return bytes.toString('utf8'); },
          });
        });
      });
      request.once('error', () => finish(wireError(options.signal?.aborted ? 'ABORT_ERR' : 'LOOPBACK_TRANSPORT_ERROR')));
      options.signal?.addEventListener('abort', abort, { once: true });
      if (options.signal?.aborted) { abort(); return; }
      request.end(body);
    } catch { request?.destroy(); finish(wireError('LOOPBACK_TRANSPORT_ERROR')); }
  });
}

export default loopbackFetch;
