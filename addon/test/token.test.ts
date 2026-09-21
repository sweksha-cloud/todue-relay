import { describe, expect, it } from "vitest";
import { claimsForLog, decodeJwtPayload } from "../src/token";

const b64url = (obj: object) => Buffer.from(JSON.stringify(obj)).toString("base64url");
const decode = (s: string) => Buffer.from(s, "base64url").toString("utf8");
const jwt = (claims: object) => `${b64url({ alg: "RS256" })}.${b64url(claims)}.signature`;

describe("decodeJwtPayload", () => {
  it("reads the claims out of a token", () => {
    const claims = decodeJwtPayload(jwt({ aud: "client-1", email: "me@example.com", iss: "https://accounts.google.com" }), decode);

    expect(claims).toMatchObject({ aud: "client-1", email: "me@example.com" });
  });
  it("rejects something that is not a token", () => {
    expect(() => decodeJwtPayload("not-a-jwt", decode)).toThrow(/three/);
    expect(() => decodeJwtPayload("a.b", decode)).toThrow();
  });
});

describe("claimsForLog", () => {
  it("names the settings each claim is for", () => {
    const lines = claimsForLog({ aud: "client-1", email: "me@example.com", iss: "google" });

    expect(lines[0]).toContain("ADDON_OAUTH_CLIENT_ID");
    expect(lines[0]).toContain("client-1");
    expect(lines[1]).toContain("ADDON_ALLOWED_EMAIL");
    expect(lines[1]).toContain("me@example.com");
  });
  it("says so when the email scope is missing", () => {
    expect(claimsForLog({ aud: "c" })[1]).toContain("userinfo.email");
  });
});
