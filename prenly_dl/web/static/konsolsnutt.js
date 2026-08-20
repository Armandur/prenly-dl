/*
 * prenly-dl: hämta inloggningsuppgifter från en Prenly-läsare.
 *
 * Klistra in i webbläsarens konsol (F12) på tidningens läsarsida när du är
 * inloggad. Skriver ut en färdig konfiguration per publikation som kontot
 * kan läsa - inklusive textalk-auth, som annars måste grävas fram ur
 * Network-fliken.
 *
 * Allt utom sessionstoken ligger i sidans egen PrenlyData-global:
 * PrenlyData.client.authToken är klientnyckeln och PrenlyData.media_host
 * är den CDN sajten använder.
 */
(async () => {
  const rubrik = "prenly-dl";
  const fel = (text) => console.error(`%c${rubrik}%c ${text}`,
    "background:#c9403f;color:#fff;padding:2px 6px;border-radius:3px", "");
  const info = (text) => console.log(`%c${rubrik}%c ${text}`,
    "background:#0e639c;color:#fff;padding:2px 6px;border-radius:3px", "");

  const data = window.PrenlyData;
  if (!data || !data.client) {
    fel("Hittade ingen PrenlyData på sidan. Kör snutten på tidningens läsarsida, inte på en inloggningssida.");
    return;
  }

  const textalkAuth = data.client.authToken;
  const auth = (localStorage.getItem("prenlyreadersessiontoken") || "").replace(/^Bearer /, "");
  const site = location.origin;
  const mediaHost = (data.media_host || "").replace(/\/+$/, "");
  const apiUrl = (data.context_token && data.context_token.api_url) || "https://apicdn.prenly.com";

  if (!textalkAuth) {
    fel("PrenlyData.client.authToken saknas - sidan kan ha ändrat format.");
    return;
  }
  if (!auth) {
    fel("Ingen sessionstoken i localStorage. Logga in på tidningen först och kör om snutten.");
    return;
  }

  const huvuden = {
    "Accept": "*/*",
    "Content-Type": "application/json-rpc",
    "X-Requested-With": "XMLHttpRequest",
    "X-Textalk-Content-Client-Authorize": textalkAuth,
    "X-Textalk-Content-Product-Type": "webapp",
    "Authorization": `Bearer ${auth}`,
  };

  let titlar;
  try {
    const tokenSvar = await fetch(
      "https://content.textalk.se/api/web-reader/v1/context-token", { headers: huvuden });
    if (!tokenSvar.ok) throw new Error(`context-token gav ${tokenSvar.status}`);
    const { token } = await tokenSvar.json();
    const titelSvar = await fetch(
      `${apiUrl}/api/web-reader/v1/titles?context_token=${token}`);
    if (!titelSvar.ok) throw new Error(`titles gav ${titelSvar.status}`);
    titlar = await titelSvar.json();
  } catch (error) {
    fel(`Kunde inte hämta publikationslistan: ${error.message}`);
    return;
  }

  // Standard-CDN behöver inte anges i konfigurationen.
  const cdn = mediaHost === "https://mediacdn.prenly.com" ? "" : mediaHost;
  const credentials = { site, cdn, "textalk-auth": textalkAuth, auth };

  // Publikationsnamn kan innehålla tecken som inte fungerar i ett filnamn,
  // till exempel "TÅ/ÖA Bilaga".
  const filnamnssakert = (text) =>
    text.replace(/[<>:"/\\|?*\x00-\x1f]/g, "-").trim().replace(/^\.+|\.+$/g, "");

  const konfig = (titel) => ({
    credentials,
    publication: { title: titel.id, limit: 1 },
    prefix: `${filnamnssakert(titel.name)} - `,
    folder: filnamnssakert(titel.name),
  });

  const lasbara = titlar.filter((t) => t.user_readable);
  const ovriga = titlar.filter((t) => !t.user_readable);

  info(`${titlar.length} publikationer på ${site}, ${lasbara.length} läsbara med det här kontot.`);

  // En tabell först: sajter som DN och Ångermanland listar trettio titlar,
  // och då är överblicken viktigare än trettio JSON-block.
  console.table(titlar.map((t) => ({
    id: t.id, namn: t.name, läsbar: t.user_readable ? "ja" : "nej",
  })));

  if (!lasbara.length) {
    fel("Kontot kan inte läsa någon publikation på den här sajten. "
      + "Uppgifterna stämmer, men prenumerationen täcker ingen av titlarna.");
    return;
  }

  for (const titel of lasbara) {
    console.log(`\n--- ${titel.name} (titel ${titel.id}) ---\n${JSON.stringify(konfig(titel), null, 2)}`);
  }

  // Ett anrop åt gången: kopiering kräver att sidan har fokus.
  window.prenlyKonfig = (titelId) => {
    const vald = titelId ? lasbara.find((t) => t.id === Number(titelId)) : lasbara[0];
    if (!vald) {
      fel(`Ingen läsbar publikation med id ${titelId}.`);
      return;
    }
    const text = JSON.stringify(konfig(vald), null, 2);
    navigator.clipboard.writeText(text).then(
      () => info(`Kopierade konfigurationen för ${vald.name}. Klistra in den i Lägg till publikation.`),
      () => console.log(text),
    );
  };

  info("Kör prenlyKonfig() för att kopiera den första, eller prenlyKonfig(TITEL_ID) för en annan.");
})();
