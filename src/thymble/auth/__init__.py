"""Accounts, households and tokens (architecture §9, D-015).

How the pieces fit:
- `apple` verifies Sign in with Apple identity tokens; `email` sends magic links.
- `accounts` finds or creates users and gives every new user a household.
- `tokens` issues the api's own credentials: a short-lived HS256 access token (JWT) and a
  rotating, revocable refresh token (opaque, stored hashed), plus PowerSync sync tokens.
- `deps` turns a bearer token into the current user and checks household membership.

OAuth2 / Alexa account linking (later): Alexa needs an authorization-code grant. Add
`GET /oauth/authorize` (signed-in user approves; store a hashed one-time code with
client_id, redirect_uri and PKCE challenge) and `POST /oauth/token` handling
`grant_type=authorization_code` and `refresh_token`. Both can reuse
`tokens.issue_token_pair(..., client_id="alexa")` and `tokens.rotate_refresh_token`:
refresh tokens already record their `client_id`, and access tokens carry it as a claim.
Full design: docs/oauth-account-linking.md.
"""
