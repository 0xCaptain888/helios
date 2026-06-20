/* Helios dashboard — zero-dependency vanilla JS.
   Polls data/feed.json every 5s.
   Testnet mode: renders contract links + real cspr.live tx hashes. */

(function () {
  "use strict";

  var CSPR = 1e9;
  var lastGenerated = 0;
  var EXPLORER = "https://testnet.cspr.live";

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function cspr(motes) { return (motes / CSPR).toFixed(motes % CSPR ? 2 : 0); }
  function nav(micro)  { return (micro / 1e6).toFixed(6); }

  function shortHash(h) {
    if (!h) return "";
    var s = String(h);
    return s.length > 20 ? s.slice(0, 10) + "\u2026" + s.slice(-6) : s;
  }

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
    var href = explorer || "";
    if (href && href.indexOf("http") === 0) {
      return '<a class="deploy-hash mono" target="_blank" rel="noopener" href="'
        + esc(href) + '">' + esc(shortHash(hash)) + "</a>";
    }
    return '<span class="deploy-hash mono" title="' + esc(hash) + '">'
      + esc(shortHash(hash)) + "</span>";
  }

  function contractLink(hash, label) {
    if (!hash) return "<span class=\"mono dim\">\u2014</span>";
    var url = EXPLORER + "/contract/" + hash;
    return '<a class="contract-addr mono" target="_blank" rel="noopener" href="'
      + esc(url) + '" title="' + esc(hash) + '">' + esc(label || shortHash(hash)) + "</a>";
  }

  function renderKpis(d) {
    $("kpi-payments").textContent  = d.kpis.payments;
    $("kpi-volume").innerHTML      = esc(cspr(d.kpis.volume_motes)) + " <small>CSPR</small>";
    $("kpi-oracles").textContent   = d.kpis.oracles + " / " + d.kpis.listings;
    $("kpi-attest").textContent    = d.kpis.attestations;
    $("kpi-nav").textContent       = nav(d.kpis.nav_micro);
    $("kpi-vetoes").textContent    = d.kpis.vetoes;
    $("mode-badge").textContent    = d.mode === "testnet" ? "casper testnet" : "local simulation";
    $("network-label").textContent = d.network;
    $("nav-now").textContent       = nav(d.kpis.nav_micro);
    $("updated").textContent       = "feed " + timeStr(d.generated_at);
  }

  function renderContracts(d) {
    var bar = $("contracts-bar");
    if (d.mode !== "testnet" || !d.contracts) { bar.style.display = "none"; return; }
    bar.style.display = "";
    var c = d.contracts;
    var names = { registry: "OracleRegistry", market: "DataMarket",
                  vault: "FundVault", gov: "Governance" };
    var html = Object.keys(names).map(function (k) {
      var h = (c[k] && c[k].hash) ? c[k].hash : "";
      return '<span class="contract-item">'
        + '<span class="contract-name">' + esc(names[k]) + "</span>"
        + contractLink(h, h ? h.slice(0, 8) + "\u2026" : "\u2014")
        + "</span>";
    }).join("");
    $("contract-links").innerHTML = html;
  }

  function renderTape(d) {
    var track = $("tape-track");
    var items = (d.tape || []).map(function (x) {
      return '<span class="tape-item">'
        + '<span class="tape-oracle">' + esc(x.oracle_name || x.oracle || "") + "</span>"
        + " \u00b7 " + esc(x.feed_key || "") + " \u00b7 "
        + '<span class="tape-amount">' + cspr(x.amount_motes || 0) + " CSPR</span>"
        + (x.explorer && x.explorer.indexOf("http") === 0
          ? ' <a class="tape-link" href="' + esc(x.explorer) + '" target="_blank" rel="noopener">#</a>'
          : "")
        + "</span>";
    }).join("");
    track.innerHTML = items || '<span class="tape-item dim">waiting for settlements\u2026</span>';
  }

  function renderOracles(d) {
    var html = (d.oracles || []).map(function (o) {
      var score    = (o.reputation && o.reputation.score_bps != null)
        ? (o.reputation.score_bps / 100).toFixed(1) + "%" : "\u2014";
      var settled  = (o.reputation && o.reputation.settlements) || 0;
      var attested = (o.reputation && o.reputation.attestations) || 0;
      var addrHtml = "";
      if (d.mode === "testnet" && o.address && !o.address.startsWith("account-hash-oracle")) {
        var acctUrl = EXPLORER + "/account/" + o.address;
        addrHtml = ' <a class="oracle-addr mono" href="' + esc(acctUrl)
          + '" target="_blank" rel="noopener">' + esc(shortAddr(o.address)) + "</a>";
      }
      return '<div class="oracle-card">'
        + '<div class="oracle-head">'
        + '<span class="oracle-name">' + esc(o.name) + "</span>"
        + '<span class="oracle-cat badge">' + esc(o.category) + "</span>"
        + addrHtml + "</div>"
        + '<div class="oracle-meta mono">'
        + '<span class="feed-key">' + esc(o.feed_key) + "</span>"
        + " &nbsp;|&nbsp; score " + esc(score)
        + " &nbsp;|&nbsp; " + settled + " settled"
        + " &nbsp;|&nbsp; " + attested + " attested"
        + "</div>"
        + (o.last_value !== undefined && o.last_value !== "\u2014"
          ? '<div class="oracle-val mono">' + esc(String(o.last_value)) + "</div>"
          : "")
        + "</div>";
    }).join("");
    $("oracle-cards").innerHTML = html || '<p class="panel-sub">No oracles registered yet.</p>';
  }

  function renderSpark(d) {
    var hist = (d.nav_history || []).map(function (h) { return h.nav_micro; });
    if (hist.length < 2) return;
    var mn = Math.min.apply(null, hist), mx = Math.max.apply(null, hist);
    var range = mx - mn || 1;
    var W = 560, H = 96, pad = 6;
    var pts = hist.map(function (v, i) {
      return (i / (hist.length - 1) * (W - 2*pad) + pad).toFixed(1) + ","
        + ((1 - (v - mn) / range) * (H - 2*pad) + pad).toFixed(1);
    }).join(" ");
    $("nav-spark").innerHTML = '<polyline points="' + pts
      + '" fill="none" stroke="var(--accent)" stroke-width="2"/>';
  }

  function renderDecisions(d) {
    var html = (d.decisions || []).slice(-4).reverse().map(function (dec) {
      return '<div class="decision">'
        + (dec.status === "vetoed"
          ? '<span class="decision-veto">VETOED</span> '
          : dec.status === "approved" ? '<span class="decision-ok">APPROVED</span> ' : "")
        + '<span class="decision-summary">' + esc(dec.summary || dec.rationale || "") + "</span>"
        + (dec.positions ? '<div class="positions">'
          + (dec.positions || []).map(function (p) {
            return '<span class="pos">' + esc(p.asset) + " "
              + (p.weight_bps / 100).toFixed(0) + "%</span>";
          }).join("") + "</div>" : "")
        + "</div>";
    }).join("");
    $("decisions").innerHTML = html || '<p class="panel-sub">No rebalance decisions yet.</p>';
  }

  function renderGovernance(d) {
    var html = (d.proposals || []).slice(-5).reverse().map(function (p) {
      var st = p.status === "vetoed"
        ? '<span class="status-veto">VETOED</span>'
        : p.status === "approved"
        ? '<span class="status-ok">APPROVED</span>'
        : '<span class="status-pending">PENDING</span>';
      return '<div class="proposal">' + st
        + ' <span class="proposal-summary">' + esc(p.summary || p.description || "") + "</span>"
        + (p.veto_reason ? ' <span class="veto-reason">\u2014 ' + esc(p.veto_reason) + "</span>" : "")
        + "</div>";
    }).join("");
    $("governance").innerHTML = html || '<p class="panel-sub">No proposals yet.</p>';
  }

  function renderDeploys(d) {
    var html = (d.deploys || []).slice(0, 60).map(function (x) {
      var explorerUrl = x.explorer || "";
      if (d.mode === "testnet" && x.hash && !explorerUrl) {
        explorerUrl = EXPLORER + "/deploy/" + x.hash;
      }
      return '<div class="deploy-line">'
        + '<span class="deploy-kind">' + timeStr(x.ts) + " \u00b7 " + esc(x.kind) + "</span>"
        + link(explorerUrl, x.hash) + "</div>";
    }).join("");
    $("deploys").innerHTML = html || '<p class="panel-sub">No transactions yet.</p>';
  }

  function render(d) {
    renderKpis(d);
    renderContracts(d);
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
        $("live-dot").classList.toggle("stale",
          (Date.now() / 1000 - d.generated_at) > 30);
        if (fresh) { render(d); }
      })
      .catch(function () {
        $("live-dot").classList.add("stale");
        $("updated").textContent =
          "feed unavailable \u2014 run: python3 demo.py  or  python3 scripts/serve_dashboard.py";
      });
  }

  poll();
  setInterval(poll, 5000);
})();
