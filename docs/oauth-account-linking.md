# OAuth2 account linking (Alexa) — design

Status: **design only, not implemented.** This describes how the server will let an Alexa
skill (and later other third parties) act for a Thymble user. Nothing here exists yet.

## Goal

A user links the Thymble Alexa skill to their Thymble account once, then says "Alexa, add
milk to Thymble". Alexa account linking requires an OAuth 2.0 **authorization-code grant**
(RFC 6749 §4.1): Alexa sends the user to our authorization endpoint, we send back a code,
and Alexa's servers exchange it at our token endpoint for an access and refresh token,
which they then refresh on their own.

## What already exists

- Refresh tokens record a `client_id` (`auth_refresh_token.client_id`) and access tokens
  carry it as a claim. The app is `thymble-app`; Alexa becomes `alexa`.
- `tokens.issue_token_pair(..., client_id=...)` and `tokens.rotate_refresh_token` already do
  issuing and rotation, including the reuse grace (`REFRESH_REUSE_GRACE_SECONDS`), which
  matters here: Alexa retries refreshes, and a retry must not log the user out.
- Rate limiting (`thymble.ratelimit`) for the new endpoints.

## Endpoints

| Endpoint | Who calls it | What it does |
|---|---|---|
| `GET /oauth/authorize` | The user's browser, sent by Alexa | Validates `response_type=code`, `client_id`, `redirect_uri` (exact match against the client's registered list), `state`, `scope`, optional PKCE `code_challenge` (`S256` only). Shows a small server-rendered page: sign in (email magic link that returns to this page, or Sign in with Apple JS), then a consent screen: "Let Alexa read and add to the shopping list of *household*?" with a household picker. Invalid `client_id`/`redirect_uri` shows an error page and never redirects. |
| `POST /oauth/authorize` | The consent form (CSRF token, same-site session cookie) | On approve: stores a one-time code and redirects `302` to `redirect_uri?code=...&state=...`. On deny: `redirect_uri?error=access_denied&state=...`. |
| `POST /v1/oauth/authorize` | The Thymble iOS app (bearer token), for **app-to-app linking** | Same checks as above, but the user is already signed in in the app, so no web sign-in: the app shows its own consent sheet and gets back the redirect URL to hand to Alexa. Preferred path on iPhone. |
| `POST /oauth/token` | Alexa's servers | `grant_type=authorization_code` (code, redirect_uri, code_verifier if PKCE was used) or `grant_type=refresh_token`. Client authentication with HTTP Basic (`client_id:client_secret`) or form fields. Returns `{"access_token", "token_type": "bearer", "expires_in", "refresh_token", "scope"}` per RFC 6749 §5.1; errors per §5.2 (`invalid_grant`, `invalid_client`, ...). `Cache-Control: no-store`. |
| `GET /v1/oauth/grants`, `DELETE /v1/oauth/grants/{client_id}` | The app | Lists linked services and unlinks one (revokes every refresh token of that user + client). |

## Storage (one future migration)

- `oauth_client`: `client_id`, hashed `client_secret`, allowed `redirect_uris`, allowed
  scopes, display name. Alexa's is seeded from env (`ALEXA_CLIENT_ID`,
  `ALEXA_CLIENT_SECRET`, `ALEXA_REDIRECT_URIS`; the redirect URIs come from the skill's
  account-linking page in the Alexa developer console).
- `oauth_authorization_code`: hashed code, `client_id`, `user_id`, `household_id`,
  `redirect_uri`, `scope`, `code_challenge`, `expires_at` (5 minutes), `used_at`. Single
  use; a second redemption revokes the tokens issued from the first (RFC 6749 §4.1.2).
- `auth_refresh_token` gains nullable `household_id` and `scope` columns, copied into the
  access token claims, so a third-party token is pinned to one household and to the
  granted scopes.

## Scopes and enforcement

Start with one scope, `shopping_list` (read the list, add and check items). Access tokens
for any `client_id` other than `thymble-app` must carry `household_id` and `scope`; a
dependency next to `get_current_household` checks both and ignores `X-Thymble-Household`.
Third-party tokens never reach sync, account or household-management routes.

## Security notes

- Exact `redirect_uri` matching; `state` is required and echoed back unchanged.
- Codes and client secrets are stored hashed, like every other credential here.
- Access token lifetime stays short (`ACCESS_TOKEN_TTL_SECONDS`); Alexa refreshes.
- Rate limits on `/oauth/token` per client and per IP, and on the authorize page.
- Unlinking in the app, signing out everywhere, or leaving the household revokes the
  grant. Alexa's "skill disabled" event (if the skill backend receives it) also unlinks.
