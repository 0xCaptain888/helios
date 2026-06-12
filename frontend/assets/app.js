/* Helios dashboard — zero-dependency vanilla JS.
   Polls data/feed.json every 2s and renders the machine economy. */

(function () {
  "use strict";

  var CSPR = 1e9;
  var lastGenerated = 0;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function cspr(motes) { return (motes / CSPR).toFixed(motes % CSPR ? 2 : 0); }
  function nav(micro) { return (micro / 1e6).toFixed(6); }
  function shortHash(h) { return h ? h.slice(0, 10) + "\u2026" + h.slice(-6) : ""; }
  function shortAddr(a) {
    if (!a) return "";
    var s = a.replace("account-hash-", "");
    return s.slice(0, 6) + "\u2026" + s.slice(-4);
  }
  function timeStr(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour12: false });
  }
  function link(explorer, hash) {
    if (explorer && explorer.indexOf("http") === 0) {
      return '<a class="deploy-hash mono" target="_blank" rel="noopener" href="'
        + esc(explorer) + '">' + esc(shortHash(hash)) + "</a>";
    }
    return '<span class="deploy-hash mono" title="' + esc(hash) + '">'
      + esc(shortHash(hash)) + "</span>";
  }

  function renderKpis(d) {
    $("kpi-payments").textContent = d.kpis.payments;
    $("kpi-volume").innerHTML = esc(cspr(d.kpis.volume_motes)) + " <small>CSPR</small>";
    $("kpi-oracles").textContent = d.kpis.oracles + " / " + d.kpis.listings;
    $("kpi-attest").textContent = d.kpis.attestations;
    $("kpi-nav").textContent = nav(d.kpis.nav_micro);
    $("kpi-vetoes").textContent = d.kpis.vetoes;
    $("mode-badge").textContent = d.mode === "testnet" ? "casper testnet" : "local simulation";
    $("network-label").textContent = d.network;
    $("nav-now").textContent = nav(d.kpis.nav_micro);
    $("updated").textContent = "feed " + timeStr(d.generated_at);
  }

  function renderTape(d) {
    var track = $("tape-track");
    if (!d.payments.length) {
      track.innerHTML = '<span class="tape-empty">Waiting for the first x402 settlement\u2026 run <span class="mono">python3 demo.py</span></span>';
      return;
    }
    var items = d.payments.slice(0, 24).map(function (p) {
      return '<span class="tape-item">' + timeStr(p.ts) + "  "
        + esc(shortAddr(p.from)) + " \u2192 " + esc(shortAddr(p.to))
        + "  <b>" + esc(cspr(p.amount_motes)) + " CSPR</b>  "
        + esc(p.feed_key) + "  x402:" + esc(shortHash(p.receipt)) + "</span>";
    }).join("");
    track.innerHTML = items + items; // duplicated for seamless loop
  }

  function renderOracles(d) {
    var html = d.oracles.map(function (o) {
      var pct = Math.min(100, o.score_bps / 100);
      return '<article class="oracle">'
        + '<div class="oracle-head"><span class="oracle-name">' + esc(o.name)
        + '</span><span class="oracle-cat">' + esc(o.category) + "</span></div>"
        + '<div class="oracle-row"><span>feed</span><span class="mono">' + esc(o.feed_key) + "</span></div>"
        + '<div class="oracle-row"><span>price / request</span><span class="mono">' + esc(cspr(o.price_motes)) + " CSPR</span></div>"
        + '<div class="oracle-row"><span>last value</span><span class="mono">' + esc(o.last_value == null ? "\u2014" : o.last_value) + "</span></div>"
        + '<div class="oracle-row"><span>revenue</span><span class="mono">' + esc(cspr(o.revenue_motes)) + " CSPR</span></div>"
        + '<div class="repmeter"><span style="width:' + pct + '%"></span></div>'
        + '<div class="replabel"><span>reputation \u00b7 ' + o.settlements + " settlements \u00b7 "
        + o.attestations + ' attestations</span><span class="mono">'
        + (o.score_bps / 100).toFixed(1) + "%</span></div>"
        + "</article>";
    }).join("");
    $("oracle-cards").innerHTML = html || '<p class="panel-sub">No oracles registered yet.</p>';
  }

  function renderSpark(d) {
    var svg = $("nav-spark");
    var hist = d.nav_history;
    if (!hist || hist.length < 2) { svg.innerHTML = ""; return; }
    var w = 560, h = 96, pad = 6;
    var values = hist.map(function (p) { return p.nav_micro; });
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    if (max === min) { max = min + 1; }
    var pts = hist.map(function (p, i) {
      var x = pad + (w - 2 * pad) * i / (hist.length - 1);
      var y = h - pad - (h - 2 * pad) * (p.nav_micro - min) / (max - min);
      return [x, y];
    });
    var path = pts.map(function (p, i) {
      return (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1);
    }).join(" ");
    var area = path + " L" + pts[pts.length - 1][0].toFixed(1) + "," + (h - pad)
      + " L" + pts[0][0].toFixed(1) + "," + (h - pad) + " Z";
    var last = pts[pts.length - 1];
    svg.innerHTML =
      '<path d="' + area + '" fill="rgba(27,116,102,0.12)"></path>'
      + '<path d="' + path + '" fill="none" stroke="#1B7466" stroke-width="2"></path>'
      + '<circle cx="' + last[0] + '" cy="' + last[1] + '" r="3.5" fill="#1B7466"></circle>';
  }

  function renderDecisions(d) {
    var html = d.decisions.slice(0, 8).map(function (dec) {
      var cls = dec.status === "vetoed" || dec.status === "aborted" ? " vetoed" : "";
      var weights = (dec.positions || []).map(function (p) {
        return '<span class="weight">' + esc(p.asset) + " " + (p.weight_bps / 100).toFixed(1) + "%</span>";
      }).join("");
      var veto = dec.veto_reason
        ? '<div class="veto-reason">VETOED \u2014 ' + esc(dec.veto_reason) + "</div>" : "";
      var receipts = (dec.receipts || []).length
        ? '<div class="receipts">paid via x402: ' + dec.receipts.map(shortHash).map(esc).join(", ") + "</div>" : "";
      return '<article class="decision' + cls + '">'
        + '<div class="decision-head"><span>round ' + dec.round + " \u00b7 proposal #"
        + dec.proposal_id + "</span><span>" + esc(dec.status) + " \u00b7 spent "
        + esc(cspr(dec.spent_motes || 0)) + " CSPR</span></div>"
        + '<p class="decision-rationale">' + esc(dec.rationale) + "</p>"
        + '<div class="weights">' + weights + "</div>" + veto + receipts
        + "</article>";
    }).join("");
    $("decisions").innerHTML = html || '<p class="panel-sub">No decisions yet \u2014 the fund agent has not traded.</p>';
  }

  function renderGovernance(d) {
    var html = d.governance.slice(0, 10).map(function (g) {
      var reason = g.reason ? '<div class="gov-reason">' + esc(g.reason) + "</div>" : "";
      return '<div class="gov-item">'
        + '<span class="gov-status ' + esc(g.status) + '">' + esc(g.status) + "</span>"
        + '<span class="gov-summary">' + esc(g.summary) + reason + "</span>"
        + link(g.explorer, g.deploy)
        + "</div>";
    }).join("");
    $("governance").innerHTML = html || '<p class="panel-sub">No proposals yet.</p>';
  }

  function renderDeploys(d) {
    var html = d.deploys.slice(0, 60).map(function (x) {
      return '<div class="deploy-line"><span class="deploy-kind">' + timeStr(x.ts)
        + " \u00b7 " + esc(x.kind) + "</span>" + link(x.explorer, x.hash) + "</div>";
    }).join("");
    $("deploys").innerHTML = html || '<p class="panel-sub">No transactions yet.</p>';
  }

  function render(d) {
    renderKpis(d);
    renderTape(d);
    renderOracles(d);
    renderSpark(d);
    renderDecisions(d);
    renderGovernance(d);
    renderDeploys(d);
  }

  function poll() {
    fetch("data/feed.json?_=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.json(); })
      .then(function (d) {
        var fresh = d.generated_at !== lastGenerated;
        lastGenerated = d.generated_at;
        $("live-dot").classList.toggle("stale", (Date.now() / 1000 - d.generated_at) > 30);
        if (fresh) { render(d); }
      })
      .catch(function () {
        $("live-dot").classList.add("stale");
        $("updated").textContent = "feed unavailable \u2014 serve this folder over http (python3 demo.py)";
      });
  }

  poll();
  setInterval(poll, 2000);
})();
