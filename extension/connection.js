// Formline backend connection: one websocket, authenticated with the installation's random token
// (never the phone number). Reconnects with backoff; sends a keepalive every 20 s, which also keeps
// the MV3 service worker from being suspended while connected.

export class Connection {
  constructor({ onMessage, onStatus }) {
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.ws = null;
    this.cfg = null;
    this.status = "unpaired";
    this.retry = 0;
    this.retryTimer = null;
    this.keepalive = null;
    this.helloTab = null;
  }

  configure(cfg) {
    this.cfg = cfg && cfg.installationId && cfg.token ? cfg : null;
    this.stop();
    if (this.cfg) this.open();
    else this.setStatus("unpaired");
  }

  setStatus(status) {
    if (this.status !== status) {
      this.status = status;
      this.onStatus(status);
    }
  }

  wsUrl() {
    const u = new URL(this.cfg.serverUrl);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = "/browser/ws";
    u.search = "";
    return u.toString();
  }

  open() {
    if (!this.cfg || (this.ws && this.ws.readyState <= 1)) return;
    this.setStatus("connecting");
    let ws;
    try {
      ws = new WebSocket(this.wsUrl());
    } catch (e) {
      this.scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({
        type: "hello", installation_id: this.cfg.installationId, token: this.cfg.token,
        version: chrome.runtime.getManifest().version, user_agent: navigator.userAgent, tab: this.helloTab,
      }));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "welcome") {
        this.retry = 0;
        this.setStatus("connected");
        this.keepalive = setInterval(() => this.send({ type: "pong" }), 20000);
      } else if (msg.type === "ping") {
        this.send({ type: "pong" });
      } else if (msg.type === "error" && msg.error === "unauthorized") {
        this.setStatus("unauthorized");
      } else {
        this.onMessage(msg);
      }
    };
    ws.onclose = (ev) => {
      clearInterval(this.keepalive);
      this.keepalive = null;
      if (this.ws === ws) this.ws = null;
      if (ev.code === 4401 || this.status === "unauthorized") {
        this.setStatus("unauthorized");  // credential revoked: needs pairing again
        return;
      }
      if (this.cfg) this.scheduleRetry();
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  scheduleRetry() {
    this.setStatus("disconnected");
    clearTimeout(this.retryTimer);
    const delay = [1000, 2000, 4000, 8000, 15000][Math.min(this.retry++, 4)];
    this.retryTimer = setTimeout(() => this.open(), delay);
  }

  stop() {
    clearTimeout(this.retryTimer);
    clearInterval(this.keepalive);
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try { ws.close(); } catch (e) { /* already closed */ }
    }
  }

  send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }
}
