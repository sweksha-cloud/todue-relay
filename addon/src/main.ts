// Entry points Gmail calls by name (see appsscript.json). Everything here is glue between Google's
// services and the tested modules; the bundle attaches these to the global object at the bottom.

import { ApiError, appsScriptDeps, fetchSummary, runAction } from "./api";
import { renderError, renderHome } from "./cards";
import { buildHomeModel, errorModel, parseActionParameters } from "./format";
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

function notify(text: string, card?: GoogleAppsScript.Card_Service.Card): GoogleAppsScript.Card_Service.ActionResponse {
  const response = CardService.newActionResponseBuilder().setNotification(CardService.newNotification().setText(text));
  if (card) response.setNavigation(CardService.newNavigation().updateCard(card));
  return response.build();
}

/** A button on an item was pressed: ask the API to do it, say what happened, and redraw the card. */
function onAction(e: { parameters?: unknown }): GoogleAppsScript.Card_Service.ActionResponse {
  const request = parseActionParameters(e.parameters);
  if (!request) return notify("That button was not recognised.");
  try {
    const result = runAction(appsScriptDeps(), request.emailId, request.action);
    return notify(result.message, buildHomeCard());
  } catch (err) {
    // The card stays as it was; the person is told why, in the API's own words where it gave one.
    return notify(err instanceof Error ? err.message : "Something went wrong.");
  }
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

Object.assign(globalThis, { onHomepage, onRefresh, onAction, debugToken });
