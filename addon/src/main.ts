// Entry points Gmail calls by name (see appsscript.json). Everything here is glue between Google's
// services and the tested modules; the bundle attaches these to the global object at the bottom.

import { ApiError, appsScriptDeps, fetchSummary } from "./api";
import { renderError, renderHome } from "./cards";
import { buildHomeModel, errorModel } from "./format";
import { claimsForLog, decodeJwtPayload } from "./token";

function buildHomeCard(): GoogleAppsScript.Card_Service.Card {
  try {
    return renderHome(buildHomeModel(fetchSummary(appsScriptDeps())));
  } catch (e) {
    if (e instanceof ApiError) return renderError(errorModel(e.kind, e.message));
    return renderError(errorModel("server", `Unexpected error: ${e instanceof Error ? e.message : String(e)}`));
  }
}

/** The add-on's home card, opened from Gmail's side panel. */
function onHomepage(): GoogleAppsScript.Card_Service.Card {
  return buildHomeCard();
}

/** The Refresh button: replaces the card in place. */
function onRefresh(): GoogleAppsScript.Card_Service.ActionResponse {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(buildHomeCard()))
    .build();
}

/** Run once from the editor: logs the client id and email the API must be configured with. */
function debugToken(): void {
  const token = ScriptApp.getIdentityToken();
  if (!token) {
    Logger.log("No identity token. Check appsscript.json lists the openid and userinfo.email scopes.");
    return;
  }
  const claims = decodeJwtPayload(token, (s) => Utilities.newBlob(Utilities.base64DecodeWebSafe(s)).getDataAsString());
  claimsForLog(claims).forEach((line) => Logger.log(line));
}

Object.assign(globalThis, { onHomepage, onRefresh, debugToken });
