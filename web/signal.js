// Talks to the Porthole server: messages arrive over Server-Sent Events, go out as POST requests.
// The server sends a "ping" event every 15 s. If nothing at all arrives for `silenceMs` (or a POST
// says this connection is unknown), the stream is dead without the browser noticing: open a new one.
// A viewer names its connection before (`prev`) in each new stream, so Porthole can hand the device's row to the
// new connection instead of listing it twice while the old one times out; the browser's own reconnects send it as
// Last-Event-ID (the hello's event id). Porthole decides whether it counts (the same device only).

export class Signal {
  // `params`: more details for the server, added to the events address when set (e.g. { touch: 1 }).
  // `previous`: a viewer's id from before a reload, if the page kept one.
  constructor({ room, role, client, takeover, params = {}, previous = '', onMessage, onStatus, timeoutMs = 8000, silenceMs = 40000 }) {
    this.room = room;
    this.resume = role === 'viewer'; // only a viewer has a row to come back to
    this.previous = this.resume ? previous || '' : ''; // the id of the last connection, sent with the next one
    this.timeoutMs = timeoutMs;
    this.silenceMs = silenceMs;
    this.id = null;
    this.onMessage = onMessage;
    this.onStatus = onStatus || (() => {});
    this.queue = Promise.resolve();
    this.closed = false;
    const token = client ? `&client=${encodeURIComponent(client)}` : '';
    const take = takeover ? '&takeover=1' : '';
    const more = Object.entries(params)
      .filter(([, value]) => value)
      .map(([key, value]) => `&${key}=${encodeURIComponent(value)}`)
      .join('');
    this.base = `/events?room=${encodeURIComponent(room)}&role=${role}${token}${take}${more}`;
    this.open();
    if (silenceMs > 0) {
      this.watchdog = setInterval(() => {
        if (Date.now() - this.lastMessageAt >= this.silenceMs) this.reopen();
      }, 1000);
      if (this.watchdog && typeof this.watchdog.unref === 'function') this.watchdog.unref(); // Node tests
    }
  }

  // The events address, with the connection before when there is one.
  get url() {
    return this.previous ? `${this.base}&prev=${encodeURIComponent(this.previous)}` : this.base;
  }

  open() {
    this.lastMessageAt = Date.now(); // the time of the last message (or of opening)
    const source = new EventSource(this.url);
    this.source = source;
    source.onmessage = (event) => {
      if (source !== this.source) return; // a stream we already gave up on
      this.lastMessageAt = Date.now();
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!msg || msg.type === 'ping') return; // only proof of life
      if (msg.type === 'hello') {
        this.id = msg.id;
        if (this.resume && msg.id) this.previous = msg.id;
        this.onStatus('open');
      }
      this.onMessage(msg);
    };
    source.onerror = () => {
      if (source !== this.source) return;
      this.id = null;
      // CLOSED means the server refused us (e.g. it restarted with a new link); otherwise it retries.
      this.onStatus(source.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting');
    };
  }

  // Close the current stream and open a new one; the next hello resets the page.
  reopen() {
    if (this.closed) return;
    this.source.close();
    this.id = null;
    this.onStatus('reconnecting');
    this.open();
  }

  // Messages go out one at a time, in order: a candidate must never overtake its offer.
  send(to, message) {
    const id = this.id; // the session this message belongs to
    const result = this.queue.then(() => this.post(id, to, message));
    this.queue = result;
    return result;
  }

  async post(id, to, message) {
    if (!id || id !== this.id) return false; // not connected yet, or queued before a reconnect
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs); // a hung request mustn't block the queue
    try {
      const url = `/signal?room=${encodeURIComponent(this.room)}&id=${encodeURIComponent(id)}`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to, ...message }),
        signal: controller.signal,
      });
      if (res.status === 404 && (await unknownPeer(res)) && id === this.id) this.reopen(); // the server forgot us
      return res.ok;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  close() {
    this.closed = true;
    clearInterval(this.watchdog);
    this.source.close();
  }
}

async function unknownPeer(res) {
  try {
    const body = await res.json();
    return Boolean(body) && body.error === 'unknown peer';
  } catch {
    return false;
  }
}

// Calls `send` (which resolves true on success); if that fails, once more after `delayMs`,
// but only while `stillCurrent()` says the message is still wanted.
export async function retryOnce(send, stillCurrent, delayMs = 2000) {
  if (await send()) return true;
  await new Promise((resolve) => setTimeout(resolve, delayMs));
  if (!stillCurrent()) return false;
  return send();
}

// One WebRTC connection. Local network only, so no STUN/TURN servers: works offline.
// Remote candidates that arrive before the remote description are held until it is set.
export function createPeer(sendCandidate) {
  const pc = new RTCPeerConnection({ iceServers: [] });
  const pending = [];
  let remoteReady = false;
  pc.onicecandidate = (event) => {
    if (event.candidate) sendCandidate(event.candidate.toJSON());
  };
  const add = async (candidate) => {
    try {
      await pc.addIceCandidate(candidate);
    } catch {
      // The server also sends a copy with the real IP, which can be a duplicate. Ignore.
    }
  };
  return {
    pc,
    addCandidate(candidate) {
      if (remoteReady) return add(candidate);
      pending.push(candidate);
      return Promise.resolve();
    },
    async remoteSet() {
      remoteReady = true;
      for (const candidate of pending.splice(0)) await add(candidate);
    },
    close() {
      pc.close();
    },
  };
}

// Runs `task` again and again (first after `firstDelay`, then doubling up to `max`) until stopped.
export function retrier(task, { max = 10000 } = {}) {
  let timer = null;
  let delay = 0;
  const run = () => {
    task();
    delay = Math.min(delay * 2, max);
    timer = setTimeout(run, delay);
  };
  return {
    get running() {
      return timer !== null;
    },
    start(firstDelay = 3000) {
      if (timer !== null) return;
      delay = firstDelay;
      timer = setTimeout(run, delay);
    },
    stop() {
      clearTimeout(timer);
      timer = null;
    },
  };
}
