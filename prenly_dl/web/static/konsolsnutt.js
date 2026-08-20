/*
 * prenly-dl: hämta inloggningsuppgifter från en Prenly-läsare.
 *
 * Klistra in i webbläsarens konsol (F12) på tidningens läsarsida när du är
 * inloggad, och klistra sedan in resultatet i prenly-dl. Vilka
 * publikationer uppgifterna ger tillgång till frågar prenly-dl självt.
 *
 * Allt utom sessionstoken ligger i sidans egen PrenlyData-global:
 * PrenlyData.client.authToken är klientnyckeln, som annars måste grävas
 * fram ur Network-fliken, och PrenlyData.media_host är sajtens CDN.
 */
(() => {
  const data = window.PrenlyData;
  const auth = (localStorage.getItem("prenlyreadersessiontoken") || "").replace(/^Bearer /, "");

  if (!data || !data.client || !data.client.authToken) {
    console.error("prenly-dl: hittade ingen PrenlyData. Kör snutten på tidningens läsarsida.");
    return;
  }
  if (!auth) {
    console.error("prenly-dl: ingen sessionstoken. Logga in på tidningen och kör om snutten.");
    return;
  }

  const mediaHost = (data.media_host || "").replace(/\/+$/, "");
  const konfig = {
    credentials: {
      site: location.origin,
      // Standard-CDN behöver inte anges.
      cdn: mediaHost === "https://mediacdn.prenly.com" ? "" : mediaHost,
      "textalk-auth": data.client.authToken,
      auth,
    },
  };

  const text = JSON.stringify(konfig, null, 2);
  console.log(text);
  navigator.clipboard.writeText(text).then(
    () => console.log("prenly-dl: kopierat. Klistra in i prenly-dl under Uppgifter."),
    () => console.log("prenly-dl: kopiera texten ovan och klistra in under Uppgifter."),
  );
})();
