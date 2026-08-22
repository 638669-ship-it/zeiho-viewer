/* 税務六法 Web アクセス計測（GoatCounter） */
(function () {
  var GC_CODE = "zeiho638669";

  if (!GC_CODE) return;
  if (/^(localhost|127\.)/.test(location.hostname)) return;

  window.goatcounter = { no_onload: true };

  var s = document.createElement("script");
  s.async = true;
  s.src = "https://gc.zgo.at/count.js";
  s.setAttribute("data-goatcounter",
    "https://" + GC_CODE + ".goatcounter.com/count");
  s.addEventListener("load", send);
  document.head.appendChild(s);

  var last = "";
  function send() {
    if (!window.goatcounter || !window.goatcounter.count) return;
    var p = location.pathname + (location.hash || "");
    if (p === last) return;
    last = p;
    window.goatcounter.count({ path: p });
  }
  window.addEventListener("hashchange", send);
})();
