"""A status page, so that "is anything happening?" is answerable without reading the log.

One page and one endpoint, no dependencies, nothing to install. It is read-only on purpose: the
previous attempt's window could start and stop the server and that window was itself what kept
taking the server down.
"""

import asyncio
import json
import time

from . import log as logs

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GRFS Emulator</title>
<style>
  :root {
    --bg: #11151a; --panel: #171d24; --line: #232c36; --text: #e6edf3; --dim: #8b98a5;
    --accent: #4ea1ff; --good: #3fb950; --warn: #d29922; --bad: #f85149;
    font-family: "Segoe UI", system-ui, sans-serif;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-size: 14px; }
  header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
           padding: 14px 20px; border-bottom: 1px solid var(--line); background: var(--panel); }
  h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  h1 small { color: var(--dim); font-weight: 400; margin-left: 8px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good);
         display: inline-block; margin-right: 6px; }
  .stale .dot { background: var(--bad); }
  main { padding: 20px; display: grid; gap: 20px; max-width: 1100px; }
  .cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
          padding: 12px 14px; }
  .card b { display: block; font-size: 22px; font-weight: 600; }
  .card span { color: var(--dim); font-size: 12px; text-transform: uppercase;
               letter-spacing: .6px; }
  section > h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .6px;
                 color: var(--dim); margin: 0 0 8px; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 600; font-size: 12px; text-transform: uppercase;
       letter-spacing: .5px; }
  tr:last-child td { border-bottom: none; }
  td.mono, .urls { font-family: Consolas, ui-monospace, monospace; font-size: 12px; }
  .urls { color: var(--dim); }
  .empty { color: var(--dim); padding: 14px 12px; background: var(--panel);
           border: 1px solid var(--line); border-radius: 8px; }
  #log { background: #0d1117; border: 1px solid var(--line); border-radius: 8px;
         padding: 10px 12px; height: 340px; overflow-y: auto;
         font-family: Consolas, ui-monospace, monospace; font-size: 12px; line-height: 1.55; }
  #log div { white-space: pre-wrap; word-break: break-word; }
  .at { display: inline-block; width: 68px; color: var(--dim); }
  .lvl { display: inline-block; width: 54px; color: var(--dim); }
  .src { display: inline-block; min-width: 96px; color: var(--accent); }
  .WARNING .lvl, .WARNING .msg { color: var(--warn); }
  .ERROR .lvl, .ERROR .msg, .CRITICAL .lvl, .CRITICAL .msg { color: var(--bad); }
  .DEBUG, .TRACE { opacity: .65; }
  @media (prefers-color-scheme: light) {
    :root { --bg: #f6f8fa; --panel: #fff; --line: #d8dee4; --text: #1f2328; --dim: #636c76; }
    #log { background: #fbfcfd; }
  }
</style>
<header>
  <h1><span class="dot"></span>GRFS Emulator <small id="ports"></small></h1>
  <div id="uptime" class="urls"></div>
</header>
<main>
  <div class="cards">
    <div class="card"><span>Players</span><b id="n-clients">0</b></div>
    <div class="card"><span>Accounts</span><b id="n-accounts">0</b></div>
    <div class="card"><span>Sessions</span><b id="n-sessions">0</b></div>
    <div class="card"><span>NAT answered</span><b id="n-nat">0</b></div>
    <div class="card"><span>Packets in</span><b id="n-in">0</b></div>
    <div class="card"><span>Packets out</span><b id="n-out">0</b></div>
  </div>
  <section>
    <h2>Connected</h2>
    <div id="clients"></div>
  </section>
  <section>
    <h2>Sessions</h2>
    <div id="sessions"></div>
  </section>
  <section>
    <h2>Log</h2>
    <div id="log"></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let shown = 0;

function ago(seconds) {
  if (seconds < 1) return "just now";
  if (seconds < 60) return Math.round(seconds) + "s ago";
  if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
  return Math.round(seconds / 3600) + "h ago";
}

function duration(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60);
  const s = Math.floor(seconds % 60);
  return (h ? h + "h " : "") + (h || m ? m + "m " : "") + s + "s";
}

function clientsTable(rows, now) {
  if (!rows.length) return '<div class="empty">Nobody is signed in. Start the game.</div>';
  const head = "<tr><th>Player</th><th>PID</th><th>Address</th><th>Links</th>" +
               "<th>Last heard</th><th>Friends</th><th>Station URLs</th></tr>";
  return "<table>" + head + rows.map(c =>
    "<tr><td>" + escapeHtml(c.name || "") + (c.name ? "" : "<i>signing in</i>") + "</td>" +
    '<td class="mono">0x' + c.pid.toString(16).toUpperCase() + "</td>" +
    '<td class="mono">' + c.address + "</td>" +
    "<td>" + c.connections + "</td>" +
    "<td>" + ago(now - c.last_seen) + (c.away ? " (away)" : "") + "</td>" +
    "<td>" + friendsCell(c) + "</td>" +
    '<td class="urls">' + (c.urls.map(escapeHtml).join("<br>") || "-") + "</td></tr>").join("") +
    "</table>";
}

// Relationship 1 is a friend, 2 a request sent, 3 a request received (grfs.friends).
function friendsCell(c) {
  const mark = {1: "", 2: " (asked)", 3: " (asks)"};
  const names = c.friends.map(f => escapeHtml(f.name) + (mark[f.relationship] || "") +
                                   (f.online ? " *" : ""));
  const blocked = c.blocked.map(n => escapeHtml(n) + " (blocked)");
  return names.concat(blocked).join(", ") || "-";
}

function sessionsTable(rows) {
  if (!rows.length) return '<div class="empty">Nobody is hosting.</div>';
  const head = "<tr><th>Session</th><th>Host</th><th>Public</th><th>Private</th>" +
               "<th>Invitations</th><th>Attributes</th></tr>";
  return "<table>" + head + rows.map(s =>
    '<tr><td class="mono">' + s.type + "/" + s.id + "</td>" +
    "<td>" + (s.host ? escapeHtml(s.host_name || "0x" + s.host.toString(16)) :
              "<i>waiting for a new host</i>") + "</td>" +
    "<td>" + s.used[0] + " / " + s.capacity[0] + "</td>" +
    "<td>" + s.used[1] + " / " + s.capacity[1] + "</td>" +
    "<td>" + s.invitations + "</td>" +
    '<td class="urls">' + Object.entries(s.attributes).map(([k, v]) => k + "=" + v).join(" ") +
    "</td></tr>").join("") + "</table>";
}

async function tick() {
  let data;
  try {
    data = await (await fetch("api/status", {cache: "no-store"})).json();
  } catch (e) {
    document.querySelector("header").classList.add("stale");
    return;
  }
  document.querySelector("header").classList.remove("stale");
  const now = data.now;
  $("ports").textContent = data.ports;
  $("uptime").textContent = "up " + duration(data.uptime);
  $("n-clients").textContent = data.clients.length;
  $("n-accounts").textContent = data.accounts;
  $("n-nat").textContent = data.nat.answered;
  let into = 0, out = 0;
  for (const name in data.packets) { into += data.packets[name].in; out += data.packets[name].out; }
  $("n-in").textContent = into;
  $("n-out").textContent = out;
  $("clients").innerHTML = clientsTable(data.clients, now);
  $("n-sessions").textContent = data.sessions.length;
  $("sessions").innerHTML = sessionsTable(data.sessions);

  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  if (data.log_total < shown) { log.innerHTML = ""; shown = 0; }   // the server restarted
  // The server keeps the last few hundred lines and says how many there have ever been, so what
  // is new is the tail - clamped, in case more arrived than it keeps.
  const fresh = Math.min(data.log_total - shown, data.log.length);
  for (const line of data.log.slice(data.log.length - fresh)) {
    const row = document.createElement("div");
    row.className = line.level;
    row.innerHTML = '<span class="at">' +
      new Date(line.time * 1000).toTimeString().slice(0, 8) + '</span>' +
      '<span class="lvl">' + line.level + '</span>' +
      '<span class="src">' + escapeHtml(line.source) + '</span>' +
      '<span class="msg">' + escapeHtml(line.text) + '</span>';
    log.appendChild(row);
  }
  while (log.childElementCount > 600) log.removeChild(log.firstChild);
  shown = data.log_total;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function escapeHtml(text) {
  return text.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
}

tick();
setInterval(tick, 2000);
</script>
</html>
"""


class Panel:
    def __init__(self, backend):
        self.backend = backend
        self.log = logs.get("panel")
        self.port = backend.config.int("panel_port", 8080)
        self.server = None

    async def start(self, port: int = None):
        """Configured port 0 switches the page off. An explicit `port` of 0 instead means any
        free one - the test harness's way of getting a page that cannot collide with anything."""
        if port is None and not self.port:
            return None
        bind = self.backend.config.get("bind", "0.0.0.0")
        self.server = await asyncio.start_server(self.handle, bind,
                                                 self.port if port is None else port)
        self.port = self.server.sockets[0].getsockname()[1]
        shown = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
        self.log.info("status page on http://%s:%d/", shown, self.port)
        return self.server

    def close(self):
        if self.server is not None:
            self.server.close()

    async def handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                return
            path = (line.decode("latin-1").split() + ["", ""])[1]
            while True:
                header = await asyncio.wait_for(reader.readline(), 15)
                if header in (b"\r\n", b"\n", b""):
                    break
            if path.rstrip("/") in ("", "/index.html"):
                writer.write(_http(200, PAGE.encode("utf-8"), "text/html; charset=utf-8"))
            elif path.startswith("/api/status"):
                writer.write(_http(200, json.dumps(self.status()).encode("utf-8"),
                                   "application/json"))
            else:
                writer.write(_http(404, b"not found", "text/plain"))
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    def status(self) -> dict:
        data = self.backend.status()
        data["now"] = time.time()
        data["ports"] = self.backend.config.describe()
        lines = logs.ring.recent(200)
        data["log"] = lines
        data["log_total"] = len(logs.ring.lines)
        return data


def _http(status: int, body: bytes, content_type: str) -> bytes:
    reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
    return (("HTTP/1.1 %d %s\r\nContent-Length: %d\r\nContent-Type: %s\r\n"
             "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
             % (status, reason, len(body), content_type)).encode("latin-1") + body)
