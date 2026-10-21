// auth.js — Cognito Hosted UI, authorization-code + PKCE, token refresh.
// In mock mode this is bypassed entirely (app.js never calls getToken()).
//
// No username/password form is ever built in this app (spec 3.2): sign-in
// redirects to the Cognito Hosted UI; the code exchange happens on return.

const cfg = (typeof window !== "undefined" && window.APP_CONFIG) || {};
const STORE_KEY = "kbagent.tokens";

// ----- PKCE helpers ---------------------------------------------------------
function b64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function sha256(str) {
  const data = new TextEncoder().encode(str);
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
}
function randomVerifier() {
  const a = new Uint8Array(32); crypto.getRandomValues(a); return b64url(a);
}

function redirectUri() { return window.location.origin + window.location.pathname; }

// ----- public API -----------------------------------------------------------

// Begin sign-in: stash a PKCE verifier, then redirect to the Hosted UI.
export async function beginSignIn() {
  const verifier = randomVerifier();
  const challenge = b64url(await sha256(verifier));
  sessionStorage.setItem("kbagent.pkce", verifier);
  const url = new URL(`${cfg.COGNITO_DOMAIN}/oauth2/authorize`);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", cfg.COGNITO_APP_CLIENT_ID); // Cognito "app client" — technical use of the word
  url.searchParams.set("redirect_uri", redirectUri());
  url.searchParams.set("scope", "openid email");
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");
  window.location.assign(url.toString());
}

// If we came back from Cognito with ?code=..., exchange it for tokens.
export async function completeSignInIfReturning() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) return false;
  const verifier = sessionStorage.getItem("kbagent.pkce");
  const res = await fetch(`${cfg.COGNITO_DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: cfg.COGNITO_APP_CLIENT_ID,
      code,
      redirect_uri: redirectUri(),
      code_verifier: verifier || "",
    }),
  });
  if (!res.ok) throw new Error("Token exchange failed.");
  const tokens = await res.json();
  tokens.obtained_at = Date.now();
  localStorage.setItem(STORE_KEY, JSON.stringify(tokens));
  // clean the ?code= from the URL without losing the run hash
  const clean = redirectUri() + window.location.hash;
  window.history.replaceState({}, "", clean);
  return true;
}

function loadTokens() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { return null; }
}

export function isSignedIn() { return !!loadTokens()?.id_token; }

export function currentEmail() {
  const t = loadTokens();
  if (!t?.id_token) return null;
  try {
    const payload = JSON.parse(atob(t.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.email || payload["cognito:username"] || null;
  } catch { return null; }
}

// Return a usable access token, refreshing silently if expired.
export async function getToken() {
  let t = loadTokens();
  if (!t) return null;
  const ageSec = (Date.now() - (t.obtained_at || 0)) / 1000;
  if (t.access_token && ageSec < (t.expires_in || 3600) - 60) return t.access_token;
  if (t.refresh_token) {
    const res = await fetch(`${cfg.COGNITO_DOMAIN}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        client_id: cfg.COGNITO_APP_CLIENT_ID,
        refresh_token: t.refresh_token,
      }),
    });
    if (res.ok) {
      const fresh = await res.json();
      t = { ...t, ...fresh, obtained_at: Date.now() };
      localStorage.setItem(STORE_KEY, JSON.stringify(t));
      return t.access_token;
    }
  }
  return t.access_token || null; // let the caller get a 401 and prompt re-sign-in
}

export function signOut() {
  localStorage.removeItem(STORE_KEY);
  if (cfg.COGNITO_DOMAIN && cfg.COGNITO_APP_CLIENT_ID) {
    const url = new URL(`${cfg.COGNITO_DOMAIN}/logout`);
    url.searchParams.set("client_id", cfg.COGNITO_APP_CLIENT_ID);
    url.searchParams.set("logout_uri", redirectUri());
    window.location.assign(url.toString());
  } else {
    window.location.reload();
  }
}
