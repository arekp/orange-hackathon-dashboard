/* Strona startowa: odliczanie do startu, potem do końca hackatonu (daty z .env przez /api/meta). */
(() => {
  const $ = (id) => document.getElementById(id);
  let meta = null, offset = 0;

  async function load() {
    try {
      const res = await fetch("/api/meta", { cache: "no-store" });
      meta = await res.json();
      offset = Date.parse(meta.now) - Date.now();
      const fmt = (iso) => new Date(iso).toLocaleString("pl-PL", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: meta.timezone });
      $("dates").textContent = `Start: ${fmt(meta.start)} · Koniec: ${fmt(meta.end)}`;
      tick();
    } catch (e) {
      $("phase").textContent = "Brak połączenia z serwerem";
    }
  }

  const pad = (n) => String(n).padStart(2, "0");

  function tick() {
    if (!meta) return;
    const now = Date.now() + offset, start = Date.parse(meta.start), end = Date.parse(meta.end);
    let ms, phase;
    if (now < start) { ms = start - now; phase = "Start za"; }
    else if (now < end) { ms = end - now; phase = "Do końca hackatonu"; }
    else { ms = 0; phase = "Koniec hackatonu! Dziękujemy"; }

    const s = Math.floor(ms / 1000);
    const days = Math.floor(s / 86400);
    $("d").textContent = pad(days);
    $("h").textContent = pad(Math.floor((s % 86400) / 3600));
    $("m").textContent = pad(Math.floor((s % 3600) / 60));
    $("s").textContent = pad(s % 60);
    // dni pokazujemy tylko, gdy są potrzebne
    const units = document.querySelectorAll("#digits > *");
    units[0].classList.toggle("hide", days === 0);
    units[1].classList.toggle("hide", days === 0);
    $("phase").textContent = phase;

    const running = now >= start && now < end;
    $("bar").hidden = !running;
    if (running) $("bar-fill").style.width = `${((now - start) / (end - start)) * 100}%`;
  }

  load();
  setInterval(tick, 1000);
  setInterval(load, 60000);  // korekta zegara względem serwera
})();
