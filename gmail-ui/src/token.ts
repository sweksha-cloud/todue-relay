// Reading the claims out of the identity token, only so the person can copy the client id ("aud")
// the API must expect. The add-on never trusts these claims for anything: the API verifies the token.

export interface TokenClaims {
  aud?: string;
  iss?: string;
  email?: string;
  exp?: number;
}

/** Decodes a JWT's payload with the given base64url decoder (Utilities in Apps Script, Buffer in tests). */
export function decodeJwtPayload(token: string, decodeBase64Url: (segment: string) => string): TokenClaims {
  const parts = token.split(".");
  const payload = parts[1];
  if (parts.length !== 3 || !payload) throw new Error("Not a JWT: expected three dot-separated parts.");
  return JSON.parse(decodeBase64Url(payload)) as TokenClaims;
}

/** Only the claims worth showing, as lines for the execution log. */
export function claimsForLog(c: TokenClaims): string[] {
  return [
    `aud (set this as ADDON_OAUTH_CLIENT_ID): ${c.aud ?? "(missing)"}`,
    `email (set this as ADDON_ALLOWED_EMAIL): ${c.email ?? "(missing: the userinfo.email scope is needed)"}`,
    `iss: ${c.iss ?? "(missing)"}`,
  ];
}
